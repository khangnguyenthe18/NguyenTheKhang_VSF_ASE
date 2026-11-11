"""Simulator danh sách giao dịch + agent quét 30 phút + thông báo + evaluator (OPS_AGENT_PLAN)."""

from __future__ import annotations

import asyncio
import email
import email.policy
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest

from ase.agent.__main__ import load_digests
from ase.agent.__main__ import run as run_agent
from ase.agent.analyzer import Analyzer, poisson_tail
from ase.agent.models import Detection, ScanDigest, ScanResult
from ase.agent.runner import ScanAgent, scan_ticks
from ase.agent.scanner import Scanner
from ase.agent.source import MockTransactionSource, view_as_of
from ase.common.config import Settings
from ase.common.models import AlertLevel, SwapTransaction, TxnStatus
from ase.common.transport import SimClock
from ase.evaluator.txn_eval import evaluate, load_ground_truth, render_markdown
from ase.knowledge.loader import default_error_table, default_knowledge
from ase.notifier.email_smtp import build_mime_message
from ase.notifier.render import RenderedEmail
from ase.notifier.scan_digest import ScanNotifier, build_scan_card, render_scan_email
from ase.notifier.teams_mock_client import TeamsMockNotifier
from ase.simulator.txn_generator import (
    TXN_GROUND_TRUTH,
    generate_transactions,
    load_txn_scenarios,
    simulate_transactions,
)

TABLE = default_error_table()
SPEC = default_knowledge().detector.scan
T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def smoke_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("txn_smoke") / "smoke"
    asyncio.run(generate_transactions("smoke", out))
    return out


# --- simulator --------------------------------------------------------------------------------


def test_scenario_codes_all_in_standard_table() -> None:
    assert load_txn_scenarios().all_codes() <= TABLE.codes


def test_generator_is_deterministic_and_matches_real_screen_format() -> None:
    a, b = simulate_transactions("smoke"), simulate_transactions("smoke")
    assert a.transactions == b.transactions and a.ground_truth == b.ground_truth
    assert len(a.transactions) == len(a.ground_truth) > 200
    for t in a.transactions[:50]:
        assert t.station_id.startswith("B.") and t.cabinet_id is not None
        assert t.cabinet_id.startswith(t.station_id + ".")
        assert len(t.emsp_txn_id) == 24 and int(t.emsp_txn_id, 16) >= 0
        assert t.cabinet_serial is not None and t.cabinet_serial.startswith("BSS-")
        assert t.cabinet_type in {"CC_06_P", "CC_12_P"}
    for t, g in zip(a.transactions, a.ground_truth, strict=True):
        assert t.emsp_txn_id == g.emsp_txn_id
        assert (t.status is TxnStatus.FAILED) == (t.error_code is not None)
        assert g.is_failure == (t.status is not TxnStatus.COMPLETED)
        if t.error_code:
            assert TABLE.has(t.error_code) and g.level is TABLE.lookup(t.error_code).level  # type: ignore[union-attr]
        if t.status is TxnStatus.COMPLETED:
            assert t.battery_in_id and t.battery_out_id and t.battery_out_at
        else:
            assert t.battery_out_id is None  # lỗi/kẹt: không bao giờ có pin mới
    kinds = Counter(g.kind for g in a.ground_truth if g.is_failure)
    assert kinds["FAILED"] > 0 and kinds["STUCK"] > 0
    assert any(g.scenario == "slow_but_completed" and not g.is_failure for g in a.ground_truth)


# --- nguồn dữ liệu ----------------------------------------------------------------------------


def _txn(i: int, **kw: object) -> SwapTransaction:
    base: dict[str, object] = {
        "station_id": "B.TST00001",
        "cabinet_id": "B.TST00001.001",
        "cabinet_type": "CC_06_P",
        "emsp_txn_id": f"{i:024x}",
        "started_at": T0 + timedelta(minutes=5),
        "status": TxnStatus.COMPLETED,
        "country": "VN",
        "timezone": "Asia/Ho_Chi_Minh",
        "updated_at": T0,
    }
    return SwapTransaction.model_validate({**base, **kw})


