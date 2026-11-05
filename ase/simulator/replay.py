"""CLI replay file JSONL qua `JsonlSource` + `SimClock` (MOCK_PLAN §7 bước 11, §9).

Chạy: `python -m ase.simulator.replay data/smoke/ase.swap.events.jsonl`
Kiểm tra:
- Mọi dòng parse được `SwapEvent`.
- `SimClock` tiến đơn điệu (không bao giờ lùi dù gặp event đến trễ).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from ase.common.transport import JsonlSource, SimClock


class ReplaySummary(BaseModel):
    total_events: int
    unique_event_ids: int
    duplicate_events: int
    late_events: int
    unique_sessions: int
    final_clock: datetime


async def replay_file(path: Path, speed: float | None = None) -> ReplaySummary:
    clock = SimClock()
    source = JsonlSource(path, clock, speed=speed)
    seen_event_ids: set[str] = set()
    seen_sessions: set[str] = set()
    total = 0
    duplicates = 0
    late = 0
    prev_clock: datetime | None = None
    max_occurred: datetime | None = None

    async for ev in source.events():
        total += 1
        if max_occurred is not None and ev.occurred_at < max_occurred:
            late += 1
        if max_occurred is None or ev.occurred_at > max_occurred:
            max_occurred = ev.occurred_at

        now = clock.now()
        if prev_clock is not None and now < prev_clock:
            raise RuntimeError(f"SimClock bị lùi từ {prev_clock} về {now}")
        prev_clock = now

        if ev.event_id in seen_event_ids:
            duplicates += 1
        else:
            seen_event_ids.add(ev.event_id)
        seen_sessions.add(ev.session_id)

    return ReplaySummary(
        total_events=total,
        unique_event_ids=len(seen_event_ids),
        duplicate_events=duplicates,
        late_events=late,
        unique_sessions=len(seen_sessions),
        final_clock=clock.now(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="ase.simulator.replay")
    parser.add_argument("path", type=Path, help="Đường dẫn tới ase.swap.events.jsonl")
    parser.add_argument(
        "--speed", type=float, default=None, help="Tốc độ replay (None = nhanh nhất)"
    )
    args = parser.parse_args()
    summary = asyncio.run(replay_file(args.path, speed=args.speed))
    print(
        f"replayed {summary.total_events} events "
        f"(unique={summary.unique_event_ids}, dup={summary.duplicate_events}, "
        f"late={summary.late_events}, sessions={summary.unique_sessions}, "
        f"clock={summary.final_clock.isoformat()})"
    )


if __name__ == "__main__":
    main()
