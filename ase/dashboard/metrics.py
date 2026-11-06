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
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import polars as pl

from ase.common.config import Settings, get_settings
from ase.common.models import Step
from ase.common.time import ensure_utc, format_local, format_offset_for, to_local
from ase.dashboard.data import NotifierSnapshot, ProfileData, extract_session_from_card_json
from ase.dashboard.models import (
    AlarmItem,
    AlarmsResponse,
    AlertsPageResponse,
    AlertStats,
    BatteriesPageResponse,
    CabinetDetail,
    CabinetTemperatureTimeseriesResponse,
    CabinetTempPoint,
    CabinetTempRow,
    CabinetTempSeries,
    CountryKpi,
    DailyRevenueRow,
    DashboardMeta,
    FeedbackItem,
    FunnelStep,
    GroundTruthCountRow,
    GroundTruthPageResponse,
    HeartbeatBucket,
    HeartbeatGap,
    HotspotItem,
    Hotspots24h,
    HourlySwapPoint,
    IncidentsPlaceholderResponse,
    LotSohBox,
    LowSohIssuedRow,
    MatrixCell,
    MetricsPlaceholderResponse,
    OutboxItem,
    OverviewResponse,
    PaymentsPageResponse,
    ProviderPaymentStat,
    ProviderTimeoutPoint,
    ReadyBatteriesTimeseriesResponse,
    ReadyBatteryPoint,
    ReconciliationRow,
    ScenarioInstanceTimelineItem,
    SessionDetailResponse,
    SessionEventItem,
    SessionPaymentDetail,
    SessionsPageResponse,
    SessionSummaryRow,
    SessionTelemetryItem,
    SlotCell,
    SocHistogramBin,
    StationChoice,
    StationDetailResponse,
    StationMapItem,
    StationReadyBatterySeries,
    StationsListResponse,
    StationSummaryRow,
    StepErrorMatrix,
    StepLatencyStat,
    StuckSessionItem,
    SuccessFactorSpec,
    TeamsCardItem,
)
from ase.knowledge.loader import Knowledge, default_knowledge
from ase.simulator.session_fsm import step_chain

FEEDBACK_ACTION_LABELS: dict[str, str] = {
    "confirm": "Xác nhận đúng",
    "wrong_category": "Sai phân loại",
    "resolved": "Đã xử lý",
}

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


STEP_VI_LABELS: dict[Step, str] = {
    Step.OPEN_APP: "1. Mở App",
    Step.SELECT_SERVICE: "2. Chọn dịch vụ",
    Step.SCAN_QR: "3. Quét QR",
    Step.CHECK_ELIGIBILITY: "4. Kiểm tra ĐK",
    Step.PAYMENT: "5. Thanh toán",
    Step.RETURN_BATTERY: "6. Trả pin cũ",
    Step.RECEIVE_BATTERY: "7. Nhận pin mới",
}

SF_SPECS: list[tuple[str, str, str, str]] = [
    ("SF1", "Tiếp cận dữ liệu", "% giao dịch được ingest", "100%"),
    ("SF2", "Phát hiện đúng (kể cả lỗi im lặng)", "Recall", "≥ 95%"),
    ("SF3", "Không báo nhầm", "Precision", "≥ 90%"),
    ("SF4", "Phân loại đúng nhóm lỗi", "Accuracy / macro-F1", "≥ 85%"),
    ("SF5", "Kịp thời", "Thời gian lỗi → alert, p95", "< 60s"),
    ("SF6", "Hiểu hệ thống", "% incident gợi ý đúng nguyên nhân (scenario)", "≥ 70%"),
    ("SF7", "Thông báo đúng", "% alert đúng team, hợp lệ schema, đủ mã GD + thời gian", "100%"),
    ("SF8", "Không gây nhiễu", "Số alert / số incident thật", "≤ 1.2"),
    (
        "SF9",
        "An toàn",
        "Số action ghi; % evidence ảo; % prompt injection thành công",
        "0 / 0% / 0%",
    ),
]


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


def compute_revenue_by_currency(
    payments: pl.DataFrame,
    sessions: pl.DataFrame | None = None,
    as_of: datetime | None = None,
) -> dict[str, float]:
    """Tổng `amount` của payment `captured=true`, tách theo `currency`, không quy đổi."""
    if payments.height == 0:
        return {}
    df = payments.filter(pl.col("captured") == True)  # noqa: E712
    if sessions is not None:
        sess_sub = sessions
        if as_of is not None:
            sess_sub = sess_sub.filter(pl.col("started_at") <= ensure_utc(as_of))
        df = df.join(sess_sub.select("session_id"), on="session_id", how="inner")
    elif as_of is not None and "callback_at" in df.columns:
        df = df.filter(
            pl.col("callback_at").is_null() | (pl.col("callback_at") <= ensure_utc(as_of))
        )

    if df.height == 0:
        return {}
    grouped = df.group_by("currency").agg(pl.col("amount").sum().alias("total"))
    return {
        str(r["currency"]): round(float(r["total"]), 2)
        for r in grouped.sort("currency").iter_rows(named=True)
    }


def compute_funnel(
    events: pl.DataFrame,
    as_of: datetime,
    knowledge: Knowledge | None = None,
) -> list[FunnelStep]:
    """Tính phễu 7 bước trên các event `occurred_at <= as_of`."""
    k = knowledge or default_knowledge()
    chain = step_chain(k.state_machine)
    as_of_utc = ensure_utc(as_of)
    ev = events.filter(pl.col("occurred_at") <= as_of_utc) if events.height > 0 else events

    reached_by_step: dict[str, int] = {}
    ok_by_step: dict[str, int] = {}
    fail_by_step: dict[str, int] = {}

    if ev.height > 0:
        for row in (
            ev.group_by("step")
            .agg(
                pl.col("session_id").n_unique().alias("reached"),
                pl.col("session_id").filter(pl.col("status") == "OK").n_unique().alias("ok_cnt"),
                pl.col("session_id")
                .filter(pl.col("status").is_in(["FAILED", "TIMEOUT"]))
                .n_unique()
                .alias("fail_cnt"),
            )
            .iter_rows(named=True)
        ):
            st_key = str(row["step"])
            reached_by_step[st_key] = int(row["reached"])
            ok_by_step[st_key] = int(row["ok_cnt"])
            fail_by_step[st_key] = int(row["fail_cnt"])

    funnel: list[FunnelStep] = []
    for idx, step in enumerate(chain):
        reached = reached_by_step.get(step.value, 0)
        ok_cnt = ok_by_step.get(step.value, 0)
        fail_cnt = fail_by_step.get(step.value, 0)
        if idx < len(chain) - 1:
            next_reached = reached_by_step.get(chain[idx + 1].value, 0)
            drop_cnt = max(0, reached - next_reached)
        else:
            drop_cnt = max(0, reached - ok_cnt)
        drop_rate = round(drop_cnt / reached * 100.0, 2) if reached > 0 else 0.0
        funnel.append(
            FunnelStep(
                step=step,
                step_label=STEP_VI_LABELS.get(step, step.value),
                service=k.state_machine.steps[step].service,
                reached_count=reached,
                ok_count=ok_cnt,
                failed_count=fail_cnt,
                drop_count=drop_cnt,
                drop_rate_pct=drop_rate,
            )
        )
    return funnel


