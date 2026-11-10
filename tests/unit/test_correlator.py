"""Test Correlator, Dispatcher (lịch gửi), output và tin gom notifier (DETECTOR_PLAN §8)."""

from __future__ import annotations

import builtins
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from ase.common.config import Settings
from ase.common.models import (
    BatchRow,
    Battery,
    Category,
    Severity,
    Station,
    Step,
    SwapSession,
    TeamBatch,
)
from ase.detector.classifier import Classification
from ase.detector.context import ContextStore
from ase.detector.correlator import GLOBAL_COUNTRY, Correlator
from ase.detector.dispatch import Dispatcher
from ase.detector.fields import FieldView
from ase.detector.main import run_detector
from ase.detector.output import build_incident_records
from ase.knowledge.loader import Knowledge, default_knowledge
from ase.notifier.batch import build_batch_card, render_batch_email
from ase.notifier.email_smtp import SmtpEmailNotifier
from ase.notifier.router import Alert, SendResult
from ase.notifier.service import NotifierService
from ase.simulator.generator import generate
from ase.simulator.profiles import load_profiles

K = default_knowledge()
T = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)
STATIONS = {
    "VN-ST-001": ("VN", "Asia/Ho_Chi_Minh"),
    "VN-ST-002": ("VN", "Asia/Ho_Chi_Minh"),
    "PH-ST-001": ("PH", "Asia/Manila"),
    "ID-ST-001": ("ID", "Asia/Makassar"),
}
PROVIDER = {"VN": "VN_WALLET_1", "PH": "PH_WALLET_1", "ID": "ID_WALLET_1"}


def _ctx(session_ids: list[tuple[str, str]], lots: dict[str, str] | None = None) -> ContextStore:
    stations = [
        Station(
            station_id=s,
            country=c,
            timezone=tz,
            model_gen="GEN2",
            install_date=T - timedelta(days=90),
        )
        for s, (c, tz) in STATIONS.items()
    ]
    sessions = [
        SwapSession(
            session_id=sid,
            user_id="u",
            station_id=st,
            cabinet_id=f"{st}-CAB-01",
            slot_in="01",
            slot_out="02",
            battery_in_id="B-IN",
            battery_out_id=f"B-{sid}",
            app_version="3.2.0",
            payment_provider=PROVIDER[STATIONS[st][0]],
            final_status="FAILED",
            started_at=T - timedelta(hours=1),
        )
        for sid, st in session_ids
    ]
    batteries = [
        Battery(battery_id=f"B-{sid}", lot_id=lot, soh=70.0, cycle_count=10)
        for sid, lot in (lots or {}).items()
    ]
    return ContextStore.from_models(stations=stations, sessions=sessions, batteries=batteries)


def _cls(
    sid: str,
    station: str,
    at: datetime,
    *,
    category: Category,
    severity: Severity = Severity.P2,
    code: str | None = None,
    step: Step = Step.SCAN_QR,
    cabinet: str | None = None,
    rule_id: str = "R_CATALOG",
) -> Classification:
    unknown = category is Category.UNKNOWN
    return Classification(
        session_id=sid,
        station_id=station,
        cabinet_id=cabinet,
        slot_id="01" if cabinet else None,
        step=step,
        kind="EXPLICIT_FAILURE",
        error_code=code,
        status="FAILED",
        occurred_at=at,
        detected_at=at,
        event_ids=[f"evt-{sid}"],
        category=category,
        severity=severity,
        rule_id="R_UNKNOWN" if unknown else rule_id,
        confidence=0.3 if unknown else 0.95,
        needs_human_review=unknown,
    )


def _corr(
    items: list[Classification], k: Knowledge = K, lots: dict[str, str] | None = None
) -> Correlator:
    ctx = _ctx([(c.session_id, c.station_id) for c in items], lots)
    return Correlator(FieldView(ctx, preset="ALL", knowledge=k), knowledge=k)


# --- Correlator -------------------------------------------------------------------------------


