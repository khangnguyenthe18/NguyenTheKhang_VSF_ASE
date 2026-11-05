"""Xáo trộn và nhân bản một phần dòng SwapEvent (MOCK_PLAN §8, §10; CLAUDE.md §5.1, §13).

Quy tắc:
- Event trùng: giữ nguyên `event_id` (và toàn bộ payload) để detector kiểm chứng dedup.
- Event đến trễ / sai thứ tự: chỉ dời qua các event kế tiếp cách không quá `max_delay_seconds`
  (mặc định 8 giây, nhỏ hơn SLA tối thiểu 10 giây ở bước 4) để không gây báo nhầm STEP_TIMEOUT
  trên các phiên bình thường.
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
    late_rate: float = 0.025,
    max_shift: int = 8,
    max_delay_seconds: float = 8.0,
) -> list[SwapEvent]:
    """Trả về danh sách event theo thứ tự đến hệ thống (có trùng + đến trễ trong giới hạn giây)."""
    if not events:
        return []
    rng = substream(seed, "disorder")
    out: list[SwapEvent] = list(events)
    n = len(out)

    # 1. Dịch event lùi xuống sau các event kế tiếp nhưng bảo đảm mọi event bị vượt qua
    # đều cách t0 không quá max_delay_seconds.
    idx = 0
    while idx < n - 1:
        if rng.random() < late_rate:
            t0 = out[idx].occurred_at
            valid_shifts: list[int] = []
            for s in range(1, min(max_shift, n - 1 - idx) + 1):
                max_between = max(e.occurred_at for e in out[idx + 1 : idx + s + 1])
                diff = (max_between - t0).total_seconds()
                if 0.5 <= diff <= max_delay_seconds:
                    valid_shifts.append(s)
            if valid_shifts:
                shift = rng.choice(valid_shifts)
                ev = out.pop(idx)
                out.insert(idx + shift, ev)
                idx += shift + 1
                continue
        idx += 1

    # 2. Nhân bản một số event (giữ nguyên event_id để test idempotency/dedup)
    # Đặt bản sao ngay sau event gốc (hoặc cách 1 dòng nếu vẫn trong giới hạn max_delay_seconds).
    with_dups: list[SwapEvent] = []
    running_max = out[0].occurred_at
    for ev in out:
        if ev.occurred_at > running_max:
            running_max = ev.occurred_at
        with_dups.append(ev)
        if rng.random() < duplicate_rate:
            if (running_max - ev.occurred_at).total_seconds() <= max_delay_seconds:
                with_dups.append(ev.model_copy(deep=True))
    return with_dups