def compute_stuck_sessions(
    events: pl.DataFrame,
    stations: pl.DataFrame,
    payments: pl.DataFrame | None,
    as_of: datetime,
    knowledge: Knowledge | None = None,
    stale_after_seconds: float = 3600.0,
) -> list[StuckSessionItem]:
    """Tìm các phiên chưa đóng đã vượt quá SLA của bước hiện tại tại thời điểm `as_of`.

    Quy tắc tính SLA (DASHBOARD_PLAN §5.3, §7):
    - Tính từ event **cuối** (`max(occurred_at)`) của bước liền trước.
    - Nếu một bước (vd bước 5) có `PENDING` rồi `OK` trước `as_of`: bước đó đã hoàn tất (`OK`),
      thời gian chờ của bước 6 được tính từ event `OK` (cuối) của bước 5, không bị tính treo sai.
    - Nếu bước hiện tại đang `PENDING` (chưa có `OK`/`FAILED`/`TIMEOUT` tại `as_of`), thời gian chờ
      của bước hiện tại được tính từ event cuối của bước liền trước.
    """
    if events.height == 0:
        return []
    k = knowledge or default_knowledge()
    sm = k.state_machine
    chain = step_chain(sm)
    step_idx_map = {s.value: i for i, s in enumerate(chain)}
    as_of_utc = ensure_utc(as_of)

    ev = events.filter(pl.col("occurred_at") <= as_of_utc)
    if ev.height == 0:
        return []

    # Loại nhanh các phiên đã đóng (có event FAILED/TIMEOUT hoặc có event 7_receive_battery OK)
    closed_sids_df = ev.filter(
        pl.col("status").is_in(sm.terminal_statuses)
        | ((pl.col("step") == Step.RECEIVE_BATTERY.value) & (pl.col("status") == "OK"))
    ).select(pl.col("session_id").unique())
    open_ev = ev.join(closed_sids_df, on="session_id", how="anti")
    if open_ev.height == 0:
        return []

    st_meta: dict[str, tuple[str, str]] = {}
    for row in stations.iter_rows(named=True):
        st_meta[str(row["station_id"])] = (str(row["country"]), str(row["timezone"]))

    captured_sids: set[str] = set()
    if payments is not None and payments.height > 0:
        cap_df = payments.filter(pl.col("captured") == True).select("session_id")  # noqa: E712
        captured_sids = {str(s) for s in cap_df["session_id"].to_list()}

    # Gom theo session_id trên tập phiên đang mở (số lượng nhỏ)
    by_session: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in open_ev.iter_rows(named=True):
        by_session[str(row["session_id"])].append(row)

    stuck_list: list[StuckSessionItem] = []
    for sid, rows in by_session.items():
        last_at_by_idx: dict[int, datetime] = {}
        statuses_by_idx: dict[int, set[str]] = defaultdict(set)
        station_id = str(rows[0]["station_id"])
        cabinet_id: str | None = None

        for r in rows:
            s_val = str(r["step"])
            idx = step_idx_map.get(s_val, 0)
            occ = ensure_utc(r["occurred_at"])  # type: ignore[arg-type]
            if idx not in last_at_by_idx or occ > last_at_by_idx[idx]:
                last_at_by_idx[idx] = occ
            statuses_by_idx[idx].add(str(r["status"]))
            if r.get("cabinet_id") is not None:
                cabinet_id = str(r["cabinet_id"])

        max_idx = max(last_at_by_idx)
        if "OK" in statuses_by_idx[max_idx]:
            if max_idx >= len(chain) - 1:
                continue
            last_completed_step: Step | None = chain[max_idx]
            stuck_step = chain[max_idx + 1]
            wait_started_at = last_at_by_idx[max_idx]
        else:
            # Bước max_idx đang PENDING (chưa có OK/FAILED/TIMEOUT)
            stuck_step = chain[max_idx]
            last_completed_step = chain[max_idx - 1] if max_idx > 0 else None
            if max_idx > 0 and (max_idx - 1) in last_at_by_idx:
                wait_started_at = last_at_by_idx[max_idx - 1]
            else:
                wait_started_at = last_at_by_idx[max_idx]

        sla_s = sm.sla(stuck_step)
        if sla_s is None:
            continue
        elapsed_s = (as_of_utc - wait_started_at).total_seconds()
        if elapsed_s > float(sla_s):
            country, tz = st_meta.get(station_id, ("VN", "Asia/Ho_Chi_Minh"))
            stuck_list.append(
                StuckSessionItem(
                    session_id=sid,
                    station_id=station_id,
                    country=country,
                    cabinet_id=cabinet_id,
                    last_completed_step=last_completed_step,
                    stuck_step=stuck_step,
                    stuck_step_label=STEP_VI_LABELS.get(stuck_step, stuck_step.value),
                    wait_started_at_utc=wait_started_at,
                    wait_started_at_local=format_local(wait_started_at, tz),
                    elapsed_seconds=round(elapsed_s, 1),
                    sla_seconds=sla_s,
                    payment_captured=sid in captured_sids,
                    stale=elapsed_s > stale_after_seconds,
                )
            )

    # Phiên treo gần đây (còn xử lý kịp) lên trước; phiên treo quá `stale_after_seconds` xuống sau
    # để danh sách không bị che bởi các lỗi im lặng cũ. Trong mỗi nhóm: mới nhất trước.
    stuck_list.sort(key=lambda x: (x.stale, -x.wait_started_at_utc.timestamp()))
    return stuck_list


def compute_reconciliation(
    sessions: pl.DataFrame,
    payments: pl.DataFrame,
    events: pl.DataFrame,
    users: pl.DataFrame,
    stations: pl.DataFrame,
    as_of: datetime,
    knowledge: Knowledge | None = None,
) -> list[ReconciliationRow]:
    """Đối soát giao dịch rủi ro tiền (§5.4, §7):
    1. `captured=true` nhưng phiên `FAILED`/`TIMEOUT` hoặc không có event bước 6/7.
    2. `amount` lệch bảng giá `PricingSpec.standard_swap_amount(package_type)` của nước.
    """
    if payments.height == 0 or sessions.height == 0:
        return []
    k = knowledge or default_knowledge()
    as_of_utc = ensure_utc(as_of)

    sess_upto = sessions.filter(pl.col("started_at") <= as_of_utc)
    if sess_upto.height == 0:
        return []

    ev_upto = events.filter(pl.col("occurred_at") <= as_of_utc) if events.height > 0 else events
    sids_with_step_6_7: set[str] = set()
    if ev_upto.height > 0:
        step67_df = ev_upto.filter(
            pl.col("step").is_in([Step.RETURN_BATTERY.value, Step.RECEIVE_BATTERY.value])
        ).select(pl.col("session_id").unique())
        sids_with_step_6_7 = {str(s) for s in step67_df["session_id"].to_list()}

    joined = (
        payments.join(sess_upto, on="session_id", how="inner")
        .join(
            stations.select(["station_id", "country", "timezone"]),
            on="station_id",
            how="left",
        )
        .join(
            users.select(["user_id", "package_type"]),
            on="user_id",
            how="left",
        )
    )

    issues: list[ReconciliationRow] = []
    for row in joined.sort("started_at", descending=True).iter_rows(named=True):
        sid = str(row["session_id"])
        pid = str(row["payment_id"])
        station_id = str(row["station_id"])
        country = str(row.get("country") or "VN")
        tz = str(row.get("timezone") or "Asia/Ho_Chi_Minh")
        user_id = str(row["user_id"])
        pkg = str(row.get("package_type") or "RENT_1")
        provider = str(row["payment_provider"]) if row.get("payment_provider") else None
        amount = float(row["amount"])
        currency = str(row["currency"])
        pay_status = str(row["status"])
        captured = bool(row["captured"])
        final_status = str(row["final_status"])
        started_at = ensure_utc(row["started_at"])

        country_spec = k.countries.countries.get(country)
        if country_spec is not None and pkg in country_spec.pricing.packages:
            expected = float(country_spec.pricing.standard_swap_amount(pkg))
        else:
            expected = amount

        has_6_7 = sid in sids_with_step_6_7
        if captured and (final_status in ("FAILED", "TIMEOUT") or not has_6_7):
            issues.append(
                ReconciliationRow(
                    session_id=sid,
                    payment_id=pid,
                    station_id=station_id,
                    country=country,
                    user_id=user_id,
                    package_type=pkg,
                    payment_provider=provider,
                    amount=amount,
                    expected_amount=expected,
                    currency=currency,
                    payment_status=pay_status,
                    captured=captured,
                    session_final_status=final_status,
                    has_step_6_or_7=has_6_7,
                    issue_type="CAPTURED_WITHOUT_DELIVERY",
                    issue_label="Đã trừ tiền (captured=true) nhưng không nhả pin / thiếu bước 6–7",
                    started_at_local=format_local(started_at, tz),
                )
            )
        if abs(amount - expected) > 1e-6:
            issues.append(
                ReconciliationRow(
                    session_id=sid,
                    payment_id=pid,
                    station_id=station_id,
                    country=country,
                    user_id=user_id,
                    package_type=pkg,
                    payment_provider=provider,
                    amount=amount,
                    expected_amount=expected,
                    currency=currency,
                    payment_status=pay_status,
                    captured=captured,
                    session_final_status=final_status,
                    has_step_6_or_7=has_6_7,
                    issue_type="AMOUNT_MISMATCH",
                    issue_label=(
                        f"Lệch bảng giá gói {pkg}: thu {amount:g} ≠ chuẩn {expected:g} {currency}"
                    ),
                    started_at_local=format_local(started_at, tz),
                )
            )
    return issues


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
        online_window_seconds=settings.cabinet_online_window_seconds,
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

        st_tel = data.cabinet_telemetry.filter(
            pl.col("cabinet_id").is_in(st_cabs)
            & (pl.col("heartbeat_at") <= as_of)
            & pl.col("temperature").is_not_null()
        )
        max_t_val = st_tel["temperature"].max() if st_tel.height > 0 else None
        max_temp_c = round(float(max_t_val), 1) if isinstance(max_t_val, (int, float)) else None

        has_hw_err = False
        if st_tel.height > 0 and "hw_error_code" in st_tel.columns:
            has_hw_err = bool(st_tel.filter(pl.col("hw_error_code").is_not_null()).height > 0)

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


def _build_country_kpi(
    country_label: str,
    station_rows: list[StationSummaryRow],
    sess_today: pl.DataFrame,
    payments: pl.DataFrame,
    as_of: datetime,
    alert_counts: Counter[tuple[str, str]],
) -> CountryKpi:
    st_ids = {s.station_id for s in station_rows}
    st_total = len(station_rows)
    st_online = sum(1 for s in station_rows if s.cabinets_online > 0)
    cab_total = sum(s.cabinets_total for s in station_rows)
    cab_online = sum(s.cabinets_online for s in station_rows)
    ready_bat = sum(s.ready_batteries for s in station_rows)
    total_slots = sum(s.total_slots for s in station_rows)

    sub_sess = (
        sess_today.filter(pl.col("station_id").is_in(list(st_ids)))
        if sess_today.height > 0 and st_ids
        else sess_today.clear()
    )
    swaps_today = sub_sess.height
    ended_df = (
        sub_sess.filter(pl.col("ended_at").is_not_null() & (pl.col("ended_at") <= as_of))
        if swaps_today > 0
        else sub_sess.clear()
    )
    ended_today = ended_df.height
    comp_df = (
        ended_df.filter(pl.col("final_status") == "COMPLETED")
        if ended_today > 0
        else ended_df.clear()
    )
    completed_today = comp_df.height
    success_rate = round(completed_today / ended_today, 4) if ended_today > 0 else None

    dur_median: float | None = None
    dur_p95: float | None = None
    if completed_today > 0:
        durs = comp_df.select(
            (pl.col("ended_at") - pl.col("started_at")).dt.total_milliseconds() / 1000.0
        ).to_series()
        m_val = durs.median()
        p95_val = durs.quantile(0.95)
        dur_median = round(float(m_val), 2) if isinstance(m_val, (int, float)) else None
        dur_p95 = round(float(p95_val), 2) if isinstance(p95_val, (int, float)) else None

    rev_by_curr = compute_revenue_by_currency(payments, sub_sess, as_of)
    return CountryKpi(
        country=country_label,
        stations_online=st_online,
        stations_total=st_total,
        cabinets_online=cab_online,
        cabinets_total=cab_total,
        ready_batteries=ready_bat,
        total_slots=total_slots,
        swaps_today=swaps_today,
        completed_today=completed_today,
        ended_today=ended_today,
        success_rate_today=success_rate,
        swap_duration_median_s=dur_median,
        swap_duration_p95_s=dur_p95,
        revenue_today_by_currency=rev_by_curr,
        open_alerts_p1=alert_counts.get((country_label, "P1"), 0),
        open_alerts_p2=alert_counts.get((country_label, "P2"), 0),
    )


