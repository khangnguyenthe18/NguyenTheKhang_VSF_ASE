"""Pydantic v2 response models cho Dashboard API và Jinja2 templates (DASHBOARD_PLAN §3, §5)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DashboardMeta(BaseModel):
    profile: str
    available_profiles: list[str]
    country: str | None = None
    available_countries: list[str]
    as_of: datetime
    as_of_clamped: bool = False
    min_time: datetime
    max_time: datetime


# --- §5.1 Tổng quan mạng lưới ------------------------------------------------


class StationMapItem(BaseModel):
    station_id: str
    country: str
    timezone: str
    timezone_offset: str
    lat: float
    lon: float
    status_color: Literal["green", "blue", "yellow", "orange", "red"]
    cabinets_online: int
    cabinets_total: int
    ready_batteries: int
    total_slots: int
    ready_battery_pct: float = 0.0
    swaps_1h: int
    failed_1h: int
    fail_rate_1h: float
    swaps_today: int
    failed_today: int
    fail_rate_today: float
    has_p1_alert: bool = False
    as_of_local: str
    max_temp_c: float | None = None


# --- Timeseries & Alarms models (DASHBOARD_UI_PLAN §3) ----------------------


class CabinetTempPoint(BaseModel):
    timestamp_utc: str
    timestamp_local: str
    temperature_c: float


class CabinetTempSeries(BaseModel):
    cabinet_id: str
    points: list[CabinetTempPoint]
    avg_c: float | None = None
    min_c: float | None = None
    max_c: float | None = None


class StationChoice(BaseModel):
    station_id: str
    label: str


class CabinetTemperatureTimeseriesResponse(BaseModel):
    meta: DashboardMeta
    station_id: str
    station_name: str
    timezone: str = "UTC"  # IANA của trạm, để JS hiển thị trục thời gian theo giờ địa phương
    hours: int
    warn_threshold_c: float
    critical_threshold_c: float
    series: list[CabinetTempSeries]
    available_stations: list[StationChoice]


class OverviewResponse(BaseModel):
    """Trang Bản đồ trạm: chỉ còn bản đồ."""

    meta: DashboardMeta
    stations_map: list[StationMapItem]


# --- §5.2 Trạm & Tủ ----------------------------------------------------------


class StationSummaryRow(BaseModel):
    station_id: str
    country: str
    timezone: str
    timezone_offset: str
    model_gen: str
    lat: float
    lon: float
    cabinets_total: int
    cabinets_online: int
    ready_batteries: int
    total_slots: int
    ready_battery_pct: float = 0.0
    swaps_today: int
    failed_today: int
    fail_rate_today: float
    last_heartbeat_at: datetime | None = None
    last_heartbeat_local: str | None = None
    status_color: Literal["green", "blue", "yellow", "orange", "red"]
    max_temp_c: float | None = None


class StationsListResponse(BaseModel):
    meta: DashboardMeta
    stations: list[StationSummaryRow]
    ready_battery_note: str


class SlotCell(BaseModel):
    slot_id: str
    cabinet_id: str
    door_state: str
    battery_detected: bool
    soc: float | None = None
    temperature: float | None = None
    hw_error_code: str | None = None
    recent_error_code: str | None = None
    recent_error_desc: str | None = None
    failed_24h: int = 0
    state_color: Literal["empty", "ready", "charging", "error", "overheat"]
    state_label: str
    assumed_initial: bool = False


class HeartbeatBucket(BaseModel):
    bucket_start_utc: datetime
    bucket_end_utc: datetime
    label_local: str
    heartbeat_count: int
    expected_count: int
    status: Literal["ok", "intermittent", "lost"]


class HeartbeatGap(BaseModel):
    start_utc: datetime
    end_utc: datetime
    start_local: str
    end_local: str
    gap_seconds: float


class CabinetDetail(BaseModel):
    cabinet_id: str
    station_id: str
    model_gen: str
    online: bool
    last_heartbeat_at: datetime | None = None
    last_heartbeat_local: str | None = None
    max_temp_24h: float | None = None
    slots: list[SlotCell]
    heartbeat_buckets_24h: list[HeartbeatBucket]
    heartbeat_gaps_24h: list[HeartbeatGap]


class StationDetailResponse(BaseModel):
    meta: DashboardMeta
    station: StationSummaryRow
    cabinets: list[CabinetDetail]
    ready_battery_note: str
    temperature_timeseries: CabinetTemperatureTimeseriesResponse | None = None


# --- Danh sách giao dịch đổi pin (màn hình hệ thống vận hành thật) -----------


class TransactionRow(BaseModel):
    """1 dòng/giao dịch; thời gian đã format theo giờ địa phương của trạm (dd/MM/yyyy HH:mm:ss)."""

    stt: int
    station_id: str
    cabinet_id: str | None = None
    cabinet_serial: str | None = None
    emsp_txn_id: str
    cabinet_txn_id: str | None = None
    started_local: str
    status: str  # CREATED | BATTERY_IN | COMPLETED | FAILED
    status_label: str
    error_code: str | None = None
    battery_in_id: str | None = None
    battery_out_id: str | None = None
    slot_in: str | None = None
    soh_in: float | None = None
    soc_in: int | None = None
    slot_out: str | None = None
    soh_out: float | None = None
    soc_out: int | None = None
    battery_in_local: str | None = None
    battery_out_local: str | None = None


class TransactionFiltersView(BaseModel):
    """Giá trị bộ lọc đang áp dụng, để điền lại vào form."""

    q: str = ""
    station_id: str = ""
    cabinet_id: str = ""
    cabinet_serial: str = ""
    emsp_txn_id: str = ""
    cabinet_txn_id: str = ""
    battery_serial: str = ""
    date_from: str = ""  # định dạng input datetime-local: YYYY-MM-DDTHH:MM
    date_to: str = ""
    statuses: list[str] = Field(default_factory=list)
    active_count: int = 0  # số tiêu chí đang lọc (hiện trên nút "Bộ lọc")


class TransactionsPageResponse(BaseModel):
    meta: DashboardMeta
    filters: TransactionFiltersView
    status_choices: list[tuple[str, str]]  # (giá trị, nhãn) theo thứ tự màn hình thật
    columns: list[tuple[str, str]]  # (khoá, tiêu đề cột)
    rows: list[TransactionRow]
    total: int
    page: int
    page_size: int
    total_pages: int