def test_view_as_of_hides_the_future() -> None:
    start = T0 + timedelta(minutes=5)
    t_in, t_end = start + timedelta(seconds=30), start + timedelta(seconds=70)
    ok = _txn(
        1,
        battery_in_id="BATA",
        battery_in_at=t_in,
        battery_out_id="BATB",
        battery_out_at=t_end,
        updated_at=t_end,
    )
    failed = _txn(
        2,
        status=TxnStatus.FAILED,
        error_code="D6038",
        battery_in_id="BATC",
        battery_in_at=t_in,
        updated_at=t_end,
    )
    df = pl.DataFrame([t.model_dump() for t in (ok, failed)], infer_schema_length=None)

    def at(t: datetime) -> list[dict[str, object]]:
        return view_as_of(df, t).sort("emsp_txn_id").to_dicts()

    assert at(start - timedelta(seconds=1)) == []
    early = at(start + timedelta(seconds=10))
    assert [r["status"] for r in early] == ["CREATED", "CREATED"]
    assert all(r["battery_in_id"] is None and r["error_code"] is None for r in early)
    mid = at(t_in)
    assert [r["status"] for r in mid] == ["BATTERY_IN", "BATTERY_IN"]
    assert mid[0]["battery_in_id"] == "BATA" and mid[0]["battery_out_id"] is None
    assert mid[1]["error_code"] is None and mid[1]["updated_at"] == t_in
    late = at(t_end)
    assert [r["status"] for r in late] == ["COMPLETED", "FAILED"]
    assert late[0]["battery_out_id"] == "BATB" and late[1]["error_code"] == "D6038"


# --- scanner ----------------------------------------------------------------------------------


class _ListSource:
    def __init__(self, txns: list[SwapTransaction]) -> None:
        self.txns = txns

    def fetch(self, as_of: datetime, since: datetime) -> list[SwapTransaction]:
        return [t for t in self.txns if since < t.started_at <= as_of]


def test_scanner_reports_each_transaction_once_and_flags_stuck_by_threshold() -> None:
    scan_at = T0 + timedelta(minutes=30)
    txns = [
        _txn(1, status=TxnStatus.FAILED, error_code="C6029", updated_at=T0 + timedelta(minutes=1)),
        _txn(2, status=TxnStatus.FAILED, error_code="X0000"),  # mã giả, ngoài bảng chuẩn
        _txn(3, status=TxnStatus.CREATED, updated_at=scan_at - timedelta(minutes=10)),  # kẹt
        _txn(
            4,
            status=TxnStatus.BATTERY_IN,
            battery_in_id="B",
            battery_in_at=T0,
            updated_at=scan_at - timedelta(minutes=9, seconds=59),
        ),
        _txn(5),
    ]
    scanner = Scanner(_ListSource(txns), TABLE, SPEC)
    res = scanner.scan(scan_at)
    assert res.n_scanned == 5 and res.new_by_cabinet == {("B.TST00001", "B.TST00001.001"): 5}
    by_id = {int(d.emsp_txn_id, 16): d for d in res.detections}
    assert set(by_id) == {1, 2, 3}
    assert by_id[1].error_name == "LockTimeoutError" and by_id[1].level is AlertLevel.HIGH
    assert by_id[2].in_table is False and by_id[2].level is None and by_id[2].error_name is None
    assert by_id[3].kind == "STUCK" and by_id[3].event_at == scan_at
    assert all(d.detected_at == scan_at for d in res.detections)

    again = scanner.scan(scan_at + timedelta(minutes=30))  # giao dịch 4 giờ đã quá ngưỡng
    assert [int(d.emsp_txn_id, 16) for d in again.detections] == [4]
    assert again.detections[0].returned_not_received is True
    assert again.new_by_cabinet == {} and len(scanner.seen) == 5


# --- analyzer ---------------------------------------------------------------------------------


