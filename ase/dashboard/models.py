"""Pydantic v2 response models cho Dashboard API và Jinja2 templates (DASHBOARD_PLAN §3, §5)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ase.common.models import Step


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


class CountryKpi(BaseModel):
    country: str
    stations_online: int
    stations_total: int
    cabinets_online: int
    cabinets_total: int
    ready_batteries: int
    total_slots: int
    swaps_today: int
    completed_today: int
    ended_today: int
    success_rate_today: float | None = None
    swap_duration_median_s: float | None = None
    swap_duration_p95_s: float | None = None
    revenue_today_by_currency: dict[str, float] = Field(default_factory=dict)
    open_alerts_p1: int = 0
    open_alerts_p2: int = 0


class StationMapItem(BaseModel):
    station_id: str
    country: str
    timezone: str
    timezone_offset: str
    lat: float
    lon: float
    status_color: Literal["green", "yellow", "red"]
    cabinets_online: int
    cabinets_total: int
    ready_batteries: int
    total_slots: int
    swaps_1h: int
    failed_1h: int
    fail_rate_1h: float
    swaps_today: int
    failed_today: int
    fail_rate_today: float
    has_p1_alert: bool = False
    as_of_local: str


class HourlySwapPoint(BaseModel):
    hour_local: str
    ok_count: int
    failed_count: int


class HotspotItem(BaseModel):
    entity_id: str
    station_id: str
    country: str
    failed_sessions: int
    total_sessions: int
    fail_rate: float


class Hotspots24h(BaseModel):
    top_stations: list[HotspotItem]
    top_cabinets: list[HotspotItem]
    top_slots: list[HotspotItem]


class OverviewResponse(BaseModel):
    meta: DashboardMeta
    kpi_total: CountryKpi
    kpi_by_country: list[CountryKpi]
    stations_map: list[StationMapItem]
    hourly_swaps_24h: list[HourlySwapPoint]
    hotspots_24h: Hotspots24h
    ready_battery_note: str


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
    swaps_today: int
    failed_today: int
    fail_rate_today: float
    last_heartbeat_at: datetime | None = None
    last_heartbeat_local: str | None = None
    status_color: Literal["green", "yellow", "red"]


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


# --- §5.3 Giao dịch ----------------------------------------------------------


class FunnelStep(BaseModel):
    step: Step
    step_label: str
    service: str
    reached_count: int
    ok_count: int
    failed_count: int
    drop_count: int
    drop_rate_pct: float


class MatrixCell(BaseModel):
    step: Step
    error_code: str
    error_label: str
    count: int


class StepErrorMatrix(BaseModel):
    steps: list[Step]
    step_labels: dict[str, str]
    error_codes: list[str]
    error_labels: dict[str, str]
    cells: list[MatrixCell]


class StepLatencyStat(BaseModel):
    step: Step
    step_label: str
    count: int
    p50_ms: float | None = None
    p95_ms: float | None = None
    sla_ms: int | None = None


class StuckSessionItem(BaseModel):
    session_id: str
    station_id: str
    country: str
    cabinet_id: str | None = None
    last_completed_step: Step | None = None
    stuck_step: Step
    stuck_step_label: str
    wait_started_at_utc: datetime
    wait_started_at_local: str
    elapsed_seconds: float
    sla_seconds: int
    payment_captured: bool
    stale: bool = False  # treo quá lâu (> stale_after_seconds): lỗi im lặng chưa ai xử lý


class SessionSummaryRow(BaseModel):
    session_id: str
    user_id: str
    station_id: str
    country: str
    timezone: str
    cabinet_id: str | None = None
    slot_in: str | None = None
    slot_out: str | None = None
    battery_in_id: str | None = None
    battery_out_id: str | None = None
    app_version: str
    payment_provider: str | None = None
    final_status: str
    last_step: Step
    error_code: str | None = None
    started_at_utc: datetime
    started_at_local: str
    ended_at_utc: datetime | None = None
    ended_at_local: str | None = None
    duration_seconds: float | None = None


class SessionsPageResponse(BaseModel):
    meta: DashboardMeta
    window_hours: int
    filter_status: str | None = None
    filter_station_id: str | None = None
    filter_step: Step | None = None
    available_stations: list[str]
    funnel: list[FunnelStep]
    error_matrix: StepErrorMatrix
    step_latency: list[StepLatencyStat]
    stuck_sessions: list[StuckSessionItem]
    recent_sessions: list[SessionSummaryRow]


class SessionEventItem(BaseModel):
    event_id: str
    step: Step
    step_label: str
    service: str
    status: str
    occurred_at_utc: datetime
    occurred_at_local: str
    latency_ms: int | None = None
    sla_seconds: int | None = None
    error_code: str | None = None
    error_description: str | None = None
    error_message: str | None = None
    untrusted_badge: str = "dữ liệu chưa kiểm chứng"


class SessionPaymentDetail(BaseModel):
    payment_id: str
    amount: float
    expected_amount: float | None = None
    amount_mismatch: bool = False
    currency: str
    status: str
    captured: bool
    payment_provider: str | None = None
    callback_at_local: str | None = None


class SessionTelemetryItem(BaseModel):
    cabinet_id: str
    slot_id: str | None = None
    door_state: str | None = None
    battery_detected: bool | None = None
    soc: float | None = None
    temperature: float | None = None
    hw_error_code: str | None = None
    heartbeat_at_utc: datetime
    heartbeat_at_local: str


class SessionDetailResponse(BaseModel):
    meta: DashboardMeta
    session: SessionSummaryRow
    user_package_type: str | None = None
    events: list[SessionEventItem]
    payment: SessionPaymentDetail | None = None
    telemetry: list[SessionTelemetryItem]


# --- §5.4 Thanh toán ---------------------------------------------------------


class ProviderPaymentStat(BaseModel):
    payment_provider: str
    country: str
    currency: str
    total_tx: int
    success_count: int
    failed_count: int
    timeout_count: int
    declined_count: int
    cancelled_count: int
    voided_count: int
    pending_count: int
    success_rate: float
    failed_rate: float
    timeout_rate: float
    declined_rate: float
    cancelled_rate: float
    voided_rate: float


class ProviderTimeoutPoint(BaseModel):
    bucket_utc: datetime
    bucket_label: str
    payment_provider: str
    country: str
    timeout_count: int
    total_count: int


class ReconciliationRow(BaseModel):
    session_id: str
    payment_id: str
    station_id: str
    country: str
    user_id: str
    package_type: str
    payment_provider: str | None = None
    amount: float
    expected_amount: float
    currency: str
    payment_status: str
    captured: bool
    session_final_status: str
    has_step_6_or_7: bool
    issue_type: Literal["CAPTURED_WITHOUT_DELIVERY", "AMOUNT_MISMATCH"]
    issue_label: str
    started_at_local: str


class DailyRevenueRow(BaseModel):
    local_date: str
    country: str
    currency: str
    revenue: float
    captured_tx_count: int


class PaymentsPageResponse(BaseModel):
    meta: DashboardMeta
    by_provider: list[ProviderPaymentStat]
    timeout_series: list[ProviderTimeoutPoint]
    reconciliation_issues: list[ReconciliationRow]
    daily_revenue: list[DailyRevenueRow]


# --- §5.5 Pin ----------------------------------------------------------------


class LotSohBox(BaseModel):
    lot_id: str
    battery_count: int
    min_soh: float
    q1_soh: float
    median_soh: float
    q3_soh: float
    max_soh: float
    mean_soh: float
    is_low_soh_lot: bool


class SocHistogramBin(BaseModel):
    bin_label: str
    bin_min: float
    bin_max: float
    count: int
    is_below_threshold: bool


class LowSohIssuedRow(BaseModel):
    session_id: str
    station_id: str
    country: str
    cabinet_id: str | None = None
    slot_out: str | None = None
    battery_id: str
    lot_id: str
    soh: float
    soc_out: float | None = None
    final_status: str
    error_code: str | None = None
    started_at_local: str


class CabinetTempRow(BaseModel):
    cabinet_id: str
    station_id: str
    country: str
    max_temp_c: float
    latest_temp_c: float | None = None
    exceeds_warn_1: bool
    exceeds_warn_2: bool
    warn_1_events: int
    warn_2_events: int
    hw_error_codes: list[str]


class BatteriesPageResponse(BaseModel):
    meta: DashboardMeta
    low_soh_threshold_pct: float
    low_soc_threshold_pct: float
    temp_warn_1_c: float
    temp_warn_2_c: float
    soh_by_lot: list[LotSohBox]
    soc_out_histogram: list[SocHistogramBin]
    low_soc_out_count: int
    total_soc_out_readings: int
    low_soh_issued: list[LowSohIssuedRow]
    cabinet_temperatures: list[CabinetTempRow]


# --- §5.6 Cảnh báo -----------------------------------------------------------


class OutboxItem(BaseModel):
    key: str
    incident_id: str
    revision: int
    channel: str
    mode: str
    status: str
    attempts: int
    last_error: str | None = None
    created_at: str
    sent_at: str | None = None
    first_session_id: str | None = None


class TeamsCardItem(BaseModel):
    incident_id: str
    revision: int
    channel: str
    channel_name: str
    team: str
    category: str | None = None
    severity: str | None = None
    received_at: str
    first_session_id: str | None = None
    latest_feedback_action: str | None = None
    latest_feedback_label: str | None = None
    latest_feedback_at: str | None = None
    teams_link: str
    session_link: str | None = None


class FeedbackItem(BaseModel):
    incident_id: str
    action: str
    action_label: str
    at: str


class AlertStats(BaseModel):
    total_cards: int
    by_team: dict[str, int]
    by_category: dict[str, int]
    by_severity: dict[str, int]
    feedback_total: int
    wrong_category_count: int
    wrong_category_rate: float


class AlertsPageResponse(BaseModel):
    meta: DashboardMeta
    stats: AlertStats
    outbox: list[OutboxItem]
    cards: list[TeamsCardItem]
    latest_feedback: list[FeedbackItem]


# --- §5.7 Để chỗ sẵn & Kiểm chứng (mock) --------------------------------------


class IncidentsPlaceholderResponse(BaseModel):
    meta: DashboardMeta
    has_detector_data: bool = False
    message: str = "Chưa có dữ liệu detector"
    incidents: list[dict[str, object]] = Field(default_factory=list)


class SuccessFactorSpec(BaseModel):
    sf_id: str
    name: str
    kpi: str
    target: str
    rule_only_value: str | None = None
    rule_llm_value: str | None = None


class MetricsPlaceholderResponse(BaseModel):
    meta: DashboardMeta
    has_evaluator_data: bool = False
    message: str = "Chưa có dữ liệu evaluator"
    factors: list[SuccessFactorSpec]


class GroundTruthCountRow(BaseModel):
    category: str
    scenario: str
    country: str
    split: str
    count: int


class ScenarioInstanceTimelineItem(BaseModel):
    scenario_instance_id: str
    scenario: str
    category: str
    severity: str
    error_code: str
    countries: list[str]
    split: str
    scenario_start_utc: datetime
    last_injected_at_utc: datetime
    session_count: int


class GroundTruthPageResponse(BaseModel):
    meta: DashboardMeta
    banner_warning: str = "Chỉ có trong mock — OPS thật không thấy màn hình này"
    total_sessions: int
    failure_sessions: int
    normal_sessions: int
    counts: list[GroundTruthCountRow]
    scenario_instances: list[ScenarioInstanceTimelineItem]