def _feedback_status_line(fb: tuple[str, str] | None) -> str:
    if not fb:
        return "Chưa xác nhận"
    if fb[0] in ("confirm", "wrong_category"):
        return "Đã xác nhận"
    if fb[0] == "resolved":
        return "Đã xử lý"
    return "Chưa xác nhận"


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


def build_ready_batteries_timeseries(
    data: ProfileData,
    *,
    hours: int = 6,
    step_minutes: int = 15,
    as_of: datetime | None = None,
    country: str | None = None,
    settings: Settings | None = None,
    knowledge: Knowledge | None = None,
    profiles: list[str] | None = None,
) -> ReadyBatteriesTimeseriesResponse:
    """API GET /api/dashboard/timeseries/ready-batteries (DASHBOARD_UI_PLAN §3)."""
    cfg = settings or get_settings()
    k = knowledge or default_knowledge()
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    eff_as_of = meta.as_of
    hours = max(1, min(24, int(hours)))
    step_minutes = max(5, min(60, int(step_minutes)))

    st_df = data.stations.filter(pl.col("country") == country) if country else data.stations
    if st_df.height == 0:
        st_df = data.stations

    st_rows = list(st_df.sort("station_id").iter_rows(named=True))
    cabs_by_st: dict[str, list[str]] = defaultdict(list)
    for r in data.cabinets.iter_rows(named=True):
        cabs_by_st[str(r["station_id"])].append(str(r["cabinet_id"]))

    total_steps = (hours * 60) // step_minutes
    time_points = [
        eff_as_of - timedelta(minutes=(total_steps - i) * step_minutes)
        for i in range(total_steps + 1)
    ]

    series_points_by_station: dict[str, list[ReadyBatteryPoint]] = defaultdict(list)
    timestamp_labels: list[str] = []

    for t_step in time_points:
        by_cab = compute_ready_batteries_by_cabinet(
            data.slots,
            data.cabinet_telemetry,
            t_step,
            ready_soc_pct=cfg.ready_battery_soc_pct,
            charge_rate_pct_per_hour=cfg.battery_charge_rate_pct_per_hour,
        )
        t_utc_str = ensure_utc(t_step).isoformat()
        timestamp_labels.append(t_utc_str)

        for s_row in st_rows:
            sid = str(s_row["station_id"])
            tz = str(s_row["timezone"])
            c_ids = cabs_by_st.get(sid, [])
            ready_cnt = sum(by_cab.get(cid, (0, 0))[0] for cid in c_ids)
            total_cnt = sum(by_cab.get(cid, (0, 0))[1] for cid in c_ids)
            pct = round((ready_cnt / total_cnt) * 100.0, 1) if total_cnt > 0 else 0.0
            series_points_by_station[sid].append(
                ReadyBatteryPoint(
                    timestamp_utc=t_utc_str,
                    timestamp_local=format_local(t_step, tz),
                    ready_count=ready_cnt,
                    total_slots=total_cnt,
                    ready_pct=pct,
                )
            )

    series_list: list[StationReadyBatterySeries] = []
    for s_row in st_rows:
        sid = str(s_row["station_id"])
        pts = series_points_by_station.get(sid, [])
        curr_pct = pts[-1].ready_pct if pts else 0.0
        series_list.append(
            StationReadyBatterySeries(
                station_id=sid,
                station_name=sid,
                country=str(s_row["country"]),
                points=pts,
                current_ready_pct=curr_pct,
            )
        )

    return ReadyBatteriesTimeseriesResponse(
        meta=meta,
        hours=hours,
        step_minutes=step_minutes,
        timestamps=timestamp_labels,
        series=series_list,
    )


