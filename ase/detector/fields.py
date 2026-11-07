"""Tầng trường dữ liệu T0–T3, `FieldView` và báo cáo `CoverageReport` (DETECTOR_PLAN §3, §5, §6).

`FieldView` bọc `SwapEvent` và `ContextStore`: bất kỳ trường nào không thuộc `available_fields`
sẽ trả về `None` như thể dữ liệu thực tế không cung cấp trường đó. `error_message` luôn bị che
ở tầng phân loại vì là dữ liệu untrusted (CLAUDE.md §10.1, DETECTOR_PLAN §2.6).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ase.common.models import CabinetTelemetry, Payment, SwapEvent, User
from ase.detector.context import ContextStore
from ase.knowledge.loader import Knowledge, default_knowledge


class DisabledRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    missing_fields: list[str]
    reason: str


class CoverageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str
    available_fields: list[str]
    disabled_rules: list[DisabledRule] = Field(default_factory=list)
    default_country_sessions: int = 0
    duplicate_events_dropped: int = 0
    late_events_handled: int = 0
    backward_transitions_ignored: int = 0
    total_events_processed: int = 0
    total_sessions_seen: int = 0
    max_arrival_lag_seconds: float = 0.0


class FieldView:
    """Lớp truy cập dữ liệu theo `available_fields` cho detector và field ablation."""

    def __init__(
        self,
        context: ContextStore,
        *,
        preset: str = "ALL",
        available_fields: Iterable[str] | None = None,
        knowledge: Knowledge | None = None,
    ) -> None:
        self.context = context
        self.preset = preset
        k = knowledge or default_knowledge()
        if available_fields is not None:
            self.available_fields: frozenset[str] = frozenset(available_fields)
        else:
            self.available_fields = k.detector.fields_for_preset(preset)
        self._default_country_session_ids: set[str] = set()

    @property
    def default_country_sessions_count(self) -> int:
        return len(self._default_country_session_ids)

    def has_field(self, field_name: str) -> bool:
        return field_name in self.available_fields

    def has_all(self, field_names: Iterable[str]) -> bool:
        return all(f in self.available_fields for f in field_names)

    def missing_fields(self, required: Iterable[str]) -> list[str]:
        return sorted(f for f in required if f not in self.available_fields)

    def mask_event(self, event: SwapEvent) -> SwapEvent:
        """Trả về bản sao `SwapEvent` đã ẩn trường ngoài `available_fields` và `error_message`."""
        return SwapEvent(
            event_id=event.event_id,
            session_id=event.session_id,
            station_id=event.station_id,
            cabinet_id=event.cabinet_id if "cabinet_id" in self.available_fields else None,
            slot_id=event.slot_id if "slot_id" in self.available_fields else None,
            step=event.step,
            status=event.status,
            error_code=event.error_code if "error_code" in self.available_fields else None,
            error_message=None,  # UNTRUSTED: không bao giờ đọc để phân loại
            latency_ms=event.latency_ms if "latency_ms" in self.available_fields else None,
            occurred_at=event.occurred_at,
        )

    def _mask_telemetry(self, tel: CabinetTelemetry) -> CabinetTelemetry:
        return CabinetTelemetry(
            cabinet_id=tel.cabinet_id,
            slot_id=tel.slot_id if "slot_id" in self.available_fields else None,
            door_state=tel.door_state if "door_state" in self.available_fields else None,
            battery_detected=(
                tel.battery_detected if "battery_detected" in self.available_fields else None
            ),
            soc=tel.soc if "soc" in self.available_fields else None,
            temperature=tel.temperature if "temperature" in self.available_fields else None,
            heartbeat_at=tel.heartbeat_at,
            hw_error_code=tel.hw_error_code if "hw_error_code" in self.available_fields else None,
        )

    def station_country_timezone(
        self, station_id: str, session_id: str | None = None
    ) -> tuple[str, str]:
        """Suy `(country, timezone)` từ bảng `stations`. Thiếu thì mặc định VN và ghi coverage."""
        if self.has_all(("country", "timezone")):
            st = self.context.get_station(station_id)
            if st is not None:
                return st.country, st.timezone
        if session_id is not None:
            self._default_country_session_ids.add(session_id)
        return "VN", "Asia/Ho_Chi_Minh"

    def payment(self, session_id: str, now: datetime) -> Payment | None:
        if "payment.captured" not in self.available_fields:
            return None
        return self.context.get_payment(session_id, now)

    def payment_captured(self, session_id: str, now: datetime) -> bool | None:
        p = self.payment(session_id, now)
        return p.captured if p is not None else None

    def payment_provider(self, session_id: str, now: datetime) -> str | None:
        if "payment_provider" not in self.available_fields:
            return None
        sess = self.context.get_session(session_id, now)
        return sess.payment_provider if sess is not None else None

    def app_version(self, session_id: str, now: datetime) -> str | None:
        if "app_version" not in self.available_fields:
            return None
        sess = self.context.get_session(session_id, now)
        return sess.app_version if sess is not None else None

    def user(self, session_id: str, now: datetime) -> User | None:
        if "user_id" not in self.available_fields:
            return None
        sess = self.context.get_session(session_id, now)
        if sess is None:
            return None
        return self.context.get_user(sess.user_id)

    def battery_out_soh(self, session_id: str, now: datetime) -> float | None:
        if not self.has_all(("battery_id", "soh")):
            return None
        sess = self.context.get_session(session_id, now)
        if sess is None or sess.battery_out_id is None:
            return None
        bat = self.context.get_battery(sess.battery_out_id)
        return bat.soh if bat is not None else None

    def battery_out_lot_id(self, session_id: str, now: datetime) -> str | None:
        if not self.has_all(("battery_id", "lot_id")):
            return None
        sess = self.context.get_session(session_id, now)
        if sess is None or sess.battery_out_id is None:
            return None
        bat = self.context.get_battery(sess.battery_out_id)
        return bat.lot_id if bat is not None else None

    def slot_telemetry_window(
        self,
        cabinet_id: str | None,
        slot_id: str | None,
        now: datetime,
        *,
        lookback_seconds: float = 120.0,
    ) -> list[CabinetTelemetry]:
        if cabinet_id is None or slot_id is None:
            return []
        if not self.has_all(("cabinet_id", "slot_id")):
            return []
        rows = self.context.get_slot_telemetry_window(
            cabinet_id, slot_id, now, lookback_seconds=lookback_seconds
        )
        return [self._mask_telemetry(r) for r in rows]

    def latest_slot_telemetry(
        self, cabinet_id: str | None, slot_id: str | None, now: datetime
    ) -> CabinetTelemetry | None:
        if cabinet_id is None or slot_id is None:
            return None
        if not self.has_all(("cabinet_id", "slot_id")):
            return None
        tel = self.context.get_latest_slot_telemetry(cabinet_id, slot_id, now)
        return self._mask_telemetry(tel) if tel is not None else None

    def latest_temperature(
        self, cabinet_id: str | None, slot_id: str | None, now: datetime
    ) -> float | None:
        if cabinet_id is None or not self.has_all(("cabinet_id", "temperature")):
            return None
        eff_slot = slot_id if "slot_id" in self.available_fields else None
        return self.context.get_latest_temperature(cabinet_id, eff_slot, now)

    def cabinet_heartbeat_gap_seconds(
        self, cabinet_id: str | None, around_time: datetime, now: datetime
    ) -> float | None:
        if cabinet_id is None or not self.has_all(("cabinet_id", "heartbeat_at")):
            return None
        return self.context.get_cabinet_heartbeat_gap_seconds(cabinet_id, around_time, now)
