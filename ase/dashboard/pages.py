"""APIRouter `/dashboard/...` trả trang HTML render qua Jinja2 (autoescape bắt buộc)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from ase.common.models import Step
from ase.dashboard.api import (
    normalize_country,
    resolve_notifier,
    resolve_profile_data,
    resolve_settings,
)
from ase.dashboard.data import load_ground_truth
from ase.dashboard.metrics import (
    STEP_VI_LABELS,
    build_alerts_page,
    build_batteries_page,
    build_ground_truth_page,
    build_incidents_placeholder,
    build_metrics_placeholder,
    build_overview,
    build_payments_page,
    build_session_detail,
    build_sessions_page,
    build_station_detail,
    build_stations_list,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

router = APIRouter(prefix="/dashboard", tags=["dashboard-pages"])


def _to_safe_json(obj: object) -> str:
    if isinstance(obj, BaseModel):
        raw = json.dumps(obj.model_dump(mode="json"), ensure_ascii=False)
    else:
        raw = json.dumps(obj, ensure_ascii=False, default=str)
    return raw.replace("</", "<\\/").replace("<!--", "<\\!--")


_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(
        ["html", "htm", "xml", "j2"], default_for_string=True, default=True
    ),
)
_env.filters["safe_json"] = _to_safe_json


def _render(template_name: str, **context: object) -> HTMLResponse:
    tmpl = _env.get_template(template_name)
    return HTMLResponse(tmpl.render(**context))


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def page_overview(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> HTMLResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    payload = build_overview(
        data,
        as_of=as_of,
        country=c_norm,
        settings=resolve_settings(request),
        notifier=resolve_notifier(request),
        profiles=profs,
    )
    return _render(
        "overview.html.j2",
        active_tab="overview",
        meta=payload.meta,
        data=payload,
    )


@router.get("/stations", response_class=HTMLResponse)
async def page_stations(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> HTMLResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    payload = build_stations_list(
        data,
        as_of=as_of,
        country=c_norm,
        settings=resolve_settings(request),
        notifier=resolve_notifier(request),
        profiles=profs,
    )
    return _render(
        "stations.html.j2",
        active_tab="stations",
        meta=payload.meta,
        data=payload,
    )


@router.get("/stations/{station_id}", response_class=HTMLResponse)
async def page_station_detail(
    request: Request,
    station_id: str,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> HTMLResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    try:
        payload = build_station_detail(
            data,
            station_id,
            as_of=as_of,
            country=c_norm,
            settings=resolve_settings(request),
            notifier=resolve_notifier(request),
            profiles=profs,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy trạm {station_id}") from exc
    return _render(
        "station_detail.html.j2",
        active_tab="stations",
        meta=payload.meta,
        data=payload,
    )


@router.get("/sessions", response_class=HTMLResponse)
async def page_sessions(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
    window_hours: Annotated[int, Query(ge=1, le=720)] = 24,
    status: str | None = None,
    station_id: str | None = None,
    step: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> HTMLResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    st_clean = status.strip() if status and status.strip() else None
    sid_clean = station_id.strip() if station_id and station_id.strip() else None
    step_enum: Step | None = None
    if step and step.strip():
        try:
            step_enum = Step(step.strip())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Bước không hợp lệ: {step}") from exc

    payload = build_sessions_page(
        data,
        as_of=as_of,
        country=c_norm,
        window_hours=window_hours,
        status=st_clean,
        station_id=sid_clean,
        step=step_enum,
        limit=limit,
        profiles=profs,
    )
    return _render(
        "sessions.html.j2",
        active_tab="sessions",
        meta=payload.meta,
        data=payload,
        step_choices=[(s.value, lbl) for s, lbl in STEP_VI_LABELS.items()],
    )


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def page_session_detail(
    request: Request,
    session_id: str,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> HTMLResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    try:
        payload = build_session_detail(
            data,
            session_id,
            as_of=as_of,
            country=c_norm,
            profiles=profs,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy phiên {session_id}") from exc
    return _render(
        "session_detail.html.j2",
        active_tab="sessions",
        meta=payload.meta,
        data=payload,
    )


@router.get("/payments", response_class=HTMLResponse)
async def page_payments(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> HTMLResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    payload = build_payments_page(
        data,
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )
    return _render(
        "payments.html.j2",
        active_tab="payments",
        meta=payload.meta,
        data=payload,
    )


@router.get("/batteries", response_class=HTMLResponse)
async def page_batteries(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> HTMLResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    payload = build_batteries_page(
        data,
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )
    return _render(
        "batteries.html.j2",
        active_tab="batteries",
        meta=payload.meta,
        data=payload,
    )


@router.get("/alerts", response_class=HTMLResponse)
async def page_alerts(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> HTMLResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    payload = build_alerts_page(
        data,
        resolve_notifier(request),
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )
    return _render(
        "alerts.html.j2",
        active_tab="alerts",
        meta=payload.meta,
        data=payload,
    )


@router.get("/incidents", response_class=HTMLResponse)
async def page_incidents(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> HTMLResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    payload = build_incidents_placeholder(
        data,
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )
    return _render(
        "incidents.html.j2",
        active_tab="incidents",
        meta=payload.meta,
        data=payload,
    )


@router.get("/metrics", response_class=HTMLResponse)
async def page_metrics(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> HTMLResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    payload = build_metrics_placeholder(
        data,
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )
    return _render(
        "metrics.html.j2",
        active_tab="metrics",
        meta=payload.meta,
        data=payload,
    )


@router.get("/ground-truth", response_class=HTMLResponse)
async def page_ground_truth(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> HTMLResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    settings = resolve_settings(request)
    try:
        gt_df = load_ground_truth(data.profile, settings=settings)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = build_ground_truth_page(
        data,
        gt_df,
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )
    return _render(
        "ground_truth.html.j2",
        active_tab="ground_truth",
        meta=payload.meta,
        data=payload,
    )
