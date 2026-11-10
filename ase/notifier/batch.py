"""Render tin gom P2/P3/USER theo team (CLAUDE.md §8 "Lịch gửi thông báo").

- Mail: bảng tối đa `notify.max_rows_mail` dòng (nhóm theo incident) + CSV đầy đủ đính kèm.
- Card Teams: chỉ số + top 10 incident + link dashboard.
- Giờ chu kỳ hiển thị theo `notify.daily_report_timezone` (giờ của đội OPS trung tâm); giờ từng
  lỗi hiển thị theo `timezone` của trạm. Không hardcode GMT+7.
"""

from __future__ import annotations

import csv
import io
from collections import Counter

from pydantic import BaseModel

from ase.common.models import BatchRow, Category, Severity, TeamBatch
from ase.common.time import format_local, format_offset_for, to_local
from ase.knowledge.loader import Knowledge, default_knowledge
from ase.notifier.render import (
    _ENV,
    CATEGORY_LABELS,
    SEVERITY_COLORS,
    STEP_LABELS,
    dashboard_alerts_url,
    dashboard_session_url,
    error_description,
)

CSV_COLUMNS: tuple[str, ...] = (
    "session_id",
    "incident_id",
    "category",
    "severity",
    "error_code",
    "step",
    "country",
    "station_id",
    "cabinet_id",
    "slot_id",
    "local_time",
    "timezone",
    "occurred_at_utc",
    "detected_at_utc",
)


class RenderedBatchEmail(BaseModel):
    subject: str
    text_body: str
    html_body: str
    csv_filename: str
    csv_text: str
    n_rows_shown: int
    n_rows_total: int


def batch_file_stem(batch_id: str) -> str:
    return batch_id.replace(":", "_")


def batch_csv(batch: TeamBatch) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in batch.rows:
        w.writerow(
            [
                r.session_id,
                r.incident_id or "",
                r.category.value,
                r.severity.value,
                r.error_code or "",
                r.step.value,
                r.country,
                r.station_id,
                r.cabinet_id or "",
                r.slot_id or "",
                format_local(r.occurred_at, r.timezone),
                r.timezone,
                r.occurred_at.isoformat(),
                r.detected_at.isoformat(),
            ]
        )
    return buf.getvalue()


def _window_display(batch: TeamBatch, tz: str) -> str:
    s = to_local(batch.window_start, tz)
    e = to_local(batch.window_end, tz)
    return f"{s:%H:%M}–{e:%H:%M} ngày {s:%d/%m/%Y} ({format_offset_for(batch.window_start, tz)})"


_CARD_SEVERITY_COLOR: dict[Severity, str] = {Severity.P1: "Attention", Severity.P2: "Warning"}


def _first_session_by_incident(batch: TeamBatch) -> dict[str, str]:
    """Phiên đầu tiên của mỗi incident trong tin gom, để làm link "Xem" từ bảng incident."""
    out: dict[str, str] = {}
    for r in batch.rows:
        if r.incident_id and r.incident_id not in out:
            out[r.incident_id] = r.session_id
    return out


def max_batch_severity(batch: TeamBatch) -> Severity | None:
    sevs = [r.severity for r in batch.rows] + [i.severity for i in batch.incidents]
    for s in (Severity.P1, Severity.P2, Severity.P3):
        if s in sevs:
            return s
    return None


