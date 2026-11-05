"""Data contract của ASE (CLAUDE.md §7) và các bảng mock (§10).

Quy tắc: chỉ thêm field, không đổi tên/xoá field của contract §7 và bảng §10 (MOCK_PLAN §3.9).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _require_aware(v: datetime | None) -> datetime | None:
    if v is not None and v.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return v


class Step(StrEnum):
    OPEN_APP = "1_open_app"
    SELECT_SERVICE = "2_select_service"
    SCAN_QR = "3_scan_qr"
    CHECK_ELIGIBILITY = "4_check_eligibility"
    PAYMENT = "5_payment"
    RETURN_BATTERY = "6_return_battery"
    RECEIVE_BATTERY = "7_receive_battery"


class Category(StrEnum):
    SYSTEM = "SYSTEM"
    HARDWARE = "HARDWARE"
    PAYMENT = "PAYMENT"
    BUSINESS = "BUSINESS"
    USER = "USER"
    BATTERY_QUALITY = "BATTERY_QUALITY"
    UNKNOWN = "UNKNOWN"


# 6 nhóm thật: ground truth chỉ ghi các nhóm này, không bao giờ ghi UNKNOWN (MOCK_PLAN §3a).
REAL_CATEGORIES: tuple[Category, ...] = tuple(c for c in Category if c is not Category.UNKNOWN)


class Severity(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


EventStatus = Literal["OK", "FAILED", "TIMEOUT", "PENDING"]


class SwapEvent(BaseModel):
    """Đúng 11 trường §7. Trường T1-T3 khác nằm ở các bảng riêng (MOCK_PLAN §5.1)."""

    event_id: str  # idempotency key
    session_id: str
    station_id: str
    cabinet_id: str | None = None
    slot_id: str | None = None
    step: Step
    status: EventStatus
    error_code: str | None = None
    error_message: str | None = None  # UNTRUSTED
    latency_ms: int | None = None  # latency service ở bước này (T2)
    occurred_at: datetime  # UTC, tz-aware bắt buộc

    @field_validator("occurred_at")
    @classmethod
    def must_be_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return v


class TriageResult(BaseModel):
    """Output bắt buộc của LLM. Parse fail → fallback kết quả rule-based."""

    category: Category
    severity: Severity
    confidence: float = Field(ge=0, le=1)
    probable_cause: str = Field(max_length=500)
    evidence: list[str] = Field(max_length=5)  # ID có thật, được validate
    suggested_actions: list[str] = Field(max_length=5)
    runbook_ids: list[str] = []
    needs_human_review: bool = True


class OpenPoint(BaseModel):
    id: str  # ổn định qua các kỳ
    group: Literal["OPERATION", "AI_SYSTEM"]
    title: str = Field(max_length=120)
    related_sf: list[str] = []
    category: Category | None
    scope: str
    impact: str
    evidence: list[str]
    hypothesis: str
    recommendations: list[str]
    priority: Severity  # tính bằng công thức, KHÔNG để LLM chấm
    owner_team: str
    status: Literal["NEW", "ACKED", "IN_PROGRESS", "RESOLVED", "WONT_FIX"]


# ---------------------------------------------------------------------------
# Bảng mock (§10 "Bảng"). `app_logs` DEFERRED, không có model.
# ---------------------------------------------------------------------------


class _Table(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Station(_Table):
    station_id: str
    country: str
    timezone: str  # IANA
    model_gen: str
    install_date: datetime

    _aware = field_validator("install_date")(_require_aware)


class Cabinet(_Table):
    cabinet_id: str
    station_id: str
    country: str
    timezone: str
    model_gen: str
    install_date: datetime

    _aware = field_validator("install_date")(_require_aware)


class Slot(_Table):
    slot_id: str
    cabinet_id: str
    station_id: str
    country: str
    timezone: str
    model_gen: str
    install_date: datetime

    _aware = field_validator("install_date")(_require_aware)


class Battery(_Table):
    battery_id: str
    lot_id: str
    soh: float = Field(ge=0, le=100)
    cycle_count: int = Field(ge=0)


class User(_Table):
    user_id: str  # hash
    package_type: str
    package_expiry: datetime
    debt_flag: bool

    _aware = field_validator("package_expiry")(_require_aware)


class SwapSession(_Table):
    session_id: str
    user_id: str
    station_id: str
    cabinet_id: str | None = None
    slot_in: str | None = None
    slot_out: str | None = None
    app_version: str
    payment_provider: str | None = None
    final_status: str
    started_at: datetime
    ended_at: datetime | None = None

    _aware = field_validator("started_at", "ended_at")(_require_aware)


class Payment(_Table):
    payment_id: str
    session_id: str
    amount: int | float
    currency: str
    status: str
    captured: bool
    callback_at: datetime | None = None

    _aware = field_validator("callback_at")(_require_aware)


class CabinetTelemetry(_Table):
    cabinet_id: str
    slot_id: str | None = None
    door_state: str | None = None
    battery_detected: bool | None = None
    soc: float | None = None
    temperature: float | None = None
    heartbeat_at: datetime
    hw_error_code: str | None = None

    _aware = field_validator("heartbeat_at")(_require_aware)


class GroundTruthRow(_Table):
    """1 dòng/phiên. Phiên bình thường: category/severity/error_code/scenario_instance_id rỗng."""

    session_id: str
    is_failure: bool
    category: Category | None = None  # không bao giờ UNKNOWN (kiểm ở validator)
    scenario: str | None = None
    scenario_start: datetime | None = None
    injected_at: datetime | None = None
    severity: Severity | None = None
    error_code: str | None = None
    scenario_instance_id: str | None = None
    country: str
    split: str

    _aware = field_validator("scenario_start", "injected_at")(_require_aware)

    @field_validator("category")
    @classmethod
    def not_unknown(cls, v: Category | None) -> Category | None:
        if v is Category.UNKNOWN:
            raise ValueError("ground truth category must be a real category, never UNKNOWN")
        return v