def build_alarms_page(
    data: ProfileData,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    settings: Settings | None = None,
    knowledge: Knowledge | None = None,
    notifier: NotifierSnapshot | None = None,
    profiles: list[str] | None = None,
) -> AlarmsResponse:
    """API GET /api/dashboard/alarms: hợp nhất 3 nguồn theo DASHBOARD_UI_PLAN §2.3."""
    cfg = settings or get_settings()
    k = knowledge or default_knowledge()
    notif = notifier or NotifierSnapshot(outbox=[], cards=[], latest_feedback={})
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    eff_as_of = meta.as_of

    alarms: list[AlarmItem] = []
    seen_ids: set[str] = set()

    st_df = data.stations
    st_country_map = {str(r["station_id"]): str(r["country"]) for r in st_df.iter_rows(named=True)}
    st_tz_map = {str(r["station_id"]): str(r["timezone"]) for r in st_df.iter_rows(named=True)}

    cabs_df = data.cabinets
    cab_to_st = {str(r["cabinet_id"]): str(r["station_id"]) for r in cabs_df.iter_rows(named=True)}

    # Nguồn 1: incidents.jsonl của detector nếu có (§2.3)
    incidents_candidates = [
        data.profile_dir / "incidents.jsonl",
        cfg.data_dir / "incidents.jsonl",
    ]
    for inc_file in incidents_candidates:
        if inc_file.exists():
            try:
                for line in inc_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    inc_id = str(obj.get("incident_id") or "")
                    if not inc_id or inc_id in seen_ids:
                        continue

                    fs_str = obj.get("first_seen_at")
                    if fs_str:
                        fs_dt = ensure_utc(datetime.fromisoformat(fs_str.replace("Z", "+00:00")))
                        if fs_dt > eff_as_of:
                            continue
                    else:
                        fs_dt = eff_as_of

                    inc_country = obj.get("country")
                    if (
                        country
                        and inc_country
                        and str(inc_country) != country
                        and str(inc_country) != "GLOBAL"
                    ):
                        continue

                    err_code = str(obj.get("error_code") or "")
                    err_entry = k.error_catalog.by_code.get(err_code) if err_code else None
                    fallback_type = str(obj.get("probable_cause") or err_code or "Sự cố BSS")
                    alarm_type = err_entry.description if err_entry else fallback_type

                    raw_sev = str(obj.get("severity") or "P2").upper()
                    sev_mapped: Literal["Critical", "Major", "Minor"] = "Major"
                    if "P1" in raw_sev:
                        sev_mapped = "Critical"
                    elif "P3" in raw_sev:
                        sev_mapped = "Minor"

                    fb = notif.latest_feedback.get(inc_id)
                    st2 = _feedback_status_line(fb)

                    st_id = str(obj.get("station_id") or "")
                    cab_id = str(obj.get("cabinet_id") or "")
                    originator = cab_id if cab_id else (st_id if st_id else "BSS Network")
                    session_ids = obj.get("session_ids") or []
                    first_sid = session_ids[0] if session_ids else None

                    alarms.append(
                        AlarmItem(
                            alarm_id=inc_id,
                            start_time_utc=fs_dt.isoformat(),
                            start_time_local=format_local(fs_dt, st_tz_map.get(st_id, "UTC")),
                            originator=originator,
                            station_id=st_id if st_id else None,
                            cabinet_id=cab_id if cab_id else None,
                            alarm_type=alarm_type,
                            severity=sev_mapped,
                            status_line1="Active",
                            status_line2=st2,
                            is_mock=False,
                            details_link=f"/dashboard/sessions/{first_sid}" if first_sid else None,
                            ack_link=f"/feedback/{inc_id}/confirm",
                        )
                    )
                    seen_ids.add(inc_id)
            except Exception:
                pass

    # Nguồn 2: trạng thái gửi và feedback trong notifier.sqlite (cards & outbox)
    for card in notif.cards:
        inc_id = str(card.get("incident_id") or "")
        if not inc_id or inc_id in seen_ids:
            continue
        card_json_str = str(card.get("card_json") or "{}")
        first_sid, _cat_str, sev_str = extract_session_from_card_json(card_json_str)
        try:
            parsed = json.loads(card_json_str)
        except Exception:
            parsed = {}

        card_country = parsed.get("country")
        if (
            country
            and card_country
            and str(card_country) != country
            and str(card_country) != "GLOBAL"
        ):
            continue

        raw_sev = str(sev_str or parsed.get("severity") or "P2").upper()
        sev_mapped = "Major"
        if "P1" in raw_sev:
            sev_mapped = "Critical"
        elif "P3" in raw_sev:
            sev_mapped = "Minor"

        fb = notif.latest_feedback.get(inc_id)
        st2 = _feedback_status_line(fb)

        err_code = str(parsed.get("error_code") or "")
        err_entry = k.error_catalog.by_code.get(err_code) if err_code else None
        fallback_msg = str(
            parsed.get("probable_cause") or parsed.get("title") or "Cảnh báo hệ thống"
        )
        alarm_type = err_entry.description if err_entry else fallback_msg

        st_id = str(parsed.get("station_id") or "")
        cab_id = str(parsed.get("cabinet_id") or "")
        originator = cab_id if cab_id else (st_id if st_id else "BSS Network")
        rec_at = str(card.get("received_at") or eff_as_of.isoformat())
        start_utc_str, start_local_str = rec_at, rec_at
        fs_raw = parsed.get("first_seen_at")
        if isinstance(fs_raw, str) and fs_raw:
            fs_dt = ensure_utc(datetime.fromisoformat(fs_raw.replace("Z", "+00:00")))
            card_tz = str(parsed.get("timezone") or st_tz_map.get(st_id, "UTC"))
            start_utc_str, start_local_str = fs_dt.isoformat(), format_local(fs_dt, card_tz)
        sids = parsed.get("session_ids")
        if isinstance(sids, list) and sids and isinstance(sids[0], str):
            first_sid = sids[0]

        ch = card.get("channel", "it-ops")
        det_link = f"/dashboard/sessions/{first_sid}" if first_sid else f"/#channel-{ch}"

        alarms.append(
            AlarmItem(
                alarm_id=inc_id,
                start_time_utc=start_utc_str,
                start_time_local=start_local_str,
                originator=originator,
                station_id=st_id if st_id else None,
                cabinet_id=cab_id if cab_id else None,
                alarm_type=alarm_type,
                severity=sev_mapped,
                status_line1="Active",
                status_line2=st2,
                is_mock=True,  # Nhãn 'mẫu' như mục 6.3
                details_link=det_link,
                ack_link=f"/feedback/{inc_id}/confirm",
            )
        )
        seen_ids.add(inc_id)

    # Nguồn 3: Cảnh báo thiết bị tính tại as_of (mất heartbeat, nhiệt > 55°C hoặc > 60°C)
    cab_online = compute_cabinet_online_map(
        data.cabinets,
        data.cabinet_telemetry,
        eff_as_of,
        online_window_seconds=cfg.cabinet_online_window_seconds,
    )
    for cid, (online, last_hb) in cab_online.items():
        st_id = cab_to_st.get(cid, "")
        st_c = st_country_map.get(st_id, "VN")
        if country and st_c != country:
            continue
        tz = st_tz_map.get(st_id, "UTC")

        if not online:
            alarm_id = f"DEV-{cid}-HEARTBEAT"
            fb = notif.latest_feedback.get(alarm_id)
            st2 = _feedback_status_line(fb)
            hb_utc = ensure_utc(last_hb) if last_hb else (eff_as_of - timedelta(minutes=2))
            alarms.append(
                AlarmItem(
                    alarm_id=alarm_id,
                    start_time_utc=hb_utc.isoformat(),
                    start_time_local=format_local(hb_utc, tz),
                    originator=cid,
                    station_id=st_id if st_id else None,
                    cabinet_id=cid,
                    alarm_type="Mất kết nối heartbeat (> 2 phút)",
                    severity="Critical",
                    status_line1="Active",
                    status_line2=st2,
                    is_mock=False,
                    details_link=f"/dashboard/stations/{st_id}" if st_id else None,
                    ack_link=f"/feedback/{alarm_id}/confirm",
                )
            )

    thresholds = k.error_catalog.thresholds
    warn_t = float(thresholds.temp_warn_1_c)
    crit_t = float(thresholds.temp_warn_2_c)
    win_15m = eff_as_of - timedelta(minutes=15)
    recent_tel = data.cabinet_telemetry.filter(
        (pl.col("heartbeat_at") >= win_15m)
        & (pl.col("heartbeat_at") <= eff_as_of)
        & pl.col("temperature").is_not_null()
    )
    if recent_tel.height > 0:
        latest_cabs = recent_tel.sort("heartbeat_at").group_by("cabinet_id").last()
        for r in latest_cabs.iter_rows(named=True):
            cid = str(r["cabinet_id"])
            st_id = cab_to_st.get(cid, "")
            st_c = st_country_map.get(st_id, "VN")
            if country and st_c != country:
                continue
            tz = st_tz_map.get(st_id, "UTC")
            t_val = float(r["temperature"])
            hb_utc = ensure_utc(r["heartbeat_at"])

            if t_val > crit_t:
                alarm_id = f"DEV-{cid}-TEMP-CRIT"
                fb = notif.latest_feedback.get(alarm_id)
                st2 = _feedback_status_line(fb)
                alarms.append(
                    AlarmItem(
                        alarm_id=alarm_id,
                        start_time_utc=hb_utc.isoformat(),
                        start_time_local=format_local(hb_utc, tz),
                        originator=cid,
                        station_id=st_id if st_id else None,
                        cabinet_id=cid,
                        alarm_type=f"Quá nhiệt tủ (> {crit_t:.0f}°C ngắt nguồn - {t_val:.1f}°C)",
                        severity="Critical",
                        status_line1="Active",
                        status_line2=st2,
                        is_mock=False,
                        details_link=f"/dashboard/stations/{st_id}" if st_id else None,
                        ack_link=f"/feedback/{alarm_id}/confirm",
                    )
                )
            elif t_val > warn_t:
                alarm_id = f"DEV-{cid}-TEMP-WARN"
                fb = notif.latest_feedback.get(alarm_id)
                st2 = _feedback_status_line(fb)
                alarms.append(
                    AlarmItem(
                        alarm_id=alarm_id,
                        start_time_utc=hb_utc.isoformat(),
                        start_time_local=format_local(hb_utc, tz),
                        originator=cid,
                        station_id=st_id if st_id else None,
                        cabinet_id=cid,
                        alarm_type=f"Nhiệt độ tủ cao (> {warn_t:.0f}°C - {t_val:.1f}°C)",
                        severity="Major",
                        status_line1="Active",
                        status_line2=st2,
                        is_mock=False,
                        details_link=f"/dashboard/stations/{st_id}" if st_id else None,
                        ack_link=f"/feedback/{alarm_id}/confirm",
                    )
                )

    alarms.sort(key=lambda a: a.start_time_utc, reverse=True)
    crit_cnt = sum(1 for a in alarms if a.severity == "Critical")
    maj_cnt = sum(1 for a in alarms if a.severity == "Major")
    min_cnt = sum(1 for a in alarms if a.severity == "Minor")

    return AlarmsResponse(
        meta=meta,
        alarms=alarms,
        total=len(alarms),
        critical_count=crit_cnt,
        major_count=maj_cnt,
        minor_count=min_cnt,
    )


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
    cfg = settings or get_settings()
    k = knowledge or default_knowledge()
    notif = notifier or NotifierSnapshot(outbox=[], cards=[], latest_feedback={})
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    eff_as_of = meta.as_of

    all_station_rows, _, _ = _station_rows_and_map(data, eff_as_of, None, cfg, notif)
    filtered_station_rows, map_items, _ = _station_rows_and_map(
        data, eff_as_of, country, cfg, notif
    )
    sess_today_all = filter_sessions_today(data.swap_sessions, data.stations, eff_as_of)
    alert_counts, _ = _open_alerts_from_snapshot(notif)

    kpi_total = _build_country_kpi(
        country or "ALL",
        filtered_station_rows,
        sess_today_all,
        data.payments,
        eff_as_of,
        alert_counts,
    )

    present_countries = sorted({s.country for s in all_station_rows})
    kpi_by_country = [
        _build_country_kpi(
            c_code,
            [s for s in all_station_rows if s.country == c_code],
            sess_today_all,
            data.payments,
            eff_as_of,
            alert_counts,
        )
        for c_code in present_countries
    ]

    # Biểu đồ lượt đổi theo giờ địa phương (00:00..23:00) trong 24 giờ gần nhất trước `as_of`
    win_24h_start = eff_as_of - timedelta(hours=24)
    st_tz_map = {
        str(r["station_id"]): str(r["timezone"]) for r in data.stations.iter_rows(named=True)
    }
    st_country_map = {
        str(r["station_id"]): str(r["country"]) for r in data.stations.iter_rows(named=True)
    }

    sess_24h = data.swap_sessions.filter(
        (pl.col("started_at") > win_24h_start) & (pl.col("started_at") <= eff_as_of)
    )
    if country:
        allowed_sts = [sid for sid, c_code in st_country_map.items() if c_code == country]
        sess_24h = sess_24h.filter(pl.col("station_id").is_in(allowed_sts))

    ok_by_hour: Counter[int] = Counter()
    fail_by_hour: Counter[int] = Counter()
    st_tot_24h: Counter[str] = Counter()
    st_fail_24h: Counter[str] = Counter()
    cab_tot_24h: Counter[str] = Counter()
    cab_fail_24h: Counter[str] = Counter()
    slot_tot_24h: Counter[str] = Counter()
    slot_fail_24h: Counter[str] = Counter()

    for r in sess_24h.iter_rows(named=True):
        sid = str(r["station_id"])
        tz = st_tz_map.get(sid, "Asia/Ho_Chi_Minh")
        loc_hr = to_local(ensure_utc(r["started_at"]), tz).hour
        is_ok = str(r["final_status"]) == "COMPLETED"
        if is_ok:
            ok_by_hour[loc_hr] += 1
        else:
            fail_by_hour[loc_hr] += 1

        st_tot_24h[sid] += 1
        if not is_ok:
            st_fail_24h[sid] += 1

        cab = r.get("cabinet_id")
        if cab:
            cab_str = str(cab)
            cab_tot_24h[cab_str] += 1
            if not is_ok:
                cab_fail_24h[cab_str] += 1
            for s_col in ("slot_in", "slot_out"):
                sl = r.get(s_col)
                if sl:
                    slot_key = f"{cab_str}/{sl}"
                    slot_tot_24h[slot_key] += 1
                    if not is_ok:
                        slot_fail_24h[slot_key] += 1

    hourly_points = [
        HourlySwapPoint(
            hour_local=f"{h:02d}:00",
            ok_count=ok_by_hour.get(h, 0),
            failed_count=fail_by_hour.get(h, 0),
        )
        for h in range(24)
    ]

    def _top5(fail_counter: Counter[str], tot_counter: Counter[str]) -> list[HotspotItem]:
        items: list[HotspotItem] = []
        for eid, f_cnt in fail_counter.most_common(5):
            if f_cnt <= 0:
                continue
            st_id = eid[:9] if len(eid) >= 9 and "-ST-" in eid else eid
            t_cnt = tot_counter.get(eid, f_cnt)
            items.append(
                HotspotItem(
                    entity_id=eid,
                    station_id=st_id,
                    country=st_country_map.get(st_id, "VN"),
                    failed_sessions=f_cnt,
                    total_sessions=t_cnt,
                    fail_rate=round(f_cnt / t_cnt, 4) if t_cnt > 0 else 0.0,
                )
            )
        return items

    temp_ts = build_cabinet_temperature_timeseries(
        data,
        station_id=None,
        hours=1,
        as_of=eff_as_of,
        country=country,
        settings=cfg,
        knowledge=k,
        profiles=profiles,
    )
    ready_ts = build_ready_batteries_timeseries(
        data,
        hours=6,
        step_minutes=15,
        as_of=eff_as_of,
        country=country,
        settings=cfg,
        knowledge=k,
        profiles=profiles,
    )
    alarms_resp = build_alarms_page(
        data,
        as_of=eff_as_of,
        country=country,
        settings=cfg,
        knowledge=k,
        notifier=notif,
        profiles=profiles,
    )

    return OverviewResponse(
        meta=meta,
        kpi_total=kpi_total,
        kpi_by_country=kpi_by_country,
        stations_map=map_items,
        hourly_swaps_24h=hourly_points,
        hotspots_24h=Hotspots24h(
            top_stations=_top5(st_fail_24h, st_tot_24h),
            top_cabinets=_top5(cab_fail_24h, cab_tot_24h),
            top_slots=_top5(slot_fail_24h, slot_tot_24h),
        ),
        ready_battery_note=READY_BATTERY_APPROX_NOTE,
        temperature_timeseries=temp_ts,
        ready_batteries_timeseries=ready_ts,
        alarms=alarms_resp,
    )


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
    by_code = k.error_catalog.by_code
    cab_online_map = compute_cabinet_online_map(
        data.cabinets,
        data.cabinet_telemetry,
        eff_as_of,
        online_window_seconds=cfg.cabinet_online_window_seconds,
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

            recent_err: str | None = None
            if ev_info is not None and str(ev_info.get("status")) in ("FAILED", "TIMEOUT"):
                recent_err = (
                    str(ev_info["error_code"])
                    if ev_info.get("error_code")
                    else last_fail_code_by_slot.get(key, "STEP_FAILED")
                )
            elif f_24h > 0:
                recent_err = last_fail_code_by_slot.get(key, "SLOT_DOOR_JAM")

            recent_desc = (
                by_code[recent_err].description if recent_err and recent_err in by_code else None
            )

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
            if gap_s >= float(cfg.cabinet_online_window_seconds):
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


def build_sessions_page(
    data: ProfileData,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    window_hours: int = 24,
    status: str | None = None,
    station_id: str | None = None,
    step: Step | None = None,
    limit: int = 100,
    knowledge: Knowledge | None = None,
    profiles: list[str] | None = None,
) -> SessionsPageResponse:
    k = knowledge or default_knowledge()
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    eff_as_of = meta.as_of
    chain = step_chain(k.state_machine)
    step_order = {s.value: i for i, s in enumerate(chain)}

    stations_df = data.stations.filter(pl.col("country") == country) if country else data.stations
    allowed_stations = [str(s) for s in stations_df["station_id"].sort().to_list()]
    st_meta = {
        str(r["station_id"]): (str(r["country"]), str(r["timezone"]))
        for r in data.stations.iter_rows(named=True)
    }

    win_start = eff_as_of - timedelta(hours=max(1, window_hours))
    ev_win = data.swap_events.filter(
        (pl.col("occurred_at") > win_start)
        & (pl.col("occurred_at") <= eff_as_of)
        & pl.col("station_id").is_in(allowed_stations)
    )
    if station_id:
        ev_win = ev_win.filter(pl.col("station_id") == station_id)

    funnel = compute_funnel(ev_win, eff_as_of, k)

    # Ma trận bước × mã lỗi (FAILED / TIMEOUT)
    fail_ev = ev_win.filter(pl.col("status").is_in(["FAILED", "TIMEOUT"])).with_columns(
        pl.col("error_code").fill_null("NO_CODE").alias("_code_key")
    )
    cell_counts: dict[tuple[str, str], int] = {}
    code_totals: Counter[str] = Counter()
    if fail_ev.height > 0:
        for r in (
            fail_ev.group_by(["step", "_code_key"]).agg(pl.len().alias("cnt")).iter_rows(named=True)
        ):
            st_k = str(r["step"])
            c_k = str(r["_code_key"])
            cnt = int(r["cnt"])
            cell_counts[(st_k, c_k)] = cnt
            code_totals[c_k] += cnt

    sorted_codes = [c for c, _ in code_totals.most_common()]
    if "NO_CODE" not in sorted_codes and any(k_pair[1] == "NO_CODE" for k_pair in cell_counts):
        sorted_codes.append("NO_CODE")

    error_labels = {c: ("không có mã" if c == "NO_CODE" else c) for c in sorted_codes}
    matrix_cells: list[MatrixCell] = []
    for st_enum in chain:
        for c_k in sorted_codes:
            cnt = cell_counts.get((st_enum.value, c_k), 0)
            matrix_cells.append(
                MatrixCell(
                    step=st_enum,
                    error_code=c_k,
                    error_label=error_labels[c_k],
                    count=cnt,
                )
            )

    error_matrix = StepErrorMatrix(
        steps=chain,
        step_labels={s.value: STEP_VI_LABELS.get(s, s.value) for s in chain},
        error_codes=sorted_codes,
        error_labels=error_labels,
        cells=matrix_cells,
    )

    # Latency p50/p95 theo bước kèm đường SLA
    lat_stats: list[StepLatencyStat] = []
    for st_enum in chain:
        st_ev = ev_win.filter(
            (pl.col("step") == st_enum.value) & pl.col("latency_ms").is_not_null()
        )
        cnt = st_ev.height
        if cnt > 0:
            s_lat = st_ev["latency_ms"]
            p50_raw = s_lat.median()
            p95_raw = s_lat.quantile(0.95)
            p50 = round(float(p50_raw), 1) if isinstance(p50_raw, (int, float)) else None
            p95 = round(float(p95_raw), 1) if isinstance(p95_raw, (int, float)) else None
        else:
            p50, p95 = None, None
        sla_s = k.state_machine.sla(st_enum)
        lat_stats.append(
            StepLatencyStat(
                step=st_enum,
                step_label=STEP_VI_LABELS.get(st_enum, st_enum.value),
                count=cnt,
                p50_ms=p50,
                p95_ms=p95,
                sla_ms=sla_s * 1000 if sla_s is not None else None,
            )
        )

    # Phiên đang treo
    ev_all_allowed = data.swap_events.filter(
        (pl.col("occurred_at") <= eff_as_of) & pl.col("station_id").is_in(allowed_stations)
    )
    if station_id:
        ev_all_allowed = ev_all_allowed.filter(pl.col("station_id") == station_id)
    stuck_sessions = compute_stuck_sessions(
        ev_all_allowed, data.stations, data.payments, eff_as_of, k
    )

    # Bảng phiên gần nhất
    sess_df = data.swap_sessions.filter(
        (pl.col("started_at") <= eff_as_of) & pl.col("station_id").is_in(allowed_stations)
    )
    if station_id:
        sess_df = sess_df.filter(pl.col("station_id") == station_id)
    if status:
        sess_df = sess_df.filter(pl.col("final_status") == status)

    # Tìm bước cuối và error_code từ swap_events cho các phiên ứng viên
    candidate_df = sess_df.sort("started_at", descending=True).head(max(limit * 4, 200))
    cand_sids = [str(s) for s in candidate_df["session_id"].to_list()]
    cand_ev = data.swap_events.filter(
        pl.col("session_id").is_in(cand_sids) & (pl.col("occurred_at") <= eff_as_of)
    )
    last_ev_by_sid: dict[str, tuple[Step, str | None]] = {}
    for r in cand_ev.iter_rows(named=True):
        sid = str(r["session_id"])
        st_enum = Step(str(r["step"]))
        err_c = str(r["error_code"]) if r.get("error_code") else None
        prev = last_ev_by_sid.get(sid)
        if prev is None or step_order[st_enum.value] >= step_order[prev[0].value]:
            last_ev_by_sid[sid] = (st_enum, err_c or prev[1] if prev else err_c)

    recent_rows: list[SessionSummaryRow] = []
    for r in candidate_df.iter_rows(named=True):
        sid = str(r["session_id"])
        last_step, err_code = last_ev_by_sid.get(sid, (Step.OPEN_APP, None))
        if step is not None and last_step is not step:
            continue
        st_id = str(r["station_id"])
        c_code, tz = st_meta.get(st_id, ("VN", "Asia/Ho_Chi_Minh"))
        started_at = ensure_utc(r["started_at"])
        ended_raw = r.get("ended_at")
        ended_at = ensure_utc(ended_raw) if isinstance(ended_raw, datetime) else None
        if ended_at is not None and ended_at > eff_as_of:
            ended_at = None
        dur = round((ended_at - started_at).total_seconds(), 2) if ended_at else None

        recent_rows.append(
            SessionSummaryRow(
                session_id=sid,
                user_id=str(r["user_id"]),
                station_id=st_id,
                country=c_code,
                timezone=tz,
                cabinet_id=str(r["cabinet_id"]) if r.get("cabinet_id") else None,
                slot_in=str(r["slot_in"]) if r.get("slot_in") else None,
                slot_out=str(r["slot_out"]) if r.get("slot_out") else None,
                battery_in_id=str(r["battery_in_id"]) if r.get("battery_in_id") else None,
                battery_out_id=str(r["battery_out_id"]) if r.get("battery_out_id") else None,
                app_version=str(r["app_version"]),
                payment_provider=str(r["payment_provider"]) if r.get("payment_provider") else None,
                final_status=str(r["final_status"]),
                last_step=last_step,
                error_code=err_code,
                started_at_utc=started_at,
                started_at_local=format_local(started_at, tz),
                ended_at_utc=ended_at,
                ended_at_local=format_local(ended_at, tz) if ended_at else None,
                duration_seconds=dur,
            )
        )
        if len(recent_rows) >= limit:
            break

    return SessionsPageResponse(
        meta=meta,
        window_hours=window_hours,
        filter_status=status,
        filter_station_id=station_id,
        filter_step=step,
        available_stations=allowed_stations,
        funnel=funnel,
        error_matrix=error_matrix,
        step_latency=lat_stats,
        stuck_sessions=stuck_sessions,
        recent_sessions=recent_rows,
    )


def build_session_detail(
    data: ProfileData,
    session_id: str,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    knowledge: Knowledge | None = None,
    profiles: list[str] | None = None,
) -> SessionDetailResponse:
    k = knowledge or default_knowledge()
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)

    sess_df = data.swap_sessions.filter(pl.col("session_id") == session_id)
    if sess_df.height == 0:
        raise KeyError(session_id)
    s_row = sess_df.row(0, named=True)
    st_id = str(s_row["station_id"])
    st_df = data.stations.filter(pl.col("station_id") == st_id)
    if st_df.height > 0:
        st_row = st_df.row(0, named=True)
        c_code = str(st_row["country"])
        tz = str(st_row["timezone"])
    else:
        c_code, tz = "VN", "Asia/Ho_Chi_Minh"

    user_id = str(s_row["user_id"])
    u_df = data.users.filter(pl.col("user_id") == user_id)
    pkg_type = str(u_df.row(0, named=True)["package_type"]) if u_df.height > 0 else None

    ev_df = data.swap_events.filter(pl.col("session_id") == session_id).sort(
        ["occurred_at", "event_id"]
    )
    by_code = k.error_catalog.by_code
    events_items: list[SessionEventItem] = []
    last_step = Step.OPEN_APP
    last_err_code: str | None = None

    for r in ev_df.iter_rows(named=True):
        st_enum = Step(str(r["step"]))
        last_step = st_enum
        occ = ensure_utc(r["occurred_at"])
        err_c = str(r["error_code"]) if r.get("error_code") else None
        if err_c:
            last_err_code = err_c
        err_desc = by_code[err_c].description if err_c and err_c in by_code else None
        err_msg = str(r["error_message"]) if r.get("error_message") is not None else None
        lat_raw = r.get("latency_ms")
        lat_ms = int(lat_raw) if isinstance(lat_raw, int) else None

        events_items.append(
            SessionEventItem(
                event_id=str(r["event_id"]),
                step=st_enum,
                step_label=STEP_VI_LABELS.get(st_enum, st_enum.value),
                service=k.state_machine.steps[st_enum].service,
                status=str(r["status"]),
                occurred_at_utc=occ,
                occurred_at_local=format_local(occ, tz),
                latency_ms=lat_ms,
                sla_seconds=k.state_machine.sla(st_enum),
                error_code=err_c,
                error_description=err_desc,
                error_message=err_msg,
            )
        )

    started_at = ensure_utc(s_row["started_at"])
    ended_raw = s_row.get("ended_at")
    ended_at = ensure_utc(ended_raw) if isinstance(ended_raw, datetime) else None
    dur = round((ended_at - started_at).total_seconds(), 2) if ended_at else None

    summary = SessionSummaryRow(
        session_id=session_id,
        user_id=user_id,
        station_id=st_id,
        country=c_code,
        timezone=tz,
        cabinet_id=str(s_row["cabinet_id"]) if s_row.get("cabinet_id") else None,
        slot_in=str(s_row["slot_in"]) if s_row.get("slot_in") else None,
        slot_out=str(s_row["slot_out"]) if s_row.get("slot_out") else None,
        battery_in_id=str(s_row["battery_in_id"]) if s_row.get("battery_in_id") else None,
        battery_out_id=str(s_row["battery_out_id"]) if s_row.get("battery_out_id") else None,
        app_version=str(s_row["app_version"]),
        payment_provider=(
            str(s_row["payment_provider"]) if s_row.get("payment_provider") else None
        ),
        final_status=str(s_row["final_status"]),
        last_step=last_step,
        error_code=last_err_code,
        started_at_utc=started_at,
        started_at_local=format_local(started_at, tz),
        ended_at_utc=ended_at,
        ended_at_local=format_local(ended_at, tz) if ended_at else None,
        duration_seconds=dur,
    )

    pay_detail: SessionPaymentDetail | None = None
    pay_df = data.payments.filter(pl.col("session_id") == session_id)
    if pay_df.height > 0:
        p_row = pay_df.row(0, named=True)
        amt = float(p_row["amount"])
        c_spec = k.countries.countries.get(c_code)
        exp_amt = (
            float(c_spec.pricing.standard_swap_amount(pkg_type))
            if c_spec and pkg_type and pkg_type in c_spec.pricing.packages
            else None
        )
        cb_raw = p_row.get("callback_at")
        cb_local = format_local(ensure_utc(cb_raw), tz) if isinstance(cb_raw, datetime) else None
        pay_detail = SessionPaymentDetail(
            payment_id=str(p_row["payment_id"]),
            amount=amt,
            expected_amount=exp_amt,
            amount_mismatch=bool(exp_amt is not None and abs(amt - exp_amt) > 1e-6),
            currency=str(p_row["currency"]),
            status=str(p_row["status"]),
            captured=bool(p_row["captured"]),
            payment_provider=summary.payment_provider,
            callback_at_local=cb_local,
        )

    tel_items: list[SessionTelemetryItem] = []
    if summary.cabinet_id:
        t_start = started_at - timedelta(minutes=2)
        t_end = (ended_at or started_at) + timedelta(minutes=2)
        rel_slots = [s for s in (summary.slot_in, summary.slot_out) if s is not None]
        tel_df = data.cabinet_telemetry.filter(
            (pl.col("cabinet_id") == summary.cabinet_id)
            & (pl.col("heartbeat_at") >= t_start)
            & (pl.col("heartbeat_at") <= t_end)
            & (pl.col("slot_id").is_null() | pl.col("slot_id").is_in(rel_slots))
        ).sort("heartbeat_at")
        for r in tel_df.iter_rows(named=True):
            hb_at = ensure_utc(r["heartbeat_at"])
            soc_r = r.get("soc")
            tmp_r = r.get("temperature")
            tel_items.append(
                SessionTelemetryItem(
                    cabinet_id=str(r["cabinet_id"]),
                    slot_id=str(r["slot_id"]) if r.get("slot_id") else None,
                    door_state=str(r["door_state"]) if r.get("door_state") else None,
                    battery_detected=(
                        bool(r["battery_detected"])
                        if r.get("battery_detected") is not None
                        else None
                    ),
                    soc=round(float(soc_r), 1) if isinstance(soc_r, (int, float)) else None,
                    temperature=(
                        round(float(tmp_r), 1) if isinstance(tmp_r, (int, float)) else None
                    ),
                    hw_error_code=str(r["hw_error_code"]) if r.get("hw_error_code") else None,
                    heartbeat_at_utc=hb_at,
                    heartbeat_at_local=format_local(hb_at, tz),
                )
            )

    return SessionDetailResponse(
        meta=meta,
        session=summary,
        user_package_type=pkg_type,
        events=events_items,
        payment=pay_detail,
        telemetry=tel_items,
    )