def test_poisson_tail() -> None:
    assert poisson_tail(0, 1.0) == 1.0
    assert poisson_tail(1, 1.0) == pytest.approx(0.6321, abs=1e-4)
    assert poisson_tail(3, 0.16) == pytest.approx(0.0006, abs=1e-4)
    assert poisson_tail(2, 0.16) > SPEC.baseline_alpha > poisson_tail(3, 0.16)


def _det(
    i: int,
    code: str | None,
    station: str = "B.TST00001",
    cab: str = "001",
    country: str = "VN",
    at: datetime = T0,
    kind: str = "FAILED",
) -> Detection:
    row = TABLE.lookup(code)
    return Detection(
        emsp_txn_id=f"{i:024x}",
        kind="FAILED" if kind == "FAILED" else "STUCK",
        status=TxnStatus.FAILED if kind == "FAILED" else TxnStatus.CREATED,
        station_id=station,
        cabinet_id=f"{station}.{cab}",
        country=country,
        timezone="Asia/Ho_Chi_Minh",
        error_code=code,
        error_name=row.name if row else None,
        level=row.level if row else None,
        in_table=row is not None,
        started_at=at,
        event_at=at,
        detected_at=at,
    )


def _result(dets: list[Detection], per_cabinet: int = 8) -> ScanResult:
    cabs = {(d.station_id, d.cabinet_id) for d in dets}
    return ScanResult(
        n_scanned=per_cabinet * len(cabs),
        detections=dets,
        new_by_cabinet={c: per_cabinet for c in cabs},
    )


def test_analyzer_groups_by_distribution_and_ignores_background_noise() -> None:
    a = Analyzer(SPEC)
    dets = [
        # Lan rộng: B0207 ở 3 trạm, 2 nước
        *[
            _det(i, "B0207", station=f"B.TST0000{i % 3 + 1}", country="PH" if i % 3 == 2 else "VN")
            for i in range(1, 7)
        ],
        # Mất điện trạm 5: mã High ở 2 tủ
        _det(10, "A0122", "B.TST00005", "001"),
        _det(11, "B6025", "B.TST00005", "002"),
        _det(12, "B6025", "B.TST00005", "002"),
        # Một mã High đơn lẻ tại một tủ → sự cố mức tủ
        _det(20, "C6100", "B.TST00006", "001"),
        # Lỗi nền mức Low rải rác: không thành sự cố
        _det(30, "D6031", "B.TST00007", "001"),
        _det(31, "D6033", "B.TST00007", "002"),
        _det(32, "D6031", "B.TST00008", "001"),
    ]
    incidents = {i.key: i for i in a.analyze(T0, _result(dets))}
    assert set(incidents) == {"WIDE|B0207", "STATION|B.TST00005", "CABINET|B.TST00006.001"}
    assert incidents["WIDE|B0207"].scope == "GLOBAL (PH, VN)"
    assert len(incidents["WIDE|B0207"].txn_ids) == 6
    assert incidents["STATION|B.TST00005"].groups == ["A0122", "B6025"]
    assert all(i.level is AlertLevel.HIGH and i.revision == 1 for i in incidents.values())
    assert [d.incident_id for d in dets[-3:]] == [None, None, None]
    assert all(d.incident_id for d in dets[:-3])


