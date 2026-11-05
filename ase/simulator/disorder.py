"""Xáo trộn và nhân bản một phần dòng SwapEvent (MOCK_PLAN §8, §10; CLAUDE.md §5.1, §13).

Quy tắc:
- Event trùng: giữ nguyên `event_id` (và toàn bộ payload) để detector kiểm chứng dedup.
- Event đến trễ / sai thứ tự: đổi vị trí dòng trong file JSONL (không sửa `occurred_at`),
  khiến một số dòng đứng sau có `occurred_at` nhỏ hơn dòng đứng trước.
- Không làm đổi tập `session_id` hay ground truth theo phiên.
"""

from __future__ import annotations

from collections.abc import Sequence

from ase.common.models import SwapEvent
from ase.simulator.rng import substream


def apply_disorder(
    events: Sequence[SwapEvent],
    seed: int,
    *,
    duplicate_rate: float = 0.015,
    late_rate: float = 0.015,
    max_shift: int = 8,
) -> list[SwapEvent]:
    """Trả về danh sách event theo thứ tự đến hệ thống (đã có trùng + đến trễ)."""
    if not events:
        return []
    rng = substream(seed, "disorder")
    out: list[SwapEvent] = list(events)
    n = len(out)

    # 1. Dịch một số event lùi xuống vài vị trí (mô phỏng gói tin đến trễ / sai thứ tự)
    idx = 0
    while idx < n - 2:
        if rng.random() < late_rate:
            shift = rng.randint(1, min(max_shift, n - 1 - idx))
            ev = out.pop(idx)
            out.insert(idx + shift, ev)
            idx += shift + 1
        else:
            idx += 1

    # 2. Nhân bản một số event (giữ nguyên event_id để test idempotency/dedup)
    with_dups: list[SwapEvent] = []
    pending_dups: list[tuple[int, SwapEvent]] = []
    for pos, ev in enumerate(out):
        with_dups.append(ev)
        due = [d for d in pending_dups if d[0] <= pos]
        if due:
            for _, dup_ev in due:
                with_dups.append(dup_ev)
            pending_dups = [d for d in pending_dups if d[0] > pos]
        if rng.random() < duplicate_rate:
            delay = rng.randint(0, 3)
            if delay == 0:
                with_dups.append(ev.model_copy(deep=True))
            else:
                pending_dups.append((pos + delay, ev.model_copy(deep=True)))
    for _, dup_ev in pending_dups:
        with_dups.append(dup_ev)
    return with_dups
