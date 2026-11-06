from __future__ import annotations

import json
import shutil
import socket
from datetime import UTC, datetime
from pathlib import Path

import aiosmtplib
import aiosqlite
import httpx
import pytest
from pydantic import SecretStr, ValidationError

from ase.common.config import Settings
from ase.common.models import Category, Incident, Severity, Step
from ase.common.transport import SimClock
from ase.knowledge.loader import KNOWLEDGE_DIR, load_knowledge
from ase.notifier.adaptive_card import build_adaptive_card
from ase.notifier.demo import run_demo, sample_incidents
from ase.notifier.email_smtp import NotifierConfigError, SmtpEmailNotifier
from ase.notifier.mock_teams.app import create_app
from ase.notifier.render import format_subject, render_email
from ase.notifier.router import Alert, AlertRouter, SendResult, route_incident
from ase.notifier.service import NotifierService
from ase.notifier.teams_mock_client import TeamsMockNotifier

T0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)


def _make_incident(
    *,
    incident_id: str = "INC-20261104-0007",
    revision: int = 1,
    category: Category = Category.SYSTEM,
    severity: Severity = Severity.P2,
    error_code: str | None = "SESS_CREATE_5XX",
    step: Step = Step.SCAN_QR,
    station_id: str = "VN-ST-001",
    cabinet_id: str | None = "VN-ST-001-CAB-01",
    slot_id: str | None = "03",
    country: str = "VN",
    timezone: str = "Asia/Ho_Chi_Minh",
    countries: list[str] | None = None,
    session_ids: list[str] | None = None,
    first_seen_at: datetime = T0,
    last_seen_at: datetime = T0,
    probable_cause: str = "Session-service trả lỗi 503 sau khi deploy.",
    needs_human_review: bool = False,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        revision=revision,
        category=category,
        severity=severity,
        error_code=error_code,
        step=step,
        station_id=station_id,
        cabinet_id=cabinet_id,
        slot_id=slot_id,
        country=country,
        timezone=timezone,
        countries=countries if countries is not None else [country],
        session_ids=session_ids or ["SW20261104-000123"],
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        n_sessions=12,
        n_stations=3,
        window_minutes=5,
        confidence=0.88,
        probable_cause=probable_cause,
        evidence=["evt-SW20261104-000123-3-0", "evt-SW20261104-000124-3-0"],
        suggested_actions=["Kiểm tra session-service", "Rollback nếu cần"],
        runbook_ids=["RB-SYS-001"],
        needs_human_review=needs_human_review,
    )


def test_incident_requires_timezone_aware_datetime() -> None:
    with pytest.raises(ValidationError):
        _make_incident(first_seen_at=datetime(2026, 11, 4, 10, 0, 0))  # noqa: DTZ001


@pytest.mark.parametrize(
    ("timezone", "expected_time", "expected_offset"),
    [
        ("Asia/Ho_Chi_Minh", "17:00:00", "GMT+7"),
        ("Asia/Manila", "18:00:00", "GMT+8"),
        ("Asia/Kolkata", "15:30:00", "GMT+5:30"),
    ],
)
def test_subject_and_body_local_time_formats(
    timezone: str, expected_time: str, expected_offset: str
) -> None:
    inc = _make_incident(
        category=Category.PAYMENT,
        error_code="PAY_GATEWAY_TIMEOUT",
        step=Step.PAYMENT,
        country="PH",
        timezone=timezone,
        session_ids=["SW20261104-000999", "SW20261104-001000"],
    )
    alert = route_incident(inc)
    assert alert is not None

    subj = format_subject(inc)
    assert subj == (
        f"[ASE] Cảnh báo giao dịch lỗi có thể liên quan đến thanh toán"
        f" _ SW20261104-000999 _ {expected_time} 04/11/2026"
    )

    rendered = render_email(alert, feedback_base_url="http://localhost:8000")
    assert rendered.subject == subj
    expected_time_line = (
        f"{expected_time} ngày 04/11/2026 ({expected_offset} – giờ địa phương của trạm)"
    )
    assert expected_time_line in rendered.text_body
    assert expected_time_line in rendered.html_body
    assert "Quốc gia" in rendered.text_body
    assert "/feedback/INC-20261104-0007/confirm" in rendered.text_body
    assert "/feedback/INC-20261104-0007/wrong_category" in rendered.text_body
    assert "/feedback/INC-20261104-0007/resolved" in rendered.text_body
    assert "/feedback/INC-20261104-0007/confirm" in rendered.html_body
    assert "Payment gateway" in rendered.text_body  # từ state_machine.yaml
    assert "Gateway timeout/lỗi" in rendered.text_body  # từ error_catalog.yaml