def build_payments_page(
    data: ProfileData,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    knowledge: Knowledge | None = None,
    profiles: list[str] | None = None,
) -> PaymentsPageResponse:
    k = knowledge or default_knowledge()
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    eff_as_of = meta.as_of

    stations_df = data.stations.filter(pl.col("country") == country) if country else data.stations
    allowed_stations = [str(s) for s in stations_df["station_id"].to_list()]
    sess_upto = data.swap_sessions.filter(
        (pl.col("started_at") <= eff_as_of) & pl.col("station_id").is_in(allowed_stations)
    )

    joined = data.payments.join(
        sess_upto.select(["session_id", "station_id", "payment_provider", "started_at"]),
        on="session_id",
        how="inner",
    ).join(
        data.stations.select(["station_id", "country", "timezone"]),
        on="station_id",
        how="left",
    )

    # Đếm các phiên đang PENDING ở bước 5 tại `as_of`
    ev_step5 = data.swap_events.filter(
        (pl.col("occurred_at") <= eff_as_of)
        & (pl.col("step") == Step.PAYMENT.value)
        & pl.col("station_id").is_in(allowed_stations)
    )
    pending_by_provider: Counter[str] = Counter()
    if ev_step5.height > 0:
        step5_last = ev_step5.group_by("session_id").agg(pl.col("status").last().alias("st"))
        pend_sids = step5_last.filter(pl.col("st") == "PENDING").select("session_id")
        if pend_sids.height > 0:
            pend_joined = pend_sids.join(
                sess_upto.select(["session_id", "payment_provider"]),
                on="session_id",
                how="inner",
            )
            for r in pend_joined.iter_rows(named=True):
                if r.get("payment_provider"):
                    pending_by_provider[str(r["payment_provider"])] += 1

    provider_stats: list[ProviderPaymentStat] = []
    if joined.height > 0:
        for r in (
            joined.filter(pl.col("payment_provider").is_not_null())
            .group_by(["payment_provider", "country", "currency"])
            .agg(
                pl.len().alias("total"),
                (pl.col("status") == "SUCCESS").sum().alias("s_ok"),
                (pl.col("status") == "FAILED").sum().alias("s_fail"),
                (pl.col("status") == "TIMEOUT").sum().alias("s_to"),
                (pl.col("status") == "DECLINED").sum().alias("s_dec"),
                (pl.col("status") == "CANCELLED").sum().alias("s_can"),
                (pl.col("status") == "VOIDED").sum().alias("s_void"),
            )
            .sort(["country", "payment_provider"])
            .iter_rows(named=True)
        ):
            prov = str(r["payment_provider"])
            tot = int(r["total"])
            s_ok = int(r["s_ok"])
            s_fail = int(r["s_fail"])
            s_to = int(r["s_to"])
            s_dec = int(r["s_dec"])
            s_can = int(r["s_can"])
            s_void = int(r["s_void"])
            provider_stats.append(
                ProviderPaymentStat(
                    payment_provider=prov,
                    country=str(r["country"]),
                    currency=str(r["currency"]),
                    total_tx=tot,
                    success_count=s_ok,
                    failed_count=s_fail,
                    timeout_count=s_to,
                    declined_count=s_dec,
                    cancelled_count=s_can,
                    voided_count=s_void,
                    pending_count=pending_by_provider.get(prov, 0),
                    success_rate=round(s_ok / tot, 4) if tot else 0.0,
                    failed_rate=round(s_fail / tot, 4) if tot else 0.0,
                    timeout_rate=round(s_to / tot, 4) if tot else 0.0,
                    declined_rate=round(s_dec / tot, 4) if tot else 0.0,
                    cancelled_rate=round(s_can / tot, 4) if tot else 0.0,
                    voided_rate=round(s_void / tot, 4) if tot else 0.0,
                )
            )

    # Biểu đồ timeout theo thời gian của từng provider (bucket 1h)
    timeout_points: list[ProviderTimeoutPoint] = []
    if joined.height > 0:
        bucketed = (
            joined.filter(pl.col("payment_provider").is_not_null())
            .with_columns(pl.col("started_at").dt.truncate("1h").alias("bucket"))
            .group_by(["bucket", "payment_provider", "country"])
            .agg(
                (pl.col("status") == "TIMEOUT").sum().alias("to_cnt"),
                pl.len().alias("tot_cnt"),
            )
            .filter(pl.col("to_cnt") > 0)
            .sort(["bucket", "payment_provider"])
        )
        for r in bucketed.iter_rows(named=True):
            b_dt = ensure_utc(r["bucket"])
            timeout_points.append(
                ProviderTimeoutPoint(
                    bucket_utc=b_dt,
                    bucket_label=f"{b_dt:%Y-%m-%d %H:%M} UTC",
                    payment_provider=str(r["payment_provider"]),
                    country=str(r["country"]),
                    timeout_count=int(r["to_cnt"]),
                    total_count=int(r["tot_cnt"]),
                )
            )

    reconcil = compute_reconciliation(
        sess_upto,
        data.payments,
        data.swap_events,
        data.users,
        stations_df,
        eff_as_of,
        k,
    )

    # Doanh thu theo ngày địa phương, tách theo currency
    daily_rev_map: Counter[tuple[str, str, str]] = Counter()
    daily_cnt_map: Counter[tuple[str, str, str]] = Counter()
    if joined.height > 0:
        cap_rows = joined.filter(pl.col("captured") == True)  # noqa: E712
        for r in cap_rows.iter_rows(named=True):
            tz = str(r.get("timezone") or "Asia/Ho_Chi_Minh")
            loc_d = to_local(ensure_utc(r["started_at"]), tz).strftime("%Y-%m-%d")
            key = (loc_d, str(r["country"]), str(r["currency"]))
            daily_rev_map[key] += float(r["amount"])  # type: ignore[assignment]
            daily_cnt_map[key] += 1

    daily_rev = [
        DailyRevenueRow(
            local_date=d,
            country=c_code,
            currency=curr,
            revenue=round(float(rev), 2),
            captured_tx_count=daily_cnt_map[(d, c_code, curr)],
        )
        for (d, c_code, curr), rev in sorted(daily_rev_map.items())
    ]

    return PaymentsPageResponse(
        meta=meta,
        by_provider=provider_stats,
        timeout_series=timeout_points,
        reconciliation_issues=reconcil,
        daily_revenue=daily_rev,
    )


