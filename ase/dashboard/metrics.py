"""Hàm thuần tính chỉ số vận hành cho Dashboard (DASHBOARD_PLAN §3, §5, §6).

Định nghĩa chỉ số tại thời điểm `as_of` (DASHBOARD_PLAN §6):
- **Tủ online**: Có heartbeat trong 2 phút (`cabinet_online_window_seconds`) trước `as_of`.
  Ngưỡng đặt trong `Settings`, không hardcode.
- **Trạm online**: Ít nhất 1 tủ thuộc trạm đang online tại `as_of`.
- **Pin sẵn sàng**: Khoang có lần đọc telemetry gần nhất (`<= as_of`) với
  `battery_detected=true` và `soc >= 90%` (`ready_battery_soc_pct`), hoặc chưa có lần đọc
  nào (giả định đầy pin ban đầu). Đây là **xấp xỉ**, vì simulator chưa mô phỏng tồn kho
  pin theo từng khoang.
- **Lượt đổi hôm nay**: Số phiên có `started_at` (`<= as_of`) nằm trong ngày địa phương
  của trạm chứa `as_of`.
- **Tỷ lệ thành công**: Số phiên `final_status == "COMPLETED"` / số phiên đã kết thúc
  (`ended_at <= as_of`).
- **Thời gian đổi**: `ended_at - started_at` (giây) của các phiên `COMPLETED`.
- **Doanh thu**: Tổng `amount` của payment có `captured == true`, tách riêng theo `currency`,
  không quy đổi ngoại tệ.
- **Phiên đang treo**: Phiên chưa đóng tại `as_of` (chưa có event bước 7 `OK` và chưa có event
  `FAILED`/`TIMEOUT`) mà thời gian chờ tính từ event **cuối** của bước trước đã vượt quá
  SLA của bước hiện tại (đọc từ `state_machine.yaml`).
"""

from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import polars as pl

from ase.common.config import Settings, get_settings
from ase.common.time import ensure_utc, format_local, format_offset_for, to_local
from ase.dashboard.data import NotifierSnapshot, ProfileData, extract_session_from_card_json
from ase.dashboard.models import (
    CabinetDetail,
    CabinetTemperatureTimeseriesResponse,
    CabinetTempPoint,
    CabinetTempSeries,
    DashboardMeta,
    HeartbeatBucket,
    HeartbeatGap,
    OverviewResponse,
    SlotCell,
    StationChoice,
    StationDetailResponse,
    StationMapItem,
    StationsListResponse,
    StationSummaryRow,
)
from ase.knowledge.loader import Knowledge, default_error_table, default_knowledge

NO_ERROR_CODE = "không có mã lỗi"
READY_BATTERY_APPROX_NOTE = (
    "Xấp xỉ: SOC hiện tại được ước lượng = SOC của lần đọc telemetry cuối + tốc độ sạc giả định "
    "(ASE_BATTERY_CHARGE_RATE_PCT_PER_HOUR) x thời gian đã qua; khoang sẵn sàng khi ước lượng "
    "≥ 90%. Khoang không có pin (đã xuất, chưa nạp lại) tính là trống: simulator chưa mô phỏng "
    "tồn kho pin theo khoang nên số khoang trống bị phóng đại."
)


def estimate_soc(
    soc: float | None,
    read_at: datetime,
    as_of: datetime,
    charge_rate_pct_per_hour: float,
) -> float | None:
    """SOC ước lượng tại `as_of`: SOC lần đọc cuối cộng phần đã sạc thêm, kẹp tối đa 100.

    Không có SOC thì trả `None` (coi như đầy, theo quy ước cũ). `as_of` trước `read_at` không
    làm SOC giảm.
    """
    if soc is None:
        return None
    hours = max(0.0, (ensure_utc(as_of) - ensure_utc(read_at)).total_seconds() / 3600.0)
    return min(100.0, float(soc) + charge_rate_pct_per_hour * hours)


def online_window(data: ProfileData, settings: Settings) -> int:
    """Tủ online nếu có heartbeat trong cửa sổ này: ít nhất 2,5 chu kỳ heartbeat của dữ liệu
    (danh sách giao dịch gửi heartbeat 10–15 phút/lần, pipeline cũ 1 phút/lần)."""
    return max(settings.cabinet_online_window_seconds, int(2.5 * data.heartbeat_seconds))