def test_text_body_not_html_escaped_while_html_body_is_escaped() -> None:
    inc = _make_incident(probable_cause='Lỗi "503" & <timeout>')
    alert = route_incident(inc)
    assert alert is not None
    rendered = render_email(alert, feedback_base_url="http://localhost:8000")

    assert 'Lỗi "503" & <timeout>' in rendered.text_body
    assert "&#34;" not in rendered.text_body
    assert "&amp;" not in rendered.text_body
    assert "&lt;" not in rendered.text_body

    assert "&amp;" in rendered.html_body
    assert "&lt;timeout&gt;" in rendered.html_body


def test_router_user_unknown_and_country_overrides(tmp_path: Path) -> None:
    # USER -> không gửi alert
    user_inc = _make_incident(category=Category.USER, error_code="QR_INVALID")
    assert route_incident(user_inc) is None

    # UNKNOWN -> IT Ops + needs_human_review=True
    unknown_inc = _make_incident(
        category=Category.UNKNOWN,
        error_code="STEP_TIMEOUT",
        needs_human_review=False,
    )
    unknown_alert = route_incident(unknown_inc)
    assert unknown_alert is not None
    assert unknown_alert.team == "IT Ops"
    assert unknown_alert.channel == "it-ops"
    assert unknown_alert.incident.needs_human_review is True

    # country_overrides được áp dụng
    for f in KNOWLEDGE_DIR.glob("*.yaml"):
        shutil.copy(f, tmp_path / f.name)
    (tmp_path / "routing.yaml").write_text(
        (KNOWLEDGE_DIR / "routing.yaml")
        .read_text(encoding="utf-8")
        .replace(
            "country_overrides: []",
            "country_overrides:\n  - {country: PH, category: PAYMENT, team: 'Payment Ops PH'}",
        ),
        encoding="utf-8",
    )
    custom_routing = load_knowledge(tmp_path).routing
    router = AlertRouter(custom_routing)

    ph_pay = _make_incident(category=Category.PAYMENT, country="PH", timezone="Asia/Manila")
    vn_pay = _make_incident(category=Category.PAYMENT, country="VN")

    ph_alert = router.route(ph_pay)
    vn_alert = router.route(vn_pay)
    assert ph_alert is not None and ph_alert.team == "Payment Ops PH"
    assert ph_alert.channel == "payment-ops-ph"
    assert vn_alert is not None and vn_alert.team == "Payment Ops"
    assert vn_alert.channel == "payment-ops"


def test_adaptive_card_content_and_p1_attention() -> None:
    p1_inc = _make_incident(
        incident_id="INC-20261104-0099",
        category=Category.PAYMENT,
        severity=Severity.P1,
        error_code="PAID_NOT_DELIVERED",
        step=Step.PAYMENT,
        country="PH",
        timezone="Asia/Manila",
        session_ids=["SW20261104-000555"],
    )
    alert = route_incident(p1_inc)
    assert alert is not None

    card = build_adaptive_card(alert, feedback_base_url="http://localhost:8000")
    assert card["type"] == "AdaptiveCard"
    assert card["version"] == "1.5"

    card_str = json.dumps(card, ensure_ascii=False)
    assert "SW20261104-000555" in card_str
    assert "18:00:00 04/11/2026 (GMT+8)" in card_str
    assert "@Người trực" in card_str

    body = card["body"]
    assert isinstance(body, list)
    header_container = body[0]
    assert isinstance(header_container, dict)
    assert header_container["style"] == "attention"

    actions = card["actions"]
    assert isinstance(actions, list) and len(actions) == 3
    urls = [a["url"] for a in actions if isinstance(a, dict)]
    assert urls == [
        "http://localhost:8000/feedback/INC-20261104-0099/confirm",
        "http://localhost:8000/feedback/INC-20261104-0099/wrong_category",
        "http://localhost:8000/feedback/INC-20261104-0099/resolved",
    ]


class _RecorderNotifier:
    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.sent_tags: list[str] = []

    async def send(self, alert: Alert) -> SendResult:
        tag = f"{alert.incident.incident_id}:r{alert.incident.revision}"
        self.sent_tags.append(tag)
        return SendResult(channel=self.channel, status="SENT", detail=tag)