def test_system_error_in_three_countries_is_one_global_incident() -> None:
    items = [
        _cls("S1", "VN-ST-001", T, category=Category.SYSTEM, code="SESS_CREATE_5XX"),
        _cls(
            "S2",
            "PH-ST-001",
            T + timedelta(minutes=2),
            category=Category.SYSTEM,
            code="SESS_CREATE_5XX",
        ),
        _cls(
            "S3",
            "ID-ST-001",
            T + timedelta(minutes=4),
            category=Category.SYSTEM,
            code="SESS_CREATE_5XX",
        ),
    ]
    corr = _corr(items)
    evs = [corr.add(c) for c in items]
    assert evs[0].opened and not evs[1].opened and not evs[2].opened
    assert len(corr.live_incidents()) == 1
    snap = corr.snapshot(evs[0].incident_id or "")
    assert snap.country == GLOBAL_COUNTRY
    assert snap.countries == ["ID", "PH", "VN"]
    assert (snap.n_sessions, snap.n_stations) == (3, 3)
    assert snap.evidence == ["evt-S1", "evt-S2", "evt-S3"]


def test_payment_incident_scoped_to_provider_and_country() -> None:
    items = [
        _cls(
            "P1",
            "PH-ST-001",
            T,
            category=Category.PAYMENT,
            code="PAY_GATEWAY_TIMEOUT",
            step=Step.PAYMENT,
        ),
        _cls(
            "P2",
            "PH-ST-001",
            T + timedelta(minutes=1),
            category=Category.PAYMENT,
            code="PAY_GATEWAY_TIMEOUT",
            step=Step.PAYMENT,
        ),
        _cls(
            "P3",
            "VN-ST-001",
            T + timedelta(minutes=1),
            category=Category.PAYMENT,
            code="PAY_GATEWAY_TIMEOUT",
            step=Step.PAYMENT,
        ),
    ]
    corr = _corr(items)
    ids = [corr.add(c).incident_id for c in items]
    assert ids[0] == ids[1] != ids[2]
    ph = corr.snapshot(ids[0] or "")
    assert (ph.country, ph.countries, ph.n_sessions) == ("PH", ["PH"], 2)
    assert "PH_WALLET_1" in ph.scope


def test_door_jam_on_two_cabinets_gives_two_incidents() -> None:
    items = [
        _cls(
            "H1",
            "VN-ST-001",
            T,
            category=Category.HARDWARE,
            code="SLOT_DOOR_JAM",
            step=Step.RETURN_BATTERY,
            cabinet="CAB-A",
        ),
        _cls(
            "H2",
            "VN-ST-001",
            T + timedelta(minutes=1),
            category=Category.HARDWARE,
            code="SLOT_DOOR_JAM",
            step=Step.RETURN_BATTERY,
            cabinet="CAB-B",
        ),
        _cls(
            "H3",
            "VN-ST-001",
            T + timedelta(minutes=2),
            category=Category.HARDWARE,
            code="SLOT_DOOR_JAM",
            step=Step.RETURN_BATTERY,
            cabinet="CAB-A",
        ),
    ]
    corr = _corr(items)
    ids = [corr.add(c).incident_id for c in items]
    assert ids[0] == ids[2] != ids[1]
    assert corr.snapshot(ids[0] or "").cabinet_id == "CAB-A"


def test_scattered_user_errors_create_no_incident() -> None:
    items = [
        _cls(
            f"U{i}",
            "VN-ST-001",
            T + timedelta(minutes=7 * i),
            category=Category.USER,
            severity=Severity.P3,
            code="QR_INVALID",
        )
        for i in range(4)
    ]
    corr = _corr(items)
    evs = [corr.add(c) for c in items]
    assert all(e.incident_id is None and e.spike_incident_id is None for e in evs)
    assert corr.live_incidents() == []


def test_user_spike_opens_one_it_ops_incident() -> None:
    n = K.detector.correlator.user_spike_min_sessions
    items = [
        _cls(
            f"U{i}",
            "VN-ST-001",
            T + timedelta(seconds=30 * i),
            category=Category.USER,
            severity=Severity.P3,
            code="QR_INVALID",
        )
        for i in range(n + 3)
    ]
    corr = _corr(items)
    spikes = [e.spike_incident_id for e in (corr.add(c) for c in items) if e.spike_incident_id]
    assert len(spikes) == 1  # không mở lại trong cùng cửa sổ
    snap = corr.snapshot(spikes[0])
    assert snap.is_user_spike and snap.category is Category.UNKNOWN
    assert snap.needs_human_review and snap.n_sessions == n
    assert K.routing.route_for(snap.category).team == "IT Ops"


