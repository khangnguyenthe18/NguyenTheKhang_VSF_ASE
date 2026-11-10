"""Render subject và body (text + HTML) cho cảnh báo Mail theo CLAUDE.md §8 (NOTIFIER_PLAN §3.4)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel

from ase.common.models import Category, Incident, Step
from ase.common.time import format_offset_for, to_local
from ase.knowledge.loader import Knowledge, default_knowledge
from ase.notifier.router import Alert

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Nhãn nhóm tiếng Việt theo đúng CLAUDE.md §8
CATEGORY_LABELS: dict[Category, str] = {
    Category.SYSTEM: "hệ thống",
    Category.HARDWARE: "thiết bị tủ/khoang",
    Category.PAYMENT: "thanh toán",
    Category.BUSINESS: "nghiệp vụ",
    Category.USER: "người dùng",
    Category.BATTERY_QUALITY: "chất lượng pin",
    Category.UNKNOWN: "chưa xác định",
}


def _should_autoescape(template_name: str | None) -> bool:
    """Chỉ bật autoescape cho template HTML (`email.html.j2`), không escape `email.txt.j2`."""
    if template_name is None:
        return False
    return template_name.endswith((".html", ".htm", ".xml", ".html.j2"))


_ENV = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=_should_autoescape,
    trim_blocks=True,
    lstrip_blocks=True,
)


# Tên bước tiếng Việt cho người đọc mail/card (CLAUDE.md §2); mã bước vẫn có trong CSV.
STEP_LABELS: dict[Step, str] = {
    Step.OPEN_APP: "Bước 1 · Mở App",
    Step.SELECT_SERVICE: "Bước 2 · Chọn dịch vụ đổi pin",
    Step.SCAN_QR: "Bước 3 · Quét QR",
    Step.CHECK_ELIGIBILITY: "Bước 4 · Kiểm tra điều kiện",
    Step.PAYMENT: "Bước 5 · Thanh toán",
    Step.RETURN_BATTERY: "Bước 6 · Trả pin cũ",
    Step.RECEIVE_BATTERY: "Bước 7 · Nhận pin mới",
}

SEVERITY_COLORS: dict[str, str] = {"P1": "#dc2626", "P2": "#d97706", "P3": "#64748b"}


def _dashboard_url(base_url: str, params: dict[str, str], profile: str | None) -> str:
    """Link tới trang Danh sách giao dịch đổi pin (`/dashboard`) kèm bộ lọc."""
    query = {"profile": profile, **params} if profile else params
    url = f"{base_url.rstrip('/')}/dashboard"
    return f"{url}?{urlencode(query)}" if query else url


def dashboard_session_url(base_url: str, session_id: str, profile: str | None = None) -> str:
    """Link mở danh sách giao dịch lọc đúng mã giao dịch này (chỉ đọc)."""
    return _dashboard_url(base_url, {"emsp_txn_id": session_id}, profile)


def dashboard_alerts_url(base_url: str, profile: str | None = None) -> str:
    """Link mở danh sách giao dịch thất bại (trang Cảnh báo riêng đã bỏ)."""
    return _dashboard_url(base_url, {"status": "FAILED"}, profile)


def error_description(error_code: str | None, knowledge: Knowledge | None = None) -> str:
    k = knowledge or default_knowledge()
    if error_code and k.error_catalog.has(error_code):
        return k.error_catalog.by_code[error_code].description
    return ""


class RenderedEmail(BaseModel):
    subject: str
    text_body: str
    html_body: str


def feedback_urls(
    incident_id: str, feedback_base_url: str = "http://localhost:8000"
) -> dict[str, str]:
    base = feedback_base_url.rstrip("/")
    return {
        "confirm": f"{base}/feedback/{incident_id}/confirm",
        "wrong_category": f"{base}/feedback/{incident_id}/wrong_category",
        "resolved": f"{base}/feedback/{incident_id}/resolved",
    }


def format_country(incident: Incident) -> str:
    if incident.countries:
        joined = ", ".join(incident.countries)
        if incident.country == "GLOBAL" or len(incident.countries) > 1:
            return f"{incident.country} ({joined})"
    return incident.country


def format_station_scope(incident: Incident) -> str:
    cab = incident.cabinet_id or "—"
    slot = incident.slot_id or "—"
    return f"{incident.station_id} / {cab} / Khoang {slot}"


def step_description_for(
    step: Step, error_code: str | None = None, knowledge: Knowledge | None = None
) -> str:
    """Lấy mô tả bước từ state_machine.yaml và error_catalog.yaml (không hardcode)."""
    k = knowledge or default_knowledge()
    service = k.state_machine.steps[step].service
    if error_code and k.error_catalog.has(error_code):
        err_desc = k.error_catalog.by_code[error_code].description
        return f"{service} ({err_desc})"
    return service


def format_subject(incident: Incident) -> str:
    """Subject chuẩn CLAUDE.md §8 theo giờ địa phương của trạm."""
    label = CATEGORY_LABELS[incident.category]
    session_id = incident.session_ids[0]
    local_dt = to_local(incident.first_seen_at, incident.timezone)
    return (
        f"[ASE] Cảnh báo giao dịch lỗi có thể liên quan đến {label}"
        f" _ {session_id} _ {local_dt:%H:%M:%S %d/%m/%Y}"
    )


def render_email(
    alert: Alert,
    feedback_base_url: str = "http://localhost:8000",
    knowledge: Knowledge | None = None,
    dashboard_profile: str | None = None,
) -> RenderedEmail:
    inc = alert.incident
    k = knowledge or default_knowledge()
    local_dt = to_local(inc.first_seen_at, inc.timezone)
    offset = format_offset_for(inc.first_seen_at, inc.timezone)
    time_display = (
        f"{local_dt:%H:%M:%S} ngày {local_dt:%d/%m/%Y} ({offset} – giờ địa phương của trạm)"
    )
    urls = feedback_urls(inc.incident_id, feedback_base_url)
    subject = format_subject(inc)
    label = CATEGORY_LABELS[inc.category]

    ctx: dict[str, object] = {
        "subject": subject,
        "team": alert.team,
        "incident_id": inc.incident_id,
        "revision": inc.revision,
        "session_id": inc.session_ids[0],
        "time_display": time_display,
        "country_display": format_country(inc),
        "station_display": format_station_scope(inc),
        "step": inc.step.value,
        "step_label": STEP_LABELS[inc.step],
        "step_description": step_description_for(inc.step, inc.error_code, k),
        "error_description": error_description(inc.error_code, k),
        "severity_color": SEVERITY_COLORS.get(inc.severity.value, "#64748b"),
        "session_url": dashboard_session_url(
            feedback_base_url, inc.session_ids[0], dashboard_profile
        ),
        "alerts_url": dashboard_alerts_url(feedback_base_url, dashboard_profile),
        "error_code_display": inc.error_code or "—",
        "category_code": inc.category.value,
        "category_label": label,
        "severity": inc.severity.value,
        "confidence_display": f"{inc.confidence:.2f}",
        "needs_human_review": inc.needs_human_review,
        "n_sessions": inc.n_sessions,
        "n_stations": inc.n_stations,
        "window_minutes": inc.window_minutes,
        "probable_cause": inc.probable_cause,
        "evidence_display": ", ".join(inc.evidence) if inc.evidence else "—",
        "suggested_actions_display": "; ".join(inc.suggested_actions)
        if inc.suggested_actions
        else "—",
        "runbook_display": ", ".join(inc.runbook_ids) if inc.runbook_ids else "—",
        "confirm_url": urls["confirm"],
        "wrong_category_url": urls["wrong_category"],
        "resolved_url": urls["resolved"],
    }

    text_body = _ENV.get_template("email.txt.j2").render(**ctx)
    html_body = _ENV.get_template("email.html.j2").render(**ctx)
    return RenderedEmail(subject=subject, text_body=text_body, html_body=html_body)