async def test_outbox_idempotency_revision_and_max_alerts_via_service(tmp_path: Path) -> None:
    clock = SimClock()
    clock.advance(T0)
    settings = Settings(data_dir=tmp_path, max_alerts_per_run=2, notify_mode="dry_run")
    email_rec = _RecorderNotifier("email")
    teams_rec = _RecorderNotifier("teams")
    service = NotifierService(
        settings=settings,
        clock=clock,
        email_notifier=email_rec,
        teams_notifier=teams_rec,
    )

    inc1_r1 = _make_incident(incident_id="INC-1", revision=1)
    # Lần 1 gửi INC-1 r1 -> SENT trên cả email và teams
    rep1 = await service.notify(inc1_r1)
    assert [r.status for r in rep1.results] == ["SENT", "SENT"]

    # Gửi lại cùng INC-1 r1 -> SKIPPED (already_sent)
    rep1_dup = await service.notify(inc1_r1)
    assert [r.status for r in rep1_dup.results] == ["SKIPPED", "SKIPPED"]
    assert all(r.detail == "already_sent" for r in rep1_dup.results)

    # Revision mới INC-1 r2 -> được gửi lại (alert thứ 2 trong run)
    inc1_r2 = _make_incident(incident_id="INC-1", revision=2)
    rep2 = await service.notify(inc1_r2)
    assert [r.status for r in rep2.results] == ["SENT", "SENT"]

    # INC-2 r1 vượt quá max_alerts_per_run=2 -> dừng gửi
    inc2_r1 = _make_incident(incident_id="INC-2", revision=1)
    rep3 = await service.notify(inc2_r1)
    assert [r.status for r in rep3.results] == ["SKIPPED", "SKIPPED"]
    assert all(r.detail == "max_alerts_per_run" for r in rep3.results)

    assert email_rec.sent_tags == ["INC-1:r1", "INC-1:r2"]
    assert teams_rec.sent_tags == ["INC-1:r1", "INC-1:r2"]


async def test_dry_run_does_not_block_subsequent_live_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = SimClock()
    clock.advance(T0)
    db_path = tmp_path / "notifier.sqlite"
    app = create_app(db_path=db_path, clock=clock)
    asgi_transport = httpx.ASGITransport(app=app)

    sent_live_emails: list[object] = []

    async def fake_smtp_send(msg: object, **kwargs: object) -> object:
        sent_live_emails.append(msg)
        return ({}, "250 OK")

    monkeypatch.setattr(aiosmtplib, "send", fake_smtp_send)

    inc = _make_incident(incident_id="INC-20261104-0077", revision=1)

    # Bước 1: chạy dry_run trước -> ghi preview file, không gọi SMTP
    dry_settings = Settings(
        data_dir=tmp_path,
        notifier_db=db_path,
        teams_url="http://mock-teams",
        feedback_base_url="http://mock-teams",
        notify_mode="dry_run",
    )
    dry_service = NotifierService(
        settings=dry_settings,
        clock=clock,
        teams_notifier=TeamsMockNotifier(settings=dry_settings, transport=asgi_transport),
    )
    rep_dry = await dry_service.notify(inc)
    assert [r.status for r in rep_dry.results] == ["SENT", "SENT"]
    assert len(sent_live_emails) == 0
    assert (tmp_path / "outbox_preview" / "INC-20261104-0077-r1.html").exists()

    # Bước 2: chạy live trên cùng file notifier_db -> gửi mail qua SMTP
    live_settings = Settings(
        data_dir=tmp_path,
        notifier_db=db_path,
        teams_url="http://mock-teams",
        feedback_base_url="http://mock-teams",
        notify_mode="live",
        smtp_user="ase-bot@ase.local",
        smtp_password=SecretStr("abcd1234efgh5678"),
        mail_to="ops@ase.local",
    )
    live_service = NotifierService(
        settings=live_settings,
        clock=clock,
        teams_notifier=TeamsMockNotifier(settings=live_settings, transport=asgi_transport),
    )
    rep_live = await live_service.notify(inc)
    assert [r.status for r in rep_live.results] == ["SENT", "SENT"]
    assert len(sent_live_emails) == 1

    # Bước 3: chạy lại live lần 2 -> bị chặn bởi outbox live (không gửi trùng)
    rep_live_dup = await live_service.notify(inc)
    assert [r.status for r in rep_live_dup.results] == ["SKIPPED", "SKIPPED"]
    assert len(sent_live_emails) == 1