def test_revision_increases_only_when_severity_increases() -> None:
    items = [
        _cls(
            "B1",
            "VN-ST-001",
            T,
            category=Category.BATTERY_QUALITY,
            code="BAT_RETURN_FAULT",
            step=Step.RETURN_BATTERY,
        ),
        _cls(
            "B2",
            "VN-ST-001",
            T + timedelta(minutes=1),
            category=Category.BATTERY_QUALITY,
            code="BAT_RETURN_FAULT",
            step=Step.RETURN_BATTERY,
        ),
        _cls(
            "B3",
            "VN-ST-001",
            T + timedelta(minutes=2),
            category=Category.BATTERY_QUALITY,
            severity=Severity.P1,
            code="BAT_RETURN_FAULT",
            step=Step.RETURN_BATTERY,
        ),
    ]
    corr = _corr(items)
    evs = [corr.add(c) for c in items]
    assert [e.escalated for e in evs] == [False, False, True]
    inc = corr.get(evs[0].incident_id or "")
    assert (inc.revision, inc.severity) == (2, Severity.P1)
    disp = Dispatcher(corr, knowledge=K)
    recs = build_incident_records(corr, disp)
    assert [(r.revision, r.severity, r.n_sessions) for r in recs] == [
        (1, Severity.P2, 2),
        (2, Severity.P1, 3),
    ]


def test_window_closes_after_gap_and_system_uses_longer_window() -> None:
    gap = timedelta(minutes=10)  # > 5' mặc định, < 15' của SYSTEM
    items = [
        _cls(
            "H1", "VN-ST-001", T, category=Category.HARDWARE, code="CABINET_NO_ACK", cabinet="CAB-A"
        ),
        _cls(
            "H2",
            "VN-ST-001",
            T + gap,
            category=Category.HARDWARE,
            code="CABINET_NO_ACK",
            cabinet="CAB-A",
        ),
        _cls("S1", "VN-ST-001", T, category=Category.SYSTEM, code="SESS_CREATE_5XX"),
        _cls("S2", "VN-ST-002", T + gap, category=Category.SYSTEM, code="SESS_CREATE_5XX"),
    ]
    corr = _corr(items)
    ids = [corr.add(c).incident_id for c in items]
    assert ids[0] != ids[1]
    assert ids[2] == ids[3]


def test_unknown_sessions_are_clustered_into_open_coded_incident() -> None:
    items = [
        # UNKNOWN đến trước incident có mã (bị "hút" khi incident mở)
        _cls("X0", "VN-ST-002", T, category=Category.UNKNOWN),
        _cls(
            "S1",
            "PH-ST-001",
            T + timedelta(minutes=1),
            category=Category.SYSTEM,
            code="SESS_CREATE_5XX",
        ),
        # UNKNOWN đến sau, cùng bước
        _cls("X1", "VN-ST-001", T + timedelta(minutes=2), category=Category.UNKNOWN),
        # UNKNOWN khác bước → không gắn
        _cls(
            "X2",
            "VN-ST-001",
            T + timedelta(minutes=2),
            category=Category.UNKNOWN,
            step=Step.CHECK_ELIGIBILITY,
        ),
    ]
    corr = _corr(items)
    evs = [corr.add(c) for c in items]
    sys_id = evs[1].incident_id
    for sid in ("X0", "X1"):
        d = corr.detections[sid]
        assert (d.incident_id, d.category, d.rule_id) == (sys_id, Category.SYSTEM, "R_CLUSTER")
        assert d.error_code == "SESS_CREATE_5XX" and d.needs_human_review
        assert d.original_rule_id == "R_UNKNOWN"
    assert corr.detections["X2"].category is Category.UNKNOWN
    live = {i.incident_id for i in corr.live_incidents()}
    assert evs[0].incident_id not in live  # incident UNKNOWN cũ đã bị gộp
    assert corr.snapshot(sys_id or "").n_sessions == 3


# --- Dispatcher -------------------------------------------------------------------------------