def test_analyzer_low_code_needs_to_exceed_baseline_and_incident_continues() -> None:
    a = Analyzer(SPEC)
    # Lịch sử: D6031 là lỗi nền quen thuộc (2% giao dịch)
    a.baseline.update(T0 - timedelta(hours=1), 5000, Counter({"D6031": 100}))
    assert a.baseline.rate("D6031") > 10 * a.baseline.rate("D6038")
    two = [_det(1, "D6031"), _det(2, "D6031")]
    assert a.analyze(T0, _result(two)) == []  # 2 lỗi D6031 trên 8 giao dịch: chưa vượt nền
    t1 = T0 + timedelta(minutes=30)
    four = [_det(i, "D6038", at=t1) for i in range(10, 14)]
    (first,) = a.analyze(t1, _result(four))
    assert first.kind == "CABINET" and first.level is AlertLevel.LOW and first.n_total == 4

    # Lần quét sau: 1 lỗi cùng mã nối vào sự cố đang mở; lỗi Low mã khác thì không.
    t2 = t1 + timedelta(minutes=30)
    nxt = [_det(20, "D6038", at=t2), _det(21, "D6033", at=t2)]
    (cont,) = a.analyze(t2, _result(nxt))
    assert cont.incident_id == first.incident_id and cont.revision == 2 and cont.n_total == 5
    assert nxt[0].incident_id == first.incident_id and nxt[1].incident_id is None

    # Quá thời gian nối của sự cố mức tủ → mở sự cố mới
    t3 = t2 + timedelta(minutes=SPEC.continue_minutes["CABINET"] + 30)
    later = [_det(i, "D6038", at=t3) for i in range(30, 34)]
    (reopened,) = a.analyze(t3, _result(later))
    assert reopened.incident_id != first.incident_id and reopened.revision == 1


def test_scan_ticks_are_round_marks() -> None:
    ticks = list(scan_ticks(T0 + timedelta(minutes=7), T0 + timedelta(minutes=65), 30))
    assert ticks == [T0 + timedelta(minutes=m) for m in (30, 60, 90)]
    assert list(scan_ticks(T0, T0, 30)) == [T0 + timedelta(minutes=30)]


# --- thông báo --------------------------------------------------------------------------------


def _digest(smoke_dir: Path) -> ScanDigest:
    src = MockTransactionSource(smoke_dir)
    agent = ScanAgent(src, TABLE, SPEC)
    digests = list(agent.run_replay(*src.time_range, SimClock()))
    return max(digests, key=lambda g: len(g.detections))


def test_scan_email_and_card_content(smoke_dir: Path) -> None:
    digest = _digest(smoke_dir)
    assert digest.detections and digest.scan_id.startswith("scan:")
    mail = render_scan_email(digest, base_url="http://ops.example:8000", dashboard_profile="smoke")
    assert mail.subject.startswith("[ASE] Quét giao dịch đổi pin") and "(GMT+7)" in mail.subject
    assert "Dear Team OPS" in mail.text_body
    assert "AI chỉ gợi ý, quyết định cuối cùng thuộc về OPS." in mail.text_body
    for d in digest.detections:
        assert d.emsp_txn_id in mail.text_body and d.emsp_txn_id in mail.html_body
        assert d.emsp_txn_id in mail.csv_text
    for inc in digest.incidents:
        assert inc.incident_id in mail.text_body and inc.incident_id in mail.html_body
    assert "http://ops.example:8000/dashboard?profile=smoke&status=FAILED" in mail.text_body
    assert mail.csv_text.startswith("﻿Mã giao dịch trên eMSP")

    card = build_scan_card(digest)
    assert card["type"] == "AdaptiveCard" and card["incident_id"] == digest.scan_id
    assert card["revision"] == 1


def test_scan_email_marks_codes_outside_standard_table() -> None:
    dets = [_det(1, "X0000"), _det(2, None, kind="STUCK")]
    digest = ScanDigest(
        scan_id="scan:20261109T1000Z",
        window_start=T0,
        scan_at=T0 + timedelta(minutes=30),
        n_scanned=10,
        detections=dets,
        incidents=[],
    )
    mail = render_scan_email(digest)
    assert "X0000 – mã ngoài bảng chuẩn" in mail.text_body
    assert "Mã ngoài bảng chuẩn (cần người kiểm tra): 1" in mail.text_body
    assert "Giao dịch kẹt (không có mã lỗi)" in mail.text_body


