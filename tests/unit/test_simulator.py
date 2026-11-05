from __future__ import annotations

import hashlib
import itertools
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import orjson
import polars as pl
import pytest

from ase.common.models import CabinetTelemetry, Category, Severity, Step, SwapEvent
from ase.common.time import to_local
from ase.knowledge.loader import default_knowledge
from ase.simulator.checks import SimulationCheckError, check_eval_thresholds, check_invariants
from ase.simulator.disorder import apply_disorder, wait_starts
from ase.simulator.entities import World
from ase.simulator.generator import generate, ordered_events, simulate
from ase.simulator.ground_truth import build_row
from ase.simulator.profiles import BackgroundFailure, load_profiles
from ase.simulator.replay import replay_file
from ase.simulator.scenarios import (
    GENERIC_UNMAPPED_MESSAGE,
    PROMPT_INJECTION_PAYLOAD,
    load_scenarios,
)
from ase.simulator.session_fsm import SessionResult, step_chain
from ase.simulator.traffic import hourly_weights

K = default_knowledge()
PROFILES = load_profiles()
SCENARIOS = load_scenarios()


@pytest.fixture(scope="module")
def smoke_bg() -> tuple[World, list[SessionResult], list[CabinetTelemetry]]:
    return simulate("smoke", K, PROFILES, inject_scenarios=False)


@pytest.fixture(scope="module")
def dev_bg() -> tuple[World, list[SessionResult], list[CabinetTelemetry]]:
    return simulate("dev", K, PROFILES, inject_scenarios=False)


@pytest.fixture(scope="module")
def dev_full() -> tuple[World, list[SessionResult], list[CabinetTelemetry]]:
    return simulate("dev", K, PROFILES, inject_scenarios=True, scenarios_file=SCENARIOS)


# --- bước 5: entities + profiles -------------------------------------------------------------