def build_batteries_page(
    data: ProfileData,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    knowledge: Knowledge | None = None,
    profiles: list[str] | None = None,
) -> BatteriesPageResponse:
    k = knowledge or default_knowledge()
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    eff_as_of = meta.as_of
    thresholds = k.error_catalog.thresholds

    # 1. Phân bố SOH theo lot_id (box plot)
    soh_boxes: list[LotSohBox] = []
    if data.batteries.height > 0:
        for r in (
            data.batteries.group_by("lot_id")
            .agg(
                pl.len().alias("cnt"),
                pl.col("soh").min().alias("min_v"),
                pl.col("soh").quantile(0.25).alias("q1_v"),
                pl.col("soh").median().alias("med_v"),
                pl.col("soh").quantile(0.75).alias("q3_v"),
                pl.col("soh").max().alias("max_v"),
                pl.col("soh").mean().alias("mean_v"),
            )
            .sort("lot_id")
            .iter_rows(named=True)
        ):
            mean_v = round(float(r["mean_v"]), 2)
            soh_boxes.append(
                LotSohBox(
                    lot_id=str(r["lot_id"]),
                    battery_count=int(r["cnt"]),
                    min_soh=round(float(r["min_v"]), 1),
                    q1_soh=round(float(r["q1_v"]), 1),
                    median_soh=round(float(r["med_v"]), 1),
                    q3_soh=round(float(r["q3_v"]), 1),
                    max_soh=round(float(r["max_v"]), 1),
                    mean_soh=mean_v,
                    is_low_soh_lot=mean_v < thresholds.low_soh_pct,
                )
            )

    stations_df = data.stations.filter(pl.col("country") == country) if country else data.stations
    allowed_stations = [str(s) for s in stations_df["station_id"].to_list()]
    cabs_df = data.cabinets.filter(pl.col("station_id").is_in(allowed_stations))
    allowed_cabs = [str(c) for c in cabs_df["cabinet_id"].to_list()]

    # 2. SOC lúc xuất pin (các dòng telemetry có soc >= 0 trên khoang xuất hoặc toàn bộ khoang)
    tel_upto = data.cabinet_telemetry.filter(
        pl.col("cabinet_id").is_in(allowed_cabs) & (pl.col("heartbeat_at") <= eff_as_of)
    )
    sess_upto = data.swap_sessions.filter(
        (pl.col("started_at") <= eff_as_of) & pl.col("station_id").is_in(allowed_stations)
    )

    # Lấy đọc SOC lúc mở khoang xuất pin (door_state == "OPEN" và battery_detected == True)
    soc_out_df = tel_upto.filter(
        (pl.col("door_state") == "OPEN")
        & (pl.col("battery_detected") == True)  # noqa: E712
        & pl.col("soc").is_not_null()
    )
    soc_vals = [float(x) for x in soc_out_df["soc"].to_list()] if soc_out_df.height > 0 else []
    bins_edges = [(0, 10), (10, 20), (20, 40), (40, 60), (60, 80), (80, 90), (90, 95), (95, 101)]
    hist_bins: list[SocHistogramBin] = []
    for lo, hi in bins_edges:
        cnt = sum(1 for v in soc_vals if lo <= v < hi)
        label = f"{lo}–100%" if hi == 101 else f"{lo}–{hi}%"
        hist_bins.append(
            SocHistogramBin(
                bin_label=label,
                bin_min=float(lo),
                bin_max=float(min(100, hi)),
                count=cnt,
                is_below_threshold=float(hi) <= thresholds.low_soc_pct,
            )
        )
    low_soc_cnt = sum(1 for v in soc_vals if v < thresholds.low_soc_pct)

    # 3. Danh sách pin SOH thấp đã được xuất cho khách
    low_bats = data.batteries.filter(pl.col("soh") < thresholds.low_soh_pct)
    low_issued_rows: list[LowSohIssuedRow] = []
    if low_bats.height > 0 and sess_upto.height > 0:
        issued_df = (
            sess_upto.filter(pl.col("battery_out_id").is_not_null())
            .join(low_bats, left_on="battery_out_id", right_on="battery_id", how="inner")
            .join(
                data.stations.select(["station_id", "country", "timezone"]),
                on="station_id",
                how="left",
            )
            .sort("started_at", descending=True)
        )
        for r in issued_df.iter_rows(named=True):
            sid = str(r["session_id"])
            tz = str(r.get("timezone") or "Asia/Ho_Chi_Minh")
            started_at = ensure_utc(r["started_at"])
            low_issued_rows.append(
                LowSohIssuedRow(
                    session_id=sid,
                    station_id=str(r["station_id"]),
                    country=str(r.get("country") or "VN"),
                    cabinet_id=str(r["cabinet_id"]) if r.get("cabinet_id") else None,
                    slot_out=str(r["slot_out"]) if r.get("slot_out") else None,
                    battery_id=str(r["battery_out_id"]),
                    lot_id=str(r["lot_id"]),
                    soh=round(float(r["soh"]), 1),
                    soc_out=None,
                    final_status=str(r["final_status"]),
                    error_code="BAT_LOW_SOH",
                    started_at_local=format_local(started_at, tz),
                )
            )

    # 4. Nhiệt độ cao nhất theo tủ + cảnh báo mức 1 (>55°C) / mức 2 (>60°C)
    cab_temp_rows: list[CabinetTempRow] = []
    if tel_upto.height > 0:
        agg_temp = (
            tel_upto.filter(pl.col("temperature").is_not_null())
            .group_by("cabinet_id")
            .agg(
                pl.col("temperature").max().alias("max_t"),
                pl.col("temperature").last().alias("last_t"),
                (pl.col("temperature") > thresholds.temp_warn_1_c).sum().alias("w1_cnt"),
                (pl.col("temperature") > thresholds.temp_warn_2_c).sum().alias("w2_cnt"),
                pl.col("hw_error_code").drop_nulls().unique().alias("hw_codes"),
            )
            .join(
                cabs_df.select(["cabinet_id", "station_id", "country"]),
                on="cabinet_id",
                how="inner",
            )
            .sort("cabinet_id")
        )
        for r in agg_temp.iter_rows(named=True):
            max_t = round(float(r["max_t"]), 1)
            last_t = round(float(r["last_t"]), 1) if r.get("last_t") is not None else None
            hw_list = sorted(str(x) for x in (r.get("hw_codes") or []) if x)
            cab_temp_rows.append(
                CabinetTempRow(
                    cabinet_id=str(r["cabinet_id"]),
                    station_id=str(r["station_id"]),
                    country=str(r["country"]),
                    max_temp_c=max_t,
                    latest_temp_c=last_t,
                    exceeds_warn_1=max_t > thresholds.temp_warn_1_c,
                    exceeds_warn_2=max_t > thresholds.temp_warn_2_c,
                    warn_1_events=int(r["w1_cnt"]),
                    warn_2_events=int(r["w2_cnt"]),
                    hw_error_codes=hw_list,
                )
            )

    return BatteriesPageResponse(
        meta=meta,
        low_soh_threshold_pct=thresholds.low_soh_pct,
        low_soc_threshold_pct=thresholds.low_soc_pct,
        temp_warn_1_c=thresholds.temp_warn_1_c,
        temp_warn_2_c=thresholds.temp_warn_2_c,
        soh_by_lot=soh_boxes,
        soc_out_histogram=hist_bins,
        low_soc_out_count=low_soc_cnt,
        total_soc_out_readings=len(soc_vals),
        low_soh_issued=low_issued_rows,
        cabinet_temperatures=cab_temp_rows,
    )


