"""Unit & integration tests cho BSS Operational NOC Dashboard (DASHBOARD_PLAN §7)."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import polars as pl
import pytest
from fastapi.testclient import TestClient

from ase.common.config import Settings
from ase.common.models import Step
from ase.dashboard.data import (
    NotifierSnapshot,
    ProfileData,
    clear_cache,
)
from ase.dashboard.metrics import (
    build_alarms_page,
    compute_cabinet_online_map,
    compute_funnel,
    compute_ready_batteries_by_cabinet,
    compute_reconciliation,
    compute_revenue_by_currency,
    compute_stuck_sessions,
    estimate_soc,
    filter_sessions_today,
)
from ase.dashboard.models import (
    AlarmsResponse,
    AlertsPageResponse,
    BatteriesPageResponse,
    CabinetTemperatureTimeseriesResponse,
    GroundTruthPageResponse,
    IncidentsPlaceholderResponse,
    MetricsPlaceholderResponse,
    OverviewResponse,
    PaymentsPageResponse,
    ReadyBatteriesTimeseriesResponse,
    SessionDetailResponse,
    SessionsPageResponse,
    StationDetailResponse,
    StationsListResponse,
)
from ase.knowledge.loader import default_knowledge
from ase.notifier.mock_teams.app import create_app
from ase.simulator.entities import build_world
from ase.simulator.generator import generate
from ase.simulator.profiles import load_profiles

K = default_knowledge()
PROFILES = load_profiles()

_TEST_EVENT_SCHEMA: dict[str, pl.DataType] = {
    "event_id": pl.String(),
    "session_id": pl.String(),
    "user_id": pl.String(),
    "station_id": pl.String(),
    "cabinet_id": pl.String(),
    "slot_id": pl.String(),
    "step": pl.String(),
    "status": pl.String(),
    "error_code": pl.String(),
    "error_message": pl.String(),
    "occurred_at": pl.Datetime("us", "UTC"),
}


# --- 1. Unit tests cho metrics.py trên synthetic DataFrame ------------------------------------


def test_metrics_cabinet_online_threshold_around_120s() -> None:
    as_of = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)
    cabs = pl.DataFrame(
        {
            "cabinet_id": ["CAB-119", "CAB-120", "CAB-121", "CAB-NONE"],
            "station_id": ["ST-1", "ST-1", "ST-1", "ST-1"],
        }
    )
    tel = pl.DataFrame(
        {
            "cabinet_id": ["CAB-119", "CAB-120", "CAB-121"],
            "station_id": ["ST-1", "ST-1", "ST-1"],
            "slot_id": [None, None, None],
            "heartbeat_at": [
                as_of - timedelta(seconds=119),
                as_of - timedelta(seconds=120),
                as_of - timedelta(seconds=121),
            ],
        }
    )
    online_map = compute_cabinet_online_map(cabs, tel, as_of=as_of, online_window_seconds=120)
    assert online_map["CAB-119"][0] is True
    assert online_map["CAB-120"][0] is True
    assert online_map["CAB-121"][0] is False
    assert online_map["CAB-NONE"] == (False, None)


def test_metrics_filter_sessions_today_by_station_timezone() -> None:
    # as_of = 2026-11-04T16:30:00Z ->
    #   Asia/Ho_Chi_Minh (+7) = 2026-11-04 23:30:00 (local date 2026-11-04)
    #   Asia/Manila (+8)      = 2026-11-05 00:30:00 (local date 2026-11-05)
    as_of = datetime(2026, 11, 4, 16, 30, 0, tzinfo=UTC)
    stations = pl.DataFrame(
        {
            "station_id": ["ST-VN-01", "ST-PH-01"],
            "country": ["VN", "PH"],
            "timezone": ["Asia/Ho_Chi_Minh", "Asia/Manila"],
        }
    )
    sessions = pl.DataFrame(
        {
            "session_id": ["S-VN-TODAY", "S-VN-YESTERDAY", "S-PH-TODAY", "S-PH-YESTERDAY"],
            "station_id": ["ST-VN-01", "ST-VN-01", "ST-PH-01", "ST-PH-01"],
            "started_at": [
                # 2026-11-04 10:00Z -> 17:00+07 (2026-11-04 in VN)
                datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC),
                # 2026-11-03 10:00Z -> 17:00+07 (2026-11-03 in VN)
                datetime(2026, 11, 3, 10, 0, 0, tzinfo=UTC),
                # 2026-11-04 16:10Z -> 00:10+08 (2026-11-05 in PH = today in Manila!)
                datetime(2026, 11, 4, 16, 10, 0, tzinfo=UTC),
                # 2026-11-04 15:30Z -> 23:30+08 (2026-11-04 in PH = yesterday in Manila!)
                datetime(2026, 11, 4, 15, 30, 0, tzinfo=UTC),
            ],
            "final_status": ["COMPLETED", "COMPLETED", "COMPLETED", "COMPLETED"],
        }
    )
    today_df = filter_sessions_today(sessions, stations, as_of=as_of)
    ids = set(today_df["session_id"].to_list())
    assert ids == {"S-VN-TODAY", "S-PH-TODAY"}


def test_metrics_funnel_7_steps() -> None:
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)
    steps_list = list(Step)
    rows = []
    # Session 1: reaches step 7 OK (with step 5 PENDING then OK)
    for idx, st in enumerate(steps_list, start=1):
        if st == Step.PAYMENT:
            rows.append(
                {
                    "event_id": f"E-S1-{idx}-P",
                    "session_id": "S1",
                    "user_id": "U1",
                    "station_id": "VN-ST-001",
                    "cabinet_id": "VN-ST-001-CAB-01",
                    "slot_id": None,
                    "step": st.value,
                    "status": "PENDING",
                    "error_code": None,
                    "error_message": None,
                    "occurred_at": t0 + timedelta(seconds=idx * 2),
                }
            )
        rows.append(
            {
                "event_id": f"E-S1-{idx}",
                "session_id": "S1",
                "user_id": "U1",
                "station_id": "VN-ST-001",
                "cabinet_id": "VN-ST-001-CAB-01",
                "slot_id": None,
                "step": st.value,
                "status": "OK",
                "error_code": None,
                "error_message": None,
                "occurred_at": t0 + timedelta(seconds=idx * 2 + 1),
            }
        )
    # Session 2: fails at step 3 (3_scan_qr)
    for idx in (1, 2, 3):
        st = steps_list[idx - 1]
        rows.append(
            {
                "event_id": f"E-S2-{idx}",
                "session_id": "S2",
                "user_id": "U2",
                "station_id": "VN-ST-001",
                "cabinet_id": "VN-ST-001-CAB-01",
                "slot_id": None,
                "step": st.value,
                "status": "FAILED" if idx == 3 else "OK",
                "error_code": "CABINET_OFFLINE" if idx == 3 else None,
                "error_message": "Offline" if idx == 3 else None,
                "occurred_at": t0 + timedelta(seconds=idx * 2),
            }
        )
    ev_df = pl.DataFrame(rows, schema=_TEST_EVENT_SCHEMA)
    funnel = compute_funnel(ev_df, as_of=t0 + timedelta(seconds=60), knowledge=K)
    assert len(funnel) == 7
    assert funnel[0].step == Step.OPEN_APP
    assert funnel[0].reached_count == 2
    assert funnel[0].ok_count == 2
    assert funnel[2].step == Step.SCAN_QR
    assert funnel[2].reached_count == 2
    assert funnel[2].ok_count == 1
    assert funnel[2].failed_count == 1
    assert funnel[4].step == Step.PAYMENT
    assert funnel[4].ok_count == 1
    assert funnel[6].step == Step.RECEIVE_BATTERY
    assert funnel[6].ok_count == 1


def test_metrics_stuck_sessions_measured_from_last_event_of_prev_step() -> None:
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)
    steps_list = list(Step)
    # Step 5 (5_payment) SLA is sla_seconds=90; Step 6 (6_return_battery) SLA is sla_seconds=120.
    # Session S-NORMAL-5: step 1..4 OK (step 4 OK at t0+4s), step 5 PENDING at t0+6s,
    # step 5 OK at t0+50s. At as_of = t0+100s, waiting for step 6 (elapsed 50s <= 120s) -> OK!
    # Session S-STUCK-5: step 1..4 OK (step 4 OK at t0+4s), step 5 PENDING at t0+6s,
    # and no OK event at step 5 by as_of = t0+100s (elapsed 96s > 90s SLA of step 5) -> STUCK!
    rows = [
        *[
            {
                "event_id": f"EN-{idx}",
                "session_id": "S-NORMAL-5",
                "user_id": "U1",
                "station_id": "VN-ST-001",
                "cabinet_id": "VN-ST-001-CAB-01",
                "slot_id": None,
                "step": steps_list[idx - 1].value,
                "status": "OK",
                "error_code": None,
                "error_message": None,
                "occurred_at": t0 + timedelta(seconds=idx),
            }
            for idx in (1, 2, 3, 4)
        ],
        {
            "event_id": "EN-5P",
            "session_id": "S-NORMAL-5",
            "user_id": "U1",
            "station_id": "VN-ST-001",
            "cabinet_id": "VN-ST-001-CAB-01",
            "slot_id": None,
            "step": Step.PAYMENT.value,
            "status": "PENDING",
            "error_code": None,
            "error_message": None,
            "occurred_at": t0 + timedelta(seconds=6),
        },
        {
            "event_id": "EN-5OK",
            "session_id": "S-NORMAL-5",
            "user_id": "U1",
            "station_id": "VN-ST-001",
            "cabinet_id": "VN-ST-001-CAB-01",
            "slot_id": None,
            "step": Step.PAYMENT.value,
            "status": "OK",
            "error_code": None,
            "error_message": None,
            "occurred_at": t0 + timedelta(seconds=50),
        },
        *[
            {
                "event_id": f"ES-{idx}",
                "session_id": "S-STUCK-5",
                "user_id": "U2",
                "station_id": "VN-ST-001",
                "cabinet_id": "VN-ST-001-CAB-01",
                "slot_id": None,
                "step": steps_list[idx - 1].value,
                "status": "OK",
                "error_code": None,
                "error_message": None,
                "occurred_at": t0 + timedelta(seconds=idx),
            }
            for idx in (1, 2, 3, 4)
        ],
        {
            "event_id": "ES-5P",
            "session_id": "S-STUCK-5",
            "user_id": "U2",
            "station_id": "VN-ST-001",
            "cabinet_id": "VN-ST-001-CAB-01",
            "slot_id": None,
            "step": Step.PAYMENT.value,
            "status": "PENDING",
            "error_code": None,
            "error_message": None,
            "occurred_at": t0 + timedelta(seconds=6),
        },
    ]
    ev_df = pl.DataFrame(rows, schema=_TEST_EVENT_SCHEMA)
    stations = pl.DataFrame(
        {
            "station_id": ["VN-ST-001"],
            "country": ["VN"],
            "timezone": ["Asia/Ho_Chi_Minh"],
        }
    )
    as_of = t0 + timedelta(seconds=100)
    stuck = compute_stuck_sessions(ev_df, stations, None, as_of=as_of, knowledge=K)
    assert len(stuck) == 1
    assert stuck[0].session_id == "S-STUCK-5"
    assert stuck[0].stuck_step == Step.PAYMENT
    assert stuck[0].sla_seconds == 90
    assert stuck[0].elapsed_seconds == pytest.approx(96.0)


def test_metrics_reconciliation_and_revenue_by_currency() -> None:
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)
    steps_list = list(Step)
    # S-OK-VN: captured 9000 VND (RENT_1 in VN), reaches step 7 OK
    # S-ORPHAN-VN: captured 9000 VND, only reaches step 5 OK (no step 6/7)
    # S-MISMATCH-PH: captured 999 PHP (RENT_1 in PH is 35 PHP), reaches step 7 OK
    ev_rows = []
    for idx, st in enumerate(steps_list, start=1):
        ev_rows.append(
            {
                "event_id": f"E1-{idx}",
                "session_id": "S-OK-VN",
                "user_id": "USR-1",
                "station_id": "VN-ST-001",
                "cabinet_id": "VN-ST-001-CAB-01",
                "slot_id": None,
                "step": st.value,
                "status": "OK",
                "error_code": None,
                "error_message": None,
                "occurred_at": t0 + timedelta(seconds=idx),
            }
        )
        ev_rows.append(
            {
                "event_id": f"E3-{idx}",
                "session_id": "S-MISMATCH-PH",
                "user_id": "USR-2",
                "station_id": "PH-ST-001",
                "cabinet_id": "PH-ST-001-CAB-01",
                "slot_id": None,
                "step": st.value,
                "status": "OK",
                "error_code": None,
                "error_message": None,
                "occurred_at": t0 + timedelta(seconds=idx),
            }
        )
    for idx in range(1, 6):
        st = steps_list[idx - 1]
        ev_rows.append(
            {
                "event_id": f"E2-{idx}",
                "session_id": "S-ORPHAN-VN",
                "user_id": "USR-1",
                "station_id": "VN-ST-001",
                "cabinet_id": "VN-ST-001-CAB-01",
                "slot_id": None,
                "step": st.value,
                "status": "OK",
                "error_code": None,
                "error_message": None,
                "occurred_at": t0 + timedelta(seconds=idx),
            }
        )
    ev_df = pl.DataFrame(ev_rows, schema=_TEST_EVENT_SCHEMA)
    pay_df = pl.DataFrame(
        {
            "payment_id": ["PAY-1", "PAY-2", "PAY-3"],
            "session_id": ["S-OK-VN", "S-ORPHAN-VN", "S-MISMATCH-PH"],
            "amount": [9000.0, 9000.0, 999.0],
            "currency": ["VND", "VND", "PHP"],
            "status": ["OK", "OK", "OK"],
            "captured": [True, True, True],
            "callback_at": [t0, t0, t0],
        }
    )
    ss_df = pl.DataFrame(
        {
            "session_id": ["S-OK-VN", "S-ORPHAN-VN", "S-MISMATCH-PH"],
            "user_id": ["USR-1", "USR-1", "USR-2"],
            "station_id": ["VN-ST-001", "VN-ST-001", "PH-ST-001"],
            "cabinet_id": ["VN-ST-001-CAB-01", "VN-ST-001-CAB-01", "PH-ST-001-CAB-01"],
            "started_at": [t0, t0, t0],
            "ended_at": [t0 + timedelta(seconds=10)] * 3,
            "final_status": ["COMPLETED", "IN_PROGRESS", "COMPLETED"],
            "payment_provider": ["VN_ZALOPAY", "VN_ZALOPAY", "PH_GCASH"],
            "payment_id": ["PAY-1", "PAY-2", "PAY-3"],
        }
    )
    users_df = pl.DataFrame(
        {
            "user_id": ["USR-1", "USR-2"],
            "package_type": ["RENT_1", "RENT_1"],
        }
    )
    stations_df = pl.DataFrame(
        {
            "station_id": ["VN-ST-001", "PH-ST-001"],
            "country": ["VN", "PH"],
            "timezone": ["Asia/Ho_Chi_Minh", "Asia/Manila"],
        }
    )

    reconcile = compute_reconciliation(
        ss_df,
        pay_df,
        ev_df,
        users_df,
        stations_df,
        as_of=t0 + timedelta(seconds=60),
        knowledge=K,
    )
    by_session = {r.session_id: r for r in reconcile}
    assert "S-OK-VN" not in by_session
    assert by_session["S-ORPHAN-VN"].issue_type == "CAPTURED_WITHOUT_DELIVERY"
    assert by_session["S-MISMATCH-PH"].issue_type == "AMOUNT_MISMATCH"
    assert by_session["S-MISMATCH-PH"].expected_amount == 35.0

    rev = compute_revenue_by_currency(pay_df, ss_df, as_of=t0 + timedelta(seconds=60))
    assert rev == {"PHP": 999.0, "VND": 18000.0}


# --- 2. FastAPI TestClient tests trên dữ liệu smoke sinh vào tmp_path ------------------------


@pytest.fixture(scope="module")
def smoke_env(tmp_path_factory: pytest.TempPathFactory) -> tuple[Settings, Path]:
    base_dir = tmp_path_factory.mktemp("dashboard_smoke")
    data_dir = base_dir / "data"
    out_dir = data_dir / "smoke"
    asyncio.run(generate("smoke", out_dir, knowledge=K, profiles=PROFILES))

    db_path = base_dir / "outbox" / "mock_teams.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                incident_id TEXT NOT NULL UNIQUE,
                revision INTEGER NOT NULL,
                card_json TEXT NOT NULL,
                received_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT NOT NULL,
                action TEXT NOT NULL,
                at TEXT NOT NULL
            );
            """
        )
        card_payload = {
            "type": "AdaptiveCard",
            "version": "1.5",
            "incident_id": "INC-SMOKE-001",
            "severity": "P1",
            "category": "HARDWARE_FAULT",
            "station_id": "VN-ST-001",
            "cabinet_id": "VN-ST-001-CAB-01",
            "body": [{"type": "TextBlock", "text": "Cảnh báo kẹt khóa hộc trả pin"}],
        }
        conn.execute(
            "INSERT INTO cards (channel, incident_id, revision, card_json, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "field-ops",
                "INC-SMOKE-001",
                1,
                orjson.dumps(card_payload).decode("utf-8"),
                "2026-10-26T10:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO feedback (incident_id, action, at) VALUES (?, ?, ?)",
            ("INC-SMOKE-001", "confirm", "2026-10-26T10:05:00+00:00"),
        )
        conn.commit()

    settings = Settings(data_dir=data_dir, notifier_db=db_path)
    clear_cache()
    return settings, db_path


