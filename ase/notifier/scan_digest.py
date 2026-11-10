"""Thông báo mỗi lần quét: 1 mail + 1 card Teams cho đội OPS, chỉ khi có lỗi (CLAUDE.md §8).

Khoá idempotency qua outbox là `scan_id` (`scan:{window_start}`): chạy lại cùng dữ liệu không gửi
thêm. Thông báo do code dựng từ `ScanDigest` đã validate; LLM không tham gia gửi.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from collections.abc import Awaitable, Callable
from email.message import EmailMessage

import structlog
from pydantic import BaseModel

from ase.agent.models import Detection, ScanDigest, ScanIncident
from ase.common.config import Settings, get_settings
from ase.common.models import AlertLevel, TxnStatus
from ase.common.time import format_local, format_offset, to_local
from ase.common.transport import Clock, WallClock
from ase.knowledge.loader import ScanSpec, default_knowledge
from ase.notifier.email_smtp import SmtpEmailNotifier, build_mime_message
from ase.notifier.outbox import Outbox
from ase.notifier.render import _ENV, RenderedEmail, _dashboard_url, dashboard_session_url
from ase.notifier.router import SendResult
from ase.notifier.teams_mock_client import TeamsMockNotifier

log = structlog.get_logger(__name__)

OPS_TEAM = "OPS"
OPS_CHANNEL = "ops"
STATUS_LABELS: dict[TxnStatus, str] = {
    TxnStatus.CREATED: "Tạo mới (kẹt)",
    TxnStatus.BATTERY_IN: "Đưa pin vào (kẹt)",
    TxnStatus.COMPLETED: "Hoàn thành",
    TxnStatus.FAILED: "Thất bại",
}
KIND_LABELS = {"WIDE": "Lan rộng", "STATION": "Trạm", "CABINET": "Tủ"}
GROUP_LABELS = {"STUCK": "Giao dịch kẹt (không có mã lỗi)", "NO_CODE": "Thất bại không có mã lỗi"}
OUTSIDE_TABLE = "mã ngoài bảng chuẩn"
_LEVEL_COLORS = {"High": "#dc2626", "Low": "#d97706", "—": "#64748b"}
CSV_COLUMNS = [
    "Mã giao dịch trên eMSP",
    "Mã giao dịch trên tủ",
    "Giờ địa phương trạm",
    "Quốc gia",
    "Mã trạm",
    "Mã tủ",
    "Loại tủ",
    "Connector",
    "Trạng thái",
    "Lý do lỗi",
    "Tên cảnh báo",
    "Mức",
    "Đã trả pin, chưa nhận pin mới",
    "Sự cố",
]


class RenderedScanEmail(BaseModel):
    subject: str
    text_body: str
    html_body: str
    csv_text: str
    csv_filename: str


def error_label(d: Detection) -> str:
    """`D6031 – BatteryInTimeout (Low)`; mã ngoài bảng / không có mã được nói rõ."""
    if d.error_code is None:
        return GROUP_LABELS[d.group]
    if not d.in_table:
        return f"{d.error_code} – {OUTSIDE_TABLE}"
    level = d.level.value if d.level else "—"
    return f"{d.error_code} – {d.error_name} ({level})"


def window_display(digest: ScanDigest, timezone: str) -> str:
    start, end = to_local(digest.window_start, timezone), to_local(digest.scan_at, timezone)
    return f"{start:%H:%M}–{end:%H:%M} ngày {end:%d/%m/%Y} ({format_offset(end)})"


def scan_file_stem(scan_id: str) -> str:
    return scan_id.replace(":", "-")


def scan_csv(digest: ScanDigest) -> str:
    buf = io.StringIO()
    buf.write("﻿")  # BOM để Excel mở đúng tiếng Việt
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for d in digest.detections:
        w.writerow(
            [
                d.emsp_txn_id,
                d.cabinet_txn_id or "",
                format_local(d.event_at, d.timezone),
                d.country,
                d.station_id,
                d.cabinet_id or "",
                d.cabinet_type or "",
                d.connector or "",
                STATUS_LABELS[d.status],
                d.error_code or "",
                d.error_name or "",
                d.level.value if d.level else "",
                "x" if d.returned_not_received else "",
                d.incident_id or "",
            ]
        )
    return buf.getvalue()


def _group_text(inc: ScanIncident, names: dict[str, str]) -> str:
    return ", ".join(names.get(g, GROUP_LABELS.get(g, g)) for g in inc.groups)


def _summary(digest: ScanDigest) -> dict[str, int]:
    dets = digest.detections
    return {
        "n_total": len(dets),
        "n_failed": sum(d.kind == "FAILED" for d in dets),
        "n_stuck": sum(d.kind == "STUCK" for d in dets),
        "n_single": sum(d.incident_id is None for d in dets),
        "n_returned": sum(d.returned_not_received for d in dets),
        "n_outside": sum(d.error_code is not None and not d.in_table for d in dets),
        "n_new_incidents": sum(i.revision == 1 for i in digest.incidents),
    }


def _sorted_incidents(digest: ScanDigest) -> list[ScanIncident]:
    return sorted(
        digest.incidents,
        key=lambda i: (i.level is not AlertLevel.HIGH, -len(i.txn_ids), i.incident_id),
    )


def render_scan_email(
    digest: ScanDigest,
    *,
    spec: ScanSpec | None = None,
    base_url: str = "http://localhost:8000",
    dashboard_profile: str | None = None,
) -> RenderedScanEmail:
    spec = spec or default_knowledge().detector.scan
    counts = _summary(digest)
    window = window_display(digest, spec.timezone)
    labels = Counter(error_label(d) for d in digest.detections)
    names = {d.group: error_label(d) for d in digest.detections}
    # Dòng thuộc sự cố mức High lên trước, rồi theo thời gian
    high = {i.incident_id for i in digest.incidents if i.level is AlertLevel.HIGH}
    ordered = sorted(
        digest.detections,
        key=lambda d: (d.incident_id not in high, d.incident_id is None, d.event_at),
    )
    csv_filename = f"{scan_file_stem(digest.scan_id)}.csv"
    subject = (
        f"[ASE] Quét giao dịch đổi pin {window}: {counts['n_total']} giao dịch lỗi, "
        f"{len(digest.incidents)} sự cố"
    )
    ctx: dict[str, object] = {
        **counts,
        "subject": subject,
        "team": OPS_TEAM,
        "window": window,
        "scan_id": digest.scan_id,
        "n_scanned": digest.n_scanned,
        "by_code": [{"label": k, "n": n} for k, n in labels.most_common()],
        "incidents": [
            {
                "incident_id": i.incident_id,
                "revision": i.revision,
                "level": i.level.value if i.level else "—",
                "color": _LEVEL_COLORS[i.level.value if i.level else "—"],
                "kind_label": KIND_LABELS[i.kind],
                "scope": i.scope,
                "groups": _group_text(i, names),
                "n_new": len(i.txn_ids),
                "n_total": i.n_total,
                "first_seen": format_local(i.first_seen_at, spec.timezone),
            }
            for i in _sorted_incidents(digest)
        ],
        "rows": [
            {
                "emsp_txn_id": d.emsp_txn_id,
                "url": dashboard_session_url(base_url, d.emsp_txn_id, dashboard_profile),
                "local_time": format_local(d.event_at, d.timezone),
                "station_id": d.station_id,
                "cabinet_id": d.cabinet_id or "—",
                "connector": d.connector,
                "error": error_label(d),
                "status_label": STATUS_LABELS[d.status],
                "returned": d.returned_not_received,
                "incident_id": d.incident_id,
            }
            for d in ordered[: spec.max_rows_mail]
        ],
        "csv_filename": csv_filename,
        "dashboard_url": _dashboard_url(base_url, {"status": "FAILED"}, dashboard_profile),
    }
    return RenderedScanEmail(
        subject=subject,
        text_body=_ENV.get_template("scan_email.txt.j2").render(**ctx),
        html_body=_ENV.get_template("scan_email.html.j2").render(**ctx),
        csv_text=scan_csv(digest),
        csv_filename=csv_filename,
    )


def build_scan_card(
    digest: ScanDigest,
    *,
    spec: ScanSpec | None = None,
    base_url: str = "http://localhost:8000",
    dashboard_profile: str | None = None,
    top_n: int = 8,
) -> dict[str, object]:
    """Adaptive Card 1.5 rút gọn: chỉ số của lần quét + các sự cố + link dashboard."""
    spec = spec or default_knowledge().detector.scan
    counts = _summary(digest)
    names = {d.group: error_label(d) for d in digest.detections}
    has_high = any(d.level is AlertLevel.HIGH for d in digest.detections)
    facts = [
        {"title": "Chu kỳ", "value": window_display(digest, spec.timezone)},
        {
            "title": "Giao dịch lỗi mới",
            "value": f"{counts['n_total']} (Thất bại {counts['n_failed']}, "
            f"kẹt {counts['n_stuck']})",
        },
        {"title": "Sự cố", "value": str(len(digest.incidents))},
        {"title": "Lỗi lẻ", "value": str(counts["n_single"])},
    ]
    if counts["n_returned"]:
        facts.append({"title": "Đã trả pin, chưa nhận pin mới", "value": str(counts["n_returned"])})
    body: list[dict[str, object]] = [
        {
            "type": "Container",
            "style": "attention" if has_high else "emphasis",
            "items": [
                {
                    "type": "TextBlock",
                    "text": f"Quét giao dịch đổi pin — {OPS_TEAM}",
                    "weight": "Bolder",
                    "size": "Medium",
                }
            ],
        },
        {"type": "FactSet", "facts": facts},
    ]
    for i in _sorted_incidents(digest)[:top_n]:
        level = i.level.value if i.level else "—"
        body.append(
            {
                "type": "TextBlock",
                "wrap": True,
                "text": f"**[{level}] {i.incident_id}** (lần {i.revision}) · "
                f"{KIND_LABELS[i.kind]} {i.scope} · {_group_text(i, names)} · "
                f"{len(i.txn_ids)} mới / {i.n_total} tổng",
            }
        )
    body.append(
        {
            "type": "TextBlock",
            "text": "AI chỉ gợi ý, quyết định cuối cùng thuộc về OPS.",
            "isSubtle": True,
            "size": "Small",
            "wrap": True,
        }
    )
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "incident_id": digest.scan_id,
        "revision": 1,
        "body": body,
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Xem giao dịch lỗi",
                "url": _dashboard_url(base_url, {"status": "FAILED"}, dashboard_profile),
            }
        ],
    }


class ScanNotifier:
    """Gửi thông báo của một lần quét qua Mail + Teams, mỗi kênh đúng một lần (outbox)."""

    def __init__(
        self,
        settings: Settings | None = None,
        clock: Clock | None = None,
        outbox: Outbox | None = None,
        email: SmtpEmailNotifier | None = None,
        teams: TeamsMockNotifier | None = None,
        spec: ScanSpec | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.spec = spec or default_knowledge().detector.scan
        self.outbox = outbox or Outbox(
            self.settings.notifier_db,
            clock=clock or WallClock(),
            max_alerts_per_run=self.settings.max_alerts_per_run,
            mode=self.settings.notify_mode,
        )
        self.email = email or SmtpEmailNotifier(self.settings)
        self.teams = teams or TeamsMockNotifier(self.settings)

    async def notify(self, digest: ScanDigest) -> list[SendResult]:
        """Không có lỗi → không gửi gì (tránh spam OPS)."""
        if not digest.detections:
            return []
        return [
            await self._once(digest.scan_id, "email", lambda: self._send_email(digest)),
            await self._once(digest.scan_id, "teams", lambda: self._send_teams(digest)),
        ]

    async def _once(
        self, scan_id: str, channel: str, send: Callable[[], Awaitable[SendResult]]
    ) -> SendResult:
        decision = await self.outbox.acquire(scan_id, 1, channel)
        if decision == "ALREADY_SENT":
            return SendResult(channel=channel, status="SKIPPED", detail="already_sent")
        if decision == "RATE_LIMITED":
            return SendResult(channel=channel, status="SKIPPED", detail="max_alerts_per_run")
        try:
            res = await send()
        except Exception as exc:
            await self.outbox.mark_failed(scan_id, 1, channel, str(exc))
            log.error("scan_send_failed", scan_id=scan_id, channel=channel, error=str(exc))
            return SendResult(channel=channel, status="FAILED", detail=str(exc))
        if res.status == "SENT":
            await self.outbox.mark_sent(scan_id, 1, channel)
        elif res.status == "FAILED":
            await self.outbox.mark_failed(scan_id, 1, channel, res.detail or "failed")
        return res

    async def _send_email(self, digest: ScanDigest) -> SendResult:
        cfg = self.settings
        rendered = render_scan_email(
            digest,
            spec=self.spec,
            base_url=cfg.feedback_base_url,
            dashboard_profile=cfg.dashboard_link_profile,
        )

        def build(mail_from: str, mail_to: str) -> EmailMessage:
            msg = build_mime_message(
                RenderedEmail(
                    subject=rendered.subject,
                    text_body=rendered.text_body,
                    html_body=rendered.html_body,
                ),
                mail_from=mail_from,
                mail_to=mail_to,
            )
            msg.add_attachment(
                rendered.csv_text.encode("utf-8"),
                maintype="text",
                subtype="csv",
                filename=rendered.csv_filename,
            )
            return msg

        if cfg.notify_mode == "dry_run":
            cfg.preview_dir.mkdir(parents=True, exist_ok=True)
            stem = scan_file_stem(digest.scan_id)
            msg = build(cfg.mail_from or "ase-agent@ase.local", cfg.mail_to or "ops@ase.local")
            html_path = cfg.preview_dir / f"{stem}.html"
            html_path.write_text(rendered.html_body, encoding="utf-8")
            (cfg.preview_dir / f"{stem}.eml").write_bytes(msg.as_bytes())
            (cfg.preview_dir / rendered.csv_filename).write_text(
                rendered.csv_text, encoding="utf-8"
            )
            return SendResult(channel="email", status="SENT", detail=str(html_path))
        return await self.email._deliver(
            build, incident_id=digest.scan_id, session_id=None, revision=1
        )

    async def _send_teams(self, digest: ScanDigest) -> SendResult:
        cfg = self.settings
        card = build_scan_card(
            digest,
            spec=self.spec,
            base_url=cfg.feedback_base_url,
            dashboard_profile=cfg.dashboard_link_profile,
        )
        await self.teams._post(OPS_CHANNEL, card)
        return SendResult(channel="teams", status="SENT", detail=OPS_CHANNEL)