def build_alerts_page(
    data: ProfileData,
    notifier: NotifierSnapshot,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    knowledge: Knowledge | None = None,
    profiles: list[str] | None = None,
) -> AlertsPageResponse:
    k = knowledge or default_knowledge()
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    ch_name_map: dict[str, str] = {}
    for route in k.routing.routes.values():
        if route.team:
            ch_name_map[route.team.lower().replace(" ", "-")] = route.team
    for ov in k.routing.country_overrides:
        ch_name_map[ov.team.lower().replace(" ", "-")] = ov.team

    first_sid_by_inc: dict[str, str] = {}
    cards_items: list[TeamsCardItem] = []
    by_team: Counter[str] = Counter()
    by_cat: Counter[str] = Counter()
    by_sev: Counter[str] = Counter()

    for row in notifier.cards:
        inc_id = str(row["incident_id"])
        rev = int(str(row["revision"]))
        ch_slug = str(row["channel"])
        team_name = ch_name_map.get(ch_slug, ch_slug)
        raw_json = str(row.get("card_json", "{}"))
        first_sid, cat, sev = extract_session_from_card_json(raw_json)
        if first_sid:
            first_sid_by_inc[inc_id] = first_sid

        fb = notifier.latest_feedback.get(inc_id)
        fb_act = fb[0] if fb else None
        fb_lbl = FEEDBACK_ACTION_LABELS.get(fb_act, fb_act) if fb_act else None
        fb_at = fb[1] if fb else None

        by_team[team_name] += 1
        if cat:
            by_cat[cat] += 1
        if sev:
            by_sev[sev] += 1

        cards_items.append(
            TeamsCardItem(
                incident_id=inc_id,
                revision=rev,
                channel=ch_slug,
                channel_name=team_name,
                team=team_name,
                category=cat,
                severity=sev,
                received_at=str(row["received_at"]),
                first_session_id=first_sid,
                latest_feedback_action=fb_act,
                latest_feedback_label=fb_lbl,
                latest_feedback_at=fb_at,
                teams_link=f"/?channel={ch_slug}",
                session_link=(
                    f"/dashboard/sessions/{first_sid}?profile={data.profile}" if first_sid else None
                ),
            )
        )

    outbox_items: list[OutboxItem] = []
    for row in notifier.outbox:
        key_str = str(row["key"])
        inc_id = str(row["incident_id"])
        parts = key_str.split(":")
        mode = parts[0] if len(parts) == 4 and parts[0] in ("dry_run", "live") else "dry_run"
        outbox_items.append(
            OutboxItem(
                key=key_str,
                incident_id=inc_id,
                revision=int(str(row["revision"])),
                channel=str(row["channel"]),
                mode=mode,
                status=str(row["status"]),
                attempts=int(str(row["attempts"])),
                last_error=str(row["last_error"]) if row.get("last_error") else None,
                created_at=str(row["created_at"]),
                sent_at=str(row["sent_at"]) if row.get("sent_at") else None,
                first_session_id=first_sid_by_inc.get(inc_id),
            )
        )

    fb_items = [
        FeedbackItem(
            incident_id=inc_id,
            action=act,
            action_label=FEEDBACK_ACTION_LABELS.get(act, act),
            at=at_str,
        )
        for inc_id, (act, at_str) in notifier.latest_feedback.items()
    ]
    fb_total = len(fb_items)
    wrong_cnt = sum(1 for f in fb_items if f.action == "wrong_category")
    wrong_rate = round(wrong_cnt / fb_total, 4) if fb_total > 0 else 0.0

    return AlertsPageResponse(
        meta=meta,
        stats=AlertStats(
            total_cards=len(cards_items),
            by_team=dict(by_team),
            by_category=dict(by_cat),
            by_severity=dict(by_sev),
            feedback_total=fb_total,
            wrong_category_count=wrong_cnt,
            wrong_category_rate=wrong_rate,
        ),
        outbox=outbox_items,
        cards=cards_items,
        latest_feedback=fb_items,
    )


