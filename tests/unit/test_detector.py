"""Unit tests cho Detector lõi: SessionTracker, Classifier, FieldView, ContextStore (§8)."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Never

import polars as pl
import pytest

from ase.common.models import (
    Battery,
    CabinetTelemetry,
    Category,
    EventStatus,
    Payment,
    Severity,
    Station,
    Step,
    SwapEvent,
    SwapSession,
    User,
)
from ase.common.transport import SimClock
from ase.detector import (
    Classifier,
    ContextStore,
    FieldView,
    Finding,
    SessionTracker,
    format_core_summary,
    run_core_detector,
)
from ase.knowledge.loader import default_knowledge
from ase.simulator.generator import generate
from ase.simulator.profiles import load_profiles
from ase.simulator.scenarios import PROMPT_INJECTION_PAYLOAD

K = default_knowledge()
PROFILES = load_profiles()
T0_TIME = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)


def _ev(
    session_id: str,
    step: Step,
    at: datetime,
    *,
    status: EventStatus = "OK",
    error_code: str | None = None,
    error_message: str | None = None,
    event_id: str | None = None,
    station_id: str = "VN-ST-001",
    cabinet_id: str | None = "VN-ST-001-CAB-01",
    slot_id: str | None = "01",
) -> SwapEvent:
    return SwapEvent(
        event_id=event_id or f"evt-{session_id}-{step.value}-{status}-{int(at.timestamp())}",
        session_id=session_id,
        station_id=station_id,
        cabinet_id=cabinet_id,
        slot_id=slot_id,
        step=step,
        status=status,
        error_code=error_code,
        error_message=error_message,
        occurred_at=at,
    )


def _station(
    station_id: str = "VN-ST-001", country: str = "VN", tz: str = "Asia/Ho_Chi_Minh"
) -> Station:
    return Station(
        station_id=station_id,
        country=country,
        timezone=tz,
        model_gen="GEN2",
        install_date=T0_TIME - timedelta(days=90),
    )


def _session(
    session_id: str,
    *,
    user_id: str = "u_test",
    station_id: str = "VN-ST-001",
    cabinet_id: str | None = "VN-ST-001-CAB-01",
    slot_in: str | None = "01",
    slot_out: str | None = "02",
    battery_in_id: str | None = "BAT-IN",
    battery_out_id: str | None = "BAT-OUT",
    app_version: str = "3.2.0",
    payment_provider: str | None = "VN_WALLET_1",
    started_at: datetime = T0_TIME,
) -> SwapSession:
    return SwapSession(
        session_id=session_id,
        user_id=user_id,
        station_id=station_id,
        cabinet_id=cabinet_id,
        slot_in=slot_in,
        slot_out=slot_out,
        battery_in_id=battery_in_id,
        battery_out_id=battery_out_id,
        app_version=app_version,
        payment_provider=payment_provider,
        final_status="COMPLETED",
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=60),
    )


# --- 1. Knowledge detector.yaml -----------------------------------------------------------------


def test_detector_knowledge_config_loaded() -> None:
    det = K.detector
    assert det.correlator.window_minutes == 5
    assert det.notify.batch_interval_minutes == 15
    assert det.notify.max_rows_mail == 200
    assert det.notify.daily_report_local_time == "18:00"
    assert det.notify.daily_report_timezone == "Asia/Ho_Chi_Minh"
    assert set(det.field_presets) == {"T0", "T0+T1", "T0+T1+T2", "ALL"}
    assert "error_code" not in det.fields_for_preset("T0")
    assert "error_code" in det.fields_for_preset("T0+T1")
    assert "lot_id" in det.fields_for_preset("T0+T1+T2")
    assert "door_state" in det.fields_for_preset("ALL")


# --- 2. SessionTracker tests (DETECTOR_PLAN §8) -------------------------------------------------


def test_tracker_timeout_exact_at_sla_from_last_event_of_prev_step() -> None:
    clock = SimClock()
    tracker = SessionTracker(clock, knowledge=K)

    # S1: bước 1 OK lúc t0, bước 2 OK lúc t0+5s -> chờ bước 3 (SLA = 15s, hạn chót = t0+20s)
    assert tracker.on_event(_ev("S1", Step.OPEN_APP, T0_TIME)) == []
    assert tracker.on_event(_ev("S1", Step.SELECT_SERVICE, T0_TIME + timedelta(seconds=5))) == []

    # Tại t0 + 19.9s (< t0 + 20s): chưa timeout
    assert (
        tracker.on_event(
            _ev("S2", Step.OPEN_APP, T0_TIME + timedelta(milliseconds=19_900), event_id="evt-s2-1")
        )
        == []
    )

    # Đúng tại SLA t0 + 20s: nổ STEP_TIMEOUT ở bước đang chờ (3_scan_qr)
    findings = tracker.on_event(
        _ev("S2", Step.SELECT_SERVICE, T0_TIME + timedelta(seconds=20), event_id="evt-s2-2")
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.session_id == "S1"
    assert f.kind == "STEP_TIMEOUT"
    assert f.step is Step.SCAN_QR
    assert f.error_code == "STEP_TIMEOUT"
    assert f.occurred_at == T0_TIME + timedelta(seconds=20)


def test_tracker_pending_then_ok_at_step_5_does_not_timeout() -> None:
    clock = SimClock()
    tracker = SessionTracker(clock, knowledge=K)

    # Bước 1..4 OK
    t = T0_TIME
    for step in (Step.OPEN_APP, Step.SELECT_SERVICE, Step.SCAN_QR, Step.CHECK_ELIGIBILITY):
        t += timedelta(seconds=2)
        assert tracker.on_event(_ev("S-TRAP", step, t)) == []

    # Bước 5 PENDING lúc t+10s, rồi OK lúc t+75s (trong grace window 90s của bước 5)
    t_pending = t + timedelta(seconds=10)
    t_ok = t_pending + timedelta(seconds=65)
    assert (
        tracker.on_event(_ev("S-TRAP", Step.PAYMENT, t_pending, status="PENDING", event_id="e5-p"))
        == []
    )
    assert tracker.on_event(_ev("S-TRAP", Step.PAYMENT, t_ok, status="OK", event_id="e5-ok")) == []

    # Bước 6 đến lúc t_ok + 100s (> 120s tính từ t_pending, nhưng <= 120s tính từ t_ok!)
    # Phải tính SLA bước 6 từ event cuối của bước 5 (`t_ok`), nên KHÔNG bị timeout.
    t6 = t_ok + timedelta(seconds=100)
    assert tracker.on_event(_ev("S-TRAP", Step.RETURN_BATTERY, t6)) == []

    t7 = t6 + timedelta(seconds=30)
    assert tracker.on_event(_ev("S-TRAP", Step.RECEIVE_BATTERY, t7)) == []
    assert tracker.flush() == []


def test_tracker_duplicate_events_dropped_and_backward_transitions_ignored() -> None:
    clock = SimClock()
    tracker = SessionTracker(clock, knowledge=K)

    ev1 = _ev("S1", Step.OPEN_APP, T0_TIME, event_id="dup-1")
    ev2 = _ev("S1", Step.SELECT_SERVICE, T0_TIME + timedelta(seconds=3), event_id="e-2")
    ev3 = _ev("S1", Step.SCAN_QR, T0_TIME + timedelta(seconds=6), status="OK", event_id="e-3")

    assert tracker.on_event(ev1) == []
    # Nhân bản cùng event_id -> bị bỏ qua
    assert tracker.on_event(ev1.model_copy(deep=True)) == []
    assert tracker.duplicate_events_dropped == 1
    assert tracker.total_events_processed == 1

    assert tracker.on_event(ev2) == []
    assert tracker.on_event(ev3) == []

    # Transition lùi 1: trong cùng bước 3, đã OK mà nhận PENDING -> bị bỏ
    back_same_step = _ev(
        "S1",
        Step.SCAN_QR,
        T0_TIME + timedelta(seconds=7),
        status="PENDING",
        event_id="e-3-back",
    )
    assert tracker.on_event(back_same_step) == []
    assert tracker.backward_transitions_ignored == 1

    # Transition lùi 2: đang ở bước 3 mà nhận event bước 2 có timestamp muộn hơn -> bị bỏ
    back_prev_step = _ev(
        "S1",
        Step.SELECT_SERVICE,
        T0_TIME + timedelta(seconds=8),
        status="OK",
        event_id="e-2-back",
    )
    assert tracker.on_event(back_prev_step) == []
    assert tracker.backward_transitions_ignored == 2

    state = tracker.get_session_state("S1")
    assert state is not None
    assert state.current_step_idx == 2
    assert state.step_status[Step.SCAN_QR] == "OK"


def test_tracker_flush_at_end_of_stream_emits_pending_timers() -> None:
    clock = SimClock()
    tracker = SessionTracker(clock, knowledge=K)

    # S1 dừng sau bước 5 OK (chờ bước 6 SLA 120s), hết file gọi flush()
    t = T0_TIME
    for step in (
        Step.OPEN_APP,
        Step.SELECT_SERVICE,
        Step.SCAN_QR,
        Step.CHECK_ELIGIBILITY,
        Step.PAYMENT,
    ):
        t += timedelta(seconds=2)
        assert tracker.on_event(_ev("S-HANG", step, t)) == []

    flushed = tracker.flush()
    assert len(flushed) == 1
    assert flushed[0].session_id == "S-HANG"
    assert flushed[0].kind == "STEP_TIMEOUT"
    assert flushed[0].step is Step.RETURN_BATTERY
    assert flushed[0].occurred_at == t + timedelta(seconds=120)


def test_tracker_and_classifier_never_call_datetime_now(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ForbiddenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Never:
            raise AssertionError("Detector MUST NOT call datetime.now()!")

    monkeypatch.setattr("ase.detector.state_machine.datetime", _ForbiddenDatetime)
    monkeypatch.setattr("ase.detector.context.datetime", _ForbiddenDatetime)
    monkeypatch.setattr("ase.detector.fields.datetime", _ForbiddenDatetime)

    clock = SimClock()
    ctx = ContextStore.from_models(stations=[_station()])
    view = FieldView(ctx, preset="ALL", knowledge=K)
    tracker = SessionTracker(clock, knowledge=K, field_view=view)
    classifier = Classifier(clock, view, knowledge=K)

    ev_fail = _ev(
        "S-NOW",
        Step.SCAN_QR,
        T0_TIME,
        status="FAILED",
        error_code="SESS_CREATE_5XX",
    )
    findings = tracker.on_event(ev_fail)
    assert len(findings) == 1
    cls = classifier.classify(findings[0])
    assert cls.category is Category.SYSTEM


# --- 3. Classifier tests (DETECTOR_PLAN §8) -----------------------------------------------------


def test_classifier_paid_not_delivered_wins_and_respects_callback_at_time_boundary() -> None:
    clock = SimClock()
    t_fail = T0_TIME + timedelta(seconds=30)
    clock.advance(t_fail)

    # 1. payment.captured=True và callback_at <= now -> PAID_NOT_DELIVERED thắng mọi rule khác
    pay_known = Payment(
        payment_id="PAY-1",
        session_id="S-PND-1",
        amount=9000,
        currency="VND",
        status="SUCCESS",
        captured=True,
        callback_at=t_fail - timedelta(seconds=5),
    )
    # 2. payment.captured=True nhưng callback_at > now (tương lai) -> CHƯA được phép dùng
    pay_future = Payment(
        payment_id="PAY-2",
        session_id="S-PND-2",
        amount=9000,
        currency="VND",
        status="SUCCESS",
        captured=True,
        callback_at=t_fail + timedelta(seconds=10),
    )

    ctx = ContextStore.from_models(
        stations=[_station()],
        sessions=[_session("S-PND-1"), _session("S-PND-2")],
        payments=[pay_known, pay_future],
    )
    view = FieldView(ctx, preset="ALL", knowledge=K)
    classifier = Classifier(clock, view, knowledge=K)

    f1 = Finding(
        session_id="S-PND-1",
        station_id="VN-ST-001",
        cabinet_id="VN-ST-001-CAB-01",
        slot_id="01",
        step=Step.RETURN_BATTERY,
        kind="EXPLICIT_FAILURE",
        error_code="SLOT_DOOR_JAM",  # dù có mã HW nhưng đã trừ tiền bước >= 6 -> PND thắng
        status="FAILED",
        occurred_at=t_fail,
        detected_at=t_fail,
        event_ids=["evt-1"],
    )
    c1 = classifier.classify(f1)
    assert c1.rule_id == "R_PAID_NOT_DELIVERED"
    assert c1.category is Category.PAYMENT
    assert c1.severity is Severity.P1
    assert c1.error_code == "PAID_NOT_DELIVERED"
    assert c1.kind == "PAID_NOT_DELIVERED"

    f2 = Finding(
        session_id="S-PND-2",
        station_id="VN-ST-001",
        cabinet_id="VN-ST-001-CAB-01",
        slot_id=None,
        step=Step.RETURN_BATTERY,
        kind="STEP_TIMEOUT",
        error_code="STEP_TIMEOUT",
        status="TIMEOUT",
        occurred_at=t_fail,
        detected_at=t_fail,
        event_ids=["evt-2"],
    )
    c2 = classifier.classify(f2)
    assert c2.rule_id != "R_PAID_NOT_DELIVERED"
    assert c2.category is Category.UNKNOWN


def test_classifier_bat_return_fault_escalates_to_p1_above_55c() -> None:
    clock = SimClock()
    t_ev = T0_TIME + timedelta(seconds=20)
    clock.advance(t_ev)

    ctx = ContextStore.from_models(
        stations=[_station()],
        telemetry=[
            CabinetTelemetry(
                cabinet_id="CAB-LOW",
                slot_id="01",
                door_state="CLOSED",
                battery_detected=True,
                temperature=54.0,
                heartbeat_at=t_ev,
            ),
            CabinetTelemetry(
                cabinet_id="CAB-HIGH",
                slot_id="02",
                door_state="CLOSED",
                battery_detected=True,
                temperature=57.5,
                heartbeat_at=t_ev,
            ),
        ],
    )
    view = FieldView(ctx, preset="ALL", knowledge=K)
    classifier = Classifier(clock, view, knowledge=K)

    f_low = Finding(
        session_id="S-TEMP-LOW",
        station_id="VN-ST-001",
        cabinet_id="CAB-LOW",
        slot_id="01",
        step=Step.RETURN_BATTERY,
        kind="EXPLICIT_FAILURE",
        error_code="BAT_RETURN_FAULT",
        status="FAILED",
        occurred_at=t_ev,
        detected_at=t_ev,
        event_ids=["evt-low"],
    )
    f_high = f_low.model_copy(
        update={"session_id": "S-TEMP-HIGH", "cabinet_id": "CAB-HIGH", "slot_id": "02"}
    )

    c_low = classifier.classify(f_low)
    c_high = classifier.classify(f_high)
    assert c_low.category is Category.BATTERY_QUALITY
    assert c_low.severity is Severity.P2
    assert c_high.category is Category.BATTERY_QUALITY
    assert c_high.severity is Severity.P1


def test_classifier_step6_temperature_inference_rules_and_ablation() -> None:
    """Bước 6 không mã: >60°C -> CABINET_OVERHEAT; >55°C -> BAT_RETURN_FAULT; đúng rule_id."""
    clock = SimClock()
    t_ev = T0_TIME + timedelta(seconds=30)
    clock.advance(t_ev)

    def tel(cab: str, temp: float) -> CabinetTelemetry:
        # Cửa đã mở rồi đóng + có pin: không khớp DOOR_JAM / SENSOR_FAIL
        return CabinetTelemetry(
            cabinet_id=cab,
            slot_id="01",
            door_state="CLOSED",
            battery_detected=True,
            temperature=temp,
            heartbeat_at=t_ev,
        )

    open_rows = [
        CabinetTelemetry(
            cabinet_id=cab,
            slot_id="01",
            door_state="OPEN",
            battery_detected=False,
            temperature=30.0,
            heartbeat_at=t_ev - timedelta(seconds=20),
        )
        for cab in ("CAB-HOT", "CAB-WARM")
    ]
    ctx = ContextStore.from_models(
        stations=[_station()],
        telemetry=[*open_rows, tel("CAB-HOT", 62.0), tel("CAB-WARM", 57.0)],
    )
    base = Finding(
        session_id="S-HOT",
        station_id="VN-ST-001",
        cabinet_id="CAB-HOT",
        slot_id="01",
        step=Step.RETURN_BATTERY,
        kind="EXPLICIT_FAILURE",
        error_code=None,
        status="FAILED",
        occurred_at=t_ev,
        detected_at=t_ev,
        event_ids=["evt-hot"],
    )
    warm = base.model_copy(update={"session_id": "S-WARM", "cabinet_id": "CAB-WARM"})

    clf = Classifier(clock, FieldView(ctx, preset="ALL", knowledge=K), knowledge=K)
    c_hot, c_warm = clf.classify(base), clf.classify(warm)
    assert (c_hot.rule_id, c_hot.error_code, c_hot.category) == (
        "R_INF_CABINET_OVERHEAT",
        "CABINET_OVERHEAT",
        Category.HARDWARE,
    )
    assert c_hot.severity is Severity.P1
    assert (c_warm.rule_id, c_warm.error_code, c_warm.category) == (
        "R_INF_BAT_RETURN_FAULT",
        "BAT_RETURN_FAULT",
        Category.BATTERY_QUALITY,
    )
    assert c_warm.severity is Severity.P1

    # Thiếu temperature (T3) -> hai rule bị tắt, có trong coverage, rơi về UNKNOWN
    clf_t2 = Classifier(clock, FieldView(ctx, preset="T0+T1+T2", knowledge=K), knowledge=K)
    disabled = {r.rule_id for r in clf_t2.disabled_rules}
    assert {"R_INF_CABINET_OVERHEAT", "R_INF_BAT_RETURN_FAULT"} <= disabled
    assert clf_t2.classify(base).rule_id == "R_UNKNOWN"


def test_classifier_each_telemetry_and_context_inference_rule() -> None:
    clock = SimClock()
    t_ev = T0_TIME + timedelta(minutes=5)
    clock.advance(t_ev)

    users = [
        User(
            user_id="u_debt",
            country="VN",
            package_type="RENT_1",
            package_expiry=t_ev + timedelta(days=30),
            debt_flag=True,
        ),
        User(
            user_id="u_exp",
            country="VN",
            package_type="RENT_1",
            package_expiry=t_ev - timedelta(days=1),
            debt_flag=False,
        ),
    ]
    batteries = [
        Battery(battery_id="BAT-OK", lot_id="LOT-A", soh=92.0, cycle_count=50),
        Battery(battery_id="BAT-BAD-LOT", lot_id="LOT-F", soh=62.0, cycle_count=400),
    ]
    sessions = [
        _session("S-JAM", cabinet_id="CAB-1", slot_in="01"),
        _session("S-SENS", cabinet_id="CAB-1", slot_in="02"),
        _session("S-DISC", cabinet_id="CAB-DISC", slot_in="01"),
        _session("S-SOC", cabinet_id="CAB-1", slot_out="03", battery_out_id="BAT-OK"),
        _session("S-SOH", cabinet_id="CAB-1", slot_out="04", battery_out_id="BAT-BAD-LOT"),
        _session("S-DEBT", user_id="u_debt"),
        _session("S-EXP", user_id="u_exp"),
        _session("S-UNK", cabinet_id="CAB-1"),
    ]
    telemetry = [
        # Heartbeat bình thường cho CAB-1 (mỗi phút)
        *[
            CabinetTelemetry(
                cabinet_id="CAB-1",
                slot_id=None,
                temperature=31.0,
                heartbeat_at=T0_TIME + timedelta(minutes=m),
            )
            for m in range(6)
        ],
        # CAB-DISC mất heartbeat từ T0_TIME (khoảng trống 5 phút > 2 phút)
        CabinetTelemetry(
            cabinet_id="CAB-DISC",
            slot_id=None,
            temperature=31.0,
            heartbeat_at=T0_TIME,
        ),
        # Slot 01: cửa không mở (vẫn CLOSED) -> SLOT_DOOR_JAM
        CabinetTelemetry(
            cabinet_id="CAB-1",
            slot_id="01",
            door_state="CLOSED",
            battery_detected=False,
            temperature=32.0,
            heartbeat_at=t_ev,
        ),
        # Slot 02: cửa mở rồi đóng nhưng battery_detected=False -> SLOT_SENSOR_FAIL
        CabinetTelemetry(
            cabinet_id="CAB-1",
            slot_id="02",
            door_state="OPEN",
            battery_detected=False,
            temperature=32.0,
            heartbeat_at=t_ev - timedelta(seconds=15),
        ),
        CabinetTelemetry(
            cabinet_id="CAB-1",
            slot_id="02",
            door_state="CLOSED",
            battery_detected=False,
            temperature=32.0,
            heartbeat_at=t_ev,
        ),
        # Slot 03: SOC xuất thấp (< 20%) -> BAT_LOW_SOC
        CabinetTelemetry(
            cabinet_id="CAB-1",
            slot_id="03",
            door_state="OPEN",
            battery_detected=True,
            soc=14.5,
            temperature=32.0,
            heartbeat_at=t_ev - timedelta(seconds=10),
        ),
        CabinetTelemetry(
            cabinet_id="CAB-1",
            slot_id="03",
            door_state="CLOSED",
            battery_detected=False,
            soc=None,
            temperature=32.0,
            heartbeat_at=t_ev,
        ),
    ]

    ctx = ContextStore.from_models(
        stations=[_station()],
        users=users,
        batteries=batteries,
        sessions=sessions,
        telemetry=telemetry,
    )
    view = FieldView(ctx, preset="ALL", knowledge=K)
    classifier = Classifier(clock, view, knowledge=K)

    def make_finding(sid: str, step: Step, cab: str = "CAB-1", slot: str | None = None) -> Finding:
        return Finding(
            session_id=sid,
            station_id="VN-ST-001",
            cabinet_id=cab,
            slot_id=slot,
            step=step,
            kind="EXPLICIT_FAILURE",
            error_code=None,
            status="FAILED",
            occurred_at=t_ev,
            detected_at=t_ev,
            event_ids=[f"evt-{sid}"],
        )

    c_jam = classifier.classify(make_finding("S-JAM", Step.RETURN_BATTERY, slot="01"))
    assert (c_jam.category, c_jam.error_code, c_jam.rule_id) == (
        Category.HARDWARE,
        "SLOT_DOOR_JAM",
        "R_INF_SLOT_DOOR_JAM",
    )
    assert c_jam.confidence < 1.0

    c_sens = classifier.classify(make_finding("S-SENS", Step.RETURN_BATTERY, slot="02"))
    assert (c_sens.category, c_sens.error_code, c_sens.rule_id) == (
        Category.HARDWARE,
        "SLOT_SENSOR_FAIL",
        "R_INF_SLOT_SENSOR_FAIL",
    )

    c_disc = classifier.classify(
        make_finding("S-DISC", Step.RETURN_BATTERY, cab="CAB-DISC", slot="09")
    )
    assert (c_disc.category, c_disc.error_code, c_disc.rule_id) == (
        Category.HARDWARE,
        "CABINET_DISCONNECT",
        "R_INF_CABINET_DISCONNECT",
    )

    c_soc = classifier.classify(make_finding("S-SOC", Step.RECEIVE_BATTERY, slot="03"))
    assert (c_soc.category, c_soc.error_code, c_soc.rule_id) == (
        Category.BATTERY_QUALITY,
        "BAT_LOW_SOC",
        "R_INF_BAT_LOW_SOC",
    )

    c_soh = classifier.classify(make_finding("S-SOH", Step.RECEIVE_BATTERY, slot="04"))
    assert (c_soh.category, c_soh.error_code, c_soh.rule_id) == (
        Category.BATTERY_QUALITY,
        "BAT_LOW_SOH",
        "R_INF_BAT_LOW_SOH",
    )

    c_debt = classifier.classify(make_finding("S-DEBT", Step.CHECK_ELIGIBILITY))
    assert (c_debt.category, c_debt.error_code, c_debt.rule_id) == (
        Category.BUSINESS,
        "ELIG_DEBT",
        "R_INF_ELIG_DEBT",
    )

    c_exp = classifier.classify(make_finding("S-EXP", Step.CHECK_ELIGIBILITY))
    assert (c_exp.category, c_exp.error_code, c_exp.rule_id) == (
        Category.BUSINESS,
        "ELIG_PACKAGE_EXPIRED",
        "R_INF_ELIG_PACKAGE_EXPIRED",
    )

    c_unk = classifier.classify(make_finding("S-UNK", Step.SCAN_QR))
    assert c_unk.category is Category.UNKNOWN
    assert c_unk.needs_human_review is True
    assert c_unk.rule_id == "R_UNKNOWN"


def test_classifier_ignores_untrusted_error_message_prompt_injection() -> None:
    clock = SimClock()
    clock.advance(T0_TIME)
    ctx = ContextStore.from_models(stations=[_station()])
    view = FieldView(ctx, preset="ALL", knowledge=K)
    tracker = SessionTracker(clock, knowledge=K, field_view=view)
    classifier = Classifier(clock, view, knowledge=K)

    ev = _ev(
        "S-INJ",
        Step.RETURN_BATTERY,
        T0_TIME,
        status="FAILED",
        error_code="SLOT_SENSOR_FAIL",
        error_message=PROMPT_INJECTION_PAYLOAD,
    )
    findings = tracker.on_event(ev)
    assert len(findings) == 1
    cls = classifier.classify(findings[0])
    assert cls.category is Category.HARDWARE
    assert cls.severity is Severity.P2


# --- 4. Field tiers / ablation & Smoke run tests (DETECTOR_PLAN §6, §8) -------------------------


@pytest.fixture(scope="module")
def smoke_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import asyncio

    out = tmp_path_factory.mktemp("detector_smoke")
    asyncio.run(generate("smoke", out, knowledge=K, profiles=PROFILES))
    return out


async def test_field_tier_t0_disables_t1_plus_rules_and_runs_full_pipeline(
    smoke_dir: Path,
) -> None:
    cls_t0, cov_t0 = await run_core_detector(smoke_dir, preset="T0", knowledge=K)
    assert cov_t0.preset == "T0"
    assert cov_t0.total_events_processed > 0
    assert cov_t0.total_sessions_seen > 0
    assert len(cls_t0) > 0

    disabled_ids = {r.rule_id for r in cov_t0.disabled_rules}
    assert {
        "R_PAID_NOT_DELIVERED",
        "R_CATALOG",
        "R_INF_SLOT_DOOR_JAM",
        "R_INF_SLOT_SENSOR_FAIL",
        "R_INF_CABINET_DISCONNECT",
        "R_INF_BAT_LOW_SOH",
        "R_INF_BAT_LOW_SOC",
        "R_INF_ELIG_DEBT",
        "R_INF_ELIG_PACKAGE_EXPIRED",
    } <= disabled_ids
    # Ở T0 không có error_code và không có telemetry/context -> mọi phát hiện đều về UNKNOWN
    assert all(c.category is Category.UNKNOWN and c.needs_human_review for c in cls_t0)


async def test_core_detector_on_smoke_never_reads_ground_truth_and_matches_failures(
    smoke_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = builtins.open

    def _guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if "ground_truth.csv" in str(file):
            raise AssertionError("Detector MUST NOT open ground_truth.csv!")
        return real_open(file, *args, **kwargs)

    def _forbidden_read_csv(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Detector MUST NOT call pl.read_csv!")

    monkeypatch.setattr(builtins, "open", _guarded_open)
    monkeypatch.setattr(pl, "read_csv", _forbidden_read_csv)

    classifications, coverage = await run_core_detector(smoke_dir, preset="ALL", knowledge=K)
    summary_text = format_core_summary("smoke", classifications, coverage)
    assert "Detector Core Summary" in summary_text
    assert coverage.disabled_rules == []

    # Khôi phục để test đọc ground_truth.csv kiểm chứng kết quả
    monkeypatch.undo()
    gt = pl.read_csv(smoke_dir / "ground_truth.csv")
    gt_fail_sids = set(gt.filter(pl.col("is_failure"))["session_id"].to_list())
    trap_sids = set(
        gt.filter((~pl.col("is_failure")) & (pl.col("scenario") == "pending_then_ok"))[
            "session_id"
        ].to_list()
    )
    detected_sids = {c.session_id for c in classifications}

    # Không báo nhầm phiên pending_then_ok
    assert detected_sids.isdisjoint(trap_sids)
    # Phát hiện đúng toàn bộ phiên lỗi thật trên smoke
    assert detected_sids == gt_fail_sids
    assert coverage.total_sessions_seen == gt.height