def _dispatch_fixture() -> tuple[Correlator, Dispatcher, list[Classification]]:
    items = [
        _cls(
            "PAY1",
            "VN-ST-001",
            T + timedelta(minutes=1),
            category=Category.PAYMENT,
            severity=Severity.P1,
            code="PAID_NOT_DELIVERED",
            step=Step.RETURN_BATTERY,
            rule_id="R_PAID_NOT_DELIVERED",
        ),
        _cls(
            "SYS1",
            "VN-ST-001",
            T + timedelta(minutes=2),
            category=Category.SYSTEM,
            code="SESS_CREATE_5XX",
        ),
        _cls(
            "SYS2",
            "PH-ST-001",
            T + timedelta(minutes=3),
            category=Category.SYSTEM,
            code="SESS_CREATE_5XX",
        ),
        _cls(
            "BIZ1",
            "VN-ST-002",
            T + timedelta(minutes=4),
            category=Category.BUSINESS,
            severity=Severity.P3,
            code="ELIG_DEBT",
            step=Step.CHECK_ELIGIBILITY,
        ),
        _cls(
            "USR1",
            "VN-ST-001",
            T + timedelta(minutes=5),
            category=Category.USER,
            severity=Severity.P3,
            code="QR_INVALID",
        ),
        _cls(
            "USR2",
            "ID-ST-001",
            T + timedelta(minutes=16),
            category=Category.USER,
            severity=Severity.P3,
            code="PAY_USER_CANCEL",
            step=Step.PAYMENT,
        ),
    ]
    corr = _corr(items)
    return corr, Dispatcher(corr, knowledge=K), items


def test_p1_is_sent_immediately_and_p2_p3_user_wait_for_batch() -> None:
    corr, disp, items = _dispatch_fixture()
    disp.on_correlation(corr.add(items[0]))
    assert [(a.team, a.sent_at) for a in disp.p1_alerts] == [("Payment Ops", items[0].detected_at)]
    for c in items[1:5]:
        disp.on_correlation(corr.add(c))
    assert len(disp.p1_alerts) == 1 and disp.batches == []
    disp.tick(T + timedelta(minutes=14, seconds=59))
    assert disp.batches == []
    disp.tick(T + timedelta(minutes=15))  # mốc tròn :15
    teams = sorted((b.team, b.window_start, b.window_end) for b in disp.batches)
    assert teams == [
        ("Business Ops", T, T + timedelta(minutes=15)),
        ("CSKH", T, T + timedelta(minutes=15)),
        ("IT Ops", T, T + timedelta(minutes=15)),
    ]
    it_ops = next(b for b in disp.batches if b.team == "IT Ops")
    assert [r.session_id for r in it_ops.rows] == ["SYS1", "SYS2"]
    assert len(it_ops.incidents) == 1 and it_ops.incidents[0].n_sessions == 2
    assert it_ops.batch_id == f"batch:it-ops:{T:%Y%m%dT%H%MZ}"


def test_every_p2_p3_user_error_is_in_exactly_one_batch_of_the_right_team() -> None:
    corr, disp, items = _dispatch_fixture()
    for c in items:
        disp.on_correlation(corr.add(c))
        disp.tick(c.detected_at)
    disp.flush_all()
    seen: dict[str, str] = {}
    for b in disp.batches:
        for r in b.rows:
            assert r.session_id not in seen
            seen[r.session_id] = b.team
    assert seen == {
        "SYS1": "IT Ops",
        "SYS2": "IT Ops",
        "BIZ1": "Business Ops",
        "USR1": "CSKH",
        "USR2": "CSKH",
    }
    # mỗi team ≤ 1 tin / chu kỳ; team không có lỗi (Battery Ops, Field Ops) không có tin
    keys = [(b.team, b.window_start) for b in disp.batches]
    assert len(keys) == len(set(keys))
    assert {b.team for b in disp.batches} == {"IT Ops", "Business Ops", "CSKH"}
    usr2 = next(b for b in disp.batches if any(r.session_id == "USR2" for r in b.rows))
    assert usr2.window_start == T + timedelta(minutes=15)
    assert not any(a.team == "CSKH" for a in disp.p1_alerts)


# --- Notifier tin gom ------------------------------------------------------------------------