def test_long_vietnamese_subject_keeps_its_spaces_in_mime() -> None:
    """Hồi quy: tiêu đề dài bị gập header làm mất dấu cách ("giao dịch lỗi" → "giao dịchlỗi")."""
    subject = (
        "[ASE] Quét giao dịch đổi pin 22:00–22:30 ngày 26/10/2026 (GMT+7): 1 giao dịch lỗi, 0 sự cố"
    )
    msg = build_mime_message(
        RenderedEmail(subject=subject, text_body="x", html_body="<p>x</p>"),
        mail_from="ase-agent@ase.local",
        mail_to="ops@ase.local",
    )
    parsed = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
    assert parsed["Subject"] == subject


async def test_scan_notifier_sends_once_per_scan_and_skips_empty(
    smoke_dir: Path, tmp_path: Path
) -> None:
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request.url.path)
        return httpx.Response(200, json={"status": "ok"})

    settings = Settings(
        data_dir=tmp_path,
        notifier_db=tmp_path / "n.sqlite",
        preview_dir=tmp_path / "preview",
        notify_mode="dry_run",
    )
    clock = SimClock()
    clock.advance(T0)
    notifier = ScanNotifier(
        settings,
        clock=clock,
        teams=TeamsMockNotifier(settings, transport=httpx.MockTransport(handler)),
    )
    digest = _digest(smoke_dir)
    first = await notifier.notify(digest)
    assert [(r.channel, r.status) for r in first] == [("email", "SENT"), ("teams", "SENT")]
    assert posted == ["/webhook/ops"]
    stem = digest.scan_id.replace(":", "-")
    assert (settings.preview_dir / f"{stem}.html").exists()
    eml = (settings.preview_dir / f"{stem}.eml").read_bytes()
    assert b"To: ops@ase.local" in eml and b"From: ase-agent@ase.local" in eml

    again = await notifier.notify(digest)  # chạy lại: không gửi trùng
    assert [(r.status, r.detail) for r in again] == [("SKIPPED", "already_sent")] * 2
    assert posted == ["/webhook/ops"]

    empty = digest.model_copy(update={"scan_id": "scan:x", "detections": [], "incidents": []})
    assert await notifier.notify(empty) == []  # không có lỗi → không gửi


# --- end-to-end + evaluator -------------------------------------------------------------------


