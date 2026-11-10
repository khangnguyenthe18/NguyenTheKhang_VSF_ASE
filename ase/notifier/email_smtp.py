"""Gửi email cảnh báo qua SMTP (Mailpit / SMTP công ty) hoặc ghi preview ở dry_run."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
import structlog

from ase.common.config import Settings, get_settings
from ase.common.models import TeamBatch
from ase.notifier.batch import batch_file_stem, render_batch_email
from ase.notifier.render import RenderedEmail, render_email
from ase.notifier.retry import async_sleep, smtp_retrying
from ase.notifier.router import Alert, SendResult

log = structlog.get_logger(__name__)


class NotifierConfigError(RuntimeError):
    """Thiếu cấu hình bắt buộc khi gửi cảnh báo ở chế độ live."""


SmtpSendCallable = Callable[..., Awaitable[object]]


def build_mime_message(
    rendered: RenderedEmail,
    mail_from: str,
    mail_to: str,
) -> EmailMessage:
    """Tạo MIME multipart/alternative chứa cả bản text/plain và text/html."""
    msg = EmailMessage()
    msg["Subject"] = rendered.subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(rendered.text_body)
    msg.add_alternative(rendered.html_body, subtype="html")
    return msg


class SmtpEmailNotifier:
    """Notifier gửi email qua SMTP (live) hoặc lưu file .html/.eml vào preview_dir (dry_run)."""

    def __init__(
        self,
        settings: Settings | None = None,
        send_fn: SmtpSendCallable | None = None,
        sleep: Callable[[float], Awaitable[None]] = async_sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self._send_fn = send_fn
        self._sleep = sleep
        self._connection_failed: bool = False
        self._connection_error_detail: str | None = None

    def _write_preview(self, alert: Alert, rendered: RenderedEmail, msg: EmailMessage) -> Path:
        inc = alert.incident
        preview_dir = self.settings.preview_dir
        preview_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{inc.incident_id}-r{inc.revision}"
        html_path = preview_dir / f"{stem}.html"
        eml_path = preview_dir / f"{stem}.eml"
        html_path.write_text(rendered.html_body, encoding="utf-8")
        eml_path.write_bytes(msg.as_bytes())
        return html_path

    async def send(self, alert: Alert) -> SendResult:
        inc = alert.incident
        rendered = render_email(
            alert,
            feedback_base_url=self.settings.feedback_base_url,
            dashboard_profile=self.settings.dashboard_link_profile,
        )

        if self.settings.notify_mode == "dry_run":
            preview_from = (
                self.settings.mail_from or self.settings.smtp_user or "ase-alerts@ase.local"
            )
            preview_to = self.settings.mail_to or "ops@ase.local"
            msg = build_mime_message(rendered, mail_from=preview_from, mail_to=preview_to)
            html_path = self._write_preview(alert, rendered, msg)
            log.info(
                "email_preview_written",
                incident_id=inc.incident_id,
                session_id=inc.session_ids[0],
                revision=inc.revision,
                path=str(html_path),
            )
            return SendResult(channel="email", status="SENT", detail=str(html_path))

        return await self._deliver(
            lambda f, t: build_mime_message(rendered, mail_from=f, mail_to=t),
            incident_id=inc.incident_id,
            session_id=inc.session_ids[0],
            revision=inc.revision,
        )

    async def send_batch(self, batch: TeamBatch) -> SendResult:
        """Gửi tin gom của 1 team: mail có bảng + CSV đầy đủ đính kèm (CLAUDE.md §8)."""
        rendered = render_batch_email(
            batch,
            base_url=self.settings.feedback_base_url,
            dashboard_profile=self.settings.dashboard_link_profile,
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

        if self.settings.notify_mode == "dry_run":
            msg = build(
                self.settings.mail_from or self.settings.smtp_user or "ase-alerts@ase.local",
                self.settings.mail_to or "ops@ase.local",
            )
            preview_dir = self.settings.preview_dir
            preview_dir.mkdir(parents=True, exist_ok=True)
            stem = batch_file_stem(batch.batch_id)
            html_path = preview_dir / f"{stem}.html"
            html_path.write_text(rendered.html_body, encoding="utf-8")
            (preview_dir / f"{stem}.eml").write_bytes(msg.as_bytes())
            (preview_dir / rendered.csv_filename).write_text(rendered.csv_text, encoding="utf-8")
            log.info("batch_email_preview_written", batch_id=batch.batch_id, path=str(html_path))
            return SendResult(channel="email", status="SENT", detail=str(html_path))

        return await self._deliver(build, incident_id=batch.batch_id, session_id=None, revision=1)

    async def _deliver(
        self,
        build: Callable[[str, str], EmailMessage],
        *,
        incident_id: str,
        session_id: str | None,
        revision: int,
    ) -> SendResult:
        """Gửi live qua SMTP (dùng chung cho alert P1 và tin gom)."""
        # Chế độ live: dừng thử nhanh nếu lần kết nối trước đó trong phiên đã thất bại
        if self._connection_failed:
            log.error(
                "smtp_connection_previously_failed",
                incident_id=incident_id,
                session_id=session_id,
                error=self._connection_error_detail,
                hint="Mailpit chưa chạy? `make mail-up`",
            )
            return SendResult(
                channel="email",
                status="FAILED",
                detail=self._connection_error_detail,
            )

        smtp_user = self.settings.smtp_user
        smtp_password = (
            self.settings.smtp_password.get_secret_value()
            if self.settings.smtp_password is not None
            else None
        )
        mail_to = self.settings.mail_to
        mail_from = self.settings.mail_from

        missing: list[str] = []
        if not mail_from:
            missing.append("ASE_MAIL_FROM")
        if not mail_to:
            missing.append("ASE_MAIL_TO")
        if missing or not mail_from or not mail_to:
            err_msg = f"Thiếu cấu hình SMTP cho chế độ live: {', '.join(missing)}"
            log.error(
                "email_config_missing",
                incident_id=incident_id,
                session_id=session_id,
                missing=missing,
            )
            raise NotifierConfigError(err_msg)

        is_local = self.settings.smtp_host in ("localhost", "127.0.0.1", "::1")
        if smtp_user and self.settings.smtp_tls == "none" and not is_local:
            err_msg = (
                "Không được dùng smtp_tls='none' kèm username/password khi kết nối tới host ngoài"
                " localhost"
            )
            log.error("smtp_insecure_auth_rejected", host=self.settings.smtp_host)
            raise NotifierConfigError(err_msg)

        msg = build(mail_from, mail_to)
        sender: SmtpSendCallable = self._send_fn or aiosmtplib.send

        send_kwargs: dict[str, object] = {
            "hostname": self.settings.smtp_host,
            "port": self.settings.smtp_port,
            "use_tls": self.settings.smtp_tls == "ssl",
            "start_tls": self.settings.smtp_tls == "starttls",
            "timeout": 15.0,
        }
        if smtp_user is not None:
            send_kwargs["username"] = smtp_user
            send_kwargs["password"] = smtp_password

        try:
            async for attempt in smtp_retrying(sleep=self._sleep):
                with attempt:
                    await sender(msg, **send_kwargs)
        except (aiosmtplib.SMTPConnectError, ConnectionRefusedError, OSError) as exc:
            self._connection_failed = True
            is_conn_refused = (
                isinstance(exc, (ConnectionRefusedError, aiosmtplib.SMTPConnectError))
                or getattr(exc, "errno", None) == 111
            )
            hint = "Mailpit chưa chạy? `make mail-up`" if is_conn_refused else str(exc)
            err_msg = f"{hint} ({exc})"
            self._connection_error_detail = err_msg
            log.error(
                "smtp_connect_failed",
                incident_id=incident_id,
                session_id=session_id,
                hint=hint,
                error=str(exc),
            )
            return SendResult(channel="email", status="FAILED", detail=err_msg)

        log.info(
            "email_sent_live",
            incident_id=incident_id,
            session_id=session_id,
            revision=revision,
            to=mail_to,
        )
        return SendResult(channel="email", status="SENT", detail=mail_to)
