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
from ase.dashboard.data import (
    ProfileData,
    clear_cache,
    load_profile_data,
)
from ase.dashboard.metrics import (
    compute_cabinet_online_map,
    compute_ready_batteries_by_cabinet,
    estimate_soc,
    filter_sessions_today,
)
from ase.dashboard.models import (
    CabinetTemperatureTimeseriesResponse,
    OverviewResponse,
    StationDetailResponse,
    StationsListResponse,
    TransactionsPageResponse,
)
from ase.dashboard.transactions import (
    COLUMNS,
    TxnFilters,
    TxnStatus,
    cabinet_serial,
    query_transactions,
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
    client = TestClient(create_app(db_path=db_path, settings=settings))

    assert client.get("/static/dashboard/dashboard.css").status_code == 200
    assert client.get("/static/dashboard/dashboard.js").status_code == 200

    ov = OverviewResponse.model_validate(client.get("/api/dashboard/overview?profile=smoke").json())
    assert ov.meta.profile == "smoke"
    assert len(ov.stations_map) == 2

    st_list = StationsListResponse.model_validate(
        client.get("/api/dashboard/stations?profile=smoke").json()
    )
    assert len(st_list.stations) == 2
    first_station_id = st_list.stations[0].station_id
    st_det = StationDetailResponse.model_validate(
        client.get(f"/api/dashboard/stations/{first_station_id}?profile=smoke").json()
    )
    assert st_det.station.station_id == first_station_id

    temp = CabinetTemperatureTimeseriesResponse.model_validate(
        client.get("/api/dashboard/timeseries/cabinet-temperature?profile=smoke&hours=1").json()
    )
    assert temp.warn_threshold_c == K.error_catalog.thresholds.temp_warn_1_c

    txn = TransactionsPageResponse.model_validate(
        client.get("/api/dashboard/transactions?profile=smoke").json()
    )
    assert txn.total > 0 and len(txn.rows) == 20

    for path in (
        "/dashboard?profile=smoke",
        "/dashboard/map?profile=smoke",
        "/dashboard/stations?profile=smoke",
        f"/dashboard/stations/{first_station_id}?profile=smoke",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, f"Failed GET {path}: {resp.text}"
        assert "<!DOCTYPE html>" in resp.text


def test_removed_statistics_pages_are_gone(smoke_env: tuple[Settings, Path]) -> None:
    """Chủ dự án bỏ các trang thống kê: chỉ giữ Giao dịch, Bản đồ trạm, Trạm & Tủ."""
    settings, db_path = smoke_env
    client = TestClient(create_app(db_path=db_path, settings=settings))
    for page in (
        "sessions",
        "payments",
        "batteries",
        "alerts",
        "incidents",
        "metrics",
        "ground-truth",
    ):
        assert client.get(f"/dashboard/{page}?profile=smoke").status_code == 404, page
        assert client.get(f"/api/dashboard/{page}?profile=smoke").status_code == 404, page
    for api in ("timeseries/ready-batteries", "alarms"):
        assert client.get(f"/api/dashboard/{api}?profile=smoke").status_code == 404, api
    nav = client.get("/dashboard?profile=smoke").text
    assert 'href="/dashboard/map?' in nav and 'href="/dashboard/stations?' in nav
    assert "Metric SF1" not in nav and "Kiểm chứng (mock)" not in nav


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


def test_profile_must_be_a_known_name_not_a_path(smoke_env: tuple[Settings, Path]) -> None:
    settings, db_path = smoke_env
    client = TestClient(create_app(db_path=db_path, settings=settings))
    assert client.get("/api/dashboard/overview?profile=smoke").status_code == 200
    for bad in ("../smoke", "..%2Fsmoke", "/etc", "smoke/../smoke", "nonexistent"):
        r = client.get(f"/api/dashboard/overview?profile={bad}")
        assert r.status_code == 422, bad
        assert str(settings.data_dir) not in r.text, bad  # không lộ đường dẫn tuyệt đối


def test_empty_as_of_param_means_end_of_data(smoke_env: tuple[Settings, Path]) -> None:
    """Nút "Cuối dữ liệu" có thể gửi `as_of=` rỗng: phải coi như không có, không trả 422."""
    settings, db_path = smoke_env
    client = TestClient(create_app(db_path=db_path, settings=settings))
    assert client.get("/dashboard?profile=smoke&country=VN&as_of=").status_code == 200
    assert client.get("/api/dashboard/overview?profile=smoke&as_of=").status_code == 200


# --- 3. Ground-truth isolation test ----------------------------------------------------------


def test_dashboard_never_reads_ground_truth_csv(
    smoke_env: tuple[Settings, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, db_path = smoke_env
    client = TestClient(create_app(db_path=db_path, settings=settings))
    clear_cache()

    def _forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("Dashboard MUST NOT read CSV (ground_truth.csv)!")

    monkeypatch.setattr("polars.read_csv", _forbidden)
    for url in (
        "/api/dashboard/overview?profile=smoke",
        "/api/dashboard/stations?profile=smoke",
        "/api/dashboard/stations/VN-ST-001?profile=smoke",
        "/api/dashboard/transactions?profile=smoke",
        "/dashboard?profile=smoke",
        "/dashboard/transactions/export?profile=smoke",
        "/dashboard/map?profile=smoke",
        "/dashboard/stations?profile=smoke",
    ):
        assert client.get(url).status_code == 200, url


# --- 4. Untrusted data: error_message không bao giờ hiện trên danh sách giao dịch -------------


def test_transactions_never_render_untrusted_error_message(
    smoke_env: tuple[Settings, Path],
) -> None:
    settings, db_path = smoke_env
    events_path = settings.data_dir / "smoke" / "ase.swap.events.jsonl"
    injected: str | None = None
    for line in events_path.read_bytes().splitlines():
        obj = orjson.loads(line) if line else {}
        if (
            isinstance(obj.get("error_message"), str)
            and "</untrusted_data>" in obj["error_message"]
        ):
            injected = str(obj["session_id"])
            break
    assert injected is not None, "Expected prompt_injection session in smoke profile"
    client = TestClient(create_app(db_path=db_path, settings=settings))
    body = client.get(f"/dashboard?profile=smoke&emsp_txn_id={injected}").text
    assert injected in body
    assert "untrusted_data" not in body


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


# --- 7. Hồi quy: page-data an toàn, bản đồ Vietmap ----------------------------------------


def test_page_data_json_is_parseable_and_cannot_break_out_of_script(
    smoke_env: tuple[Settings, Path],
) -> None:
    """Hồi quy: page-data bị escape thành `&#34;` làm JSON.parse lỗi -> widget JS trống."""
    import json

    settings, db_path = smoke_env
    client = TestClient(create_app(db_path=db_path, settings=settings))
    for page in ("", "/map", "/stations", "/stations/VN-ST-001"):
        html = client.get(f"/dashboard{page}?profile=smoke").text
        m = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', html, re.S)
        assert m is not None, page
        raw = m.group(1)
        assert "&#34;" not in raw and "&quot;" not in raw, page
        assert "<" not in raw, page
        assert isinstance(json.loads(raw), dict), page
    m = re.search(
        r'id="page-data">(.*?)</script>', client.get("/dashboard/map?profile=smoke").text, re.S
    )
    assert m is not None and "stations_map" in json.loads(m.group(1))

    js = (Path(__file__).resolve().parents[2] / "ase/dashboard/static/dashboard.js").read_text()
    assert not re.search(r"\.innerHTML\s*=", js), "dashboard.js must not assign innerHTML (§2.6)"


def test_map_uses_vietmap_not_osm_and_key_only_from_settings(
    smoke_env: tuple[Settings, Path],
) -> None:
    from pydantic import SecretStr

    settings, db_path = smoke_env
    # Không phụ thuộc .env thật của máy dev: đặt key rỗng tường minh
    settings = settings.model_copy(update={"vietmap_api_key": None})
    html = (
        TestClient(create_app(db_path=db_path, settings=settings))
        .get("/dashboard/map?profile=smoke")
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
        TestClient(create_app(db_path=db_path, settings=keyed))
        .get("/dashboard/map?profile=smoke")
        .text
    )
    assert 'data-vietmap-key="k&#34;&lt;x&gt;"' in html2  # key được escape trong thuộc tính


def test_vinfast_bss_api_returns_43297_cabinets(smoke_env: tuple[Settings, Path]) -> None:
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
    # 3 nguồn: dòng 5 cột = tủ đổi pin VN thật (file gốc 43.298, đã loại tủ sai toạ độ
    # B.TNG10178); dòng 6 cột có mã nước = ID (trạm sạc, tham khảo) / PH (mô phỏng).
    vn = [r for r in rows if len(r) == 5]
    others = [r for r in rows if len(r) != 5]
    assert len(vn) == 43297
    assert all(len(r) == 6 and r[5] in {"ID", "PH"} for r in others)
    for lat, lon, code, name, address in vn[:1]:
        assert isinstance(lat, float) and 8.0 <= lat <= 24.0
        assert isinstance(lon, float) and 102.0 <= lon <= 110.0
        assert isinstance(code, str) and code.startswith("B.")
        assert isinstance(name, str) and len(name) > 0
        assert isinstance(address, str)


# --- 8. Danh sách giao dịch đổi pin ----------------------------------------------------------


def _smoke_data(settings: Settings) -> ProfileData:
    return load_profile_data("smoke", settings=settings)


def test_transactions_default_lists_all_started_newest_first(
    smoke_env: tuple[Settings, Path],
) -> None:
    settings, _ = smoke_env
    data = _smoke_data(settings)
    df = query_transactions(data, data.default_as_of, TxnFilters())
    started = data.swap_sessions.filter(pl.col("started_at") <= data.default_as_of)
    assert df.height == started.height  # mặc định: tất cả giao dịch, không lọc
    assert df["started_at"].is_sorted(descending=True)
    assert set(df["status"].unique()) <= {s.value for s in TxnStatus}
    # Lý do lỗi chỉ có ở giao dịch thất bại
    assert (
        df.filter((pl.col("status") != "FAILED") & pl.col("error_code").is_not_null()).height == 0
    )
    assert (
        df.filter(pl.col("status") == "FAILED")["error_code"].null_count()
        < df.filter(pl.col("status") == "FAILED").height
    )


def test_transaction_status_follows_as_of(smoke_env: tuple[Settings, Path]) -> None:
    """Phiên dở dang tại as_of: chưa có pin vào = Tạo mới; pin cũ đã vào = Đưa pin vào."""
    settings, _ = smoke_env
    data = _smoke_data(settings)
    full = query_transactions(
        data, data.max_time, TxnFilters(statuses=frozenset({TxnStatus.COMPLETED}))
    )
    row = full.filter(pl.col("battery_in_at").is_not_null()).row(0, named=True)
    sid = TxnFilters(emsp_txn_id=row["session_id"])

    def at(t: datetime) -> dict[str, object]:
        return query_transactions(data, t, sid).row(0, named=True)

    created = at(row["battery_in_at"] - timedelta(milliseconds=1))
    assert created["status"] == "CREATED"
    assert created["battery_in_local"] is None and created["battery_out_id"] is None
    mid = at(row["battery_in_at"])
    assert mid["status"] == "BATTERY_IN"
    assert mid["battery_in_local"] is not None and mid["battery_out_id"] is None
    assert at(row["ended_at"])["status"] == "COMPLETED"
    assert query_transactions(data, row["started_at"] - timedelta(seconds=1), sid).height == 0


def test_transaction_date_filter_uses_station_local_time(
    smoke_env: tuple[Settings, Path],
) -> None:
    settings, _ = smoke_env
    data = _smoke_data(settings)
    row = query_transactions(data, data.default_as_of, TxnFilters()).row(0, named=True)
    local = datetime.strptime(row["started_local"], "%d/%m/%Y %H:%M:%S")  # noqa: DTZ007
    minute = local.replace(second=0)
    hit = TxnFilters(
        emsp_txn_id=row["session_id"], date_from=minute, date_to=minute + timedelta(seconds=59)
    )
    assert query_transactions(data, data.default_as_of, hit).height == 1
    # Cùng khung giờ nhưng hiểu theo UTC (lệch 7 giờ ở VN) thì không khớp
    utc_minute = minute - timedelta(hours=7)
    miss = TxnFilters(
        emsp_txn_id=row["session_id"],
        date_from=utc_minute,
        date_to=utc_minute + timedelta(seconds=59),
    )
    assert query_transactions(data, data.default_as_of, miss).height == 0


def test_transactions_local_time_per_station_timezone() -> None:
    from ase.dashboard.transactions import _to_local_naive

    t = datetime(2026, 11, 9, 10, 29, 50, tzinfo=UTC)
    df = pl.DataFrame(
        {
            "timezone": ["Asia/Ho_Chi_Minh", "Asia/Manila", "Asia/Makassar", "Asia/Jakarta"],
            "ts": [t] * 4,
        }
    )
    out = _to_local_naive(df, "ts", "local")["local"].dt.strftime("%H:%M:%S").to_list()
    assert out == ["17:29:50", "18:29:50", "18:29:50", "17:29:50"]


def test_transactions_api_filters_and_validation(smoke_env: tuple[Settings, Path]) -> None:
    settings, db_path = smoke_env
    client = TestClient(create_app(db_path=db_path, settings=settings))

    def get(qs: str) -> TransactionsPageResponse:
        r = client.get(f"/api/dashboard/transactions?profile=smoke&{qs}")
        assert r.status_code == 200, r.text
        return TransactionsPageResponse.model_validate(r.json())

    all_rows = get("page_size=100")
    failed = get("status=FAILED&page_size=100")
    both = get("status=FAILED&status=COMPLETED")
    assert 0 < failed.total < all_rows.total
    assert all(r.status_label == "Thất bại" for r in failed.rows)
    in_progress = get("status=CREATED&status=BATTERY_IN")
    assert both.total + in_progress.total == all_rows.total
    assert failed.filters.statuses == ["FAILED"] and failed.filters.active_count == 1

    st = get("station_id=vn-st-002")  # khớp chứa, không phân biệt hoa thường
    assert st.total > 0 and all(r.station_id == "VN-ST-002" for r in st.rows)
    assert get("q=VN-ST-002").total == st.total

    sample = next(r for r in all_rows.rows if r.battery_out_id and r.cabinet_txn_id)
    assert sample.battery_out_id is not None and sample.cabinet_txn_id is not None
    by_bat = get(f"battery_serial={sample.battery_out_id}")
    assert any(r.emsp_txn_id == sample.emsp_txn_id for r in by_bat.rows)
    assert all(
        sample.battery_out_id in ((r.battery_in_id or "") + (r.battery_out_id or ""))
        for r in by_bat.rows
    )
    assert sample.cabinet_serial == cabinet_serial(sample.cabinet_id or "")
    assert get(f"cabinet_serial={sample.cabinet_serial}").total > 0

    # Phân trang: STT nối tiếp, trang vượt quá bị kẹp về trang cuối
    p2 = get("page=2")
    assert p2.page == 2 and p2.rows[0].stt == 21
    last = get("page=100000")
    assert last.page == last.total_pages

    for bad in ("status=BOGUS", "date_from=2026-13-01T00:00", "page_size=7", "page=0"):
        assert client.get(f"/api/dashboard/transactions?profile=smoke&{bad}").status_code == 422, (
            bad
        )
    assert client.get("/dashboard?profile=smoke&status=BOGUS").status_code == 422


def test_transactions_html_matches_real_screen_and_export_csv(
    smoke_env: tuple[Settings, Path],
) -> None:
    settings, db_path = smoke_env
    client = TestClient(create_app(db_path=db_path, settings=settings))
    html = client.get("/dashboard?profile=smoke&status=FAILED").text
    assert "Danh sách giao dịch đổi pin" in html
    for _, title in COLUMNS:
        assert f"<th>{title}</th>" in html, title
    for label in (
        "Trạm - Tủ",
        "Ngày thực hiện từ",
        "Ngày thực hiện đến",
        "Tạo mới",
        "Đưa pin vào",
        "Hoàn thành",
        "Thất bại",
        "Đặt lại",
        "Xác nhận",
        "Xuất dữ liệu",
        "Serial Pin",
    ):
        assert label in html, label
    assert 'name="status" value="FAILED" checked' in html
    assert "/dashboard/transactions/export?" in html and "status=FAILED" in html

    r = client.get("/dashboard/transactions/export?profile=smoke&status=FAILED")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("﻿STT,Mã trạm,Mã tủ,S/N tủ pin")
    lines = text.strip().splitlines()
    total = TransactionsPageResponse.model_validate(
        client.get("/api/dashboard/transactions?profile=smoke&status=FAILED").json()
    ).total
    assert len(lines) == total + 1  # xuất toàn bộ theo bộ lọc, không phân trang
    assert all(",Thất bại," in ln for ln in lines[1:])