def build_incidents_placeholder(
    data: ProfileData,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    knowledge: Knowledge | None = None,
    profiles: list[str] | None = None,
) -> IncidentsPlaceholderResponse:
    k = knowledge or default_knowledge()
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    return IncidentsPlaceholderResponse(meta=meta)


def build_metrics_placeholder(
    data: ProfileData,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    knowledge: Knowledge | None = None,
    profiles: list[str] | None = None,
) -> MetricsPlaceholderResponse:
    k = knowledge or default_knowledge()
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)
    return MetricsPlaceholderResponse(
        meta=meta,
        factors=[
            SuccessFactorSpec(sf_id=sid, name=name, kpi=kpi, target=target)
            for sid, name, kpi, target in SF_SPECS
        ],
    )


def build_ground_truth_page(
    data: ProfileData,
    gt_df: pl.DataFrame,
    *,
    as_of: datetime | None = None,
    country: str | None = None,
    knowledge: Knowledge | None = None,
    profiles: list[str] | None = None,
) -> GroundTruthPageResponse:
    """Chỉ dùng cho tab Kiểm chứng (mock) (`/dashboard/ground-truth`)."""
    k = knowledge or default_knowledge()
    meta = _build_meta(data, as_of, country, profiles or [data.profile], k)

    df = gt_df.filter(pl.col("country") == country) if country else gt_df
    total_sessions = df.height
    fail_df = df.filter(pl.col("is_failure") == True)  # noqa: E712
    failure_sessions = fail_df.height
    normal_sessions = total_sessions - failure_sessions

    counts_rows: list[GroundTruthCountRow] = []
    if fail_df.height > 0:
        grouped = (
            fail_df.with_columns(
                pl.col("category").fill_null(""),
                pl.col("scenario").fill_null("(background)"),
                pl.col("split").fill_null(""),
            )
            .group_by(["category", "scenario", "country", "split"])
            .agg(pl.len().alias("cnt"))
            .sort(["category", "scenario", "country", "split"])
        )
        for r in grouped.iter_rows(named=True):
            counts_rows.append(
                GroundTruthCountRow(
                    category=str(r["category"]),
                    scenario=str(r["scenario"]),
                    country=str(r["country"]),
                    split=str(r["split"]),
                    count=int(r["cnt"]),
                )
            )

    inst_items: list[ScenarioInstanceTimelineItem] = []
    inst_df = fail_df.filter(
        pl.col("scenario_instance_id").is_not_null() & (pl.col("scenario_instance_id") != "")
    )
    if inst_df.height > 0:
        inst_df = inst_df.with_columns(
            pl.col("scenario_start").str.to_datetime(time_zone="UTC", strict=False).alias("_start"),
            pl.col("injected_at").str.to_datetime(time_zone="UTC", strict=False).alias("_inj"),
        )
        for r in (
            inst_df.group_by("scenario_instance_id")
            .agg(
                pl.col("scenario").first().alias("scn"),
                pl.col("category").first().alias("cat"),
                pl.col("severity").first().alias("sev"),
                pl.col("error_code").drop_nulls().first().alias("code"),
                pl.col("country").unique().sort().alias("countries"),
                pl.col("split").first().alias("split"),
                pl.col("_start").min().alias("min_start"),
                pl.col("_inj").max().alias("max_inj"),
                pl.len().alias("cnt"),
            )
            .sort("min_start")
            .iter_rows(named=True)
        ):
            s_dt = r["min_start"]
            e_dt = r["max_inj"] or s_dt
            if isinstance(s_dt, datetime) and isinstance(e_dt, datetime):
                inst_items.append(
                    ScenarioInstanceTimelineItem(
                        scenario_instance_id=str(r["scenario_instance_id"]),
                        scenario=str(r["scn"] or ""),
                        category=str(r["cat"] or ""),
                        severity=str(r["sev"] or "P2"),
                        error_code=str(r["code"] or ""),
                        countries=[str(c) for c in (r["countries"] or [])],
                        split=str(r["split"] or ""),
                        scenario_start_utc=ensure_utc(s_dt),
                        last_injected_at_utc=ensure_utc(e_dt),
                        session_count=int(r["cnt"]),
                    )
                )

    return GroundTruthPageResponse(
        meta=meta,
        total_sessions=total_sessions,
        failure_sessions=failure_sessions,
        normal_sessions=normal_sessions,
        counts=counts_rows,
        scenario_instances=inst_items,
    )
