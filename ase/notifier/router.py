"""Routing incident sang team/channel OPS theo knowledge/routing.yaml (NOTIFIER_PLAN §3.4)."""

from __future__ import annotations

import re
from typing import Literal, Protocol

from pydantic import BaseModel

from ase.common.models import Incident
from ase.knowledge.loader import Routing, default_knowledge


class Alert(BaseModel):
    """Incident đã được định tuyến tới một đội OPS và channel tương ứng."""

    incident: Incident
    team: str
    channel: str


class SendResult(BaseModel):
    """Kết quả gửi alert trên một kênh."""

    channel: str
    status: Literal["SENT", "SKIPPED", "FAILED"]
    detail: str | None = None


class Notifier(Protocol):
    """Interface chung cho mọi kênh gửi cảnh báo (Mail, Teams...)."""

    async def send(self, alert: Alert) -> SendResult: ...


def team_to_channel(team: str) -> str:
    """Chuyển tên team (vd 'IT Ops') sang slug channel ('it-ops')."""
    slug = re.sub(r"[^a-z0-9]+", "-", team.strip().lower()).strip("-")
    return slug or "general"


class AlertRouter:
    """Định tuyến Incident dựa trên `Routing.route_for(category, country)`."""

    def __init__(self, routing: Routing | None = None) -> None:
        self._routing = routing or default_knowledge().routing

    def route(self, incident: Incident) -> Alert | None:
        r = self._routing.route_for(incident.category, incident.country)
        if not r.alert or r.team is None:
            return None
        routed_incident = incident
        if r.needs_human_review and not incident.needs_human_review:
            routed_incident = incident.model_copy(update={"needs_human_review": True})
        return Alert(
            incident=routed_incident,
            team=r.team,
            channel=team_to_channel(r.team),
        )


def route_incident(incident: Incident, routing: Routing | None = None) -> Alert | None:
    return AlertRouter(routing).route(incident)