def test_api_and_html_endpoints_return_200_and_validate(
    smoke_env: tuple[Settings, Path],
) -> None:
    settings, db_path = smoke_env
    app = create_app(db_path=db_path, settings=settings)
    client = TestClient(app)

    # Static assets
    assert client.get("/static/dashboard/dashboard.css").status_code == 200
    assert client.get("/static/dashboard/dashboard.js").status_code == 200

    # Overview API & HTML
    r_ov = client.get("/api/dashboard/overview?profile=smoke")
    assert r_ov.status_code == 200
    ov = OverviewResponse.model_validate(r_ov.json())
    assert ov.meta.profile == "smoke"
    assert ov.kpi_total.stations_total == 2

    # Stations list & detail
    r_st = client.get("/api/dashboard/stations?profile=smoke")
    assert r_st.status_code == 200
    st_list = StationsListResponse.model_validate(r_st.json())
    assert len(st_list.stations) == 2
    first_station_id = st_list.stations[0].station_id

    r_st_det = client.get(f"/api/dashboard/stations/{first_station_id}?profile=smoke")
    assert r_st_det.status_code == 200
    st_det = StationDetailResponse.model_validate(r_st_det.json())
    assert st_det.station.station_id == first_station_id
    assert len(st_det.cabinets) >= 2

    # Sessions list & detail
    r_sess = client.get("/api/dashboard/sessions?profile=smoke")
    assert r_sess.status_code == 200
    sess_page = SessionsPageResponse.model_validate(r_sess.json())
    assert len(sess_page.funnel) == 7
    assert len(sess_page.recent_sessions) > 0
    first_session_id = sess_page.recent_sessions[0].session_id

    r_sess_det = client.get(f"/api/dashboard/sessions/{first_session_id}?profile=smoke")
    assert r_sess_det.status_code == 200
    sess_det = SessionDetailResponse.model_validate(r_sess_det.json())
    assert sess_det.session.session_id == first_session_id
    assert len(sess_det.events) > 0

    # Payments, Batteries, Alerts, Incidents, Metrics, Ground-Truth APIs
    r_pay = client.get("/api/dashboard/payments?profile=smoke")
    assert r_pay.status_code == 200
    PaymentsPageResponse.model_validate(r_pay.json())

    r_bat = client.get("/api/dashboard/batteries?profile=smoke")
    assert r_bat.status_code == 200
    BatteriesPageResponse.model_validate(r_bat.json())

    r_al = client.get("/api/dashboard/alerts?profile=smoke")
    assert r_al.status_code == 200
    al = AlertsPageResponse.model_validate(r_al.json())
    assert al.stats.total_cards == 1

    r_inc = client.get("/api/dashboard/incidents?profile=smoke")
    assert r_inc.status_code == 200
    IncidentsPlaceholderResponse.model_validate(r_inc.json())

    r_met = client.get("/api/dashboard/metrics?profile=smoke")
    assert r_met.status_code == 200
    MetricsPlaceholderResponse.model_validate(r_met.json())

    r_gt = client.get("/api/dashboard/ground-truth?profile=smoke")
    assert r_gt.status_code == 200
    gt = GroundTruthPageResponse.model_validate(r_gt.json())
    assert gt.total_sessions > 0

    # HTML pages
    html_paths = [
        "/dashboard?profile=smoke",
        "/dashboard/stations?profile=smoke",
        f"/dashboard/stations/{first_station_id}?profile=smoke",
        "/dashboard/sessions?profile=smoke",
        f"/dashboard/sessions/{first_session_id}?profile=smoke",
        "/dashboard/payments?profile=smoke",
        "/dashboard/batteries?profile=smoke",
        "/dashboard/alerts?profile=smoke",
        "/dashboard/incidents?profile=smoke",
        "/dashboard/metrics?profile=smoke",
        "/dashboard/ground-truth?profile=smoke",
    ]
    for path in html_paths:
        resp = client.get(path)
        assert resp.status_code == 200, f"Failed GET {path}: {resp.text}"
        assert "<!DOCTYPE html>" in resp.text