def render_batch_email(
    batch: TeamBatch,
    *,
    knowledge: Knowledge | None = None,
    base_url: str = "http://localhost:8000",
    dashboard_profile: str | None = None,
) -> RenderedBatchEmail:
    k = knowledge or default_knowledge()
    dashboard_url = dashboard_alerts_url(base_url, dashboard_profile)
    first_sid = _first_session_by_incident(batch)
    ncfg = k.detector.notify
    tz = ncfg.daily_report_timezone
    window = _window_display(batch, tz)
    n_total = len(batch.rows)
    n_inc = len(batch.incidents)
    subject = (
        f"[ASE] Tổng hợp lỗi cho {batch.team} _ {n_total} giao dịch, {n_inc} incident _ {window}"
    )
    shown = batch.rows[: ncfg.max_rows_mail]
    rows_ctx = [
        {
            "session_id": r.session_id,
            "session_url": dashboard_session_url(base_url, r.session_id, dashboard_profile),
            "incident_id": r.incident_id or "—",
            "local_time": format_local(r.occurred_at, r.timezone),
            "country": r.country,
            "station": f"{r.station_id} / {r.cabinet_id or '—'} / {r.slot_id or '—'}",
            "step": r.step.value,
            "step_label": STEP_LABELS[r.step],
            "error_code": r.error_code or "—",
            "error_description": error_description(r.error_code, k),
            "category": f"{r.category.value} ({CATEGORY_LABELS[r.category]})",
            "category_label": CATEGORY_LABELS[r.category],
            "severity": r.severity.value,
            "severity_color": SEVERITY_COLORS.get(r.severity.value, "#64748b"),
        }
        for r in shown
    ]
    inc_ctx = [
        {
            "incident_id": i.incident_id,
            "category": f"{i.category.value} ({CATEGORY_LABELS[i.category]})",
            "category_label": CATEGORY_LABELS[i.category],
            "severity": i.severity.value,
            "severity_color": SEVERITY_COLORS.get(i.severity.value, "#64748b"),
            "error_code": i.error_code or "—",
            "error_description": error_description(i.error_code, k),
            "step": i.step.value,
            "step_label": STEP_LABELS[i.step],
            "session_url": (
                dashboard_session_url(base_url, first_sid[i.incident_id], dashboard_profile)
                if i.incident_id in first_sid
                else ""
            ),
            "scope": i.scope,
            "n_sessions": i.n_sessions,
            "n_stations": i.n_stations,
            "first_seen": format_local(i.first_seen_at, tz),
            "needs_human_review": i.needs_human_review,
        }
        for i in batch.incidents
    ]
    by_cat = Counter(r.category.value for r in batch.rows)
    by_cat_ctx = [
        {"code": c, "label": CATEGORY_LABELS[Category(c)], "n": n}
        for c, n in sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    ctx: dict[str, object] = {
        "subject": subject,
        "team": batch.team,
        "batch_id": batch.batch_id,
        "window": window,
        "n_total": n_total,
        "n_shown": len(shown),
        "n_incidents": n_inc,
        "by_category": ", ".join(f"{c}: {n}" for c, n in sorted(by_cat.items())) or "—",
        "by_category_items": by_cat_ctx,
        "rows": rows_ctx,
        "incidents": inc_ctx,
        "csv_filename": f"{batch_file_stem(batch.batch_id)}.csv",
        "dashboard_url": dashboard_url,
    }
    return RenderedBatchEmail(
        subject=subject,
        text_body=_ENV.get_template("batch_email.txt.j2").render(**ctx),
        html_body=_ENV.get_template("batch_email.html.j2").render(**ctx),
        csv_filename=str(ctx["csv_filename"]),
        csv_text=batch_csv(batch),
        n_rows_shown=len(shown),
        n_rows_total=n_total,
    )


def _session_row_item(
    r: BatchRow, base_url: str, dashboard_profile: str | None
) -> dict[str, object]:
    """1 giao dịch lỗi trên card tin gom (mã GD, giờ trạm, trạm, bước, mã lỗi); bấm mở dashboard."""
    station = r.station_id + (f" / {r.cabinet_id}" if r.cabinet_id else "")
    return {
        "type": "ColumnSet",
        "spacing": "Small",
        "separator": True,
        "selectAction": {
            "type": "Action.OpenUrl",
            "title": f"Xem giao dịch {r.session_id}",
            "url": dashboard_session_url(base_url, r.session_id, dashboard_profile),
        },
        "columns": [
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": (
                            f"**{r.session_id}** · {r.error_code or '—'}"
                            f" ({error_description(r.error_code) or CATEGORY_LABELS[r.category]})"
                        ),
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": (
                            f"{format_local(r.occurred_at, r.timezone)} · {r.country} · {station}"
                            f" · {STEP_LABELS[r.step]}"
                        ),
                        "isSubtle": True,
                        "size": "Small",
                        "spacing": "None",
                        "wrap": True,
                    },
                ],
            },
            {
                "type": "Column",
                "width": "auto",
                "verticalContentAlignment": "Center",
                "items": [{"type": "TextBlock", "text": "Xem GD →", "color": "Accent"}],
            },
        ],
    }


