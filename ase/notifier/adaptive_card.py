"""Dựng Adaptive Card 1.5 cho Microsoft Teams / mock Teams (NOTIFIER_PLAN §3.4)."""

from __future__ import annotations

from ase.common.models import Severity
from ase.common.time import format_local
from ase.knowledge.loader import Knowledge
from ase.notifier.render import (
    CATEGORY_LABELS,
    STEP_LABELS,
    dashboard_session_url,
    feedback_urls,
    format_country,
    format_station_scope,
    step_description_for,
)
from ase.notifier.router import Alert


def build_adaptive_card(
    alert: Alert,
    feedback_base_url: str = "http://localhost:8000",
    knowledge: Knowledge | None = None,
    dashboard_profile: str | None = None,
) -> dict[str, object]:
    """Sinh JSON Adaptive Card 1.5 rút gọn cho một Alert."""
    inc = alert.incident
    label = CATEGORY_LABELS[inc.category]
    is_p1 = inc.severity is Severity.P1
    urls = feedback_urls(inc.incident_id, feedback_base_url)

    rev_suffix = f" (r{inc.revision})" if inc.revision > 1 else ""
    header_items: list[dict[str, object]] = [
        {
            "type": "TextBlock",
            "text": f"[{inc.severity.value}] Cảnh báo {label} — {inc.incident_id}{rev_suffix}",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        }
    ]
    if is_p1:
        header_items.append(
            {
                "type": "TextBlock",
                "text": "@Người trực — Cảnh báo P1 cần kiểm tra ngay",
                "color": "Attention",
                "weight": "Bolder",
                "wrap": True,
            }
        )
    if inc.needs_human_review:
        header_items.append(
            {
                "type": "TextBlock",
                "text": "Cần OPS kiểm tra thủ công (needs_human_review)",
                "color": "Warning",
                "isSubtle": True,
                "wrap": True,
            }
        )

    step_desc = step_description_for(inc.step, inc.error_code, knowledge)
    session_url_fact = dashboard_session_url(
        feedback_base_url, inc.session_ids[0], dashboard_profile
    )
    facts: list[dict[str, str]] = [
        {"title": "Mã GD", "value": f"[{inc.session_ids[0]}]({session_url_fact})"},
        {"title": "Giờ địa phương", "value": format_local(inc.first_seen_at, inc.timezone)},
        {"title": "Quốc gia", "value": format_country(inc)},
        {"title": "Trạm / Tủ / Khoang", "value": format_station_scope(inc)},
        {
            "title": "Bước lỗi",
            "value": f"{STEP_LABELS[inc.step]} ({inc.step.value} – {step_desc})",
        },
        {"title": "Mã lỗi", "value": inc.error_code or "—"},
        {
            "title": "Phân loại",
            "value": (
                f"{inc.category.value} ({label}) – {inc.severity.value}"
                f" (tin cậy {inc.confidence:.2f})"
            ),
        },
        {
            "title": "Phạm vi",
            "value": (
                f"{inc.n_sessions} GD / {inc.n_stations} trạm"
                f" / {inc.window_minutes} phút ({inc.incident_id})"
            ),
        },
    ]

    body: list[dict[str, object]] = [
        {
            "type": "Container",
            "style": "attention" if is_p1 else "emphasis",
            "items": header_items,
        },
        {
            "type": "FactSet",
            "facts": facts,
        },
        {
            "type": "TextBlock",
            "text": f"**Nguyên nhân khả nghi:** {inc.probable_cause}",
            "wrap": True,
        },
    ]
    if inc.suggested_actions:
        runbooks = f" (Runbook: {', '.join(inc.runbook_ids)})" if inc.runbook_ids else ""
        body.append(
            {
                "type": "TextBlock",
                "text": f"**Đề xuất:** {'; '.join(inc.suggested_actions)}{runbooks}",
                "wrap": True,
                "isSubtle": True,
            }
        )

    session_url = dashboard_session_url(feedback_base_url, inc.session_ids[0], dashboard_profile)
    actions: list[dict[str, str]] = [
        {
            "type": "Action.OpenUrl",
            "title": "Xem giao dịch",
            "url": session_url,
            "style": "positive",
        },
        {
            "type": "Action.OpenUrl",
            "title": "Xác nhận đúng",
            "url": urls["confirm"],
        },
        {
            "type": "Action.OpenUrl",
            "title": "Sai phân loại",
            "url": urls["wrong_category"],
        },
        {
            "type": "Action.OpenUrl",
            "title": "Đã xử lý",
            "url": urls["resolved"],
        },
    ]

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "incident_id": inc.incident_id,
        "revision": inc.revision,
        "team": alert.team,
        "channel": alert.channel,
        "severity": inc.severity.value,
        "category": inc.category.value,
        # Metadata cho bảng Cảnh báo của dashboard (mock Teams lưu nguyên card)
        "station_id": inc.station_id,
        "cabinet_id": inc.cabinet_id,
        "slot_id": inc.slot_id,
        "error_code": inc.error_code,
        "country": inc.country,
        "timezone": inc.timezone,
        "first_seen_at": inc.first_seen_at.isoformat(),
        "session_ids": inc.session_ids[:5],
        "probable_cause": inc.probable_cause,
        "body": body,
        "actions": actions,
    }