def test_as_of_clamping_and_invalid_filters(smoke_env: tuple[Settings, Path]) -> None:
    settings, db_path = smoke_env
    app = create_app(db_path=db_path, settings=settings)
    client = TestClient(app)

    # Out-of-bounds early as_of -> clamp to min_time
    r_early = client.get("/api/dashboard/overview?profile=smoke&as_of=2020-01-01T00:00:00Z")
    assert r_early.status_code == 200
    ov_early = OverviewResponse.model_validate(r_early.json())
    assert ov_early.meta.as_of_clamped is True
    assert ov_early.meta.as_of == ov_early.meta.min_time

    # Out-of-bounds late as_of -> clamp to max_time
    r_late = client.get("/api/dashboard/overview?profile=smoke&as_of=2030-01-01T00:00:00Z")
    assert r_late.status_code == 200
    ov_late = OverviewResponse.model_validate(r_late.json())
    assert ov_late.meta.as_of_clamped is True
    assert ov_late.meta.as_of == ov_late.meta.max_time

    # Invalid country -> 422
    r_bad_country = client.get("/api/dashboard/overview?profile=smoke&country=US")
    assert r_bad_country.status_code == 422

    # Invalid profile -> 422
    r_bad_prof = client.get("/api/dashboard/overview?profile=nonexistent")
    assert r_bad_prof.status_code == 422


