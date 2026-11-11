"""Loader Pydantic cho knowledge/*.yaml. Mọi catalog/route/quốc gia đọc từ YAML, không hardcode."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ase.common.models import AlertLevel, Category, Severity, Step

KNOWLEDGE_DIR = Path(__file__).resolve().parent


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _load_yaml(name: str, directory: Path) -> object:
    with (directory / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# --- state_machine.yaml -----------------------------------------------------


class StepSpec(_Strict):
    service: str
    sla_seconds: int | None
    next: Step | None


class StateMachine(_Strict):
    steps: dict[Step, StepSpec]
    status_transitions: dict[str, list[str]]
    terminal_statuses: list[str]

    @model_validator(mode="after")
    def _check(self) -> StateMachine:
        missing = set(Step) - set(self.steps)
        if missing:
            raise ValueError(f"state_machine thiếu bước: {sorted(missing)}")
        return self

    def sla(self, step: Step) -> int | None:
        return self.steps[step].sla_seconds

    def next_step(self, step: Step) -> Step | None:
        return self.steps[step].next

    def status_allowed(self, src: str, dst: str) -> bool:
        return dst in self.status_transitions.get(src, [])


# --- error_catalog.yaml -----------------------------------------------------


class Thresholds(_Strict):
    temp_warn_1_c: float
    temp_warn_2_c: float
    temp_noise_c: float
    cabinet_shutdown_minutes: int
    hw_warn_level_1_code: str
    hw_warn_level_2_code: str
    low_soc_pct: float
    low_soh_pct: float
    swap_slow_seconds: int


class EscalationCondition(_Strict):
    field: Literal["temperature"]
    op: Literal[">"]
    threshold: str  # tên trường trong Thresholds
    severity: Severity


class ErrorSpec(_Strict):
    code: str
    description: str
    steps: list[Step]  # rỗng = xuyên bước
    default_category: Category
    possible_categories: list[Category] = Field(default_factory=list)
    severity: Severity
    escalation_condition: EscalationCondition | None = None


class ErrorCatalog(_Strict):
    thresholds: Thresholds
    errors: list[ErrorSpec]

    @model_validator(mode="after")
    def _unique(self) -> ErrorCatalog:
        codes = [e.code for e in self.errors]
        dup = {c for c in codes if codes.count(c) > 1}
        if dup:
            raise ValueError(f"error code trùng: {sorted(dup)}")
        for e in self.errors:
            if e.escalation_condition and not hasattr(
                self.thresholds, e.escalation_condition.threshold
            ):
                raise ValueError(f"{e.code}: threshold không tồn tại")
        return self

    @property
    def by_code(self) -> dict[str, ErrorSpec]:
        return {e.code: e for e in self.errors}

    def has(self, code: str) -> bool:
        return code in self.by_code

    def severity_for(self, code: str, temperature: float | None = None) -> Severity:
        """Mức độ thực tế, áp `escalation_condition` nếu có (vd BAT_RETURN_FAULT > 55°C → P1)."""
        spec = self.by_code[code]
        cond = spec.escalation_condition
        if cond and temperature is not None:
            if temperature > float(getattr(self.thresholds, cond.threshold)):
                return cond.severity
        return spec.severity


# --- error_catalog_v1.yaml: bảng mã lỗi chuẩn (CLAUDE.md §6) -----------------------------------

ERROR_TABLE_FILE = "error_catalog_v1.yaml"
ERROR_TABLE_COLUMNS = ("Mã Lỗi", "Mức Độ Cảnh Báo", "Nhà Sản Xuất", "Loại Tủ", "Tên cảnh báo")


class ErrorTableRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    level: AlertLevel
    manufacturer: str
    cabinet_type: str
    name: str


class ErrorTable(BaseModel):
    """Bảng mã lỗi BSS của chủ dự án: nguồn duy nhất, CHỈ ĐỌC.

    Vì sao không có trường suy diễn (nhóm lỗi, P1–P3, mô tả): chủ dự án yêu cầu dùng bảng nguyên
    văn; mọi suy luận của ASE nằm ở cấu hình khác. `rows` đã gộp các dòng giống hệt nhau
    (bảng gốc lặp 4 dòng ở cuối); `n_source_rows` giữ số dòng của bảng gốc.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: tuple[ErrorTableRow, ...]
    n_source_rows: int

    @model_validator(mode="after")
    def _one_meaning_per_code_and_type(self) -> ErrorTable:
        seen: dict[tuple[str, str], ErrorTableRow] = {}
        for r in self.rows:
            other = seen.setdefault((r.code, r.cabinet_type), r)
            if other != r:
                raise ValueError(f"mã {r.code} / {r.cabinet_type} có hai dòng khác nhau")
        return self

    @property
    def codes(self) -> frozenset[str]:
        return frozenset(r.code for r in self.rows)

    def has(self, code: str) -> bool:
        return code in self.codes

    def lookup(self, code: str | None, cabinet_type: str | None = None) -> ErrorTableRow | None:
        """Dòng của `(mã, loại tủ)`. Không biết loại tủ (hoặc loại tủ không có dòng riêng) thì lấy
        dòng đầu tiên của mã. Mã ngoài bảng chuẩn hoặc không có mã → None."""
        if not code:
            return None
        first: ErrorTableRow | None = None
        for r in self.rows:
            if r.code != code:
                continue
            if r.cabinet_type == cabinet_type:
                return r
            first = first or r
        return first


