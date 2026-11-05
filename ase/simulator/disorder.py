"""Xáo trộn và nhân bản một phần dòng SwapEvent (MOCK_PLAN §8, §10; CLAUDE.md §5.1, §13).

Quy tắc:
- Event trùng: giữ nguyên `event_id` (và toàn bộ payload) để detector kiểm chứng dedup.
- Event đến trễ / sai thứ tự: dời dòng xuống sau vài event kế tiếp (không sửa `occurred_at`).
  Độ trễ bị chặn bởi hai điều kiện:
  1. trễ so với SimClock lúc event đến <= `max_delay_seconds`;
  2. thời gian chờ của bước (từ event cuối của bước trước tới lúc event này *đến*, theo SimClock)
     vẫn <= SLA của bước trong `state_machine.yaml` trừ `sla_margin_seconds`.
  Vì sao cần (2): độ trễ cộng thêm vào thời gian của chính bước đó. Chặn chung một ngưỡng giây là
  không đủ (vd bước 4 kéo dài 4s + trễ 8s > SLA 10s), và tracker sẽ báo nhầm STEP_TIMEOUT trên
  phiên bình thường. Đọc SLA từ YAML nên đổi SLA hay `step_seconds` không phải sửa file này.
- Không làm đổi tập `session_id` hay ground truth theo phiên.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from ase.common.models import SwapEvent
from ase.knowledge.loader import StateMachine
from ase.simulator.rng import substream
from ase.simulator.session_fsm import step_chain


def wait_starts(events: Sequence[SwapEvent], state_machine: StateMachine) -> dict[str, datetime]:
    """Mốc bắt đầu chờ của từng event: `occurred_at` lớn nhất của bước liền trước cùng phiên.

    Event của bước đầu (không có bước trước) không có mốc, nên không xuất hiện trong kết quả.
    """
    order = {step: i for i, step in enumerate(step_chain(state_machine))}
    last_of: dict[tuple[str, int], datetime] = {}
    for ev in events:
        key = (ev.session_id, order[ev.step])
        if key not in last_of or ev.occurred_at > last_of[key]:
            last_of[key] = ev.occurred_at
    starts: dict[str, datetime] = {}
    for ev in events:
        prev = last_of.get((ev.session_id, order[ev.step] - 1))
        if prev is not None:
            starts[ev.event_id] = prev
    return starts


def apply_disorder(
    events: Sequence[SwapEvent],
    seed: int,
    *,
    state_machine: StateMachine,
    duplicate_rate: float = 0.015,
    late_rate: float = 0.025,
    max_shift: int = 8,
    max_delay_seconds: float = 8.0,
    sla_margin_seconds: float = 1.0,
) -> list[SwapEvent]:
    """Trả về danh sách event theo thứ tự đến hệ thống (có trùng + đến trễ không vượt SLA)."""
    if not events:
        return []
    rng = substream(seed, "disorder")
    out: list[SwapEvent] = list(events)
    n = len(out)
    starts = wait_starts(out, state_machine)

    def within_budget(ev: SwapEvent, clock: datetime) -> bool:
        lag = (clock - ev.occurred_at).total_seconds()
        if not 0.5 <= lag <= max_delay_seconds:
            return False
        sla = state_machine.sla(ev.step)
        start = starts.get(ev.event_id)
        if sla is None or start is None:
            return True
        return (clock - start).total_seconds() <= sla - sla_margin_seconds

    # 1. Dời event xuống sau vài event kế tiếp. `running` = SimClock trước vị trí idx.
    idx = 0
    running: datetime | None = None
    while idx < n - 1:
        ev = out[idx]
        if rng.random() < late_rate:
            valid_shifts: list[int] = []
            passed_max: datetime | None = None
            for s in range(1, min(max_shift, n - 1 - idx) + 1):
                t = out[idx + s].occurred_at
                passed_max = t if passed_max is None or t > passed_max else passed_max
                clock = passed_max if running is None or passed_max > running else running
                if within_budget(ev, clock):
                    valid_shifts.append(s)
            if valid_shifts:
                shift = rng.choice(valid_shifts)
                passed = out[idx + 1 : idx + shift + 1]
                out.pop(idx)
                out.insert(idx + shift, ev)
                for e in (*passed, ev):
                    if running is None or e.occurred_at > running:
                        running = e.occurred_at
                idx += shift + 1
                continue
        if running is None or ev.occurred_at > running:
            running = ev.occurred_at
        idx += 1

    # 2. Nhân bản một số event (giữ nguyên event_id để test idempotency/dedup), đặt ngay sau
    # event gốc. Bản sao đến sau bản gốc nên không ảnh hưởng thời gian chờ của bước.
    with_dups: list[SwapEvent] = []
    for ev in out:
        with_dups.append(ev)
        if rng.random() < duplicate_rate:
            with_dups.append(ev.model_copy(deep=True))
    return with_dups
