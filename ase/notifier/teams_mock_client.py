"""Client gửi Adaptive Card tới mock Teams qua HTTP POST (NOTIFIER_PLAN §3.4)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import structlog

from ase.common.config import Settings, get_settings
from ase.notifier.adaptive_card import build_adaptive_card
from ase.notifier.retry import async_sleep, http_retrying
from ase.notifier.router import Alert, SendResult

log = structlog.get_logger(__name__)


class TeamsMockNotifier:
    """POST Adaptive Card JSON tới `{teams_url}/webhook/{channel}` (chạy cả ở dry_run và live)."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = async_sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self._transport = transport
        self._sleep = sleep

    async def send(self, alert: Alert) -> SendResult:
        inc = alert.incident
        card = build_adaptive_card(alert, feedback_base_url=self.settings.feedback_base_url)
        url = f"{self.settings.teams_url.rstrip('/')}/webhook/{alert.channel}"

        async with httpx.AsyncClient(transport=self._transport, timeout=10.0) as client:
            async for attempt in http_retrying(sleep=self._sleep):
                with attempt:
                    resp = await client.post(url, json=card)
                    resp.raise_for_status()

        log.info(
            "teams_card_sent",
            incident_id=inc.incident_id,
            session_id=inc.session_ids[0],
            revision=inc.revision,
            channel=alert.channel,
        )
        return SendResult(channel="teams", status="SENT", detail=alert.channel)
