"""Orchestrator cho luồng thông báo: router → render → outbox → Mail + Teams (§3.4)."""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from ase.common.config import Settings, get_settings
from ase.common.models import Incident
from ase.common.transport import Clock, WallClock
from ase.notifier.email_smtp import SmtpEmailNotifier
from ase.notifier.outbox import Outbox
from ase.notifier.router import Alert, AlertRouter, Notifier, SendResult
from ase.notifier.teams_mock_client import TeamsMockNotifier

log = structlog.get_logger(__name__)


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


async def notify(
    incident: Incident,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> NotifyReport:
    service = NotifierService(settings=settings, clock=clock)
    return await service.notify(incident)