def _batch(n_rows: int) -> TeamBatch:
    rows = [
        BatchRow(
            session_id=f"S{i:03d}",
            incident_id="INC-20261104-0001",
            category=Category.SYSTEM,
            severity=Severity.P2,
            error_code="SESS_CREATE_5XX",
            step=Step.SCAN_QR,
            station_id="PH-ST-001",
            country="PH",
            timezone="Asia/Manila",
            occurred_at=T + timedelta(seconds=i),
            detected_at=T + timedelta(seconds=i),
        )
        for i in range(n_rows)
    ]
    return TeamBatch(
        batch_id=f"batch:it-ops:{T:%Y%m%dT%H%MZ}",
        team="IT Ops",
        channel="it-ops",
        window_start=T,
        window_end=T + timedelta(minutes=15),
        rows=rows,
    )


def test_batch_mail_truncates_rows_but_csv_has_all_and_local_time() -> None:
    notify = K.detector.notify.model_copy(update={"max_rows_mail": 3})
    k = K.model_copy(update={"detector": K.detector.model_copy(update={"notify": notify})})
    rendered = render_batch_email(_batch(7), knowledge=k)
    assert (rendered.n_rows_shown, rendered.n_rows_total) == (3, 7)
    assert rendered.csv_text.count("\n") == 8  # header + 7 dòng
    assert "S006" in rendered.csv_text and "S006" not in rendered.text_body
    assert "18:00:00 04/11/2026 (GMT+8)" in rendered.text_body  # giờ trạm PH
    assert "(GMT+7)" in rendered.subject  # chu kỳ theo giờ đội OPS (daily_report_timezone)


def test_batch_mail_and_card_link_each_session_to_dashboard() -> None:
    batch = _batch(2)
    rendered = render_batch_email(batch, knowledge=K, dashboard_profile="dev")
    sid = batch.rows[0].session_id
    url = f"http://localhost:8000/dashboard?profile=dev&emsp_txn_id={sid}"
    assert url.replace("&", "&amp;") in rendered.html_body and url in rendered.text_body
    card = build_batch_card(batch, knowledge=K, dashboard_profile="dev")
    card_str = json.dumps(card, ensure_ascii=False)
    if batch.incidents:
        assert "selectAction" in card_str
    assert "http://localhost:8000/dashboard?profile=dev&status=FAILED" in card_str


class _FakeChannel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.batches: list[str] = []

    async def send(self, alert: Alert) -> SendResult:
        return SendResult(channel=self.name, status="SENT")

    async def send_batch(self, batch: TeamBatch) -> SendResult:
        self.batches.append(batch.batch_id)
        return SendResult(channel=self.name, status="SENT")


async def test_notify_batch_is_idempotent_via_outbox(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, notify_mode="dry_run")
    email, teams = _FakeChannel("email"), _FakeChannel("teams")
    svc = NotifierService(settings=settings, email_notifier=email, teams_notifier=teams)
    first = await svc.notify_batch(_batch(2))
    second = await svc.notify_batch(_batch(2))
    assert [r.status for r in first.results] == ["SENT", "SENT"]
    assert [r.detail for r in second.results] == ["already_sent", "already_sent"]
    assert email.batches == teams.batches == [_batch(2).batch_id]


async def test_smtp_dry_run_batch_writes_preview_and_csv(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, notify_mode="dry_run")
    res = await SmtpEmailNotifier(settings).send_batch(_batch(2))
    assert res.status == "SENT"
    csvs = list(settings.preview_dir.glob("*.csv"))
    assert len(csvs) == 1 and csvs[0].read_text(encoding="utf-8").count("\n") == 3
    eml = next(settings.preview_dir.glob("*.eml")).read_bytes()
    assert b"Content-Disposition: attachment" in eml


# --- Pipeline trên smoke --------------------------------------------------------------------


@pytest.fixture(scope="module")
def smoke_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import asyncio

    out = tmp_path_factory.mktemp("correlator_smoke")
    asyncio.run(generate("smoke", out, knowledge=K, profiles=load_profiles()))
    return out


def _sha(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}


