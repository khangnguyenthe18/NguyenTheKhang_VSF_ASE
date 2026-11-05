"""UTC ↔ giờ địa phương. Chỉ dùng ở tầng hiển thị; lưu trữ luôn UTC (CLAUDE.md §3.7)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime is not allowed")
    return dt.astimezone(UTC)


def to_local(dt: datetime, timezone: str) -> datetime:
    return ensure_utc(dt).astimezone(ZoneInfo(timezone))


def format_offset(dt: datetime) -> str:
    """`GMT+7`, `GMT+8`, `GMT+5:30`, `GMT-3:30`, `GMT+0` (dt phải tz-aware)."""
    off = dt.utcoffset()
    if off is None:
        raise ValueError("naive datetime is not allowed")
    total_minutes = int(off.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"GMT{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


def format_offset_for(dt: datetime, timezone: str) -> str:
    return format_offset(to_local(dt, timezone))


def format_local(dt: datetime, timezone: str) -> str:
    """`HH:mm:ss dd/MM/yyyy (GMT+x)` theo giờ địa phương của trạm."""
    local = to_local(dt, timezone)
    return f"{local:%H:%M:%S %d/%m/%Y} ({format_offset(local)})"