def clamp_as_of(
    as_of: datetime | None,
    min_time: datetime,
    max_time: datetime,
    default_time: datetime | None = None,
) -> tuple[datetime, bool]:
    """Chuẩn hoá `as_of` về UTC và kẹp vào khoảng `[min_time, max_time]`."""
    if as_of is None:
        return (default_time or max_time), False
    dt = as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else ensure_utc(as_of)
    if dt < min_time:
        return min_time, True
    if dt > max_time:
        return max_time, True
    return dt, False


def compute_cabinet_online_map(
    cabinets: pl.DataFrame,
    telemetry: pl.DataFrame,
    as_of: datetime,
    online_window_seconds: int = 120,
) -> dict[str, tuple[bool, datetime | None]]:
    """Tính trạng thái online và thời điểm heartbeat cuối (`<= as_of`) của từng tủ."""
    as_of_utc = ensure_utc(as_of)
    cab_ids = [str(c) for c in cabinets["cabinet_id"].to_list()]
    if telemetry.height == 0:
        return {cid: (False, None) for cid in cab_ids}

    upto = telemetry.filter(pl.col("heartbeat_at") <= as_of_utc)
    if upto.height == 0:
        return {cid: (False, None) for cid in cab_ids}

    # Ưu tiên dòng heartbeat định kỳ (slot_id is null); fallback sang mọi dòng của tủ.
    hb_rows = upto.filter(pl.col("slot_id").is_null()) if "slot_id" in upto.columns else upto
    source = hb_rows if hb_rows.height > 0 else upto

    grouped = source.group_by("cabinet_id").agg(pl.col("heartbeat_at").max().alias("last_hb"))
    last_by_cab: dict[str, datetime] = {}
    for row in grouped.iter_rows(named=True):
        dt = row["last_hb"]
        if isinstance(dt, datetime):
            last_by_cab[str(row["cabinet_id"])] = ensure_utc(dt)

    result: dict[str, tuple[bool, datetime | None]] = {}
    for cid in cab_ids:
        last_hb = last_by_cab.get(cid)
        if last_hb is None:
            result[cid] = (False, None)
        else:
            diff_s = (as_of_utc - last_hb).total_seconds()
            is_online = 0.0 <= diff_s <= float(online_window_seconds)
            result[cid] = (is_online, last_hb)
    return result


