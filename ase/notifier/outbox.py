"""Transactional outbox dựa trên SQLite (aiosqlite) đảm bảo idempotency và rate limit."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import aiosqlite
import structlog
from pydantic import BaseModel

from ase.common.transport import Clock, WallClock

log = structlog.get_logger(__name__)

OutboxDecision = Literal["SEND", "ALREADY_SENT", "RATE_LIMITED"]
NotifyMode = Literal["dry_run", "live"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    key TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
"""


class OutboxEntry(BaseModel):
    key: str
    incident_id: str
    revision: int
    channel: str
    status: str
    attempts: int
    last_error: str | None = None
    created_at: str
    sent_at: str | None = None


def outbox_key(
    incident_id: str,
    revision: int,
    channel: str,
    mode: str | None = None,
) -> str:
    """Khoá idempotency: tách theo `mode` (`dry_run` / `live`) để dry_run không chặn live."""
    if mode:
        return f"{mode}:{incident_id}:{revision}:{channel}"
    return f"{incident_id}:{revision}:{channel}"


class Outbox:
    """Quản lý bảng `outbox` trong SQLite và bộ đếm `max_alerts_per_run`."""

    def __init__(
        self,
        db_path: Path,
        clock: Clock | None = None,
        max_alerts_per_run: int = 20,
        mode: NotifyMode = "dry_run",
    ) -> None:
        self.db_path = db_path
        self.clock: Clock = clock or WallClock()
        self.max_alerts_per_run = max_alerts_per_run
        self.mode: NotifyMode = mode
        self._alerts_in_run: set[tuple[str, int]] = set()
        self._initialised = False

    def _key(self, incident_id: str, revision: int, channel: str) -> str:
        return outbox_key(incident_id, revision, channel, self.mode)

    async def init_db(self) -> None:
        if self._initialised and self.db_path.exists():
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        self._initialised = True

    @property
    def alerts_sent_in_run(self) -> int:
        return len(self._alerts_in_run)

    async def is_sent(self, incident_id: str, revision: int, channel: str) -> bool:
        await self.init_db()
        key = self._key(incident_id, revision, channel)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT status FROM outbox WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return bool(row and row[0] == "SENT")

    async def acquire(self, incident_id: str, revision: int, channel: str) -> OutboxDecision:
        """Kiểm tra idempotency + rate limit trước khi gửi; lưu trạng thái vào `outbox`."""
        await self.init_db()
        key = self._key(incident_id, revision, channel)
        now_iso = self.clock.now().isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT status, attempts FROM outbox WHERE key = ?", (key,))
            row = await cursor.fetchone()
            if row is not None and row[0] == "SENT":
                return "ALREADY_SENT"

            alert_pair = (incident_id, revision)
            if (
                alert_pair not in self._alerts_in_run
                and len(self._alerts_in_run) >= self.max_alerts_per_run
            ):
                log.warning(
                    "outbox_max_alerts_exceeded",
                    incident_id=incident_id,
                    revision=revision,
                    channel=channel,
                    mode=self.mode,
                    max_alerts_per_run=self.max_alerts_per_run,
                )
                return "RATE_LIMITED"

            self._alerts_in_run.add(alert_pair)
            if row is None:
                await db.execute(
                    """
                    INSERT INTO outbox (
                        key, incident_id, revision, channel, status,
                        attempts, last_error, created_at, sent_at
                    )
                    VALUES (?, ?, ?, ?, 'PENDING', 1, NULL, ?, NULL)
                    """,
                    (key, incident_id, revision, channel, now_iso),
                )
            else:
                await db.execute(
                    """
                    UPDATE outbox
                    SET status = 'PENDING', attempts = attempts + 1
                    WHERE key = ?
                    """,
                    (key,),
                )
            await db.commit()
        return "SEND"

    async def mark_sent(self, incident_id: str, revision: int, channel: str) -> None:
        await self.init_db()
        key = self._key(incident_id, revision, channel)
        now_iso = self.clock.now().isoformat()
        self._alerts_in_run.add((incident_id, revision))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO outbox (
                    key, incident_id, revision, channel, status,
                    attempts, last_error, created_at, sent_at
                )
                VALUES (?, ?, ?, ?, 'SENT', 1, NULL, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    status = 'SENT',
                    last_error = NULL,
                    sent_at = excluded.sent_at
                """,
                (key, incident_id, revision, channel, now_iso, now_iso),
            )
            await db.commit()

    async def mark_failed(self, incident_id: str, revision: int, channel: str, error: str) -> None:
        await self.init_db()
        key = self._key(incident_id, revision, channel)
        now_iso = self.clock.now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO outbox (
                    key, incident_id, revision, channel, status,
                    attempts, last_error, created_at, sent_at
                )
                VALUES (?, ?, ?, ?, 'FAILED', 1, ?, ?, NULL)
                ON CONFLICT(key) DO UPDATE SET
                    status = 'FAILED',
                    last_error = excluded.last_error
                """,
                (key, incident_id, revision, channel, error, now_iso),
            )
            await db.commit()

    async def get(self, incident_id: str, revision: int, channel: str) -> OutboxEntry | None:
        await self.init_db()
        key = self._key(incident_id, revision, channel)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM outbox WHERE key = ?", (key,))
            row = await cursor.fetchone()
            if row is None:
                return None
            return OutboxEntry(**dict(row))

    async def list_all(self) -> list[OutboxEntry]:
        await self.init_db()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM outbox ORDER BY created_at, key")
            rows = await cursor.fetchall()
            return [OutboxEntry(**dict(r)) for r in rows]
