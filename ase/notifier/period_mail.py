"""Mail báo cáo tuần / tháng (chỉ gửi mail, CLAUDE.md §8). Khoá idempotency = `report_id`."""

from __future__ import annotations

import csv
import io
from email.message import EmailMessage

from pydantic import BaseModel

from ase.common.config import Settings, get_settings
from ase.common.time import to_local
from ase.common.transport import Clock, WallClock
from ase.knowledge.loader import KpiSpec, default_kpi
from ase.notifier.email_smtp import SmtpEmailNotifier, build_mime_message
from ase.notifier.outbox import Outbox
from ase.notifier.render import _ENV, RenderedEmail
from ase.notifier.router import SendResult
from ase.report.period import CountrySection, PeriodReport

OPS_TEAM = "OPS"
COUNTRY_LABELS = {"VN": "VN", "PH": "PH", "ID": "ID", "ALL": "toàn mạng"}
MAX_OPEN_POINTS_MAIL = 10


class RenderedReport(BaseModel):
    subject: str
    text_body: str
    html_body: str
    csv_text: str
    csv_filename: str


def period_label(r: PeriodReport) -> str:
    a, b = r.period_start, r.period_end
    if r.kind == "monthly":
        return f"tháng {a:%m/%Y} ({a.day}/{a.month}–{b.day}/{b.month})"
    prefix = "tuần" if r.kind == "weekly" else "kỳ"
    return f"{prefix} {a:%d/%m}–{b:%d/%m/%Y}"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.2f}%"


def _secs(x: float | None) -> str:
    if x is None:
        return "—"
    m, s = divmod(round(x), 60)
    return f"{m} phút {s:02d} giây" if m else f"{s} giây"


def _section_ctx(s: CountrySection) -> dict[str, object]:
    top = [{**c.model_dump(mode="json"), "pct": f"{c.pct:.2f}"} for c in s.top]
    return {
        **s.model_dump(mode="json"),
        "top": top,
        "top_pct": f"{s.top_pct:.2f}",
        "country_label": COUNTRY_LABELS.get(s.country, s.country),
        "success_rate": _pct(s.success_rate),
        "failure_rate": _pct(s.failure_rate),
        "prev_failure_rate": _pct(s.prev_failure_rate) if s.prev_failure_rate is not None else None,
        "swap_p50": _secs(s.swap_seconds_p50),
        "swap_p95": _secs(s.swap_seconds_p95),
        "open_points": [o.model_dump(mode="json") for o in s.open_points[:MAX_OPEN_POINTS_MAIL]],
        "n_open_points": len(s.open_points),
    }


def report_csv(r: PeriodReport) -> str:
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow(
        ["Hệ thống BSS", "Mã lỗi", "Tên cảnh báo", "Mức", "Số phiên lỗi", "% trên tổng phiên lỗi"]
    )
    for s in r.sections:
        for c in s.all_codes:
            w.writerow([s.country, c.code, c.name, c.level.value if c.level else "", c.n, c.pct])
    w.writerow([])
    w.writerow(
        [
            "Hệ thống BSS",
            "Open point",
            "Ưu tiên",
            "Điểm ưu tiên",
            "Ảnh hưởng",
            "Đề xuất",
            "Bằng chứng",
        ]
    )
    for s in r.sections:
        for o in s.open_points:
            w.writerow(
                [s.country, o.title, o.priority, o.priority_score, o.impact,
                 "; ".join(o.recommendations), " ".join(o.evidence)]
            )  # fmt: skip
    return buf.getvalue()


def render_period_report(r: PeriodReport, spec: KpiSpec | None = None) -> RenderedReport:
    spec = spec or default_kpi()
    label = period_label(r)
    total = sum(s.n_failures for s in r.sections if s.country != "ALL")
    subject = f"[ASE] Báo cáo phiên đổi pin lỗi {label}: {total} phiên lỗi"
    gen = to_local(r.generated_at, r.timezone)
    note = (
        f"Dữ liệu từ {r.data_start:%d/%m} tới {r.data_end:%d/%m/%Y} · lập lúc {gen:%H:%M %d/%m/%Y}"
        if r.data_start and r.data_end
        else "Không có giao dịch trong kỳ"
    )
    csv_filename = r.report_id.replace(":", "-") + ".csv"
    ctx: dict[str, object] = {
        "subject": subject,
        "team": OPS_TEAM,
        "period_label": label,
        "data_note": note,
        "report_id": r.report_id,
        "repeat_min": spec.report.repeat_user_min_failures,
        "sections": [_section_ctx(s) for s in r.sections],
        "csv_filename": csv_filename,
    }
    return RenderedReport(
        subject=subject,
        text_body=_ENV.get_template("period_report.txt.j2").render(**ctx),
        html_body=_ENV.get_template("period_report.html.j2").render(**ctx),
        csv_text=report_csv(r),
        csv_filename=csv_filename,
    )


class PeriodReportNotifier:
    def __init__(
        self,
        settings: Settings | None = None,
        clock: Clock | None = None,
        outbox: Outbox | None = None,
        email: SmtpEmailNotifier | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.outbox = outbox or Outbox(
            self.settings.notifier_db,
            clock=clock or WallClock(),
            max_alerts_per_run=10_000,  # báo cáo kỳ không tính vào giới hạn cảnh báo
            mode=self.settings.notify_mode,
        )
        self.email = email or SmtpEmailNotifier(self.settings)

    async def send(self, report: PeriodReport) -> SendResult:
        decision = await self.outbox.acquire(report.report_id, 1, "email")
        if decision == "ALREADY_SENT":
            return SendResult(channel="email", status="SKIPPED", detail="already_sent")
        rendered = render_period_report(report)

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

        cfg = self.settings
        try:
            if cfg.notify_mode == "dry_run":
                cfg.preview_dir.mkdir(parents=True, exist_ok=True)
                stem = report.report_id.replace(":", "-")
                (cfg.preview_dir / f"{stem}.html").write_text(rendered.html_body, encoding="utf-8")
                msg = build(cfg.mail_from or "ase-agent@ase.local", cfg.mail_to or "ops@ase.local")
                (cfg.preview_dir / f"{stem}.eml").write_bytes(msg.as_bytes())
                res = SendResult(channel="email", status="SENT", detail=stem)
            else:
                res = await self.email._deliver(
                    build, incident_id=report.report_id, session_id=None, revision=1
                )
        except Exception as exc:
            await self.outbox.mark_failed(report.report_id, 1, "email", str(exc))
            return SendResult(channel="email", status="FAILED", detail=str(exc))
        if res.status == "SENT":
            await self.outbox.mark_sent(report.report_id, 1, "email")
        else:
            await self.outbox.mark_failed(report.report_id, 1, "email", res.detail or "failed")
        return res