def test_estimate_soc_charges_over_time_and_never_decreases() -> None:
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)
    assert estimate_soc(30.0, t0, t0 + timedelta(minutes=30), 60.0) == pytest.approx(60.0)
    assert estimate_soc(30.0, t0, t0 + timedelta(hours=5), 60.0) == 100.0  # kẹp tối đa
    assert estimate_soc(30.0, t0, t0 - timedelta(hours=1), 60.0) == 30.0  # as_of trước lần đọc
    assert estimate_soc(None, t0, t0, 60.0) is None
    assert estimate_soc(30.0, t0, t0 + timedelta(hours=1), 0.0) == 30.0  # tốc độ sạc 0


def test_ready_batteries_use_estimated_soc_and_exclude_empty_slots() -> None:
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)
    slots = pl.DataFrame({"cabinet_id": ["C1", "C1", "C1"], "slot_id": ["01", "02", "03"]})
    tel = pl.DataFrame(
        {
            "cabinet_id": ["C1", "C1", "C1"],
            "slot_id": ["01", "02", "03"],
            "battery_detected": [True, True, False],
            "soc": [30.0, 85.0, None],
            "heartbeat_at": [t0, t0, t0],
        },
        schema_overrides={"heartbeat_at": pl.Datetime("us", "UTC")},
    )
    # Ngay lúc đọc: khoang 01 (30%) và 02 (85%) chưa đầy, 03 trống -> 0 sẵn sàng
    now = compute_ready_batteries_by_cabinet(slots, tel, t0, 90.0, 60.0)
    assert now["C1"] == (0, 3)
    # 10 phút sau: 02 lên 95% (sẵn sàng), 01 mới 40%
    later = compute_ready_batteries_by_cabinet(slots, tel, t0 + timedelta(minutes=10), 90.0, 60.0)
    assert later["C1"] == (1, 3)
    # 2 giờ sau: 01 và 02 đầy; khoang trống 03 vẫn không tính
    much_later = compute_ready_batteries_by_cabinet(slots, tel, t0 + timedelta(hours=2), 90.0, 60.0)
    assert much_later["C1"] == (2, 3)