async def test_retry_429_respects_retry_after_and_caps_at_60s_and_400_no_retry(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path, teams_url="http://mock-teams.local")
    inc = _make_incident()
    alert = route_incident(inc)
    assert alert is not None

    slept: list[float] = []

    async def record_sleep(sec: float) -> None:
        slept.append(sec)

    # Case 1: 429 kèm Retry-After=3.5 rồi Retry-After=3600 (chặn trần 60s) rồi 200 OK
    attempts_429 = 0

    def handler_429(request: httpx.Request) -> httpx.Response:
        nonlocal attempts_429
        attempts_429 += 1
        if attempts_429 == 1:
            return httpx.Response(429, headers={"Retry-After": "3.5"}, json={"error": "rate_limit"})
        if attempts_429 == 2:
            return httpx.Response(
                429, headers={"Retry-After": "3600"}, json={"error": "rate_limit"}
            )
        return httpx.Response(200, json={"status": "ok"})

    client_429 = TeamsMockNotifier(
        settings=settings,
        transport=httpx.MockTransport(handler_429),
        sleep=record_sleep,
    )
    res = await client_429.send(alert)
    assert res.status == "SENT"
    assert attempts_429 == 3
    assert slept == [3.5, 60.0]

    # Case 2: 400 Bad Request -> không retry
    slept.clear()
    attempts_400 = 0

    def handler_400(request: httpx.Request) -> httpx.Response:
        nonlocal attempts_400
        attempts_400 += 1
        return httpx.Response(400, json={"error": "bad_request"})

    client_400 = TeamsMockNotifier(
        settings=settings,
        transport=httpx.MockTransport(handler_400),
        sleep=record_sleep,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client_400.send(alert)
    assert attempts_400 == 1
    assert slept == []


async def test_smtp_dry_run_preview_and_live_config_and_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inc = _make_incident(incident_id="INC-20261104-0042", revision=1)
    alert = route_incident(inc)
    assert alert is not None

    # 1. dry_run ghi file .html và .eml, không gọi aiosmtplib.send
    smtp_called = False

    async def forbidden_smtp(*args: object, **kwargs: object) -> object:
        nonlocal smtp_called
        smtp_called = True
        return ({}, "OK")

    monkeypatch.setattr(aiosmtplib, "send", forbidden_smtp)

    dry_settings = Settings(
        data_dir=tmp_path,
        notify_mode="dry_run",
        smtp_user=None,
        smtp_password=None,
        mail_to=None,
    )
    dry_notifier = SmtpEmailNotifier(dry_settings)
    res_dry = await dry_notifier.send(alert)
    assert res_dry.status == "SENT"
    assert not smtp_called
    assert (tmp_path / "outbox_preview" / "INC-20261104-0042-r1.html").exists()
    assert (tmp_path / "outbox_preview" / "INC-20261104-0042-r1.eml").exists()

    # 2. live thiếu cấu hình -> NotifierConfigError
    bad_live_settings = Settings(
        data_dir=tmp_path,
        notify_mode="live",
        smtp_user=None,
        smtp_password=None,
        mail_to=None,
    )
    bad_notifier = SmtpEmailNotifier(bad_live_settings)
    with pytest.raises(NotifierConfigError, match="ASE_MAIL_TO"):
        await bad_notifier.send(alert)

    # 3. live đủ cấu hình (Mailpit local không cần user/password) -> gọi aiosmtplib.send
    sent_messages: list[tuple[object, dict[str, object]]] = []

    async def fake_smtp_send(msg: object, **kwargs: object) -> object:
        sent_messages.append((msg, kwargs))
        return ({}, "250 OK")

    monkeypatch.setattr(aiosmtplib, "send", fake_smtp_send)
    live_settings = Settings(
        data_dir=tmp_path,
        notify_mode="live",
        smtp_host="localhost",
        smtp_port=1025,
        smtp_tls="none",
        smtp_user=None,
        smtp_password=None,
        mail_from="ase-alerts@ase.local",
        mail_to="ops@ase.local",
    )
    live_notifier = SmtpEmailNotifier(live_settings)
    res_live = await live_notifier.send(alert)
    assert res_live.status == "SENT"
    assert len(sent_messages) == 1
    assert sent_messages[0][1]["hostname"] == "localhost"
    assert sent_messages[0][1]["port"] == 1025
    assert "username" not in sent_messages[0][1]
    assert sent_messages[0][1]["use_tls"] is False
    assert sent_messages[0][1]["start_tls"] is False

    # 4. SMTP retry: 421 tạm thời được retry; 535 xác thực không retry
    slept_smtp: list[float] = []

    async def record_smtp_sleep(sec: float) -> None:
        slept_smtp.append(sec)

    smtp_attempts = 0

    async def flaky_smtp_send(msg: object, **kwargs: object) -> object:
        nonlocal smtp_attempts
        smtp_attempts += 1
        if smtp_attempts == 1:
            raise aiosmtplib.SMTPResponseException(421, "Service temporarily unavailable")
        return ({}, "250 OK")

    inc_retry = _make_incident(incident_id="INC-20261104-0043", revision=1)
    alert_retry = route_incident(inc_retry)
    assert alert_retry is not None
    flaky_notifier = SmtpEmailNotifier(
        live_settings, send_fn=flaky_smtp_send, sleep=record_smtp_sleep
    )
    res_flaky = await flaky_notifier.send(alert_retry)
    assert res_flaky.status == "SENT"
    assert smtp_attempts == 2
    assert len(slept_smtp) == 1

    auth_attempts = 0

    async def auth_fail_smtp_send(msg: object, **kwargs: object) -> object:
        nonlocal auth_attempts
        auth_attempts += 1
        raise aiosmtplib.SMTPAuthenticationError(535, "Authentication credentials invalid")

    auth_fail_notifier = SmtpEmailNotifier(
        live_settings, send_fn=auth_fail_smtp_send, sleep=record_smtp_sleep
    )
    with pytest.raises(aiosmtplib.SMTPAuthenticationError):
        await auth_fail_notifier.send(alert_retry)
    assert auth_attempts == 1


async def test_mock_teams_webhook_revision_replace_and_feedback(tmp_path: Path) -> None:
    clock = SimClock()
    clock.advance(T0)
    db_path = tmp_path / "notifier.sqlite"
    app = create_app(db_path=db_path, clock=clock)
    asgi_transport = httpx.ASGITransport(app=app)

    settings = Settings(
        data_dir=tmp_path,
        notifier_db=db_path,
        teams_url="http://mock-teams",
        feedback_base_url="http://mock-teams",
        notify_mode="dry_run",
    )
    teams_notifier = TeamsMockNotifier(settings=settings, transport=asgi_transport)
    service = NotifierService(
        settings=settings,
        clock=clock,
        teams_notifier=teams_notifier,
    )

    # Gửi r1
    inc_r1 = _make_incident(
        incident_id="INC-20261104-0001",
        revision=1,
        category=Category.SYSTEM,
        probable_cause="Nguyên nhân ban đầu từ rule",
    )
    rep1 = await service.notify(inc_r1)
    assert all(r.status == "SENT" for r in rep1.results)

    async with httpx.AsyncClient(transport=asgi_transport, base_url="http://mock-teams") as client:
        page1 = await client.get("/?channel=it-ops")
        assert page1.status_code == 200
        assert "INC-20261104-0001" in page1.text
        assert "r1" in page1.text
        assert "Nguyên nhân ban đầu từ rule" in page1.text

        # Gửi r2 cho cùng incident_id -> cập nhật card cũ, không nhân bản card
        inc_r2 = inc_r1.model_copy(
            update={
                "revision": 2,
                "severity": Severity.P1,
                "probable_cause": "Nguyên nhân cập nhật từ LLM (r2)",
            }
        )
        rep2 = await service.notify(inc_r2)
        assert all(r.status == "SENT" for r in rep2.results)

        page2 = await client.get("/?channel=it-ops")
        assert page2.status_code == 200
        assert page2.text.count('data-incident-id="INC-20261104-0001"') == 1
        assert "r2" in page2.text
        assert "Nguyên nhân cập nhật từ LLM (r2)" in page2.text
        assert "Nguyên nhân ban đầu từ rule" not in page2.text

        # 1. Trình quét link chỉ gửi GET: trả trang xác nhận, KHÔNG ghi database
        fb_get = await client.get("/feedback/INC-20261104-0001/confirm")
        assert fb_get.status_code == 200
        assert "Xác nhận gửi phản hồi" in fb_get.text
        assert "Đã ghi nhận" not in fb_get.text

        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM feedback") as cursor:
                row_get = await cursor.fetchone()
                assert row_get is not None
                assert row_get[0] == 0

        page_after_get = await client.get("/?channel=it-ops")
        assert "Xác nhận đúng (confirm)" not in page_after_get.text

        # 2. Người dùng nhấn submit POST: ghi nhận phản hồi vào database
        fb_post = await client.post("/feedback/INC-20261104-0001/confirm")
        assert fb_post.status_code == 200
        assert "Đã ghi nhận" in fb_post.text

        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM feedback") as cursor:
                row_post = await cursor.fetchone()
                assert row_post is not None
                assert row_post[0] == 1

        page3 = await client.get("/?channel=it-ops")
        assert "Xác nhận đúng (confirm)" in page3.text


async def test_demo_three_sample_incidents_end_to_end(tmp_path: Path) -> None:
    clock = SimClock()
    clock.advance(T0)
    db_path = tmp_path / "notifier.sqlite"
    app = create_app(db_path=db_path, clock=clock)
    asgi_transport = httpx.ASGITransport(app=app)

    settings = Settings(
        data_dir=tmp_path,
        notifier_db=db_path,
        preview_dir=tmp_path / "outbox_preview",
        teams_url="http://mock-teams",
        feedback_base_url="http://mock-teams",
        notify_mode="dry_run",
    )

    samples = sample_incidents()
    assert len(samples) == 3
    # Kiểm chứng định dạng ID chuẩn simulator và PAID_NOT_DELIVERED dừng ở bước 5
    assert samples[0].station_id == "VN-ST-001"
    assert samples[0].cabinet_id == "VN-ST-001-CAB-01"
    assert samples[1].station_id == "PH-ST-001"
    assert samples[2].error_code == "PAID_NOT_DELIVERED"
    assert samples[2].step is Step.PAYMENT
    assert all("-6-" not in ev and "-7-" not in ev for ev in samples[2].evidence)

    service = NotifierService(
        settings=settings,
        clock=clock,
        teams_notifier=TeamsMockNotifier(settings=settings, transport=asgi_transport),
    )
    reports = await run_demo(live=False, settings=settings, clock=clock, service=service)
    assert len(reports) == 3
    for rep in reports:
        assert all(r.status == "SENT" for r in rep.results)

    # 3 file preview .html và .eml được sinh
    for inc in samples:
        assert (settings.preview_dir / f"{inc.incident_id}-r1.html").exists()
        assert (settings.preview_dir / f"{inc.incident_id}-r1.eml").exists()

    # Chạy lại lần 2 -> tất cả bị SKIPPED do idempotency outbox
    reports_dup = await run_demo(live=False, settings=settings, clock=clock, service=service)
    for rep_dup in reports_dup:
        assert all(r.status == "SKIPPED" for r in rep_dup.results)


async def test_smtp_tls_mapping_and_authentication(tmp_path: Path) -> None:
    inc = _make_incident()
    alert = route_incident(inc)
    assert alert is not None

    captured: list[dict[str, object]] = []

    async def mock_send(msg: object, **kwargs: object) -> object:
        captured.append(kwargs)
        return ({}, "250 OK")

    # Case 1: smtp_tls="none" và không có user -> không TLS, không gửi auth
    s_none = Settings(
        data_dir=tmp_path,
        notify_mode="live",
        smtp_host="localhost",
        smtp_port=1025,
        smtp_tls="none",
        smtp_user=None,
        smtp_password=None,
        mail_from="ase-alerts@ase.local",
        mail_to="ops@ase.local",
    )
    notifier_none = SmtpEmailNotifier(s_none, send_fn=mock_send)
    res_none = await notifier_none.send(alert)
    assert res_none.status == "SENT"
    assert captured[-1]["use_tls"] is False
    assert captured[-1]["start_tls"] is False
    assert "username" not in captured[-1]
    assert "password" not in captured[-1]

    # Case 2: smtp_tls="starttls" và có user -> start_tls=True, có username & password
    s_starttls = Settings(
        data_dir=tmp_path,
        notify_mode="live",
        smtp_host="localhost",
        smtp_port=587,
        smtp_tls="starttls",
        smtp_user="corp_user",
        smtp_password=SecretStr("p@ssword"),
        mail_from="ase-alerts@ase.local",
        mail_to="ops@ase.local",
    )
    notifier_starttls = SmtpEmailNotifier(s_starttls, send_fn=mock_send)
    res_starttls = await notifier_starttls.send(alert)
    assert res_starttls.status == "SENT"
    assert captured[-1]["use_tls"] is False
    assert captured[-1]["start_tls"] is True
    assert captured[-1]["username"] == "corp_user"
    assert captured[-1]["password"] == "p@ssword"

    # Case 3: smtp_tls="ssl" -> use_tls=True
    s_ssl = Settings(
        data_dir=tmp_path,
        notify_mode="live",
        smtp_host="localhost",
        smtp_port=465,
        smtp_tls="ssl",
        smtp_user=None,
        smtp_password=None,
        mail_from="ase-alerts@ase.local",
        mail_to="ops@ase.local",
    )
    notifier_ssl = SmtpEmailNotifier(s_ssl, send_fn=mock_send)
    res_ssl = await notifier_ssl.send(alert)
    assert res_ssl.status == "SENT"
    assert captured[-1]["use_tls"] is True
    assert captured[-1]["start_tls"] is False


async def test_smtp_insecure_auth_rejected_on_remote_host(tmp_path: Path) -> None:
    inc = _make_incident()
    alert = route_incident(inc)
    assert alert is not None

    # Host từ xa + tls="none" + có smtp_user -> từ chối gửi mật khẩu dạng rõ
    remote_settings = Settings(
        data_dir=tmp_path,
        notify_mode="live",
        smtp_host="mail.company.internal",
        smtp_port=25,
        smtp_tls="none",
        smtp_user="corp_user",
        smtp_password=SecretStr("secret"),
        mail_from="ase-alerts@ase.local",
        mail_to="ops@ase.local",
    )
    notifier_remote = SmtpEmailNotifier(remote_settings)
    with pytest.raises(NotifierConfigError, match="Không được dùng smtp_tls='none'"):
        await notifier_remote.send(alert)

    # Localhost + tls="none" + có smtp_user -> được chấp nhận (dành cho test/local)
    local_settings = Settings(
        data_dir=tmp_path,
        notify_mode="live",
        smtp_host="127.0.0.1",
        smtp_port=1025,
        smtp_tls="none",
        smtp_user="test_user",
        smtp_password=SecretStr("secret"),
        mail_from="ase-alerts@ase.local",
        mail_to="ops@ase.local",
    )

    async def mock_send(msg: object, **kwargs: object) -> object:
        return ({}, "250 OK")

    notifier_local = SmtpEmailNotifier(local_settings, send_fn=mock_send)
    res = await notifier_local.send(alert)
    assert res.status == "SENT"


async def test_smtp_live_config_missing_keys(tmp_path: Path) -> None:
    inc = _make_incident()
    alert = route_incident(inc)
    assert alert is not None

    settings = Settings(
        data_dir=tmp_path,
        notify_mode="live",
        mail_from=None,
        mail_to=None,
    )
    notifier = SmtpEmailNotifier(settings)
    with pytest.raises(NotifierConfigError) as exc_info:
        await notifier.send(alert)

    err = str(exc_info.value)
    assert "ASE_MAIL_FROM" in err
    assert "ASE_MAIL_TO" in err


async def test_smtp_connection_failure_fail_fast(tmp_path: Path) -> None:
    inc1 = _make_incident(incident_id="INC-20261104-0011")
    inc2 = _make_incident(incident_id="INC-20261104-0012")
    alert1 = route_incident(inc1)
    alert2 = route_incident(inc2)
    assert alert1 is not None and alert2 is not None

    send_call_count = 0

    async def conn_fail_send(msg: object, **kwargs: object) -> object:
        nonlocal send_call_count
        send_call_count += 1
        raise aiosmtplib.SMTPConnectError("Connection refused by target machine")

    settings = Settings(
        data_dir=tmp_path,
        notify_mode="live",
        smtp_host="localhost",
        smtp_port=1025,
        smtp_tls="none",
        mail_from="ase-alerts@ase.local",
        mail_to="ops@ase.local",
    )
    notifier = SmtpEmailNotifier(settings, send_fn=conn_fail_send)

    # Lần 1: thử kết nối bị lỗi -> trả FAILED kèm hint
    res1 = await notifier.send(alert1)
    assert res1.status == "FAILED"
    assert res1.detail is not None
    assert "Mailpit chưa chạy? `make mail-up`" in res1.detail
    assert send_call_count == 1

    # Lần 2 (cùng phiên notifier): dừng thử nhanh, trả FAILED ngay, không gọi send_fn lần nữa
    res2 = await notifier.send(alert2)
    assert res2.status == "FAILED"
    assert res2.detail is not None
    assert "Mailpit chưa chạy? `make mail-up`" in res2.detail
    assert send_call_count == 1  # Không bị gọi lại


def _is_mailpit_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 1025), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _is_mailpit_running(), reason="Mailpit không chạy trên localhost:1025")
async def test_mailpit_e2e_integration(tmp_path: Path) -> None:
    clock = SimClock()
    clock.advance(T0)
    db_path = tmp_path / "notifier.sqlite"
    app = create_app(db_path=db_path, clock=clock)
    asgi_transport = httpx.ASGITransport(app=app)

    settings = Settings(
        data_dir=tmp_path,
        notifier_db=db_path,
        preview_dir=tmp_path / "outbox_preview",
        teams_url="http://mock-teams",
        feedback_base_url="http://mock-teams",
        notify_mode="live",
        smtp_host="localhost",
        smtp_port=1025,
        smtp_tls="none",
        mail_from="ase-alerts@ase.local",
        # Địa chỉ nhận riêng cho lần chạy test: chỉ đếm/xoá mail của test, KHÔNG đụng mail demo
        mail_to=f"e2e-{tmp_path.name}@ase.local",
    )
    service = NotifierService(
        settings=settings,
        clock=clock,
        teams_notifier=TeamsMockNotifier(settings=settings, transport=asgi_transport),
    )

    mailpit_base = "http://127.0.0.1:8025/api/v1"

    async def own_messages(client: httpx.AsyncClient) -> list[dict[str, object]]:
        resp = await client.get(f"{mailpit_base}/messages", params={"limit": 500})
        assert resp.status_code == 200
        msgs: list[dict[str, object]] = resp.json()["messages"]
        return [
            m
            for m in msgs
            if any(t.get("Address") == settings.mail_to for t in m.get("To") or [])  # type: ignore[attr-defined]
        ]

    async with httpx.AsyncClient() as client:
        # Chạy demo live gửi thẳng tới Mailpit
        reports = await run_demo(live=True, settings=settings, clock=clock, service=service)
        assert len(reports) == 3
        for rep in reports:
            assert all(r.status == "SENT" for r in rep.results)

        # Kiểm tra 3 email của chính test trong Mailpit
        messages = await own_messages(client)
        assert len(messages) == 3
        for msg in messages:
            assert str(msg["Subject"]).startswith("[ASE]")

        # Kiểm tra incident PH có GMT+8
        ph_found = False
        for m in messages:
            detail_resp = await client.get(f"{mailpit_base}/message/{m['ID']}")
            if detail_resp.status_code == 200:
                body_text = detail_resp.text
                if "GMT+8" in body_text:
                    ph_found = True
                    break
        assert ph_found, "Không tìm thấy mail PH chứa GMT+8 trong Mailpit"

        # Chạy lại -> idempotency: 0 mail mới sinh ra
        reports_dup = await run_demo(live=True, settings=settings, clock=clock, service=service)
        for rep_dup in reports_dup:
            assert all(r.status == "SKIPPED" for r in rep_dup.results)
        assert len(await own_messages(client)) == 3

        # Dọn dẹp: chỉ xoá mail do test gửi, giữ nguyên mail demo trong hộp thư
        ids = [m["ID"] for m in await own_messages(client)]
        await client.request("DELETE", f"{mailpit_base}/messages", json={"IDs": ids})