def build_batch_card(
    batch: TeamBatch,
    *,
    knowledge: Knowledge | None = None,
    base_url: str = "http://localhost:8000",
    dashboard_profile: str | None = None,
    top_n: int = 10,
) -> dict[str, object]:
    """Adaptive Card 1.5 rút gọn cho tin gom: chỉ số + top incident + link dashboard.

    Mỗi dòng incident bấm được (`selectAction`) → mở giao dịch đầu tiên của incident đó.
    """
    k = knowledge or default_knowledge()
    dashboard_url = dashboard_alerts_url(base_url, dashboard_profile)
    first_sid = _first_session_by_incident(batch)
    tz = k.detector.notify.daily_report_timezone
    sev = max_batch_severity(batch)
    by_cat = Counter(r.category.value for r in batch.rows)
    top = sorted(batch.incidents, key=lambda i: (-i.n_sessions, i.incident_id))[:top_n]
    facts = [
        {"title": "Chu kỳ", "value": _window_display(batch, tz)},
        {"title": "Số giao dịch lỗi", "value": str(len(batch.rows))},
        {"title": "Số incident", "value": str(len(batch.incidents))},
        {
            "title": "Theo nhóm",
            "value": ", ".join(
                f"{CATEGORY_LABELS[Category(c)]}: {n}" for c, n in sorted(by_cat.items())
            )
            or "—",
        },
    ]
    top_items: list[dict[str, object]] = []
    for i in top:
        sid = first_sid.get(i.incident_id)
        columns: list[dict[str, object]] = [
            {
                "type": "Column",
                "width": "auto",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": i.severity.value,
                        "weight": "Bolder",
                        "color": _CARD_SEVERITY_COLOR.get(i.severity, "Default"),
                    }
                ],
            },
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    {
                        "type": "TextBlock",
                        "text": (
                            f"**{i.error_code or '—'}** · {CATEGORY_LABELS[i.category]}"
                            f" · {STEP_LABELS[i.step]}"
                        ),
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": (
                            f"{i.scope} · {i.n_sessions} GD · {i.incident_id}"
                            + (" · cần OPS xem xét" if i.needs_human_review else "")
                        ),
                        "isSubtle": True,
                        "size": "Small",
                        "spacing": "None",
                        "wrap": True,
                    },
                ],
            },
        ]
        row: dict[str, object] = {
            "type": "ColumnSet",
            "spacing": "Small",
            "separator": True,
            "columns": columns,
        }
        if sid:
            columns.append(
                {
                    "type": "Column",
                    "width": "auto",
                    "verticalContentAlignment": "Center",
                    "items": [{"type": "TextBlock", "text": "Xem GD →", "color": "Accent"}],
                }
            )
            row["selectAction"] = {
                "type": "Action.OpenUrl",
                "title": f"Xem giao dịch {sid}",
                "url": dashboard_session_url(base_url, sid, dashboard_profile),
            }
        top_items.append(row)
    body: list[dict[str, object]] = [
        {
            "type": "Container",
            "style": "emphasis",
            "items": [
                {
                    "type": "TextBlock",
                    "text": f"Tổng hợp lỗi 15 phút — {batch.team}",
                    "weight": "Bolder",
                    "size": "Medium",
                    "wrap": True,
                }
            ],
        },
        {"type": "FactSet", "facts": facts},
    ]
    if top_items:
        body.append(
            {
                "type": "TextBlock",
                "text": "**Top incident** (bấm vào dòng để xem giao dịch):",
                "wrap": True,
            }
        )
        body.extend(top_items)
    # Lỗi không thuộc incident (USER → CSKH): liệt kê từng giao dịch, bấm được.
    loose = [r for r in batch.rows if not r.incident_id]
    if loose:
        body.append(
            {
                "type": "TextBlock",
                "text": (
                    f"**Giao dịch lỗi** ({min(len(loose), top_n)}/{len(loose)}"
                    ", bấm vào dòng để xem giao dịch):"
                ),
                "wrap": True,
            }
        )
        body.extend(_session_row_item(r, base_url, dashboard_profile) for r in loose[:top_n])
    body.append(
        {
            "type": "TextBlock",
            "text": "-- AI chỉ gợi ý, quyết định cuối cùng thuộc về OPS.",
            "isSubtle": True,
            "wrap": True,
        }
    )
    first_sids = [r.session_id for r in batch.rows[:5]]
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "kind": "batch",
        "incident_id": batch.batch_id,
        "revision": 1,
        "team": batch.team,
        "channel": batch.channel,
        "severity": sev.value if sev is not None else "P3",
        "category": by_cat.most_common(1)[0][0] if by_cat else "UNKNOWN",
        "first_seen_at": batch.window_start.isoformat(),
        "session_ids": first_sids,
        "body": body,
        "actions": [{"type": "Action.OpenUrl", "title": "Xem giao dịch lỗi", "url": dashboard_url}],
    }