def test_stuck_sessions_stale_flag_and_ordering() -> None:
    t0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)
    stations = pl.DataFrame(
        {"station_id": ["VN-ST-001"], "country": ["VN"], "timezone": ["Asia/Ho_Chi_Minh"]}
    )

    def session(sid: str, start: datetime) -> list[dict[str, object]]:
        return [
            {
                "event_id": f"{sid}-{i}",
                "session_id": sid,
                "user_id": "U1",
                "station_id": "VN-ST-001",
                "cabinet_id": "VN-ST-001-CAB-01",
                "slot_id": None,
                "step": step.value,
                "status": "OK",
                "error_code": None,
                "error_message": None,
                "occurred_at": start + timedelta(seconds=i * 3),
            }
            for i, step in enumerate(list(Step)[:5])  # dừng sau bước 5 -> chờ bước 6 (SLA 120s)
        ]

    rows = [
        *session("S-OLD", t0),  # treo ~ 3 giờ
        *session("S-FRESH-A", t0 + timedelta(hours=2, minutes=50)),  # treo ~ 10 phút
        *session("S-FRESH-B", t0 + timedelta(hours=2, minutes=55)),  # treo ~ 5 phút
    ]
    events = pl.DataFrame(rows, schema=_TEST_EVENT_SCHEMA)
    as_of = t0 + timedelta(hours=3)
    stuck = compute_stuck_sessions(events, stations, None, as_of, K)
    assert [s.session_id for s in stuck] == ["S-FRESH-B", "S-FRESH-A", "S-OLD"]
    assert [s.stale for s in stuck] == [False, False, True]


def test_profile_must_be_a_known_name_not_a_path(smoke_env: tuple[Settings, Path]) -> None:
    settings, db_path = smoke_env
    client = TestClient(create_app(db_path=db_path, settings=settings))
    assert client.get("/api/dashboard/overview?profile=smoke").status_code == 200
    for bad in ("../smoke", "..%2Fsmoke", "/etc", "smoke/../smoke", "nonexistent"):
        r = client.get(f"/api/dashboard/overview?profile={bad}")
        assert r.status_code == 422, bad
        assert str(settings.data_dir) not in r.text, bad  # không lộ đường dẫn tuyệt đối


# --- 3. Ground-truth isolation test ----------------------------------------------------------


