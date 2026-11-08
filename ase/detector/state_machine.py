"""`SessionTracker` — theo dõi trạng thái phiên đổi pin, dedup và timer SLA (DETECTOR_PLAN §4.1).

Nguyên tắc:
- Chỉ dùng `Clock` được inject (`SimClock` khi replay), tuyệt đối không gọi `datetime.now()`.
- Dedup theo `event_id` (in-memory).
- Bỏ qua transition lùi (bước nhỏ hơn bước đã đóng có timestamp không sớm hơn, hoặc chuyển status
  không hợp lệ trong cùng bước như `OK -> PENDING`, hoặc event đến sau khi phiên đã đóng).
- Timer SLA của bước kế tiếp được tính từ `occurred_at` **cuối** (`max`) của bước hiện tại
  (ví dụ `PENDING -> OK` ở bước 5 thì chờ bước 6 tính từ `OK`, không báo nhầm `pending_then_ok`).
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ase.common.models import EventStatus, Step, SwapEvent
from ase.common.time import ensure_utc
from ase.common.transport import Clock, SimClock
from ase.detector.fields import FieldView
from ase.knowledge.loader import Knowledge, StateMachine, default_knowledge

FindingKind = Literal["EXPLICIT_FAILURE", "STEP_TIMEOUT", "PAID_NOT_DELIVERED"]


def _step_chain(sm: StateMachine) -> list[Step]:
    chain = [Step.OPEN_APP]
    while (nxt := sm.next_step(chain[-1])) is not None:
        chain.append(nxt)
    return chain


class Finding(BaseModel):
    """Phát hiện thô từ `SessionTracker` trước khi phân loại (DETECTOR_PLAN §3)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    station_id: str
    cabinet_id: str | None = None
    slot_id: str | None = None
    step: Step
    kind: FindingKind
    error_code: str | None = None
    status: EventStatus
    occurred_at: datetime
    detected_at: datetime
    event_ids: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("occurred_at", "detected_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        return ensure_utc(v)


@dataclass
class SessionState:
    session_id: str
    station_id: str
    cabinet_id: str | None = None
    slot_id: str | None = None
    last_occurred_at: dict[Step, datetime] = field(default_factory=dict)
    step_status: dict[Step, EventStatus] = field(default_factory=dict)
    current_step_idx: int = -1
    closed: bool = False
    event_ids: list[str] = field(default_factory=list)
    timer_version: int = 0

    def add_event_id(self, eid: str) -> None:
        if eid not in self.event_ids:
            self.event_ids.append(eid)

    def evidence_ids(self, preferred_last: str | None = None) -> list[str]:
        if preferred_last is not None:
            others = [e for e in self.event_ids if e != preferred_last]
            return [preferred_last, *others[-4:]][:5]
        return list(self.event_ids[-5:])


class SessionTracker:
    """Theo dõi máy trạng thái của mọi phiên theo luồng `SwapEvent`."""

    def __init__(
        self,
        clock: Clock,
        *,
        knowledge: Knowledge | None = None,
        field_view: FieldView | None = None,
        allowed_lateness_seconds: float = 0.0,
        advance_clock: bool = True,
    ) -> None:
        if allowed_lateness_seconds < 0:
            raise ValueError("allowed_lateness_seconds must be >= 0")
        self.clock = clock
        self.knowledge = knowledge or default_knowledge()
        self.sm = self.knowledge.state_machine
        self.field_view = field_view
        self.allowed_lateness_seconds = float(allowed_lateness_seconds)
        self.allowed_lateness = timedelta(seconds=allowed_lateness_seconds)
        self.advance_clock = advance_clock
        self.chain = _step_chain(self.sm)
        self.step_idx_map: dict[Step, int] = {s: i for i, s in enumerate(self.chain)}

        self._seen_event_ids: set[str] = set()
        self._sessions: dict[str, SessionState] = {}
        # Min-heap: (deadline_utc, seq, session_id, timer_version, waiting_step)
        self._timers: list[tuple[datetime, int, str, int, Step]] = []
        self._timer_seq: int = 0
        self._max_occurred_at: datetime | None = None

        self.duplicate_events_dropped: int = 0
        self.late_events_handled: int = 0
        self.backward_transitions_ignored: int = 0
        self.total_events_processed: int = 0
        self.max_arrival_lag_seconds: float = 0.0
        self.max_lateness_vs_clock_seconds: float = 0.0

    @property
    def total_sessions_seen(self) -> int:
        return len(self._sessions)

    def get_session_state(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)

    def _schedule_timer(self, state: SessionState, waiting_step: Step, base_time: datetime) -> None:
        state.timer_version += 1
        sla_s = self.sm.sla(waiting_step)
        if sla_s is None:
            return
        deadline = ensure_utc(base_time) + timedelta(seconds=sla_s)
        self._timer_seq += 1
        heapq.heappush(
            self._timers,
            (deadline, self._timer_seq, state.session_id, state.timer_version, waiting_step),
        )

    def _pop_expired_timers(self, up_to: datetime, *, strict_before: bool = False) -> list[Finding]:
        findings: list[Finding] = []
        limit = ensure_utc(up_to) - self.allowed_lateness
        while self._timers:
            deadline = self._timers[0][0]
            if (deadline >= limit) if strict_before else (deadline > limit):
                break
            deadline, _seq, sid, ver, waiting_step = heapq.heappop(self._timers)
            state = self._sessions.get(sid)
            if state is None or state.closed or state.timer_version != ver:
                continue
            state.closed = True
            state.timer_version += 1
            detected_at = ensure_utc(self.clock.now())
            findings.append(
                Finding(
                    session_id=state.session_id,
                    station_id=state.station_id,
                    cabinet_id=state.cabinet_id,
                    slot_id=None,
                    step=waiting_step,
                    kind="STEP_TIMEOUT",
                    error_code="STEP_TIMEOUT",
                    status="TIMEOUT",
                    occurred_at=deadline,
                    detected_at=max(deadline, detected_at),
                    event_ids=state.evidence_ids(),
                )
            )
        return findings

    def check_timeouts(self, up_to: datetime | None = None) -> list[Finding]:
        target = ensure_utc(up_to) if up_to is not None else ensure_utc(self.clock.now())
        if isinstance(self.clock, SimClock):
            self.clock.advance(target)
        return self._pop_expired_timers(target)

    def on_event(self, raw_event: SwapEvent) -> list[Finding]:
        occ_utc = ensure_utc(raw_event.occurred_at)
        prev_max = self._max_occurred_at
        is_late = prev_max is not None and occ_utc < prev_max
        if prev_max is None or occ_utc > prev_max:
            self._max_occurred_at = occ_utc

        if self.advance_clock and isinstance(self.clock, SimClock):
            self.clock.advance(occ_utc)
        now: datetime | None
        if isinstance(self.clock, SimClock) and self.clock._now is None:
            now = None
        else:
            now = ensure_utc(self.clock.now())

        # 1. Dedup theo event_id
        if raw_event.event_id in self._seen_event_ids:
            self.duplicate_events_dropped += 1
            return self._pop_expired_timers(now) if now is not None else []

        self._seen_event_ids.add(raw_event.event_id)
        self.total_events_processed += 1
        if is_late and prev_max is not None:
            self.late_events_handled += 1
            lag_s = round((prev_max - occ_utc).total_seconds(), 6)
            if lag_s > self.max_arrival_lag_seconds:
                self.max_arrival_lag_seconds = lag_s
        if now is not None and occ_utc < now:
            lateness_s = round((now - occ_utc).total_seconds(), 6)
            if lateness_s > self.max_lateness_vs_clock_seconds:
                self.max_lateness_vs_clock_seconds = lateness_s

        ev = self.field_view.mask_event(raw_event) if self.field_view is not None else raw_event

        # Trước khi cập nhật phiên của `ev`, giải phóng các timer có hạn < `min(occ_utc, now)`
        findings: list[Finding] = (
            self._pop_expired_timers(min(occ_utc, now), strict_before=True)
            if now is not None
            else []
        )

        state = self._sessions.get(ev.session_id)
        if state is None:
            state = SessionState(session_id=ev.session_id, station_id=ev.station_id)
            self._sessions[ev.session_id] = state

        idx = self.step_idx_map[ev.step]

        # 2. Kiểm tra transition lùi / event đến trễ của bước cũ
        if idx < state.current_step_idx:
            cur_step = self.chain[state.current_step_idx]
            cur_time = state.last_occurred_at.get(cur_step)
            if (
                cur_time is not None
                and occ_utc < cur_time
                and ev.status not in self.sm.terminal_statuses
            ):
                # Event bước trước đến trễ (disorder): ghi nhận nhưng không lùi trạng thái
                prev_t = state.last_occurred_at.get(ev.step)
                state.last_occurred_at[ev.step] = (
                    occ_utc if prev_t is None else max(prev_t, occ_utc)
                )
                if ev.step not in state.step_status or state.step_status[ev.step] == "PENDING":
                    state.step_status[ev.step] = ev.status
                state.add_event_id(ev.event_id)
            else:
                self.backward_transitions_ignored += 1
            if now is not None:
                findings.extend(self._pop_expired_timers(now))
            return findings

        if state.closed:
            self.backward_transitions_ignored += 1
            if now is not None:
                findings.extend(self._pop_expired_timers(now))
            return findings

        if idx == state.current_step_idx and ev.step in state.step_status:
            prev_status = state.step_status[ev.step]
            if not self.sm.status_allowed(prev_status, ev.status):
                self.backward_transitions_ignored += 1
                if now is not None:
                    findings.extend(self._pop_expired_timers(now))
                return findings

        # 3. Cập nhật trạng thái phiên
        state.station_id = ev.station_id
        if ev.cabinet_id is not None:
            state.cabinet_id = ev.cabinet_id
        if ev.slot_id is not None:
            state.slot_id = ev.slot_id
        state.add_event_id(ev.event_id)

        prev_last = state.last_occurred_at.get(ev.step)
        last_at = occ_utc if prev_last is None else max(prev_last, occ_utc)
        state.last_occurred_at[ev.step] = last_at
        state.step_status[ev.step] = ev.status
        state.current_step_idx = idx

        # 4. Xử lý kết thúc bước / đặt timer bước kế
        if ev.status in self.sm.terminal_statuses:
            state.closed = True
            state.timer_version += 1
            findings.append(
                Finding(
                    session_id=state.session_id,
                    station_id=state.station_id,
                    cabinet_id=ev.cabinet_id or state.cabinet_id,
                    slot_id=ev.slot_id or state.slot_id,
                    step=ev.step,
                    kind="EXPLICIT_FAILURE",
                    error_code=ev.error_code,
                    status=ev.status,
                    occurred_at=occ_utc,
                    detected_at=max(now, occ_utc) if now is not None else occ_utc,
                    event_ids=state.evidence_ids(preferred_last=ev.event_id),
                )
            )
        elif ev.status == "OK":
            nxt = self.sm.next_step(ev.step)
            if nxt is None:
                state.closed = True
                state.timer_version += 1
            else:
                self._schedule_timer(state, nxt, last_at)
        elif ev.status == "PENDING":
            # Đang PENDING ở `ev.step` (vd bước 5 chờ callback trong grace window 90s)
            if self.sm.sla(ev.step) is not None:
                self._schedule_timer(state, ev.step, last_at)
            else:
                nxt = self.sm.next_step(ev.step)
                if nxt is not None:
                    self._schedule_timer(state, nxt, last_at)

        if now is not None:
            findings.extend(self._pop_expired_timers(now))
        return findings

    def flush(self) -> list[Finding]:
        """Hết file: flush toàn bộ timer còn treo theo hạn chót / thời điểm cuối (§2.3)."""
        findings: list[Finding] = []
        while self._timers:
            deadline, _seq, sid, ver, waiting_step = heapq.heappop(self._timers)
            state = self._sessions.get(sid)
            if state is None or state.closed or state.timer_version != ver:
                continue
            state.closed = True
            state.timer_version += 1
            if isinstance(self.clock, SimClock):
                self.clock.advance(deadline)
            detected_at = ensure_utc(self.clock.now())
            findings.append(
                Finding(
                    session_id=state.session_id,
                    station_id=state.station_id,
                    cabinet_id=state.cabinet_id,
                    slot_id=None,
                    step=waiting_step,
                    kind="STEP_TIMEOUT",
                    error_code="STEP_TIMEOUT",
                    status="TIMEOUT",
                    occurred_at=deadline,
                    detected_at=max(deadline, detected_at),
                    event_ids=state.evidence_ids(),
                )
            )
        return findings
