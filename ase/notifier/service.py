"""Orchestrator cho luồng thông báo: router → render → outbox → Mail + Teams (§3.4)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Protocol, runtime_checkable

import structlog
from pydantic import BaseModel

from ase.common.config import Settings, get_settings
from ase.common.models import Incident, TeamBatch
from ase.common.transport import Clock, WallClock
from ase.notifier.email_smtp import SmtpEmailNotifier
from ase.notifier.outbox import Outbox
from ase.notifier.router import Alert, AlertRouter, Notifier, SendResult
from ase.notifier.teams_mock_client import TeamsMockNotifier

log = structlog.get_logger(__name__)


@runtime_checkable
class BatchNotifier(Protocol):
    """Kênh hỗ trợ gửi tin gom (`SmtpEmailNotifier`, `TeamsMockNotifier`)."""

    async def send_batch(self, batch: TeamBatch) -> SendResult: ...


class BatchReport(BaseModel):
    batch_id: str
    results: list[SendResult]


class NotifyReport(BaseModel):
    alert: Alert | None
    results: list[SendResult]


class NotifierService:
    """Điều phối gửi cảnh báo qua các kênh sau interface `Notifier` kèm transactional outbox."""

    def __init__(
        self,
        settings: Settings | None = None,
        clock: Clock | None = None,
        router: AlertRouter | None = None,
        outbox: Outbox | None = None,
        email_notifier: Notifier | None = None,
        teams_notifier: Notifier | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.clock: Clock = clock or WallClock()
        self.router = router or AlertRouter()
        self.outbox = outbox or Outbox(
            self.settings.notifier_db,
            clock=self.clock,
            max_alerts_per_run=self.settings.max_alerts_per_run,
            mode=self.settings.notify_mode,
        )
        self.email_notifier: Notifier = email_notifier or SmtpEmailNotifier(self.settings)
        self.teams_notifier: Notifier = teams_notifier or TeamsMockNotifier(self.settings)

    async def notify(self, incident: Incident) -> NotifyReport:
        session_id = incident.session_ids[0]
        alert = self.router.route(incident)
        if alert is None:
            log.info(
                "incident_suppressed",
                incident_id=incident.incident_id,
                session_id=session_id,
                category=incident.category.value,
            )
            return NotifyReport(alert=None, results=[])

        results: list[SendResult] = []
        channels: list[tuple[str, Notifier]] = [
            ("email", self.email_notifier),
            ("teams", self.teams_notifier),
        ]

        for channel_name, notifier in channels:
            decision = await self.outbox.acquire(
                incident.incident_id, incident.revision, channel_name
            )
            if decision == "ALREADY_SENT":
                log.info(
                    "alert_skipped_duplicate",
                    incident_id=incident.incident_id,
                    session_id=session_id,
                    revision=incident.revision,
                    channel=channel_name,
                )
                results.append(
                    SendResult(channel=channel_name, status="SKIPPED", detail="already_sent")
                )
                continue

            if decision == "RATE_LIMITED":
                log.warning(
                    "alert_skipped_rate_limit",
                    incident_id=incident.incident_id,
                    session_id=session_id,
                    revision=incident.revision,
                    channel=channel_name,
                    max_alerts_per_run=self.outbox.max_alerts_per_run,
                )
                results.append(
                    SendResult(channel=channel_name, status="SKIPPED", detail="max_alerts_per_run")
                )
                continue

            try:
                res = await notifier.send(alert)
            except Exception as exc:
                await self.outbox.mark_failed(
                    incident.incident_id, incident.revision, channel_name, str(exc)
                )
                log.error(
                    "alert_send_failed",
                    incident_id=incident.incident_id,
                    session_id=session_id,
                    revision=incident.revision,
                    channel=channel_name,
                    error=str(exc),
                )
                results.append(SendResult(channel=channel_name, status="FAILED", detail=str(exc)))
                continue

            if res.status == "SENT":
                await self.outbox.mark_sent(incident.incident_id, incident.revision, channel_name)
                log.info(
                    "alert_dispatched",
                    incident_id=incident.incident_id,
                    session_id=session_id,
                    revision=incident.revision,
                    channel=channel_name,
                    team=alert.team,
                )
            elif res.status == "FAILED":
                await self.outbox.mark_failed(
                    incident.incident_id,
                    incident.revision,
                    channel_name,
                    res.detail or "failed",
                )
            results.append(res)

        return NotifyReport(alert=alert, results=results)

    async def notify_batch(self, batch: TeamBatch) -> BatchReport:
        """Gửi tin gom qua Mail + Teams; idempotency qua outbox với khoá `batch_id` (rev 1)."""
        results: list[SendResult] = []
        for channel_name, notifier in (
            ("email", self.email_notifier),
            ("teams", self.teams_notifier),
        ):
            if not isinstance(notifier, BatchNotifier):
                results.append(
                    SendResult(channel=channel_name, status="SKIPPED", detail="no_batch_support")
                )
                continue
            results.append(
                await self._send_once(
                    batch.batch_id, 1, channel_name, partial(notifier.send_batch, batch)
                )
            )
        return BatchReport(batch_id=batch.batch_id, results=results)

    async def _send_once(
        self,
        item_id: str,
        revision: int,
        channel_name: str,
        send: Callable[[], Awaitable[SendResult]],
    ) -> SendResult:
        decision = await self.outbox.acquire(item_id, revision, channel_name)
        if decision == "ALREADY_SENT":
            log.info("batch_skipped_duplicate", batch_id=item_id, channel=channel_name)
            return SendResult(channel=channel_name, status="SKIPPED", detail="already_sent")
        if decision == "RATE_LIMITED":
            log.warning("batch_skipped_rate_limit", batch_id=item_id, channel=channel_name)
            return SendResult(channel=channel_name, status="SKIPPED", detail="max_alerts_per_run")
        try:
            res = await send()
        except Exception as exc:
            await self.outbox.mark_failed(item_id, revision, channel_name, str(exc))
            log.error("batch_send_failed", batch_id=item_id, channel=channel_name, error=str(exc))
            return SendResult(channel=channel_name, status="FAILED", detail=str(exc))
        if res.status == "SENT":
            await self.outbox.mark_sent(item_id, revision, channel_name)
            log.info("batch_dispatched", batch_id=item_id, channel=channel_name)
        elif res.status == "FAILED":
            await self.outbox.mark_failed(item_id, revision, channel_name, res.detail or "failed")
        return res


async def notify(
    incident: Incident,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> NotifyReport:
    service = NotifierService(settings=settings, clock=clock)
    return await service.notify(incident)