async def test_pipeline_on_smoke_is_deterministic_and_never_reads_ground_truth(
    smoke_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = builtins.open

    def guarded(file: Any, *a: Any, **kw: Any) -> Any:
        if "ground_truth" in str(file):
            raise AssertionError("detector MUST NOT open ground_truth.csv")
        return real_open(file, *a, **kw)

    def no_csv(*a: Any, **kw: Any) -> Any:
        raise AssertionError("detector MUST NOT call pl.read_csv")

    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.setattr(pl, "read_csv", no_csv)
    run1 = await run_detector(smoke_dir, out_dir=tmp_path / "r1", knowledge=K)
    run2 = await run_detector(smoke_dir, out_dir=tmp_path / "r2", knowledge=K)
    monkeypatch.undo()
    assert _sha(run1.out_dir) == _sha(run2.out_dir)
    assert run1.report.n_detections > 0


async def test_pipeline_on_smoke_matches_ground_truth_scenarios(
    smoke_dir: Path, tmp_path: Path
) -> None:
    run = await run_detector(smoke_dir, out_dir=tmp_path / "out", knowledge=K)
    out = run.out_dir
    dets = {
        d["session_id"]: d
        for d in map(json.loads, (out / "detections.jsonl").read_text("utf-8").splitlines())
    }
    incs = [json.loads(x) for x in (out / "incidents.jsonl").read_text("utf-8").splitlines()]
    gt = pl.read_csv(smoke_dir / "ground_truth.csv")

    # Phiên bẫy pending_then_ok không bị phát hiện; mọi phiên lỗi đều được phát hiện
    trap = set(gt.filter(pl.col("scenario") == "pending_then_ok")["session_id"].to_list())
    assert trap.isdisjoint(dets)
    assert set(gt.filter(pl.col("is_failure"))["session_id"].to_list()) == set(dets)

    # Evidence luôn là event_id có thật
    event_ids = {
        json.loads(x)["event_id"]
        for x in (smoke_dir / "ase.swap.events.jsonl").read_text("utf-8").splitlines()
    }
    assert all(e in event_ids for r in incs for e in r["evidence"])

    # Mỗi scenario instance (trừ USER) có ≥ 1 incident; deploy_503 ra đúng 1 incident
    inst = gt.filter(pl.col("scenario_instance_id").is_not_null() & pl.col("is_failure"))
    for (iid, cat), sub in inst.group_by(["scenario_instance_id", "category"]):
        if cat == "USER":
            continue
        ids = {dets[s]["incident_id"] for s in sub["session_id"].to_list()} - {None}
        assert ids, f"{iid} không có incident"
        if str(iid).startswith("SCN-deploy_503"):
            assert len(ids) == 1, f"{iid} ra {len(ids)} incident"

    # P1 gửi ngay, không vượt quá số incident P1
    p1 = [r for r in incs if r["severity"] == "P1" and r["dispatch"] == "IMMEDIATE"]
    alerts = (out / "alerts.jsonl").read_text("utf-8").splitlines()
    assert len(alerts) >= len({r["incident_id"] for r in p1})
    assert (out / "coverage.json").exists()
    assert (out / "ase.anomaly.transaction.v0.jsonl").exists()


def test_paid_not_delivered_is_one_network_wide_incident_across_providers() -> None:
    """SF8: PAID_NOT_DELIVERED gộp theo mã trên toàn mạng, không tách theo provider/nước."""
    pnd = dict(
        category=Category.PAYMENT,
        severity=Severity.P1,
        code="PAID_NOT_DELIVERED",
        step=Step.RETURN_BATTERY,
        rule_id="R_PAID_NOT_DELIVERED",
    )
    items = [
        _cls("N1", "VN-ST-001", T, **pnd),  # type: ignore[arg-type]
        _cls("N2", "PH-ST-001", T + timedelta(minutes=1), **pnd),  # type: ignore[arg-type]
        # UNKNOWN cùng bước trong cửa sổ: KHÔNG được gán vào incident toàn mạng
        _cls(
            "X1",
            "VN-ST-002",
            T + timedelta(minutes=2),
            category=Category.UNKNOWN,
            step=Step.RETURN_BATTERY,
        ),
    ]
    corr = _corr(items)
    evs = [corr.add(c) for c in items]
    assert evs[0].incident_id == evs[1].incident_id
    snap = corr.snapshot(evs[0].incident_id or "")
    assert (snap.country, snap.countries, snap.n_sessions) == (GLOBAL_COUNTRY, ["PH", "VN"], 2)
    assert corr.detections["X1"].category is Category.UNKNOWN
    assert corr.detections["X1"].rule_id == "R_UNKNOWN"