def compute_station_today_bounds(
    stations: pl.DataFrame,
    as_of: datetime,
) -> dict[str, tuple[datetime, datetime]]:
    """Với mỗi `station_id`, trả về `(local_day_start_utc, local_day_end_utc)` chứa `as_of`."""
    as_of_utc = ensure_utc(as_of)
    bounds: dict[str, tuple[datetime, datetime]] = {}
    for row in stations.iter_rows(named=True):
        sid = str(row["station_id"])
        tz_name = str(row["timezone"])
        tz = ZoneInfo(tz_name)
        local_dt = to_local(as_of_utc, tz_name)
        start_local = datetime.combine(local_dt.date(), time(0, 0), tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        bounds[sid] = (start_local.astimezone(UTC), min(as_of_utc, end_local.astimezone(UTC)))
    return bounds


def filter_sessions_today(
    sessions: pl.DataFrame,
    stations: pl.DataFrame,
    as_of: datetime,
) -> pl.DataFrame:
    """Lọc các phiên có `started_at <= as_of` trong ngày địa phương của trạm chứa `as_of`."""
    if sessions.height == 0 or stations.height == 0:
        return sessions.clear()

    bounds = compute_station_today_bounds(stations, as_of)
    bounds_df = pl.DataFrame(
        [{"station_id": sid, "_day_start": b[0], "_day_end": b[1]} for sid, b in bounds.items()],
        schema={
            "station_id": pl.String(),
            "_day_start": pl.Datetime("us", "UTC"),
            "_day_end": pl.Datetime("us", "UTC"),
        },
    )
    return (
        sessions.join(bounds_df, on="station_id", how="inner")
        .filter(
            (pl.col("started_at") >= pl.col("_day_start"))
            & (pl.col("started_at") <= pl.col("_day_end"))
        )
        .drop(["_day_start", "_day_end"])
    )


def compute_ready_batteries_by_cabinet(
    slots: pl.DataFrame,
    telemetry: pl.DataFrame,
    as_of: datetime,
    ready_soc_pct: float = 90.0,
    charge_rate_pct_per_hour: float = 0.0,
) -> dict[str, tuple[int, int]]:
    """Trả về `cabinet_id -> (ready_batteries, total_slots)` tại `as_of` (xấp xỉ theo §6)."""
    as_of_utc = ensure_utc(as_of)
    totals: Counter[str] = Counter()
    for row in slots.select(["cabinet_id", "slot_id"]).iter_rows(named=True):
        totals[str(row["cabinet_id"])] += 1

    slot_tel = telemetry.filter(
        pl.col("slot_id").is_not_null() & (pl.col("heartbeat_at") <= as_of_utc)
    )
    if slot_tel.height == 0:
        return {cid: (cnt, cnt) for cid, cnt in totals.items()}

    latest_slot = slot_tel.sort("heartbeat_at").group_by(["cabinet_id", "slot_id"]).last()
    not_ready_by_cab: Counter[str] = Counter()
    for row in latest_slot.iter_rows(named=True):
        cid = str(row["cabinet_id"])
        detected = row["battery_detected"]
        soc = estimate_soc(row["soc"], row["heartbeat_at"], as_of_utc, charge_rate_pct_per_hour)
        is_ready = bool(detected is True and (soc is None or soc >= ready_soc_pct))
        if not is_ready:
            not_ready_by_cab[cid] += 1

    return {
        cid: (max(0, total - not_ready_by_cab.get(cid, 0)), total) for cid, total in totals.items()
    }


# ---------------------------------------------------------------------------
# Hàm tổng hợp dữ liệu cho từng Tab / API Endpoint
# ---------------------------------------------------------------------------


def _build_meta(
    data: ProfileData,
    as_of: datetime | None,
    country: str | None,
    profiles: list[str],
    knowledge: Knowledge,
) -> DashboardMeta:
    eff_as_of, clamped = clamp_as_of(as_of, data.min_time, data.max_time, data.default_as_of)
    return DashboardMeta(
        profile=data.profile,
        available_profiles=profiles,
        country=country,
        available_countries=list(knowledge.countries.countries.keys()),
        as_of=eff_as_of,
        as_of_clamped=clamped,
        min_time=data.min_time,
        max_time=data.max_time,
    )


def _open_alerts_from_snapshot(
    notifier: NotifierSnapshot,
) -> tuple[Counter[tuple[str, str]], set[str]]:
    """Đếm số alert đang mở (chưa `resolved`) theo `(country_or_ALL, severity)` và trạm có P1."""
    counts: Counter[tuple[str, str]] = Counter()
    p1_stations: set[str] = set()
    for row in notifier.cards:
        inc_id = str(row.get("incident_id", ""))
        fb = notifier.latest_feedback.get(inc_id)
        if fb is not None and fb[0] == "resolved":
            continue
        raw_json = str(row.get("card_json", "{}"))
        _, _, sev = extract_session_from_card_json(raw_json)
        sev_str = sev or "P2"
        counts[("ALL", sev_str)] += 1
        # Tìm quốc gia và trạm trong FactSet nếu có
        for c_code in ("VN", "PH", "ID"):
            if f'"{c_code}"' in raw_json or f": {c_code}" in raw_json or c_code in raw_json:
                counts[(c_code, sev_str)] += 1
        if sev_str == "P1":
            for token in raw_json.replace('"', " ").replace("/", " ").split():
                if "-ST-" in token and len(token) >= 9:
                    p1_stations.add(token[:9])
    return counts, p1_stations


def _station_rows_and_map(
    data: ProfileData,
    as_of: datetime,
    country: str | None,
    settings: Settings,
    notifier: NotifierSnapshot,
) -> tuple[list[StationSummaryRow], list[StationMapItem], dict[str, tuple[int, int]]]:
    cab_online = compute_cabinet_online_map(
        data.cabinets,
        data.cabinet_telemetry,
        as_of,
        online_window_seconds=online_window(data, settings),
    )
    ready_by_cab = compute_ready_batteries_by_cabinet(
        data.slots,
        data.cabinet_telemetry,
        as_of,
        ready_soc_pct=settings.ready_battery_soc_pct,
        charge_rate_pct_per_hour=settings.battery_charge_rate_pct_per_hour,
    )
    _, p1_stations = _open_alerts_from_snapshot(notifier)

    sess_today = filter_sessions_today(data.swap_sessions, data.stations, as_of)
    today_by_st: dict[str, tuple[int, int]] = {}
    if sess_today.height > 0:
        for r in (
            sess_today.group_by("station_id")
            .agg(
                pl.len().alias("total"),
                (pl.col("final_status") != "COMPLETED").sum().alias("failed"),
            )
            .iter_rows(named=True)
        ):
            today_by_st[str(r["station_id"])] = (int(r["total"]), int(r["failed"]))

    win_1h_start = as_of - timedelta(hours=1)
    sess_1h = data.swap_sessions.filter(
        (pl.col("started_at") > win_1h_start) & (pl.col("started_at") <= as_of)
    )
    one_h_by_st: dict[str, tuple[int, int]] = {}
    if sess_1h.height > 0:
        for r in (
            sess_1h.group_by("station_id")
            .agg(
                pl.len().alias("total"),
                (pl.col("final_status") != "COMPLETED").sum().alias("failed"),
            )
            .iter_rows(named=True)
        ):
            one_h_by_st[str(r["station_id"])] = (int(r["total"]), int(r["failed"]))

    cabs_by_station: dict[str, list[str]] = defaultdict(list)
    for r in data.cabinets.iter_rows(named=True):
        cabs_by_station[str(r["station_id"])].append(str(r["cabinet_id"]))

    # Gom telemetry theo tủ MỘT lần (nhiệt độ max, có hw_error) thay vì lọc toàn bảng cho từng
    # trạm: profile demo có hàng triệu dòng heartbeat, lọc theo trạm là O(trạm × dòng).
    tel = data.cabinet_telemetry.filter(
        (pl.col("heartbeat_at") <= as_of) & pl.col("temperature").is_not_null()
    )
    hw_expr = (
        pl.col("hw_error_code").is_not_null().any()
        if "hw_error_code" in tel.columns
        else pl.lit(False)
    )
    tel_by_cab: dict[str, tuple[float | None, bool]] = {
        str(r["cabinet_id"]): (r["max_t"], bool(r["has_hw"]))
        for r in tel.group_by("cabinet_id")
        .agg(pl.col("temperature").max().alias("max_t"), hw_expr.alias("has_hw"))
        .iter_rows(named=True)
    }

    stations_df = data.stations.filter(pl.col("country") == country) if country else data.stations
    summary_rows: list[StationSummaryRow] = []
    map_items: list[StationMapItem] = []

    for row in stations_df.sort("station_id").iter_rows(named=True):
        sid = str(row["station_id"])
        c_code = str(row["country"])
        tz = str(row["timezone"])
        offset_str = format_offset_for(as_of, tz)
        lat = float(row.get("lat") or 0.0)
        lon = float(row.get("lon") or 0.0)
        model_gen = str(row.get("model_gen") or "G1")

        st_cabs = cabs_by_station.get(sid, [])
        c_total = len(st_cabs)
        c_online = sum(1 for cid in st_cabs if cab_online.get(cid, (False, None))[0])
        hb_times = [
            hb for cid in st_cabs if (hb := cab_online.get(cid, (False, None))[1]) is not None
        ]
        last_hb = max(hb_times) if hb_times else None
        last_hb_local = format_local(last_hb, tz) if last_hb is not None else None

        r_bat = sum(ready_by_cab.get(cid, (0, 0))[0] for cid in st_cabs)
        t_slots = sum(ready_by_cab.get(cid, (0, 0))[1] for cid in st_cabs)
        ready_pct = round((r_bat / t_slots) * 100.0, 1) if t_slots > 0 else 0.0

        cab_tel = [tel_by_cab[cid] for cid in st_cabs if cid in tel_by_cab]
        temps = [t for t, _ in cab_tel if isinstance(t, (int, float))]
        max_temp_c = round(float(max(temps)), 1) if temps else None
        has_hw_err = any(hw for _, hw in cab_tel)

        sw_today, fl_today = today_by_st.get(sid, (0, 0))
        fr_today = round(fl_today / sw_today, 4) if sw_today > 0 else 0.0

        sw_1h, fl_1h = one_h_by_st.get(sid, (0, 0))
        fr_1h = round(fl_1h / sw_1h, 4) if sw_1h > 0 else 0.0

        has_p1 = sid in p1_stations
        if c_online < c_total or has_p1:
            color: Literal["green", "blue", "yellow", "orange", "red"] = "red"
        elif fr_1h > settings.station_warn_fail_rate:
            color = "orange"
        elif has_hw_err:
            color = "blue"
        else:
            color = "green"

        summary_rows.append(
            StationSummaryRow(
                station_id=sid,
                country=c_code,
                timezone=tz,
                timezone_offset=offset_str,
                model_gen=model_gen,
                lat=lat,
                lon=lon,
                cabinets_total=c_total,
                cabinets_online=c_online,
                ready_batteries=r_bat,
                total_slots=t_slots,
                ready_battery_pct=ready_pct,
                swaps_today=sw_today,
                failed_today=fl_today,
                fail_rate_today=fr_today,
                last_heartbeat_at=last_hb,
                last_heartbeat_local=last_hb_local,
                status_color=color,
                max_temp_c=max_temp_c,
            )
        )
        map_items.append(
            StationMapItem(
                station_id=sid,
                country=c_code,
                timezone=tz,
                timezone_offset=offset_str,
                lat=lat,
                lon=lon,
                status_color=color,
                cabinets_online=c_online,
                cabinets_total=c_total,
                ready_batteries=r_bat,
                total_slots=t_slots,
                ready_battery_pct=ready_pct,
                swaps_1h=sw_1h,
                failed_1h=fl_1h,
                fail_rate_1h=fr_1h,
                swaps_today=sw_today,
                failed_today=fl_today,
                fail_rate_today=fr_today,
                has_p1_alert=has_p1,
                as_of_local=format_local(as_of, tz),
                max_temp_c=max_temp_c,
            )
        )

    return summary_rows, map_items, ready_by_cab


def build_cabinet_temperature_timeseries(
    data: ProfileData,
    *,
    station_id: str | None = None,
    hours: int = 1,
    as_of: datetime | None = None,
    country: str | None = None,
    settings: Settings | None = None,
    knowledge: Knowledge | None = None,
    profiles: list[str] | None = None,
) -> CabinetTemperatureTimeseriesResponse:
    """API GET /api/dashboard/timeseries/cabinet-temperature (DASHBOARD_UI_PLAN §3)."""
    k = knowledge or default_knowledge()
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    eff_as_of = meta.as_of
    hours = max(1, min(24, int(hours)))

    thresholds = k.error_catalog.thresholds
    warn_t = float(thresholds.temp_warn_1_c)
    crit_t = float(thresholds.temp_warn_2_c)

    st_df = data.stations.filter(pl.col("country") == country) if country else data.stations
    if st_df.height == 0:
        st_df = data.stations

    available_choices = [
        StationChoice(
            station_id=str(r["station_id"]),
            label=f"{r['station_id']} ({r['country']})",
        )
        for r in st_df.sort("station_id").iter_rows(named=True)
    ]

    selected_station = station_id
    valid_ids = {c.station_id for c in available_choices}
    if not selected_station or selected_station not in valid_ids:
        # Mặc định trạm có nhiệt độ cao nhất theo DASHBOARD_UI_PLAN §2.1
        win_start = eff_as_of - timedelta(hours=hours)
        cabs_df = data.cabinets.filter(pl.col("station_id").is_in(list(valid_ids)))
        cab_to_st = {
            str(r["cabinet_id"]): str(r["station_id"]) for r in cabs_df.iter_rows(named=True)
        }
        recent_tel = data.cabinet_telemetry.filter(
            pl.col("cabinet_id").is_in(list(cab_to_st.keys()))
            & (pl.col("heartbeat_at") >= win_start)
            & (pl.col("heartbeat_at") <= eff_as_of)
            & pl.col("temperature").is_not_null()
        )
        best_station: str | None = None
        best_temp = -999.0
        for r in recent_tel.iter_rows(named=True):
            cid = str(r["cabinet_id"])
            st_of_cab = cab_to_st.get(cid)
            if st_of_cab and r["temperature"] is not None:
                t_val = float(r["temperature"])
                if t_val > best_temp:
                    best_temp = t_val
                    best_station = st_of_cab
        default_choice = available_choices[0].station_id if available_choices else ""
        selected_station = best_station or default_choice

    st_info = data.stations.filter(pl.col("station_id") == selected_station)
    tz = str(st_info["timezone"][0]) if st_info.height > 0 else "UTC"

    cabs = data.cabinets.filter(pl.col("station_id") == selected_station).sort("cabinet_id")
    cab_ids = [str(r["cabinet_id"]) for r in cabs.iter_rows(named=True)]

    start_t = eff_as_of - timedelta(hours=hours)
    tel_window = data.cabinet_telemetry.filter(
        pl.col("cabinet_id").is_in(cab_ids)
        & (pl.col("heartbeat_at") >= start_t)
        & (pl.col("heartbeat_at") <= eff_as_of)
        & pl.col("temperature").is_not_null()
    ).sort("heartbeat_at")

    series_list: list[CabinetTempSeries] = []
    for cid in cab_ids:
        cab_tel = tel_window.filter(pl.col("cabinet_id") == cid)
        n_pts = cab_tel.height
        if n_pts == 0:
            series_list.append(CabinetTempSeries(cabinet_id=cid, points=[]))
            continue

        # Lấy mẫu xuống tối đa khoảng 120 điểm
        if n_pts > 120:
            step = max(1, n_pts // 120)
            indexes = list(range(0, n_pts, step))
            if indexes[-1] != n_pts - 1:
                indexes.append(n_pts - 1)
            cab_tel = cab_tel[indexes]

        pts: list[CabinetTempPoint] = []
        temps: list[float] = []
        for r in cab_tel.iter_rows(named=True):
            hb_utc = ensure_utc(r["heartbeat_at"])
            t_val = round(float(r["temperature"]), 1)
            temps.append(t_val)
            pts.append(
                CabinetTempPoint(
                    timestamp_utc=hb_utc.isoformat(),
                    timestamp_local=format_local(hb_utc, tz),
                    temperature_c=t_val,
                )
            )

        avg_c = round(sum(temps) / len(temps), 1) if temps else None
        min_c = min(temps) if temps else None
        max_c = max(temps) if temps else None
        series_list.append(
            CabinetTempSeries(
                cabinet_id=cid,
                points=pts,
                avg_c=avg_c,
                min_c=min_c,
                max_c=max_c,
            )
        )

    return CabinetTemperatureTimeseriesResponse(
        meta=meta,
        station_id=selected_station,
        station_name=selected_station,
        timezone=_station_timezone(data, selected_station),
        hours=hours,
        warn_threshold_c=warn_t,
        critical_threshold_c=crit_t,
        series=series_list,
        available_stations=available_choices,
    )


def _station_timezone(data: ProfileData, station_id: str) -> str:
    """IANA timezone của trạm (mặc định UTC nếu không tìm thấy)."""
    row = data.stations.filter(pl.col("station_id") == station_id)
    return str(row["timezone"][0]) if row.height > 0 else "UTC"


def build_overview(
    data: ProfileData,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    settings: Settings | None = None,
    knowledge: Knowledge | None = None,
    notifier: NotifierSnapshot | None = None,
    profiles: list[str] | None = None,
) -> OverviewResponse:
    """Trang Bản đồ trạm: chỉ bản đồ (các thẻ KPI/biểu đồ thống kê đã bỏ theo yêu cầu chủ dự án)."""
    cfg = settings or get_settings()
    k = knowledge or default_knowledge()
    notif = notifier or NotifierSnapshot(outbox=[], cards=[], latest_feedback={})
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    _, map_items, _ = _station_rows_and_map(data, meta.as_of, country, cfg, notif)
    return OverviewResponse(meta=meta, stations_map=map_items)


def build_stations_list(
    data: ProfileData,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    settings: Settings | None = None,
    knowledge: Knowledge | None = None,
    notifier: NotifierSnapshot | None = None,
    profiles: list[str] | None = None,
) -> StationsListResponse:
    cfg = settings or get_settings()
    k = knowledge or default_knowledge()
    notif = notifier or NotifierSnapshot(outbox=[], cards=[], latest_feedback={})
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    rows, _, _ = _station_rows_and_map(data, meta.as_of, country, cfg, notif)
    return StationsListResponse(
        meta=meta,
        stations=rows,
        ready_battery_note=READY_BATTERY_APPROX_NOTE,
    )


def build_station_detail(
    data: ProfileData,
    station_id: str,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    settings: Settings | None = None,
    knowledge: Knowledge | None = None,
    notifier: NotifierSnapshot | None = None,
    profiles: list[str] | None = None,
) -> StationDetailResponse:
    cfg = settings or get_settings()
    k = knowledge or default_knowledge()
    notif = notifier or NotifierSnapshot(outbox=[], cards=[], latest_feedback={})
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    eff_as_of = meta.as_of

    all_rows, _, _ = _station_rows_and_map(data, eff_as_of, None, cfg, notif)
    station_row = next((r for r in all_rows if r.station_id == station_id), None)
    if station_row is None:
        raise KeyError(station_id)

    tz = station_row.timezone
    thresholds = k.error_catalog.thresholds
    error_table = default_error_table()
    cab_online_map = compute_cabinet_online_map(
        data.cabinets,
        data.cabinet_telemetry,
        eff_as_of,
        online_window_seconds=online_window(data, cfg),
    )

    cabs_df = data.cabinets.filter(pl.col("station_id") == station_id).sort("cabinet_id")
    cab_ids = [str(c) for c in cabs_df["cabinet_id"].to_list()]
    slots_df = data.slots.filter(pl.col("station_id") == station_id).sort(["cabinet_id", "slot_id"])

    # Telemetry khoang gần nhất <= as_of
    tel_upto = data.cabinet_telemetry.filter(
        pl.col("cabinet_id").is_in(cab_ids) & (pl.col("heartbeat_at") <= eff_as_of)
    )
    slot_latest_map: dict[tuple[str, str], dict[str, object]] = {}
    if tel_upto.height > 0:
        slot_tel = tel_upto.filter(pl.col("slot_id").is_not_null())
        if slot_tel.height > 0:
            latest_slots_df = (
                slot_tel.sort("heartbeat_at").group_by(["cabinet_id", "slot_id"]).last()
            )
            for r in latest_slots_df.iter_rows(named=True):
                slot_latest_map[(str(r["cabinet_id"]), str(r["slot_id"]))] = r

    # Sự kiện lỗi trên từng khoang (trong 24h hoặc gần nhất <= as_of)
    win_24h_start = eff_as_of - timedelta(hours=24)
    ev_st = data.swap_events.filter(
        (pl.col("station_id") == station_id)
        & pl.col("slot_id").is_not_null()
        & (pl.col("occurred_at") <= eff_as_of)
    )
    last_slot_ev: dict[tuple[str, str], dict[str, object]] = {}
    fail_24h_by_slot: Counter[tuple[str, str]] = Counter()
    last_fail_code_by_slot: dict[tuple[str, str], str] = {}
    if ev_st.height > 0:
        for r in ev_st.iter_rows(named=True):
            key = (str(r["cabinet_id"]), str(r["slot_id"]))
            last_slot_ev[key] = r
            if str(r["status"]) in ("FAILED", "TIMEOUT"):
                if r.get("error_code"):
                    last_fail_code_by_slot[key] = str(r["error_code"])
                if ensure_utc(r["occurred_at"]) > win_24h_start:
                    fail_24h_by_slot[key] += 1

    # Dòng thời gian heartbeat 24h của từng tủ
    tel_24h = tel_upto.filter(pl.col("heartbeat_at") > win_24h_start)

    cabinet_details: list[CabinetDetail] = []
    for cab_row in cabs_df.iter_rows(named=True):
        cid = str(cab_row["cabinet_id"])
        model_gen = str(cab_row.get("model_gen") or "G1")
        is_online, last_hb = cab_online_map.get(cid, (False, None))

        cab_tel_24h = tel_24h.filter(pl.col("cabinet_id") == cid)
        max_temp_raw = cab_tel_24h["temperature"].max() if cab_tel_24h.height > 0 else None
        max_temp = round(float(max_temp_raw), 1) if isinstance(max_temp_raw, (int, float)) else None

        cab_slots_df = slots_df.filter(pl.col("cabinet_id") == cid).sort("slot_id")
        slot_cells: list[SlotCell] = []
        for s_row in cab_slots_df.iter_rows(named=True):
            sl_id = str(s_row["slot_id"])
            key = (cid, sl_id)
            t_info = slot_latest_map.get(key)
            ev_info = last_slot_ev.get(key)
            f_24h = fail_24h_by_slot.get(key, 0)

            if t_info is None:
                door = "CLOSED"
                detected = True
                soc: float | None = 95.0
                temp: float | None = None
                hw_err: str | None = None
                assumed = True
            else:
                door = str(t_info.get("door_state") or "CLOSED")
                detected = bool(t_info.get("battery_detected"))
                soc_raw = t_info.get("soc")
                soc_est = (
                    estimate_soc(
                        float(soc_raw),
                        t_info["heartbeat_at"],  # type: ignore[arg-type]
                        eff_as_of,
                        cfg.battery_charge_rate_pct_per_hour,
                    )
                    if isinstance(soc_raw, (int, float))
                    else None
                )
                soc = round(soc_est, 1) if soc_est is not None else None
                temp_raw = t_info.get("temperature")
                temp = round(float(temp_raw), 1) if isinstance(temp_raw, (int, float)) else None
                hw_err = str(t_info["hw_error_code"]) if t_info.get("hw_error_code") else None
                assumed = False

            # Lý do lỗi gần nhất của khoang: mã của bảng mã lỗi chuẩn, tên cảnh báo tra từ bảng.
            recent_err: str | None = None
            if ev_info is not None and str(ev_info.get("status")) in ("FAILED", "TIMEOUT"):
                recent_err = (
                    str(ev_info["error_code"])
                    if ev_info.get("error_code")
                    else last_fail_code_by_slot.get(key, NO_ERROR_CODE)
                )
            elif f_24h > 0:
                recent_err = last_fail_code_by_slot.get(key, NO_ERROR_CODE)

            table_row = error_table.lookup(recent_err)
            recent_desc = table_row.name if table_row else None

            if (temp is not None and temp > thresholds.temp_warn_1_c) or (
                hw_err in (thresholds.hw_warn_level_1_code, thresholds.hw_warn_level_2_code)
            ):
                s_color: Literal["empty", "ready", "charging", "error", "overheat"] = "overheat"
                s_label = "Quá nhiệt"
            elif hw_err is not None or recent_err is not None or f_24h > 0:
                s_color = "error"
                s_label = f"Lỗi ({recent_err or hw_err})"
            elif not detected:
                s_color = "empty"
                s_label = "Trống"
            elif soc is None or soc >= cfg.ready_battery_soc_pct:
                s_color = "ready"
                s_label = "Sẵn sàng (giả định)" if assumed else "Sẵn sàng"
            else:
                s_color = "charging"
                s_label = "Đang sạc"

            slot_cells.append(
                SlotCell(
                    slot_id=sl_id,
                    cabinet_id=cid,
                    door_state=door,
                    battery_detected=detected,
                    soc=soc,
                    temperature=temp,
                    hw_error_code=hw_err,
                    recent_error_code=recent_err,
                    recent_error_desc=recent_desc,
                    failed_24h=f_24h,
                    state_color=s_color,
                    state_label=s_label,
                    assumed_initial=assumed,
                )
            )

        # Phân tích heartbeat 24h thành 24 bucket 1 giờ + danh sách khoảng mất heartbeat (>= 120s)
        hb_24h_df = cab_tel_24h.filter(pl.col("slot_id").is_null()).sort("heartbeat_at")
        hb_times = [
            ensure_utc(dt) for dt in hb_24h_df["heartbeat_at"].to_list() if isinstance(dt, datetime)
        ]

        gaps: list[HeartbeatGap] = []
        seq = [win_24h_start, *hb_times, eff_as_of]
        for a, b in itertools.pairwise(seq):
            gap_s = (b - a).total_seconds()
            if gap_s >= float(online_window(data, cfg)):
                gaps.append(
                    HeartbeatGap(
                        start_utc=a,
                        end_utc=b,
                        start_local=format_local(a, tz),
                        end_local=format_local(b, tz),
                        gap_seconds=round(gap_s, 1),
                    )
                )

        buckets: list[HeartbeatBucket] = []
        for b_idx in range(24):
            b_start = win_24h_start + timedelta(hours=b_idx)
            b_end = b_start + timedelta(hours=1)
            cnt = sum(1 for t in hb_times if b_start <= t < b_end)
            expected = 60
            if cnt == 0:
                b_status: Literal["ok", "intermittent", "lost"] = "lost"
            elif cnt < 50:
                b_status = "intermittent"
            else:
                b_status = "ok"
            loc_start = to_local(b_start, tz)
            buckets.append(
                HeartbeatBucket(
                    bucket_start_utc=b_start,
                    bucket_end_utc=b_end,
                    label_local=f"{loc_start:%H:%M %d/%m}",
                    heartbeat_count=cnt,
                    expected_count=expected,
                    status=b_status,
                )
            )

        cabinet_details.append(
            CabinetDetail(
                cabinet_id=cid,
                station_id=station_id,
                model_gen=model_gen,
                online=is_online,
                last_heartbeat_at=last_hb,
                last_heartbeat_local=format_local(last_hb, tz) if last_hb else None,
                max_temp_24h=max_temp,
                slots=slot_cells,
                heartbeat_buckets_24h=buckets,
                heartbeat_gaps_24h=gaps[:50],
            )
        )

    st_temp_ts = build_cabinet_temperature_timeseries(
        data,
        station_id=station_id,
        hours=1,
        as_of=eff_as_of,
        country=None,
        settings=cfg,
        knowledge=k,
        profiles=profiles,
    )
    return StationDetailResponse(
        meta=meta,
        station=station_row,
        cabinets=cabinet_details,
        ready_battery_note=READY_BATTERY_APPROX_NOTE,
        temperature_timeseries=st_temp_ts,
    )
