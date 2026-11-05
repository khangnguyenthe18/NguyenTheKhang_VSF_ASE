"""Sinh ID deterministic: cùng đầu vào → cùng ID (cần cho hash output giống nhau)."""

from __future__ import annotations

import hashlib
from datetime import datetime

from ase.common.time import ensure_utc


def session_id(day: datetime, seq: int) -> str:
    """`SW20261104-000123` (ngày UTC)."""
    return f"SW{ensure_utc(day):%Y%m%d}-{seq:06d}"


def event_id(session: str, step_no: int, seq: int = 0) -> str:
    """Event trùng (nhân bản) dùng lại đúng event_id gốc."""
    return f"evt-{session}-{step_no}-{seq}"


def payment_id(session: str, seq: int = 0) -> str:
    return f"PAY-{session}-{seq}"


def hash_user_id(raw: str, salt: str = "ase") -> str:
    return "u_" + hashlib.sha256(f"{salt}:{raw}".encode()).hexdigest()[:12]


def scenario_instance_id(scenario: str, index: int) -> str:
    return f"SCN-{scenario}-{index:03d}"

