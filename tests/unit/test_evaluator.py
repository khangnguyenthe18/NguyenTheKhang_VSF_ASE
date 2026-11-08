"""Test evaluator: mỗi SF trên dữ liệu tự dựng, đáp án tính tay (EVALUATOR_PLAN §7)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ase.common.models import (
    BatchRow,
    Category,
    GroundTruthRow,
    Severity,
    Step,
    TeamBatch,
)
from ase.detector.correlator import Detection, IncidentRecord
from ase.detector.dispatch import P1Alert
from ase.detector.fields import CoverageReport
from ase.detector.main import run_detector
from ase.detector.output import RunReport
from ase.detector.state_machine import FindingKind
from ase.evaluator.ablation import PRESETS, build_report, preset_run_dir
from ase.evaluator.ablation import render_markdown as render_ablation_md
from ase.evaluator.loader import RunData, load_run
from ase.evaluator.metrics import (
    LABELS_PRED,
    build_real_incidents,
    compute_sf1,
    compute_sf2,
    compute_sf3,
    compute_sf4,
    compute_sf5,
    compute_sf7,
    compute_sf8,
    failure_time,
    percentile,
)
from ase.evaluator.report import evaluate, write_report
from ase.evaluator.report import render_markdown as render_eval_md
from ase.knowledge.loader import default_knowledge
from ase.simulator.generator import generate
from ase.simulator.profiles import load_profiles

K = default_knowledge()
T = datetime(2026, 11, 4, 3, 0, 0, tzinfo=UTC)


# --- dựng dữ liệu -----------------------------------------------------------------------------


def gt(
    sid: str,
    *,
    cat: Category | None = None,
    sev: Severity | None = None,
    code: str | None = None,
    scenario: str | None = None,
    inst: str | None = None,
    inj: datetime | None = None,
    country: str = "VN",
    split: str = "test",
) -> GroundTruthRow:
    return GroundTruthRow(
        session_id=sid,
        is_failure=cat is not None,
        category=cat,
        severity=sev,
        error_code=code,
        scenario=scenario,
        scenario_instance_id=inst,
        injected_at=inj,
        country=country,
        split=split,
    )


def det(
    sid: str,
    cat: Category,
    *,
    sev: Severity = Severity.P2,
    code: str | None = "SESS_CREATE_5XX",
    kind: FindingKind = "EXPLICIT_FAILURE",
    at: datetime = T,
    detected: datetime | None = None,
    inc: str | None = None,
    step: Step = Step.SCAN_QR,
    rule: str = "R_CATALOG",
    country: str = "VN",
    tz: str = "Asia/Ho_Chi_Minh",
) -> Detection:
    return Detection(
        session_id=sid,
        station_id="VN-ST-001",
        step=step,
        kind=kind,
        error_code=code,
        status="FAILED",
        occurred_at=at,
        detected_at=detected or at,
        event_ids=[f"evt-{sid}"],
        category=cat,
        severity=sev,
        rule_id=rule,
        confidence=0.95,
        incident_id=inc,
        country=country,
        timezone=tz,
    )


def inc_rec(
    iid: str,
    cat: Category,
    sids: list[str],
    *,
    sev: Severity = Severity.P1,
    rev: int = 1,
    at: datetime = T,
    code: str | None = "PAID_NOT_DELIVERED",
    step: Step = Step.RETURN_BATTERY,
) -> IncidentRecord:
    return IncidentRecord(
        incident_id=iid,
        revision=rev,
        category=cat,
        severity=sev,
        error_code=code,
        step=step,
        station_id="VN-ST-001",
        country="VN",
        timezone="Asia/Ho_Chi_Minh",
        countries=["VN"],
        session_ids=sids,
        first_seen_at=at,
        last_seen_at=at,
        n_sessions=len(sids),
        n_stations=1,
        window_minutes=1,
        confidence=0.9,
        probable_cause="[Rule] test",
        evidence=[f"evt-{sids[0]}"],
        detected_at=at,
        revision_at=at,
        scope="VN-ST-001",
    )


def alert(iid: str, team: str, sent: datetime, rev: int = 1) -> P1Alert:
    ch = team.lower().replace(" ", "-")
    return P1Alert(incident_id=iid, revision=rev, team=team, channel=ch, sent_at=sent)


def batch_row(sid: str, cat: Category, *, at: datetime = T) -> BatchRow:
    return BatchRow(
        session_id=sid,
        incident_id=None,
        category=cat,
        severity=Severity.P3 if cat is Category.USER else Severity.P2,
        error_code="QR_INVALID" if cat is Category.USER else "SLOT_DOOR_JAM",
        step=Step.SCAN_QR,
        station_id="VN-ST-001",
        country="VN",
        timezone="Asia/Ho_Chi_Minh",
        occurred_at=at,
        detected_at=at,
    )


def team_batch(team: str, rows: list[BatchRow], end: datetime) -> TeamBatch:
    ch = team.lower().replace(" ", "-")
    return TeamBatch(
        batch_id=f"batch:{ch}:{end:%Y%m%dT%H%MZ}",
        team=team,
        channel=ch,
        window_start=end - timedelta(minutes=15),
        window_end=end,
        rows=rows,
    )


def run_data(
    gts: list[GroundTruthRow],
    dets: list[Detection],
    *,
    incidents: list[IncidentRecord] | None = None,
    alerts: list[P1Alert] | None = None,
    batches: list[TeamBatch] | None = None,
    split: str | None = None,
    events: int = 0,
    n_event_ids: int = 0,
) -> RunData:
    gmap = {g.session_id: g for g in gts}
    return RunData(
        profile="synthetic",
        split=split,
        gt=gmap,
        eval_ids=frozenset(s for s, g in gmap.items() if split is None or g.split == split),
        detections={d.session_id: d for d in dets},
        incidents=incidents or [],
        alerts=alerts or [],
        batches=batches or [],
        run_report=RunReport(
            coverage=CoverageReport(
                preset="ALL",
                available_fields=[],
                total_events_processed=events,
                total_sessions_seen=len(gts),
            )
        ),
        n_distinct_event_ids=n_event_ids,
    )


# --- tiện ích ---------------------------------------------------------------------------------


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 50) is None
    assert percentile([4.0, 1.0, 3.0, 2.0], 50) == 2.0  # ceil(0,5*4) = 2
    assert percentile([4.0, 1.0, 3.0, 2.0], 95) == 4.0  # ceil(0,95*4) = 4
    assert percentile([7.0], 95) == 7.0


# --- SF1 --------------------------------------------------------------------------------------


def test_sf1_counts_events_and_sessions() -> None:
    data = run_data([gt("A"), gt("B"), gt("C")], [], events=95, n_event_ids=100)
    sf1 = compute_sf1(data)
    assert sf1.events_pct == 0.95 and sf1.sessions_pct == 1.0
    assert sf1.passed is False
    ok = compute_sf1(run_data([gt("A")], [], events=10, n_event_ids=10))
    assert ok.passed is True


# --- SF2 --------------------------------------------------------------------------------------


def test_sf2_recall_overall_by_category_and_silent_errors() -> None:
    gts = [
        gt("S1", cat=Category.SYSTEM, sev=Severity.P2, code="SESS_CREATE_5XX"),
        gt("S2", cat=Category.HARDWARE, sev=Severity.P2, code="SLOT_DOOR_JAM"),
        gt("S3", cat=Category.PAYMENT, sev=Severity.P1, code="PAID_NOT_DELIVERED"),
        gt("S4", cat=Category.HARDWARE, sev=Severity.P2, code="STEP_TIMEOUT"),  # bị bỏ sót
        gt("OK"),
    ]
    dets = [
        det("S1", Category.SYSTEM),
        det("S2", Category.HARDWARE, code="SLOT_DOOR_JAM"),
        det("S3", Category.PAYMENT, code="PAID_NOT_DELIVERED"),
    ]
    sf2 = compute_sf2(run_data(gts, dets))
    assert (sf2.n_failures, sf2.n_detected, sf2.recall) == (4, 3, 0.75)
    assert sf2.by_category["HARDWARE"].recall == 0.5
    assert sf2.by_category["SYSTEM"].recall == 1.0
    assert sf2.by_category["USER"].recall is None  # không có mẫu
    assert (sf2.silent.n, sf2.silent.detected, sf2.silent.recall) == (2, 1, 0.5)
    assert sf2.passed is False


# --- SF3 --------------------------------------------------------------------------------------


def test_sf3_trap_session_lowers_precision_and_is_listed() -> None:
    gts = [
        gt("S1", cat=Category.SYSTEM, code="SESS_CREATE_5XX"),
        gt("S2", cat=Category.SYSTEM, code="SESS_CREATE_5XX"),
        gt("S3", cat=Category.SYSTEM, code="SESS_CREATE_5XX"),
        gt("TRAP", scenario="pending_then_ok"),
    ]
    dets = [det(s, Category.SYSTEM) for s in ("S1", "S2", "S3", "TRAP")]
    sf3 = compute_sf3(run_data(gts, dets))
    assert (sf3.n_detected, sf3.n_true, sf3.precision) == (4, 3, 0.75)
    assert sf3.trap_sessions == ["TRAP"] and sf3.n_trap_detected == 1
    assert sf3.passed is False
    clean = compute_sf3(run_data(gts[:3], dets[:3]))
    assert clean.precision == 1.0 and clean.trap_sessions == [] and clean.passed is True


# --- SF4 --------------------------------------------------------------------------------------


def test_sf4_unknown_counts_as_wrong_and_macro_f1_matches_hand_calculation() -> None:
    gts = [
        gt("A", cat=Category.SYSTEM, code="SESS_CREATE_5XX"),
        gt("B", cat=Category.SYSTEM, code="SESS_CREATE_5XX"),
        gt("C", cat=Category.HARDWARE, code="SLOT_DOOR_JAM"),
        gt("D", cat=Category.PAYMENT, code="PAY_GATEWAY_TIMEOUT"),
    ]
    dets = [
        det("A", Category.SYSTEM),
        det("B", Category.UNKNOWN, code=None, rule="R_UNKNOWN"),
        det("C", Category.HARDWARE),
        det("D", Category.SYSTEM),  # nhầm PAYMENT thành SYSTEM
    ]
    sf4 = compute_sf4(run_data(gts, dets))
    assert sf4.n_evaluated == 4 and sf4.accuracy == 0.5
    # SYSTEM: P=1/2, R=1/2, F1=0,5 · HARDWARE: F1=1 · PAYMENT: F1=0 → macro = 0,5
    assert sf4.macro_f1 == pytest.approx(0.5)
    assert sf4.misclassified == ["B", "D"]
    assert sf4.confusion_labels == LABELS_PRED
    assert sf4.confusion[0] == [1, 0, 0, 0, 0, 0, 1]  # hàng SYSTEM: 1 đúng, 1 → UNKNOWN
    assert sf4.per_category_accuracy["SYSTEM"] == 0.5
    assert sf4.per_category_accuracy["USER"] is None
    assert sf4.passed is False


# --- incident thật + mốc lỗi ------------------------------------------------------------------


def test_real_incidents_group_scenario_instances_and_split_background() -> None:
    gts = [
        gt("A1", cat=Category.SYSTEM, sev=Severity.P2, scenario="deploy_503", inst="SCN-1", inj=T),
        gt("A2", cat=Category.SYSTEM, sev=Severity.P2, scenario="deploy_503", inst="SCN-1", inj=T),
        gt("B1", cat=Category.USER, sev=Severity.P3, code="QR_INVALID"),
        gt("B2", cat=Category.USER, sev=Severity.P3, code="QR_INVALID"),
        gt("B3", cat=Category.USER, sev=Severity.P3, code="QR_INVALID"),
        gt("OK"),
    ]
    real = build_real_incidents(run_data(gts, []))
    assert len(real) == 4  # 1 scenario (2 phiên) + 3 phiên nền
    assert {r.key for r in real} == {"SCN-1", "B1", "B2", "B3"}


def test_failure_time_prefers_injected_at_and_falls_back_to_detection_for_explicit_failures() -> (
    None
):
    gts = [
        gt("SC", cat=Category.SYSTEM, inj=T, scenario="x", inst="SCN-1"),
        gt("BG", cat=Category.USER, code="QR_INVALID"),
        gt("SILENT", cat=Category.HARDWARE, code="STEP_TIMEOUT"),
    ]
    dets = [
        det("SC", Category.SYSTEM, at=T + timedelta(seconds=999)),
        det("BG", Category.USER, at=T + timedelta(seconds=7)),
        det("SILENT", Category.UNKNOWN, kind="STEP_TIMEOUT", at=T + timedelta(seconds=120)),
    ]
    data = run_data(gts, dets)
    assert failure_time(data, "SC") == (T, False)
    assert failure_time(data, "BG") == (T + timedelta(seconds=7), True)  # mốc thay thế
    assert failure_time(data, "SILENT") == (None, False)  # không suy ra được


# --- SF5 --------------------------------------------------------------------------------------


def test_sf5_p1_latency_uses_first_alert_and_first_p1_session() -> None:
    gts = [
        # Scenario 1: phiên P2 mở đầu, phiên P1 đến sau 180s; alert P1 gửi ngay lúc đó
        gt("T1", cat=Category.HARDWARE, sev=Severity.P2, scenario="thermal", inst="SCN-1", inj=T),
        gt(
            "T2",
            cat=Category.BATTERY_QUALITY,
            sev=Severity.P1,
            scenario="thermal",
            inst="SCN-1",
            inj=T + timedelta(seconds=180),
        ),
        # Scenario 2: lỗi im lặng, alert sau 121s
        gt("P1", cat=Category.PAYMENT, sev=Severity.P1, scenario="pnd", inst="SCN-2", inj=T),
    ]
    dets = [
        det("T1", Category.HARDWARE, inc="INC-1"),
        det(
            "T2",
            Category.BATTERY_QUALITY,
            sev=Severity.P1,
            inc="INC-1",
            at=T + timedelta(seconds=180),
        ),
        det(
            "P1",
            Category.PAYMENT,
            sev=Severity.P1,
            inc="INC-2",
            kind="PAID_NOT_DELIVERED",
            at=T + timedelta(seconds=120),
        ),
    ]
    alerts = [
        alert("INC-1", "Battery Ops", T + timedelta(seconds=180)),
        alert("INC-1", "Battery Ops", T + timedelta(seconds=500), rev=2),  # revision 2: bỏ qua
        alert("INC-2", "Payment Ops", T + timedelta(seconds=121)),
    ]
    sf5 = compute_sf5(run_data(gts, dets, alerts=alerts), K).p1
    assert (sf5.n_real_p1, sf5.n_alerted) == (2, 2)
    assert sf5.overall.n == 2 and sf5.overall.max == 121.0
    assert sf5.by_scenario["thermal"].max == 0.0  # tính từ phiên P1 đầu tiên
    assert sf5.by_scenario["pnd"].max == 121.0
    assert sf5.from_incident_start.max == 180.0  # tính từ phiên sớm nhất của cả incident
    assert sf5.passed is False  # p95 = 121s ≥ 60s


def test_sf5_p1_passes_when_all_alerted_under_60s_and_fails_when_missing_alert() -> None:
    gts = [gt("A", cat=Category.PAYMENT, sev=Severity.P1, inj=T, scenario="x", inst="SCN-1")]
    d = [det("A", Category.PAYMENT, sev=Severity.P1, inc="INC-1")]
    good = compute_sf5(
        run_data(gts, d, alerts=[alert("INC-1", "Payment Ops", T + timedelta(seconds=30))]), K
    )
    assert good.p1.passed is True
    missing = compute_sf5(run_data(gts, d), K)
    assert (missing.p1.n_real_p1, missing.p1.n_alerted, missing.p1.passed) == (1, 0, False)


def test_sf5_batch_rows_within_one_interval() -> None:
    gts = [
        gt("U1", cat=Category.USER, sev=Severity.P3, code="QR_INVALID", inj=T),
        gt("U2", cat=Category.USER, sev=Severity.P3, code="QR_INVALID", inj=T),
    ]
    dets = [
        det("U1", Category.USER, code="QR_INVALID"),
        det("U2", Category.USER, code="QR_INVALID"),
    ]
    early = team_batch("CSKH", [batch_row("U1", Category.USER)], T + timedelta(minutes=14))
    late = team_batch("CSKH", [batch_row("U2", Category.USER)], T + timedelta(minutes=16))

    ok = compute_sf5(run_data(gts[:1], dets[:1], batches=[early]), K).batch
    assert ok.latency.max == 840.0 and ok.pct_within_interval == 1.0 and ok.passed is True

    both = compute_sf5(run_data(gts, dets, batches=[early, late]), K).batch
    assert both.n_within_interval == 1 and both.pct_within_interval == 0.5
    assert both.latency.max == 960.0 and both.passed is False
    assert both.n_undelivered == 0


def test_sf5_detected_session_in_no_batch_and_no_alert_is_undelivered() -> None:
    gts = [gt("U1", cat=Category.USER, sev=Severity.P3, code="QR_INVALID", inj=T)]
    dets = [det("U1", Category.USER, code="QR_INVALID")]
    batch = compute_sf5(run_data(gts, dets), K).batch
    assert batch.n_undelivered == 1 and batch.passed is False


# --- SF7 --------------------------------------------------------------------------------------


def test_sf7_counts_wrong_team_for_alerts_and_batch_rows_user_must_go_to_cskh() -> None:
    gts = [
        gt("P1", cat=Category.PAYMENT, sev=Severity.P1, code="PAID_NOT_DELIVERED"),
        gt("U1", cat=Category.USER, sev=Severity.P3, code="QR_INVALID"),
        gt("U2", cat=Category.USER, sev=Severity.P3, code="QR_INVALID"),
    ]
    dets = [
        det("P1", Category.PAYMENT, sev=Severity.P1, inc="INC-1", code="PAID_NOT_DELIVERED"),
        det("U1", Category.USER, code="QR_INVALID"),
        det("U2", Category.USER, code="QR_INVALID"),
    ]
    incs = [inc_rec("INC-1", Category.PAYMENT, ["P1"])]
    # alert P1 gửi nhầm IT Ops (đúng: Payment Ops); U1 đúng CSKH; U2 nhầm vào IT Ops
    alerts = [alert("INC-1", "IT Ops", T)]
    batches = [
        team_batch("CSKH", [batch_row("U1", Category.USER)], T + timedelta(minutes=15)),
        team_batch("IT Ops", [batch_row("U2", Category.USER)], T + timedelta(minutes=15)),
    ]
    sf7 = compute_sf7(run_data(gts, dets, incidents=incs, alerts=alerts, batches=batches), K)
    assert (sf7.n_alerts, sf7.n_batches, sf7.n_rows) == (1, 2, 2)
    assert (sf7.team_ok_alerts, sf7.team_ok_rows) == (0, 1)
    assert sf7.team_pct == pytest.approx(1 / 3)
    assert sf7.team_ok_batches == 1
    assert sf7.schema_pct == 1.0 and sf7.render_pct == 1.0  # mã GD + giờ địa phương đủ
    assert sf7.passed is False and len(sf7.wrong_team_examples) == 2


def test_sf7_all_correct_is_100_percent() -> None:
    gts = [
        gt("P1", cat=Category.PAYMENT, sev=Severity.P1, code="PAID_NOT_DELIVERED"),
        gt("U1", cat=Category.USER, sev=Severity.P3, code="QR_INVALID"),
    ]
    dets = [
        det("P1", Category.PAYMENT, sev=Severity.P1, inc="INC-1", code="PAID_NOT_DELIVERED"),
        det("U1", Category.USER, code="QR_INVALID"),
    ]
    sf7 = compute_sf7(
        run_data(
            gts,
            dets,
            incidents=[inc_rec("INC-1", Category.PAYMENT, ["P1"])],
            alerts=[alert("INC-1", "Payment Ops", T)],
            batches=[
                team_batch("CSKH", [batch_row("U1", Category.USER)], T + timedelta(minutes=15))
            ],
        ),
        K,
    )
    assert (sf7.team_pct, sf7.schema_pct, sf7.render_pct) == (1.0, 1.0, 1.0)
    assert sf7.passed is True


# --- SF8 --------------------------------------------------------------------------------------


def test_sf8_three_alerts_for_two_real_p1_incidents_is_1_5() -> None:
    gts = [
        gt("X1", cat=Category.PAYMENT, sev=Severity.P1, scenario="pnd", inst="SCN-X", inj=T),
        gt("X2", cat=Category.PAYMENT, sev=Severity.P1, scenario="pnd", inst="SCN-X", inj=T),
        gt("Y1", cat=Category.PAYMENT, sev=Severity.P1, scenario="pnd", inst="SCN-Y", inj=T),
    ]
    dets = [
        det("X1", Category.PAYMENT, sev=Severity.P1, inc="INC-A"),
        det("X2", Category.PAYMENT, sev=Severity.P1, inc="INC-B"),  # tách theo provider
        det("Y1", Category.PAYMENT, sev=Severity.P1, inc="INC-C"),
    ]
    incs = [
        inc_rec("INC-A", Category.PAYMENT, ["X1"]),
        inc_rec("INC-B", Category.PAYMENT, ["X2"]),
        inc_rec("INC-C", Category.PAYMENT, ["Y1"]),
    ]
    alerts = [alert(i, "Payment Ops", T) for i in ("INC-A", "INC-B", "INC-C")]
    sf8 = compute_sf8(run_data(gts, dets, incidents=incs, alerts=alerts))
    assert (sf8.n_alerts, sf8.n_real_p1, sf8.ratio) == (3, 2, 1.5)
    assert (sf8.by_scenario["pnd"].n_real, sf8.by_scenario["pnd"].n_alerts) == (2, 3)
    assert sf8.passed is False
    one = compute_sf8(run_data(gts[:1], dets[:1], incidents=incs[:1], alerts=alerts[:1]))
    assert one.ratio == 1.0 and one.passed is True


# --- lọc split --------------------------------------------------------------------------------


def test_split_filter_excludes_sessions_outside_the_evaluated_split() -> None:
    gts = [
        gt("T1", cat=Category.SYSTEM, code="SESS_CREATE_5XX", split="test"),
        gt("B1", cat=Category.SYSTEM, code="SESS_CREATE_5XX", split="baseline"),
        gt("B2", cat=Category.SYSTEM, code="SESS_CREATE_5XX", split="baseline"),
    ]
    dets = [det("T1", Category.SYSTEM), det("B1", Category.SYSTEM)]  # B2 bị bỏ sót
    only_test = run_data(gts, dets, split="test")
    assert compute_sf2(only_test).n_failures == 1 and compute_sf2(only_test).recall == 1.0
    assert compute_sf3(only_test).n_detected == 1  # B1 không được tính
    everything = run_data(gts, dets)
    assert compute_sf2(everything).n_failures == 3 and compute_sf2(
        everything
    ).recall == pytest.approx(2 / 3)


# --- hồi quy trên smoke -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def smoke_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("evaluator_smoke")
    asyncio.run(generate("smoke", out, knowledge=K, profiles=load_profiles()))
    return out


@pytest.fixture(scope="module")
def smoke_run(smoke_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    run = tmp_path_factory.mktemp("evaluator_smoke_run")
    asyncio.run(run_detector(smoke_dir, out_dir=run, knowledge=K))
    return run


def test_evaluator_on_smoke_is_perfect_on_detection_and_report_is_stable(
    smoke_dir: Path, smoke_run: Path, tmp_path: Path
) -> None:
    report = evaluate(load_run(smoke_dir, run_dir=smoke_run), K)
    assert report.sf1.passed is True
    assert report.sf2.recall == 1.0 and report.sf3.precision == 1.0
    assert report.sf3.n_trap_detected == 0
    assert report.sf4.accuracy is not None and report.sf4.accuracy >= 0.85
    assert report.sf5.batch.n_undelivered == 0
    assert report.sf7.schema_pct == 1.0 and report.sf7.render_pct == 1.0
    assert report.config == "rule-only" and report.profile == smoke_dir.name

    write_report(report, tmp_path / "a")
    write_report(evaluate(load_run(smoke_dir, run_dir=smoke_run), K), tmp_path / "b")
    a, b = (tmp_path / x / "report.json" for x in ("a", "b"))
    assert a.read_bytes() == b.read_bytes()  # chạy 2 lần cho kết quả giống hệt
    md = (tmp_path / "a" / "report.md").read_text(encoding="utf-8")
    assert md == render_eval_md(report) and "SF4" in md and "Confusion matrix" in md


def test_load_run_split_selects_only_sessions_of_that_split(
    smoke_dir: Path, smoke_run: Path
) -> None:
    everything = load_run(smoke_dir, run_dir=smoke_run)
    assert everything.eval_ids == frozenset(everything.gt)
    splits = {g.split for g in everything.gt.values()}
    assert splits  # có ít nhất 1 split
    for sp in splits:
        sub = load_run(smoke_dir, run_dir=smoke_run, split=sp)
        assert sub.eval_ids == {s for s, g in everything.gt.items() if g.split == sp}
    assert load_run(smoke_dir, run_dir=smoke_run, split="khong-co").eval_ids == frozenset()


def test_ablation_report_on_smoke(smoke_dir: Path) -> None:
    for preset in PRESETS:
        asyncio.run(
            run_detector(
                smoke_dir, preset=preset, out_dir=preset_run_dir(smoke_dir, preset), knowledge=K
            )
        )
    rep = build_report(smoke_dir)
    assert [r.preset for r in rep.rows] == list(PRESETS)
    assert rep.rows[0].sf4.accuracy == 0.0  # T0: mọi phiên đều UNKNOWN
    assert rep.rows[0].n_unknown == rep.rows[0].sf2.n_detected
    assert "R_CATALOG" in rep.rows[0].disabled_rules  # thiếu error_code → rule tự tắt
    assert rep.rows[-1].disabled_rules == []  # ALL: không rule nào bị tắt
    assert rep.rows[-1].sf4.accuracy is not None and rep.rows[-1].sf4.accuracy >= 0.85
    assert rep.minimal_preset in PRESETS[1:]  # T0 không bao giờ đạt
    md = render_ablation_md(rep, K)
    assert "| `T0+T1` |" in md and "Trường cần xin" in md