def load_error_table(directory: Path = KNOWLEDGE_DIR) -> ErrorTable:
    raw = _load_yaml(ERROR_TABLE_FILE, directory)
    if not isinstance(raw, dict) or tuple(raw.get("columns", ())) != ERROR_TABLE_COLUMNS:
        raise ValueError(f"{ERROR_TABLE_FILE}: cột phải đúng {list(ERROR_TABLE_COLUMNS)}")
    source = raw.get("rows")
    if not isinstance(source, list) or not source:
        raise ValueError(f"{ERROR_TABLE_FILE}: thiếu `rows`")
    rows: dict[ErrorTableRow, None] = {}
    for i, item in enumerate(source, start=1):
        if not isinstance(item, list) or len(item) != len(ERROR_TABLE_COLUMNS):
            raise ValueError(f"{ERROR_TABLE_FILE}: dòng {i} phải có đúng 5 ô")
        code, level, manufacturer, cabinet_type, name = (str(x) for x in item)
        row = ErrorTableRow(
            code=code,
            level=AlertLevel(level),
            manufacturer=manufacturer,
            cabinet_type=cabinet_type,
            name=name,
        )
        rows[row] = None  # dict giữ thứ tự gốc và gộp dòng trùng y hệt
    return ErrorTable(rows=tuple(rows), n_source_rows=len(source))


@lru_cache(maxsize=1)
def default_error_table() -> ErrorTable:
    return load_error_table()


# --- routing.yaml -----------------------------------------------------------


class Route(_Strict):
    team: str | None
    alert: bool
    needs_human_review: bool = False


class CountryOverride(_Strict):
    country: str
    category: Category
    team: str


class Routing(_Strict):
    routes: dict[Category, Route]
    country_overrides: list[CountryOverride] = Field(default_factory=list)

    @model_validator(mode="after")
    def _complete(self) -> Routing:
        missing = set(Category) - set(self.routes)
        if missing:
            raise ValueError(f"routing thiếu category: {sorted(missing)}")
        return self

    def route_for(self, category: Category, country: str | None = None) -> Route:
        base = self.routes[category]
        for o in self.country_overrides:
            if o.country == country and o.category == category:
                return base.model_copy(update={"team": o.team})
        return base


# --- countries.yaml ---------------------------------------------------------


class PackageSpec(_Strict):
    batteries: int = Field(ge=1)
    monthly_fee: float = Field(ge=0)
    deposit: float | None = Field(default=None, ge=0)


