"""Module thông báo cảnh báo của ASE (Mail + Mock Teams)."""

from __future__ import annotations

from ase.notifier.adaptive_card import build_adaptive_card
from ase.notifier.email_smtp import NotifierConfigError, SmtpEmailNotifier
from ase.notifier.outbox import Outbox, OutboxEntry, outbox_key
from ase.notifier.render import CATEGORY_LABELS, RenderedEmail, format_subject, render_email
from ase.notifier.router import Alert, AlertRouter, Notifier, SendResult, route_incident
from ase.notifier.service import NotifierService, NotifyReport, notify
from ase.notifier.teams_mock_client import TeamsMockNotifier

__all__ = [
    "CATEGORY_LABELS",
    "Alert",
    "AlertRouter",
    "Notifier",
    "NotifierConfigError",
    "NotifierService",
    "NotifyReport",
    "Outbox",
    "OutboxEntry",
    "RenderedEmail",
    "SendResult",
    "SmtpEmailNotifier",
    "TeamsMockNotifier",
    "build_adaptive_card",
    "format_subject",
    "notify",
    "outbox_key",
    "render_email",
    "route_incident",
]