async def test_agent_replay_and_evaluator_on_smoke(
    smoke_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASE_DATA_DIR", str(smoke_dir.parent))
    (tmp_path / "agent").mkdir()
    summary = await run_agent("smoke", tmp_path / "agent", notify=False, max_notify=None)
    assert summary.n_seen == summary.n_transactions > 200

    digests = load_digests(tmp_path / "agent" / "digests.jsonl")
    assert len(digests) == summary.n_scans
    assert len({g.scan_id for g in digests}) == len(digests)
    assert all(g.scan_at.minute in (0, 30) and g.scan_at.second == 0 for g in digests)

    truth = load_ground_truth(smoke_dir / TXN_GROUND_TRUTH)
    report = evaluate("smoke", digests, summary, truth)
    assert report.sf1 == 1.0
    assert report.sf2.recall == 1.0 and report.sf3 == 1.0 and report.n_trap_detected == 0
    assert report.sf4_code_level == 1.0
    assert report.sf5.max is not None and report.sf5.max <= SPEC.interval_minutes * 60
    assert report.sf7 == 1.0 and report.max_notifications_per_scan == 1
    assert report.n_instances >= 4 and report.n_instances_detected >= 1
    md = render_markdown(report)
    assert "| SF1 |" in md and "| SF8 |" in md


def test_telemetry_matches_transactions_and_overheat_scenario() -> None:
    from ase.simulator.txn_generator import slot_id_of

    r = simulate_transactions("smoke")
    tel = r.telemetry
    codes = {c.cabinet_code for c in r.cabinet_codes}
    assert set(tel["cabinet_id"].unique()) == codes  # telemetry dùng mã tủ vận hành
    # Mỗi giao dịch có pin vào → có dòng khoang đúng connector, đúng thời điểm
    slot_rows = tel.filter(pl.col("slot_id").is_not_null())
    keys = set(
        zip(slot_rows["cabinet_id"], slot_rows["slot_id"], slot_rows["heartbeat_at"], strict=True)
    )
    for t in r.transactions:
        if t.battery_in_at is not None:
            assert (t.cabinet_id, slot_id_of(t.slot_in or 0), t.battery_in_at) in keys
    # Connector không vượt số khoang của loại tủ
    n_slots = {"CC_06_P": 6, "CC_12_P": 12}
    for t in r.transactions:
        if t.slot_in:
            assert 1 <= int(t.slot_in) <= n_slots[t.cabinet_type or ""]
    # Kịch bản quá nhiệt: nhiệt độ heartbeat của đúng tủ đó vượt 55 °C trong khoảng sự cố
    by_internal = {c.cabinet_id: c.cabinet_code for c in r.cabinet_codes}
    hot = [i for i in r.instances if i.spec.overheat_temp_c]
    assert hot
    for inst in hot:
        assert inst.cabinet_id is not None
        window = tel.filter(
            (pl.col("cabinet_id") == by_internal[inst.cabinet_id])
            & pl.col("slot_id").is_null()
            & (pl.col("heartbeat_at") >= inst.start)
            & (pl.col("heartbeat_at") < inst.end)
        )
        max_temp = window["temperature"].max()
        assert isinstance(max_temp, float) and max_temp > 55
    normal = tel.filter(pl.col("slot_id").is_null())["temperature"].median()
    assert isinstance(normal, float) and normal < 40


def test_analyzer_merges_codes_of_one_critical_service_into_one_wide_incident() -> None:
    a = Analyzer(SPEC, {"D6036": "svc:payment_callback", "D6037": "svc:payment_callback"})
    dets = [
        _det(i, "D6036" if i % 2 else "D6037", station=f"B.TST0000{i % 4 + 1}") for i in range(1, 9)
    ]
    (inc,) = a.analyze(T0, _result(dets))
    assert inc.kind == "WIDE" and inc.key == "WIDE|svc:payment_callback"
    assert sorted(inc.groups) == ["D6036", "D6037"] and len(inc.txn_ids) == 8
    # Không có ánh xạ dịch vụ → mỗi mã một nhóm, D6037 riêng (4 lỗi) chưa đủ ngưỡng lan rộng
    b = Analyzer(SPEC)
    dets_b = [d.model_copy(update={"incident_id": None}) for d in dets]
    keys = {i.key for i in b.analyze(T0, _result(dets_b))}
    assert "WIDE|svc:payment_callback" not in keys


def test_traffic_matches_national_volume_and_city_density() -> None:
    """≈ 2,9 triệu GD/tháng cho toàn bộ điểm VN; thành phố lớn đông hơn mỗi điểm."""
    from ase.simulator.entities import build_world
    from ase.simulator.profiles import load_profiles
    from ase.simulator.real_sites import load_real_sites
    from ase.simulator.txn_generator import station_rates

    sites = load_real_sites()
    if not sites:
        pytest.skip("chưa có vinfast_bss.json.gz (dữ liệu ngoài, không commit)")
    profiles = load_profiles()
    traffic = profiles.txn_traffic
    common = profiles.common.model_copy(update={"users_per_station": 1, "slots_per_cabinet": 2})
    world = build_world(profiles.profiles["dev"].for_transactions(450), common, default_knowledge())
    rates = station_rates(world, traffic, sites)
    # Tổng kỳ vọng toàn quốc (mọi điểm thật) = monthly_total / days_per_month
    from collections import Counter

    per_province = Counter(s.province for s in sites)
    mean = len(sites) / len(per_province)
    national = sum(
        n * (per_province[p] / mean) ** traffic.province_alpha for p, n in per_province.items()
    )
    code = {st.station_id: st.site_code or "" for st in world.stations}
    any_st = next(iter(rates))
    prov = code[any_st].split(".")[1].rstrip("0123456789")
    base = rates[any_st] / (per_province[prov] / mean) ** traffic.province_alpha
    assert base * national == pytest.approx(traffic.monthly_total / traffic.days_per_month)
    by_prov: dict[str, float] = {}
    for st_id, r in rates.items():
        by_prov[code[st_id].split(".")[1].rstrip("0123456789")] = r
    assert by_prov["HCM"] > 1.5 * min(by_prov.values())  # thành phố lớn đông hơn mỗi điểm
    median = sorted(rates.values())[len(rates) // 2]
    assert 1.5 < median < 3.5  # bình quân 2–3 GD/điểm/ngày


def test_analyzer_accumulates_sparse_cabinet_failures_across_scans() -> None:
    """Mật độ thực tế: lỗi Low của một tủ rải rác qua nhiều lần quét vẫn thành sự cố."""
    a = Analyzer(SPEC)
    first = [_det(1, "D6038")]
    assert a.analyze(T0, _result(first, per_cabinet=1)) == []  # 1 lỗi: chưa đủ
    t1 = T0 + timedelta(hours=5)
    second = [_det(2, "D6038", at=t1)]
    (inc,) = a.analyze(t1, _result(second, per_cabinet=1))  # cộng dồn trong 24 giờ: 2 lỗi
    assert inc.kind == "CABINET" and inc.txn_ids == [second[0].emsp_txn_id]
    # Quá cửa sổ 24 giờ thì không cộng dồn với lỗi cũ
    b = Analyzer(SPEC)
    b.analyze(T0, _result([_det(10, "D6038")], per_cabinet=1))
    late = T0 + timedelta(minutes=SPEC.local_window_minutes + 30)
    assert b.analyze(late, _result([_det(11, "D6038", at=late)], per_cabinet=1)) == []


def test_station_outage_seen_at_two_cabinets_hours_apart_is_one_incident() -> None:
    """Mất điện cả trạm lộ ra ở tủ .001 rồi .002 sau vài giờ → một sự cố mức trạm."""
    a = Analyzer(SPEC)
    (first,) = a.analyze(T0, _result([_det(1, "A0122", cab="001")], per_cabinet=1))
    assert first.kind == "CABINET"
    t1 = T0 + timedelta(hours=5)
    (second,) = a.analyze(t1, _result([_det(2, "A0122", cab="002", at=t1)], per_cabinet=1))
    assert second.incident_id == first.incident_id
    assert second.kind == "STATION" and second.scope == "B.TST00001"
    # Lỗi tiếp theo ở tủ .001 vẫn vào cùng sự cố
    t2 = t1 + timedelta(hours=5)
    (third,) = a.analyze(t2, _result([_det(3, "B6025", cab="001", at=t2)], per_cabinet=1))
    assert third.incident_id == first.incident_id


def test_masked_site_codes_hide_real_codes_but_keep_province_and_traffic() -> None:
    from ase.simulator.profiles import load_profiles
    from ase.simulator.real_sites import load_real_sites

    sites = load_real_sites()
    if not sites:
        pytest.skip("chưa có vinfast_bss.json.gz (dữ liệu ngoài, không commit)")
    real = {s.code for s in sites}
    profiles = load_profiles()
    smoke = profiles.profiles["smoke"]
    masked_profiles = profiles.model_copy(
        update={
            "profiles": {
                **profiles.profiles,
                "smoke": smoke.model_copy(update={"mask_site_codes": True}),
            }
        }
    )
    plain, masked = (
        simulate_transactions("smoke"),
        simulate_transactions("smoke", profiles=masked_profiles),
    )
    plain_codes = [t.station_id for t in plain.transactions]
    masked_codes = [t.station_id for t in masked.transactions]
    assert set(plain_codes) <= real
    assert not set(masked_codes) & real  # không lộ mã thật nào
    assert [c.split(".")[1][:3] for c in plain_codes] == [c.split(".")[1][:3] for c in masked_codes]
    assert len(plain.transactions) == len(masked.transactions)  # cùng lưu lượng
    assert [t.status for t in plain.transactions] == [t.status for t in masked.transactions]
    assert all(st.site_code not in real for st in masked.stations)
    assert (
        masked.transactions == simulate_transactions("smoke", profiles=masked_profiles).transactions
    )