class Promotion(_Strict):
    id: str
    packages: list[str] = Field(min_length=1)
    from_: date | None = Field(default=None, alias="from")
    until: date | None = None
    duration_months: int | None = Field(default=None, ge=1)
    free_swaps_per_month: int | None = Field(default=None, ge=0)  # None = không giới hạn


class PricingSpec(_Strict):
    swap_fee: float = Field(gt=0)  # /pin/lần đổi
    packages: dict[str, PackageSpec] = Field(min_length=1)
    promotions: list[Promotion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _promos_valid(self) -> PricingSpec:
        for p in self.promotions:
            unknown = set(p.packages) - set(self.packages)
            if unknown:
                raise ValueError(f"promotion {p.id} trỏ gói không tồn tại: {sorted(unknown)}")
        return self

    def standard_swap_amount(self, package_type: str) -> float:
        """Tiền 1 lần đổi ở pricing_mode=standard_fee: swap_fee * số pin của gói, bỏ qua ưu đãi.

        Vì sao bỏ qua ưu đãi: thời điểm mock (11/2026) gần như mọi phiên VN/PH được miễn phí,
        bước 5 sẽ không có giao dịch tiền để kiểm chứng PAYMENT/PAY_WRONG_AMOUNT.
        """
        return self.swap_fee * self.packages[package_type].batteries


class CitySpec(_Strict):
    name: str
    timezone: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class CountrySpec(_Strict):
    traffic_weight: float = Field(gt=0)
    currency: str
    timezones: list[str] = Field(min_length=1)
    cities: list[CitySpec] = Field(default_factory=list)
    payment_providers: list[str] = Field(min_length=1)
    pricing: PricingSpec

    @model_validator(mode="after")
    def _tz_valid(self) -> CountrySpec:
        for tz in self.timezones:
            ZoneInfo(tz)  # ném ZoneInfoNotFoundError nếu sai
        if self.cities:
            city_tzs = {c.timezone for c in self.cities}
            invalid = city_tzs - set(self.timezones)
            if invalid:
                raise ValueError(f"thành phố có timezone không thuộc nước: {sorted(invalid)}")
            missing = set(self.timezones) - city_tzs
            if missing:
                raise ValueError(f"thiếu thành phố cho timezone: {sorted(missing)}")
        return self

    def cities_for_timezone(self, tz: str) -> list[CitySpec]:
        return [c for c in self.cities if c.timezone == tz]


class Countries(_Strict):
    countries: dict[str, CountrySpec]

    @model_validator(mode="after")
    def _check(self) -> Countries:
        providers = [p for c in self.countries.values() for p in c.payment_providers]
        if len(providers) != len(set(providers)):
            raise ValueError("payment provider trùng giữa các nước")
        promo_ids = [p.id for c in self.countries.values() for p in c.pricing.promotions]
        if len(promo_ids) != len(set(promo_ids)):
            raise ValueError("promotion id trùng")
        return self

    def package_types_of(self, country: str) -> list[str]:
        return list(self.countries[country].pricing.packages)

    def country_of_provider(self, provider: str) -> str:
        for code, c in self.countries.items():
            if provider in c.payment_providers:
                return code
        raise KeyError(provider)


# --- detector.yaml ----------------------------------------------------------


class CorrelatorSpec(_Strict):
    window_minutes: int = Field(gt=0)
    window_minutes_by_category: dict[Category, int] = Field(default_factory=dict)
    network_wide_error_codes: list[str] = Field(default_factory=list)
    user_spike_window_minutes: int = Field(gt=0)
    user_spike_min_sessions: int = Field(gt=0)
    cluster_confidence_threshold: float = Field(ge=0.0, le=1.0)
    max_evidence_ids: int = Field(ge=1, le=5)

    def window_for(self, category: Category) -> int:
        return self.window_minutes_by_category.get(category, self.window_minutes)


class ClassifierSpec(_Strict):
    heartbeat_gap_seconds: int = Field(gt=0)
    paid_not_delivered_confidence: float = Field(ge=0.0, le=1.0)
    catalog_confidence: float = Field(ge=0.0, le=1.0)
    inferred_confidence: float = Field(ge=0.0, le=1.0)
    unknown_confidence: float = Field(ge=0.0, le=1.0)
    timeout_category_by_step: dict[Step, Category] = Field(default_factory=dict)


class NotifySpec(_Strict):
    batch_interval_minutes: int = Field(gt=0)
    max_rows_mail: int = Field(gt=0)
    daily_report_local_time: str
    daily_report_timezone: str

    @model_validator(mode="after")
    def _validate_tz_and_time(self) -> NotifySpec:
        ZoneInfo(self.daily_report_timezone)
        parts = self.daily_report_local_time.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(
                f"daily_report_local_time phải có dạng HH:MM: {self.daily_report_local_time}"
            )
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(
                f"daily_report_local_time ngoài phạm vi: {self.daily_report_local_time}"
            )
        return self


class ScanSpec(_Strict):
    """Tham số agent quét danh sách giao dịch và gộp sự cố (khối `scan` của detector.yaml)."""

    interval_minutes: int = Field(gt=0)
    lookback_minutes: int = Field(gt=0)
    stuck_after_minutes: int = Field(gt=0)
    timezone: str
    max_rows_mail: int = Field(gt=0)
    baseline_days: int = Field(gt=0)
    baseline_prior_rate: float = Field(gt=0, lt=1)
    baseline_prior_txns: int = Field(gt=0)
    baseline_alpha: float = Field(gt=0, lt=1)
    wide_min_stations: int = Field(ge=2)
    wide_min_txns: int = Field(ge=1)
    station_min_cabinets: int = Field(ge=2)
    station_min_txns: int = Field(ge=1)
    cabinet_min_txns: int = Field(ge=1)
    continue_minutes: dict[Literal["WIDE", "STATION", "CABINET"], int]

    @model_validator(mode="after")
    def _check(self) -> ScanSpec:
        ZoneInfo(self.timezone)
        if self.lookback_minutes < self.interval_minutes + self.stuck_after_minutes:
            raise ValueError("lookback_minutes phải ≥ interval_minutes + stuck_after_minutes")
        if set(self.continue_minutes) != {"WIDE", "STATION", "CABINET"}:
            raise ValueError("continue_minutes phải có đủ WIDE, STATION, CABINET")
        return self


class StreamSpec(_Strict):
    allowed_lateness_seconds: float = Field(default=10.0, ge=0.0)


class DetectorSpec(_Strict):
    correlator: CorrelatorSpec
    classifier: ClassifierSpec
    notify: NotifySpec
    scan: ScanSpec
    stream: StreamSpec = Field(default_factory=StreamSpec)
    field_tiers: dict[str, list[str]]
    field_presets: dict[str, list[str]]

    @model_validator(mode="after")
    def _validate_presets(self) -> DetectorSpec:
        required_presets = {"T0", "T0+T1", "T0+T1+T2", "ALL"}
        missing_presets = required_presets - set(self.field_presets)
        if missing_presets:
            raise ValueError(f"field_presets thiếu preset bắt buộc: {sorted(missing_presets)}")
        for preset, tiers in self.field_presets.items():
            unknown = set(tiers) - set(self.field_tiers)
            if unknown:
                raise ValueError(
                    f"preset {preset} tham chiếu tầng không tồn tại: {sorted(unknown)}"
                )
        return self

    def fields_for_preset(self, preset: str) -> frozenset[str]:
        if preset not in self.field_presets:
            raise KeyError(f"unknown field preset: {preset}")
        result: set[str] = set()
        for tier in self.field_presets[preset]:
            result.update(self.field_tiers[tier])
        return frozenset(result)


# --- bundle -----------------------------------------------------------------


class Knowledge(_Strict):
    state_machine: StateMachine
    error_catalog: ErrorCatalog
    routing: Routing
    countries: Countries
    detector: DetectorSpec


def load_knowledge(directory: Path = KNOWLEDGE_DIR) -> Knowledge:
    detector_dir = directory if (directory / "detector.yaml").exists() else KNOWLEDGE_DIR
    return Knowledge(
        state_machine=StateMachine.model_validate(_load_yaml("state_machine.yaml", directory)),
        error_catalog=ErrorCatalog.model_validate(_load_yaml("error_catalog.yaml", directory)),
        routing=Routing.model_validate(_load_yaml("routing.yaml", directory)),
        countries=Countries.model_validate(_load_yaml("countries.yaml", directory)),
        detector=DetectorSpec.model_validate(_load_yaml("detector.yaml", detector_dir)),
    )


@lru_cache(maxsize=1)
def default_knowledge() -> Knowledge:
    return load_knowledge()


# --- kpi.yaml: KPI, health score, open point, báo cáo kỳ ---------------------------------------


class HealthSpec(_Strict):
    good_min: float = Field(gt=0, le=100)
    watch_min: float = Field(gt=0, le=100)
    high_penalty_per_pp: float = Field(ge=0)

    def label(self, score: float) -> str:
        if score >= self.good_min:
            return "Tốt"
        return "Cần theo dõi" if score >= self.watch_min else "Kém"


class ServiceSpec(_Strict):
    id: str
    name: str
    scope: Literal["cabinet", "station"] | None = None
    codes: list[str] = Field(default_factory=list)
    target_fail_pct: float = Field(ge=0)
    penalty_per_pp: float = Field(ge=0)

    @model_validator(mode="after")
    def _one_kind(self) -> ServiceSpec:
        if (self.scope is None) == (not self.codes):
            raise ValueError(f"dịch vụ {self.id}: cần đúng một trong `scope` hoặc `codes`")
        return self


class OpenPointSpec(_Strict):
    health_below: float
    code_increase_pct: float = Field(gt=0)
    code_increase_min_cases: int = Field(ge=1)
    repeat_cabinet_code_min: int = Field(ge=2)
    repeat_rate_ratio: float = Field(ge=1)
    level_weight: dict[Literal["High", "Low", "none"], float]


class ScheduleSpec(_Strict):
    weekday: int | None = Field(default=None, ge=0, le=6)
    day: int | None = Field(default=None, ge=1, le=28)
    local_time: str

    @property
    def hour_minute(self) -> tuple[int, int]:
        h, m = self.local_time.split(":")
        return int(h), int(m)


class ReportSpec(_Strict):
    timezone: str
    top_n: int = Field(ge=1)
    weekly: ScheduleSpec
    monthly: ScheduleSpec
    repeat_user_min_failures: int = Field(ge=2)

    @model_validator(mode="after")
    def _check(self) -> ReportSpec:
        ZoneInfo(self.timezone)
        if self.weekly.weekday is None or self.monthly.day is None:
            raise ValueError("weekly cần `weekday`, monthly cần `day`")
        return self


class KpiSpec(_Strict):
    health: HealthSpec
    services: list[ServiceSpec]
    open_points: OpenPointSpec
    report: ReportSpec

    def check_codes(self, table: ErrorTable) -> None:
        unknown = {c for s in self.services for c in s.codes} - table.codes
        if unknown:
            raise ValueError(f"kpi.yaml tham chiếu mã không có trong bảng chuẩn: {sorted(unknown)}")


def load_kpi(directory: Path = KNOWLEDGE_DIR) -> KpiSpec:
    spec = KpiSpec.model_validate(_load_yaml("kpi.yaml", directory))
    spec.check_codes(default_error_table())
    return spec


@lru_cache(maxsize=1)
def default_kpi() -> KpiSpec:
    return load_kpi()