def test_operational_tabs_never_read_ground_truth_csv(
    smoke_env: tuple[Settings, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, db_path = smoke_env
    app = create_app(db_path=db_path, settings=settings)
    client = TestClient(app)

    def _forbidden_ground_truth(*args: object, **kwargs: object) -> object:
        raise AssertionError("Operational tabs MUST NOT call load_ground_truth or pl.read_csv!")

    monkeypatch.setattr("ase.dashboard.data.load_ground_truth", _forbidden_ground_truth)
    monkeypatch.setattr("ase.dashboard.api.load_ground_truth", _forbidden_ground_truth)
    monkeypatch.setattr("ase.dashboard.pages.load_ground_truth", _forbidden_ground_truth)
    monkeypatch.setattr("polars.read_csv", _forbidden_ground_truth)

    r_st = client.get("/api/dashboard/stations?profile=smoke")
    assert r_st.status_code == 200
    first_station_id = r_st.json()["stations"][0]["station_id"]

    r_sess = client.get("/api/dashboard/sessions?profile=smoke")
    assert r_sess.status_code == 200
    first_session_id = r_sess.json()["recent_sessions"][0]["session_id"]

    operational_urls = [
        "/api/dashboard/overview?profile=smoke",
        "/api/dashboard/stations?profile=smoke",
        f"/api/dashboard/stations/{first_station_id}?profile=smoke",
        "/api/dashboard/sessions?profile=smoke",
        f"/api/dashboard/sessions/{first_session_id}?profile=smoke",
        "/api/dashboard/payments?profile=smoke",
        "/api/dashboard/batteries?profile=smoke",
        "/api/dashboard/alerts?profile=smoke",
        "/api/dashboard/incidents?profile=smoke",
        "/api/dashboard/metrics?profile=smoke",
        "/api/dashboard/timeseries/cabinet-temperature?profile=smoke",
        "/api/dashboard/timeseries/ready-batteries?profile=smoke",
        "/api/dashboard/alarms?profile=smoke",
        "/dashboard?profile=smoke",
        "/dashboard/stations?profile=smoke",
        f"/dashboard/stations/{first_station_id}?profile=smoke",
        "/dashboard/sessions?profile=smoke",
        f"/dashboard/sessions/{first_session_id}?profile=smoke",
        "/dashboard/payments?profile=smoke",
        "/dashboard/batteries?profile=smoke",
        "/dashboard/alerts?profile=smoke",
        "/dashboard/incidents?profile=smoke",
        "/dashboard/metrics?profile=smoke",
    ]
    for url in operational_urls:
        resp = client.get(url)
        assert resp.status_code == 200, f"Operational endpoint failed: {url}"


# --- 4. Untrusted data HTML escaping test ----------------------------------------------------


def test_untrusted_data_html_escaping_on_prompt_injection_session(
    smoke_env: tuple[Settings, Path],
) -> None:
    settings, db_path = smoke_env
    # Find a session in smoke that has the prompt_injection payload (</untrusted_data>)
    events_path = settings.data_dir / "smoke" / "ase.swap.events.jsonl"
    injected_session_id: str | None = None
    for line in events_path.read_bytes().splitlines():
        if not line:
            continue
        obj = orjson.loads(line)
        msg = obj.get("error_message")
        if isinstance(msg, str) and "</untrusted_data>" in msg:
            injected_session_id = str(obj["session_id"])
            break

    assert injected_session_id is not None, "Expected prompt_injection session in smoke profile"

    app = create_app(db_path=db_path, settings=settings)
    client = TestClient(app)
    resp = client.get(f"/dashboard/sessions/{injected_session_id}?profile=smoke")
    assert resp.status_code == 200
    body = resp.text
    assert "</untrusted_data>" not in body
    assert "&lt;/untrusted_data&gt;" in body
    assert "dữ liệu chưa kiểm chứng" in body


# --- 5. Station coordinate determinism test --------------------------------------------------


def test_station_coordinates_deterministic_and_within_country_cities() -> None:
    w1 = build_world(PROFILES.profiles["dev"], PROFILES.common, K)
    w2 = build_world(PROFILES.profiles["dev"], PROFILES.common, K)
    assert [(s.station_id, s.lat, s.lon) for s in w1.stations] == [
        (s.station_id, s.lat, s.lon) for s in w2.stations
    ]

    for st in w1.stations:
        assert st.lat != 0.0 and st.lon != 0.0
        cities = [c for c in K.countries.countries[st.country].cities if c.timezone == st.timezone]
        assert cities, f"No city configured for {st.country} {st.timezone}"
        # Station must be within +-0.03 deg of one of the matching cities
        assert any(abs(st.lat - c.lat) <= 0.03 and abs(st.lon - c.lon) <= 0.03 for c in cities), (
            f"Station {st.station_id} ({st.lat}, {st.lon}) outside city bounds"
        )


# --- 6. New Timeseries & Alarms tests (DASHBOARD_UI_PLAN §4) ----------------------------------


def test_new_timeseries_and_alarms_api_smoke(smoke_env: tuple[Settings, Path]) -> None:
    settings, db_path = smoke_env
    app = create_app(db_path=db_path, settings=settings)
    client = TestClient(app)

    # 1. Cabinet temperature timeseries
    r_temp = client.get("/api/dashboard/timeseries/cabinet-temperature?profile=smoke&hours=1")
    assert r_temp.status_code == 200
    temp_resp = CabinetTemperatureTimeseriesResponse.model_validate(r_temp.json())
    assert temp_resp.warn_threshold_c == K.error_catalog.thresholds.temp_warn_1_c
    assert temp_resp.critical_threshold_c == K.error_catalog.thresholds.temp_warn_2_c
    assert len(temp_resp.available_stations) == 2
    assert len(temp_resp.series) >= 2

    # 2. Ready batteries timeseries
    r_ready = client.get(
        "/api/dashboard/timeseries/ready-batteries?profile=smoke&hours=6&step_minutes=15"
    )
    assert r_ready.status_code == 200
    ready_resp = ReadyBatteriesTimeseriesResponse.model_validate(r_ready.json())
    assert len(ready_resp.series) == 2
    assert len(ready_resp.timestamps) > 0

    # 3. Alarms API
    r_alarms = client.get("/api/dashboard/alarms?profile=smoke")
    assert r_alarms.status_code == 200
    alarms_resp = AlarmsResponse.model_validate(r_alarms.json())
    assert alarms_resp.total >= 0


def test_alarms_severity_mapping_and_feedback_status() -> None:
    as_of = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)

    # Synthetic profile data with stations and cabinets
    st_df = pl.DataFrame(
        {
            "station_id": ["ST-01"],
            "country": ["VN"],
            "timezone": ["Asia/Ho_Chi_Minh"],
            "timezone_offset": ["+07:00"],
            "model_gen": ["G1"],
            "lat": [10.8],
            "lon": [106.6],
        }
    )
    cab_df = pl.DataFrame(
        {
            "cabinet_id": ["CAB-01"],
            "station_id": ["ST-01"],
            "model_gen": ["G1"],
        }
    )
    # Cabinet online (heartbeat 30s ago)
    tel_df = pl.DataFrame(
        {
            "cabinet_id": ["CAB-01"],
            "slot_id": [None],
            "door_state": ["CLOSED"],
            "battery_detected": [True],
            "soc": [95.0],
            "temperature": [32.0],
            "heartbeat_at": [as_of - timedelta(seconds=30)],
            "hw_error_code": [None],
        }
    )
    slots_df = pl.DataFrame({"cabinet_id": ["CAB-01"], "slot_id": ["01"]})
    empty_df = pl.DataFrame()

    data = ProfileData(
        profile="synthetic",
        profile_dir=Path("/tmp"),
        manifest_mtime=0.0,
        stations=st_df,
        cabinets=cab_df,
        slots=slots_df,
        batteries=empty_df,
        users=empty_df,
        swap_sessions=empty_df,
        payments=empty_df,
        cabinet_telemetry=tel_df,
        swap_events=empty_df,
        min_time=as_of - timedelta(days=1),
        max_time=as_of,
        default_as_of=as_of,
    )

    # Cards with P1, P2, P3
    card_p1 = {
        "channel": "it-ops",
        "incident_id": "INC-P1",
        "revision": 1,
        "card_json": orjson.dumps(
            {"incident_id": "INC-P1", "severity": "P1", "station_id": "ST-01"}
        ).decode("utf-8"),
        "received_at": (as_of - timedelta(minutes=10)).isoformat(),
    }
    card_p2 = {
        "channel": "it-ops",
        "incident_id": "INC-P2",
        "revision": 1,
        "card_json": orjson.dumps(
            {"incident_id": "INC-P2", "severity": "P2", "station_id": "ST-01"}
        ).decode("utf-8"),
        "received_at": (as_of - timedelta(minutes=8)).isoformat(),
    }
    card_p3 = {
        "channel": "it-ops",
        "incident_id": "INC-P3",
        "revision": 1,
        "card_json": orjson.dumps(
            {"incident_id": "INC-P3", "severity": "P3", "station_id": "ST-01"}
        ).decode("utf-8"),
        "received_at": (as_of - timedelta(minutes=5)).isoformat(),
    }

    # Latest feedback: INC-P1 has confirm, INC-P2 has resolved, INC-P3 has no feedback
    snapshot = NotifierSnapshot(
        outbox=[],
        cards=[card_p1, card_p2, card_p3],
        latest_feedback={
            "INC-P1": ("confirm", (as_of - timedelta(minutes=6)).isoformat()),
            "INC-P2": ("resolved", (as_of - timedelta(minutes=4)).isoformat()),
        },
    )

    alarms_resp = build_alarms_page(
        data,
        as_of=as_of,
        settings=Settings(data_dir=Path("/tmp")),
        knowledge=K,
        notifier=snapshot,
    )

    alarms_by_id = {a.alarm_id: a for a in alarms_resp.alarms}
    assert alarms_by_id["INC-P1"].severity == "Critical"
    assert alarms_by_id["INC-P1"].status_line2 == "Đã xác nhận"

    assert alarms_by_id["INC-P2"].severity == "Major"
    assert alarms_by_id["INC-P2"].status_line2 == "Đã xử lý"

    assert alarms_by_id["INC-P3"].severity == "Minor"
    assert alarms_by_id["INC-P3"].status_line2 == "Chưa xác nhận"


def test_alarms_device_alarm_on_lost_heartbeat() -> None:
    as_of = datetime(2026, 11, 4, 12, 0, 0, tzinfo=UTC)

    st_df = pl.DataFrame(
        {
            "station_id": ["ST-01"],
            "country": ["VN"],
            "timezone": ["Asia/Ho_Chi_Minh"],
            "timezone_offset": ["+07:00"],
            "model_gen": ["G1"],
            "lat": [10.8],
            "lon": [106.6],
        }
    )
    cab_df = pl.DataFrame(
        {
            "cabinet_id": ["CAB-LOST-HB"],
            "station_id": ["ST-01"],
            "model_gen": ["G1"],
        }
    )
    # Heartbeat 5 minutes ago (> 2 min threshold)
    tel_df = pl.DataFrame(
        {
            "cabinet_id": ["CAB-LOST-HB"],
            "slot_id": [None],
            "door_state": ["CLOSED"],
            "battery_detected": [True],
            "soc": [90.0],
            "temperature": [30.0],
            "heartbeat_at": [as_of - timedelta(minutes=5)],
            "hw_error_code": [None],
        }
    )
    slots_df = pl.DataFrame({"cabinet_id": ["CAB-LOST-HB"], "slot_id": ["01"]})
    empty_df = pl.DataFrame()

    data = ProfileData(
        profile="synthetic",
        profile_dir=Path("/tmp"),
        manifest_mtime=0.0,
        stations=st_df,
        cabinets=cab_df,
        slots=slots_df,
        batteries=empty_df,
        users=empty_df,
        swap_sessions=empty_df,
        payments=empty_df,
        cabinet_telemetry=tel_df,
        swap_events=empty_df,
        min_time=as_of - timedelta(days=1),
        max_time=as_of,
        default_as_of=as_of,
    )

    snapshot = NotifierSnapshot(outbox=[], cards=[], latest_feedback={})
    alarms_resp = build_alarms_page(
        data,
        as_of=as_of,
        settings=Settings(data_dir=Path("/tmp")),
        knowledge=K,
        notifier=snapshot,
    )

    dev_alarms = [a for a in alarms_resp.alarms if a.originator == "CAB-LOST-HB"]
    assert len(dev_alarms) == 1
    assert dev_alarms[0].severity == "Critical"
    assert "Mất kết nối heartbeat" in dev_alarms[0].alarm_type
    assert dev_alarms[0].status_line2 == "Chưa xác nhận"


def test_html_widget_containers_and_no_inner_html_in_dashboard_js(
    smoke_env: tuple[Settings, Path],
) -> None:
    settings, db_path = smoke_env
    app = create_app(db_path=db_path, settings=settings)
    client = TestClient(app)

    # 1. HTML overview has expected widget card containers
    resp = client.get("/dashboard?profile=smoke")
    assert resp.status_code == 200
    html = resp.text
    expected_widget_ids = [
        "widget-station-map",
        "widget-station-table",
        "widget-cabinet-temp",
        "widget-ready-batteries",
        "widget-alarms-table",
    ]
    for w_id in expected_widget_ids:
        assert f'id="{w_id}"' in html, f"Missing widget card {w_id} in overview HTML"

    # 2. dashboard.js does not use innerHTML for data rendering
    js_path = Path(__file__).resolve().parents[2] / "ase" / "dashboard" / "static" / "dashboard.js"
    js_content = js_path.read_text(encoding="utf-8")
    assert not re.search(r"\.innerHTML\s*=", js_content), (
        "dashboard.js must not assign to innerHTML (security rule §2.6)"
    )


# --- 7. Hồi quy: page-data an toàn, bản đồ Vietmap (khôi phục) ----------------------------


def test_page_data_json_is_parseable_and_cannot_break_out_of_script(
    smoke_env: tuple[Settings, Path],
) -> None:
    """Hồi quy: page-data bị escape thành `&#34;` làm JSON.parse lỗi -> widget JS trống."""
    import json
    import re

    settings, db_path = smoke_env
    client = TestClient(create_app(db_path=db_path, settings=settings))
    pages = ["", "/stations", "/stations/VN-ST-001", "/sessions", "/payments", "/batteries"]
    pages += ["/alerts", "/incidents", "/metrics", "/ground-truth"]
    for page in pages:
        html = client.get(f"/dashboard{page}?profile=smoke").text
        m = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', html, re.S)
        assert m is not None, page
        raw = m.group(1)
        assert "&#34;" not in raw and "&quot;" not in raw, page  # không bị escape kiểu HTML
        assert "<" not in raw, page  # mọi '<' đã thành \\u003c, không thể đóng thẻ script
        assert isinstance(json.loads(raw), dict), page

    # Trang tổng quan phải mang đủ dữ liệu cho các widget JS
    html = client.get("/dashboard?profile=smoke").text
    m = re.search(r'id="page-data">(.*?)</script>', html, re.S)
    assert m is not None
    data = json.loads(m.group(1))
    for key in ("stations_map", "temperature_timeseries", "ready_batteries_timeseries", "alarms"):
        assert key in data, key


def test_map_uses_vietmap_not_osm_and_key_only_from_settings(
    smoke_env: tuple[Settings, Path],
) -> None:
    from pydantic import SecretStr

    settings, db_path = smoke_env
    # Không phụ thuộc .env thật của máy dev: đặt key rỗng tường minh
    settings = settings.model_copy(update={"vietmap_api_key": None})
    html = (
        TestClient(create_app(db_path=db_path, settings=settings))
        .get("/dashboard?profile=smoke")
        .text
    )
    assert "openstreetmap" not in html.lower()
    assert "@vietmap/vietmap-gl-js@6.0.1/dist/vietmap-gl.js" in html  # version ghim
    assert "@vietmap/vietmap-gl-leaflet@0.1.0/leaflet-vietmap-gl.js" in html
    assert 'data-vietmap-key=""' in html  # chưa cấu hình key -> rỗng
    js = (Path(__file__).parents[2] / "ase/dashboard/static/dashboard.js").read_text()
    assert "openstreetmap" not in js.lower()

    keyed = settings.model_copy(update={"vietmap_api_key": SecretStr('k"<x>')})
    html2 = (
        TestClient(create_app(db_path=db_path, settings=keyed)).get("/dashboard?profile=smoke").text
    )
    assert 'data-vietmap-key="k&#34;&lt;x&gt;"' in html2  # key được escape trong thuộc tính


def test_vinfast_bss_api_returns_43298_cabinets(smoke_env: tuple[Settings, Path]) -> None:
    gz_path = Path(__file__).resolve().parents[2] / "ase/dashboard/static/vinfast_bss.json.gz"
    if not gz_path.exists():
        pytest.skip(
            "chưa có ase/dashboard/static/vinfast_bss.json.gz (dữ liệu ngoài, không commit)"
        )
    settings, db_path = smoke_env
    client = TestClient(create_app(db_path=db_path, settings=settings))
    resp = client.get("/api/dashboard/vinfast-bss")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)
    assert len(rows) == 43297  # 43.298 trừ tủ sai toạ độ B.TNG10178
    first = rows[0]
    assert len(first) == 5
    lat, lon, code, name, address = first
    assert isinstance(lat, float) and 8.0 <= lat <= 24.0
    assert isinstance(lon, float) and 102.0 <= lon <= 110.0
    assert isinstance(code, str) and code.startswith("B.")
    assert isinstance(name, str) and len(name) > 0
    assert isinstance(address, str)
