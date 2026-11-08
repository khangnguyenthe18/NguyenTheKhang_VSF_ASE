"""Transport sau interface (CLAUDE.md §5.1): detector chỉ phụ thuộc EventSource/EventSink/Clock."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import orjson

from ase.common.models import SwapEvent


class Clock(Protocol):
    def now(self) -> datetime: ...


class SimClock:
    """Thời gian tiến theo event được replay, không theo đồng hồ thật."""

    def __init__(self) -> None:
        self._now: datetime | None = None

    def advance(self, t: datetime) -> None:
        if t.tzinfo is None:
            raise ValueError("naive datetime is not allowed")
        if self._now is None or t > self._now:  # event đến trễ không làm clock lùi
            self._now = t

    def is_set(self) -> bool:
        """Đã có mốc thời gian chưa (chưa có thì `now()` ném lỗi)."""
        return self._now is not None

    def now(self) -> datetime:
        if self._now is None:
            raise RuntimeError("clock chưa khởi tạo")
        return self._now


class WallClock:
    """Đồng hồ thật, chỉ dùng khi chạy realtime."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class EventSource(Protocol):
    def events(self) -> AsyncIterator[SwapEvent]: ...


class EventSink(Protocol):
    async def write(self, event: SwapEvent) -> None: ...
    async def close(self) -> None: ...


class JsonlSource:
    def __init__(self, path: Path, clock: SimClock, speed: float | None = None) -> None:
        self.path, self.clock, self.speed = path, clock, speed  # speed=None: nhanh nhất

    async def events(self) -> AsyncIterator[SwapEvent]:
        prev: datetime | None = None
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                ev = SwapEvent.model_validate_json(line)
                if self.speed and prev and ev.occurred_at > prev:
                    await asyncio.sleep((ev.occurred_at - prev).total_seconds() / self.speed)
                prev = ev.occurred_at if prev is None else max(prev, ev.occurred_at)
                self.clock.advance(ev.occurred_at)
                yield ev


class JsonlSink:
    """Ghi theo đúng thứ tự gọi (thứ tự dòng = thứ tự đến, không sort). Output byte-deterministic."""  # noqa: E501

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._f = path.open("wb")

    async def write(self, event: SwapEvent) -> None:
        self._f.write(orjson.dumps(event.model_dump(mode="json"), option=orjson.OPT_UTC_Z) + b"\n")

    async def close(self) -> None:
        self._f.close()