async def test_incidents_from_profile_point_to_real_sessions_and_events(tmp_path: Path) -> None:
    """Incident mẫu cho demo phải trỏ tới phiên/event CÓ THẬT trong dữ liệu đang hiển thị."""
    import orjson
    import polars as pl

    from ase.notifier.demo import incidents_from_profile
    from ase.simulator.generator import generate

    out = tmp_path / "smoke"
    await generate("smoke", out)
    incs = incidents_from_profile(out)
    assert {i.category for i in incs} >= {Category.SYSTEM, Category.PAYMENT}  # smoke: deploy + P1

    sessions = set(pl.read_parquet(out / "swap_sessions.parquet")["session_id"].to_list())
    events = {
        orjson.loads(line)["event_id"]
        for line in (out / "ase.swap.events.jsonl").read_bytes().splitlines()
    }
    for inc in incs:
        assert inc.incident_id.startswith("INC-DEMO-")
        assert "MẪU" in inc.probable_cause
        assert set(inc.session_ids) <= sessions
        assert inc.evidence and set(inc.evidence) <= events  # SF9: evidence có thật
        assert inc.first_seen_at <= inc.last_seen_at
    p1 = [i for i in incs if i.severity is Severity.P1]
    assert p1 and p1[0].error_code == "PAID_NOT_DELIVERED"