def test_smoke_world_two_vn_stations(
    smoke_bg: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    world, _, _ = smoke_bg
    assert len(world.stations) == 2
    assert {s.country for s in world.stations} == {"VN"}
    for st in world.stations:
        assert 2 <= len(world.cabinets_of_station[st.station_id]) <= 4
    assert all(len(c.slot_ids) == 12 for c in world.cabinet_info.values())
    assert len({b.lot_id for b in world.batteries}) <= 6


def test_dev_world_vn2_ph1_id2_two_timezones(
    dev_bg: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    world, _, _ = dev_bg
    by_country = Counter(s.country for s in world.stations)
    assert by_country == {"VN": 2, "PH": 1, "ID": 2}
    assert {s.timezone for s in world.stations if s.country == "ID"} == {
        "Asia/Jakarta",
        "Asia/Makassar",
    }
    for s in world.stations:
        assert s.timezone in K.countries.countries[s.country].timezones


def test_fleet_package_only_vn(
    dev_bg: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    world, _, _ = dev_bg
    for u in world.users:
        assert u.package_type in K.countries.countries[u.country].pricing.packages


# --- bước 6: traffic theo giờ địa phương -----------------------------------------------------


def test_hourly_weights_peaks() -> None:
    w = hourly_weights(PROFILES.common)
    assert w[7] == w[8] == w[17] == w[18] == PROFILES.common.peak_weight
    assert w[12] < w[7] and w[3] < w[12]


def test_peak_hours_by_local_time_per_timezone(
    dev_bg: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    world_dev, results, _ = dev_bg
    tz_of = {s.station_id: s.timezone for s in world_dev.stations}
    by_tz: dict[str, list[int]] = {}
    for r in results:
        tz = tz_of[r.session.station_id]
        by_tz.setdefault(tz, []).append(to_local(r.session.started_at, tz).hour)
    assert set(by_tz) == {"Asia/Ho_Chi_Minh", "Asia/Manila", "Asia/Jakarta", "Asia/Makassar"}
    for tz, hours in by_tz.items():
        peak = sum(1 for h in hours if h in (7, 8, 17, 18)) / len(hours)
        assert peak > 0.45, (tz, peak)


# --- bước 7: FSM + payment + telemetry + lỗi nền -----------------------------------------------


def test_ok_sessions_walk_all_seven_steps(
    smoke_bg: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = smoke_bg
    chain = step_chain(K.state_machine)
    assert len(chain) == 7
    oks = [r for r in results if r.label is None]
    assert oks
    for r in oks:
        assert [e.step for e in r.events] == chain
        assert all(e.status == "OK" for e in r.events)
        assert r.session.final_status == "COMPLETED"


def test_fsm_only_follows_state_machine(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    sm = K.state_machine
    for r in results:
        steps = [e.step for e in r.events]
        assert steps[0] is Step.OPEN_APP
        for a, b in itertools.pairwise(steps):
            # pending_then_ok có 2 event liên tiếp cùng bước 5 (PENDING -> OK)
            assert a is b or sm.next_step(a) is b
        for idx, ev in enumerate(r.events[:-1]):
            if r.events[idx + 1].step is ev.step:
                assert sm.status_allowed(ev.status, r.events[idx + 1].status)
            else:
                assert ev.status == "OK"


def test_background_errors_only_catalog_codes(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    for r in results:
        for e in r.events:
            assert e.error_code is None or K.error_catalog.has(e.error_code)
        if r.label:
            assert K.error_catalog.has(r.label.error_code)
            assert r.label.category is not Category.UNKNOWN


def test_background_fail_rate_and_mostly_user(
    dev_bg: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_bg
    fails = [r for r in results if r.label]
    assert 0.03 <= len(fails) / len(results) <= 0.05
    user = sum(1 for r in fails if r.label and r.label.category is Category.USER)
    assert user / len(fails) > 0.5


def test_payment_rules(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    world, results, _ = dev_full
    for r in results:
        country = K.countries.countries[r.country]
        reached_payment = any(e.step is Step.PAYMENT for e in r.events)
        assert (r.payment is not None) == reached_payment
        if r.payment is None:
            continue
        assert r.payment.currency == country.currency
        user_pkg = world.user_by_id[r.session.user_id].package_type
        # Số tiền thanh toán luôn khớp đúng gói thuê của user (không hardcode RENT_1)
        assert r.payment.amount == country.pricing.standard_swap_amount(user_pkg)
        assert r.session.payment_provider in country.payment_providers
        if r.label is None:
            assert r.payment.captured and r.payment.status == "SUCCESS"
        elif r.label.error_code == "PAID_NOT_DELIVERED":
            assert r.payment.captured and r.payment.status == "SUCCESS"
        else:
            assert not r.payment.captured


def test_bat_low_soc_has_low_soc_telemetry(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    low_soc = [r for r in results if r.label and r.label.error_code == "BAT_LOW_SOC"]
    assert low_soc
    for r in low_soc:
        out_rows = [t for t in r.telemetry if t.slot_id == r.session.slot_out and t.soc is not None]
        assert out_rows
        assert all((t.soc or 100.0) < K.error_catalog.thresholds.low_soc_pct for t in out_rows)


def test_pre_payment_failures_have_no_provider(
    smoke_bg: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = smoke_bg
    for r in results:
        if r.payment is None:
            assert r.session.payment_provider is None


def test_telemetry_traces_hardware_failures() -> None:
    cfg = PROFILES.model_copy(deep=True)
    cfg.common.background_fail_rate = 1.0
    cfg.common.background_failures = [
        BackgroundFailure(code="SLOT_SENSOR_FAIL", weight=1, omit_code_allowed=True)
    ]
    _, results, _ = simulate("smoke", K, cfg, inject_scenarios=False)
    for r in results:
        rows = [t for t in r.telemetry if t.slot_id == r.session.slot_in]
        assert rows
        assert rows[-1].door_state == "CLOSED" and rows[-1].battery_detected is False


# --- bước 8: ground truth + generator ---------------------------------------------------------


def test_one_ground_truth_row_per_session_and_split(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    rows = [build_row(r, PROFILES.profiles["dev"].split) for r in results]
    assert len(rows) == len({r.session.session_id for r in results}) == len(results)
    for row in rows:
        assert row.category is not Category.UNKNOWN
        if not row.is_failure:
            assert row.category is None and row.error_code is None
            assert row.severity is None and row.scenario_instance_id is None


def test_dev_split_baseline_then_dev(
    dev_bg: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_bg
    rule = PROFILES.profiles["dev"].split
    days_by_split: dict[str, set[int]] = {}
    for r in results:
        days_by_split.setdefault(build_row(r, rule).split, set()).add(r.day_index)
    assert days_by_split["baseline"] == set(range(7))
    assert days_by_split["dev"] == set(range(7, 14))


def test_test_seed_differs_from_dev_seed() -> None:
    p = PROFILES.profiles["eval"]
    assert p.seed_for_day(0) == p.seed
    assert p.seed_for_day(25) == p.test_seed != p.seed


def test_missing_error_code_rate(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    all_fails = [r for r in results if r.label is not None]
    explicit_missing = [
        r
        for r in all_fails
        if r.events[-1].status in ("FAILED", "TIMEOUT") and r.events[-1].error_code is None
    ]
    rate = len(explicit_missing) / len(all_fails)
    assert 0.07 <= rate <= 0.13, rate
    for r in explicit_missing:
        assert r.label is not None and r.label.category is not Category.UNKNOWN
        # Không lộ mô tả catalog trong error_message khi đã bỏ error_code
        assert r.events[-1].error_message == GENERIC_UNMAPPED_MESSAGE


# --- bước 9: 11 scenario + lỗi im lặng + ngưỡng nhiệt -----------------------------------------


def test_deploy_503_covers_all_3_countries_with_single_instance_id(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    deploy = [r for r in results if r.label and r.label.scenario == "deploy_503"]
    assert deploy
    by_inst: dict[str, set[str]] = {}
    for r in deploy:
        assert r.label is not None and r.label.scenario_instance_id is not None
        assert r.label.error_code == "SESS_CREATE_5XX"
        assert r.label.category is Category.SYSTEM
        assert r.events[-1].step is Step.SCAN_QR
        assert (r.events[-1].latency_ms or 0) >= 2200
        by_inst.setdefault(r.label.scenario_instance_id, set()).add(r.country)
    # Mỗi đợt deploy 503 phủ đủ cả 3 nước VN, PH, ID nhưng dùng chung 1 scenario_instance_id
    for inst_id, countries in by_inst.items():
        assert countries == {"VN", "PH", "ID"}, inst_id


def test_app_v321_qr_bug_only_affects_v321(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    qr = [r for r in results if r.label and r.label.scenario == "app_v321_qr_bug"]
    assert qr
    for r in qr:
        assert r.session.app_version == "3.2.1"
        assert r.events[-1].step is Step.SCAN_QR
        assert r.label is not None and r.label.category is Category.SYSTEM


def test_slot_door_jam_cab03_slot07_and_intermittent_heartbeat(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, telemetry = dev_full
    jams = [r for r in results if r.label and r.label.scenario == "slot_door_jam"]
    assert jams
    cab_id = jams[0].session.cabinet_id
    assert cab_id is not None and cab_id.endswith("CAB-03")
    for r in jams:
        assert r.session.cabinet_id == cab_id
        assert r.session.slot_in == "07"
        assert r.label is not None and r.label.error_code == "SLOT_DOOR_JAM"
        assert r.telemetry and r.telemetry[-1].door_state == "CLOSED"
        assert r.telemetry[-1].battery_detected is False
    # Heartbeat chập chờn trong cửa sổ jam (bỏ phút chẵn)
    w_start = jams[0].label.scenario_start if jams[0].label else None
    assert w_start is not None
    hbs = [
        t
        for t in telemetry
        if t.cabinet_id == cab_id
        and t.slot_id is None
        and w_start <= t.heartbeat_at < w_start + timedelta(hours=2)
    ]
    assert 0 < len(hbs) < 80


def test_ph_wallet_1_only_affects_ph(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    outage = [r for r in results if r.label and r.label.scenario == "ph_wallet_outage"]
    assert outage
    for r in outage:
        assert r.country == "PH"
        assert r.session.payment_provider == "PH_WALLET_1"
        assert r.label is not None and r.label.category is Category.PAYMENT
        assert r.label.error_code in ("PAY_GATEWAY_TIMEOUT", "PAY_CALLBACK_LATE")


def test_tz_display_covers_ph_and_id_wita(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    world, results, _ = dev_full
    tz_of = {s.station_id: s.timezone for s in world.stations}
    tz_rows = [r for r in results if r.label and r.label.scenario == "tz_display"]
    assert {tz_of[r.session.station_id] for r in tz_rows} == {"Asia/Manila", "Asia/Makassar"}


def test_paid_not_delivered_silent_failure(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    pnd = [r for r in results if r.label and r.label.scenario == "paid_not_delivered"]
    assert pnd
    for r in pnd:
        assert r.payment is not None and r.payment.captured is True
        assert all(e.status == "OK" for e in r.events)
        assert {e.step for e in r.events}.isdisjoint({Step.RETURN_BATTERY, Step.RECEIVE_BATTERY})
        assert r.label is not None
        assert r.label.error_code == "PAID_NOT_DELIVERED"
        assert r.label.category is Category.PAYMENT
        assert r.label.severity is Severity.P1


def test_low_soh_lot_and_peak_no_charged_battery(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    world, results, _ = dev_full
    low_soh = [r for r in results if r.label and r.label.scenario == "low_soh_lot"]
    assert low_soh
    for r in low_soh:
        assert r.session.battery_out_id is not None
        bat = world.battery_by_id[r.session.battery_out_id]
        assert bat.lot_id == SCENARIOS.lot_low_soh_id and bat.soh < 70.0
        assert r.label is not None and r.label.error_code == "BAT_LOW_SOH"

    # Phiên ngoài scenario low_soh_lot tuyệt đối không nhận pin LOT-F (SOH < 70%)
    for r in results:
        if r.label and r.label.scenario == "low_soh_lot":
            continue
        for bid in (r.session.battery_in_id, r.session.battery_out_id):
            if bid is not None:
                bat = world.battery_by_id[bid]
                assert bat.lot_id != SCENARIOS.lot_low_soh_id
                assert bat.soh >= 75.0

    peak = [r for r in results if r.label and r.label.scenario == "peak_no_charged_battery"]
    assert peak
    tz_of = {s.station_id: s.timezone for s in world.stations}
    stations_by_inst: dict[str, set[str]] = {}
    for r in peak:
        assert r.label is not None and r.label.error_code == "NO_CHARGED_BATTERY"
        assert r.label.category is Category.BUSINESS
        assert to_local(r.session.started_at, tz_of[r.session.station_id]).hour in (7, 8, 17, 18)
        assert r.label.scenario_instance_id is not None
        stations_by_inst.setdefault(r.label.scenario_instance_id, set()).add(r.session.station_id)
    # Mỗi đợt thiếu pin cao điểm tập trung đúng tại 1 trạm
    for st_set in stations_by_inst.values():
        assert len(st_set) == 1


def test_pending_then_ok_is_not_failure(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    traps = [r for r in results if r.non_failure_scenario == "pending_then_ok"]
    assert traps
    rule = PROFILES.profiles["dev"].split
    for r in traps:
        assert r.label is None
        pay_evs = [e for e in r.events if e.step is Step.PAYMENT]
        assert [e.status for e in pay_evs] == ["PENDING", "OK"]
        assert 0 < (pay_evs[1].occurred_at - pay_evs[0].occurred_at).total_seconds() < 90
        gt = build_row(r, rule)
        assert gt.is_failure is False
        assert gt.scenario == "pending_then_ok"
        assert gt.category is None and gt.error_code is None


def test_prompt_injection_keeps_true_category(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    pi = [r for r in results if r.label and r.label.scenario == "prompt_injection"]
    assert pi
    for r in pi:
        assert r.events[-1].error_message == PROMPT_INJECTION_PAYLOAD
        assert r.label is not None and r.label.category is Category.HARDWARE


def test_step_timeout_silent_has_no_timeout_event_and_real_category(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    st = [r for r in results if r.label and r.label.scenario == "step_timeout"]
    assert st
    for r in st:
        assert all(e.status == "OK" for e in r.events)
        assert r.events[-1].step is Step.RETURN_BATTERY
        assert r.label is not None and r.label.error_code == "STEP_TIMEOUT"
        assert r.label.category is Category.HARDWARE


def test_thermal_overload_55c_p1_and_60c_shutdown(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, telemetry = dev_full
    th = [r for r in results if r.label and r.label.scenario == "thermal_overload"]
    assert th
    p2_faults = [
        r
        for r in th
        if r.label and r.label.error_code == "BAT_RETURN_FAULT" and r.label.severity is Severity.P2
    ]
    p1_faults = [
        r
        for r in th
        if r.label and r.label.error_code == "BAT_RETURN_FAULT" and r.label.severity is Severity.P1
    ]
    overheats = [r for r in th if r.label and r.label.error_code == "CABINET_OVERHEAT"]
    assert p2_faults and p1_faults and overheats

    for r in p2_faults:
        assert (r.telemetry[0].temperature or 0) <= K.error_catalog.thresholds.temp_warn_1_c
    for r in p1_faults:
        assert (r.telemetry[0].temperature or 0) > K.error_catalog.thresholds.temp_warn_1_c
        assert r.telemetry[0].hw_error_code == K.error_catalog.thresholds.hw_warn_level_1_code
    for r in overheats:
        assert (r.telemetry[0].temperature or 0) > K.error_catalog.thresholds.temp_warn_2_c
        assert r.telemetry[0].hw_error_code == K.error_catalog.thresholds.hw_warn_level_2_code
        t_cut = r.events[-1].occurred_at
        shut_end = t_cut + timedelta(minutes=K.error_catalog.thresholds.cabinet_shutdown_minutes)
        cab = r.telemetry[0].cabinet_id
        # Không có heartbeat định kỳ nào của tủ trong thời gian tự ngắt nguồn
        hbs = [
            t
            for t in telemetry
            if t.cabinet_id == cab and t.slot_id is None and t_cut <= t.heartbeat_at < shut_end
        ]
        assert not hbs


# --- bước 10 & 11: disorder, replay, checks, determinism --------------------------------------


def test_disorder_preserves_sessions_and_bounds_late_delay(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    _, results, _ = dev_full
    sm = K.state_machine
    ordered = ordered_events(results)
    disordered = apply_disorder(ordered, seed=1042, state_machine=sm)
    assert len(disordered) > len(ordered)
    assert {e.session_id for e in disordered} == {e.session_id for e in ordered}
    assert {e.event_id for e in disordered} == {e.event_id for e in ordered}
    assert any(a.occurred_at > b.occurred_at for a, b in itertools.pairwise(disordered))

    # Replay theo SimClock: trễ tối đa 8s, và không event nào của phiên bình thường đến sau SLA
    # của bước (tính từ event cuối của bước trước), tức tracker không thể báo nhầm STEP_TIMEOUT.
    normal = {r.session.session_id for r in results if r.label is None}
    starts = wait_starts(ordered, sm)
    clock: datetime | None = None
    max_lag_s = 0.0
    breaches: list[str] = []
    for ev in disordered:
        if clock is None or ev.occurred_at > clock:
            clock = ev.occurred_at
        max_lag_s = max(max_lag_s, (clock - ev.occurred_at).total_seconds())
        sla = sm.sla(ev.step)
        start = starts.get(ev.event_id)
        if ev.session_id in normal and sla is not None and start is not None:
            if (clock - start).total_seconds() > sla:
                breaches.append(ev.event_id)
    assert 0.5 <= max_lag_s <= 8.0
    assert breaches == []


async def test_generate_smoke_outputs_determinism_and_replay(tmp_path: Path) -> None:
    m1 = await generate("smoke", tmp_path / "a", knowledge=K, profiles=PROFILES)
    m2 = await generate("smoke", tmp_path / "b", knowledge=K, profiles=PROFILES)
    assert {k: v.sha256 for k, v in m1.files.items()} == {k: v.sha256 for k, v in m2.files.items()}

    out = tmp_path / "a"
    gt = pl.read_csv(out / "ground_truth.csv")
    ev_lines = (out / "ase.swap.events.jsonl").read_text().splitlines()
    expected_keys = set(SwapEvent.model_fields)
    for line in ev_lines:
        assert set(orjson.loads(line)) == expected_keys
    events = [SwapEvent.model_validate_json(line) for line in ev_lines]
    assert all(e.occurred_at.tzinfo is not None for e in events)
    assert gt.height == len({e.session_id for e in events})
    assert gt["session_id"].n_unique() == gt.height
    assert "UNKNOWN" not in set(gt["category"].drop_nulls().to_list())
    manifest_sha = hashlib.sha256((out / "ase.swap.events.jsonl").read_bytes()).hexdigest()
    assert manifest_sha == m1.files["ase.swap.events.jsonl"].sha256

    summary = await replay_file(out / "ase.swap.events.jsonl")
    assert summary.total_events == len(events)
    assert summary.duplicate_events > 0
    assert summary.late_events > 0
    assert summary.unique_sessions == gt.height


def test_eval_threshold_checker_enforces_rules(
    dev_full: tuple[World, list[SessionResult], list[CabinetTelemetry]],
) -> None:
    world, results, _ = dev_full
    gt_rows = [build_row(r, PROFILES.profiles["dev"].split) for r in results]
    check_invariants(
        profile_name="dev",
        events=ordered_events(results),
        sessions=[r.session for r in results],
        ground_truth=gt_rows,
        cabinets=world.cabinets,
        knowledge=K,
        scenarios_file=SCENARIOS,
    )
    # Nếu gọi check_eval_thresholds trên dữ liệu dev (không có split='test') phải báo lỗi
    with pytest.raises(SimulationCheckError):
        check_eval_thresholds(
            sessions=[r.session for r in results],
            ground_truth=gt_rows,
            cabinets=world.cabinets,
            scenarios_file=SCENARIOS,
        )
