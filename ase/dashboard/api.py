"""APIRouter `/api/dashboard/...` trả JSON theo Pydantic response models (DASHBOARD_PLAN §3, §5)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from ase.common.config import Settings, get_settings
from ase.common.models import Step
from ase.dashboard.data import (
    NotifierSnapshot,
    ProfileData,
    available_profiles,
    load_ground_truth,
    load_profile_data,
    read_notifier_sqlite,
)
from ase.dashboard.metrics import (
    build_alarms_page,
    build_alerts_page,
    build_batteries_page,
    build_cabinet_temperature_timeseries,
    build_ground_truth_page,
    build_incidents_placeholder,
    build_metrics_placeholder,
    build_overview,
    build_payments_page,
    build_ready_batteries_timeseries,
    build_session_detail,
    build_sessions_page,
    build_station_detail,
    build_stations_list,
)
from ase.dashboard.models import (
    AlarmsResponse,
    AlertsPageResponse,
    BatteriesPageResponse,
    CabinetTemperatureTimeseriesResponse,
    GroundTruthPageResponse,
    IncidentsPlaceholderResponse,
    MetricsPlaceholderResponse,
    OverviewResponse,
    PaymentsPageResponse,
    ReadyBatteriesTimeseriesResponse,
    SessionDetailResponse,
    SessionsPageResponse,
    StationDetailResponse,
    StationsListResponse,
)
from ase.knowledge.loader import default_knowledge

router = APIRouter(prefix="/api/dashboard", tags=["dashboard-api"])


def resolve_settings(request: Request) -> Settings:
    configured: Settings | None = getattr(request.app.state, "settings", None)
    return configured if configured is not None else get_settings()


def resolve_db_path(request: Request) -> Path:
    configured: Path | None = getattr(request.app.state, "db_path", None)
    return configured if configured is not None else resolve_settings(request).notifier_db


def normalize_country(country: str | None) -> str | None:
    if country is None:
        return None
    cleaned = country.strip().upper()
    if not cleaned or cleaned == "ALL":
        return None
    known = default_knowledge().countries.countries
    if cleaned not in known:
        raise HTTPException(status_code=422, detail=f"Quốc gia không hợp lệ: {country}")
    return cleaned


def resolve_profile_data(request: Request, profile: str) -> tuple[ProfileData, list[str]]:
    settings = resolve_settings(request)
    profs = available_profiles(settings)
    target = profile
    if target == "dev" and "dev" not in profs and profs:
        target = profs[0]
    if target not in profs:
        # Chỉ nhận tên profile có thật; không bao giờ dùng giá trị người dùng để dựng đường dẫn
        raise HTTPException(
            status_code=422,
            detail=f"Profile không hợp lệ: {profile!r}. Hiện có: {', '.join(profs)}",
        )
    try:
        data = load_profile_data(target, settings=settings)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return data, profs


def resolve_notifier(request: Request) -> NotifierSnapshot:
    return read_notifier_sqlite(resolve_db_path(request))


@router.get("/overview", response_model=OverviewResponse)
async def api_overview(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> OverviewResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    return build_overview(
        data,
        as_of=as_of,
        country=c_norm,
        settings=resolve_settings(request),
        notifier=resolve_notifier(request),
        profiles=profs,
    )


@router.get(
    "/timeseries/cabinet-temperature",
    response_model=CabinetTemperatureTimeseriesResponse,
)
async def api_cabinet_temperature_timeseries(
    request: Request,
    profile: str = "dev",
    station_id: str | None = None,
    hours: int = 1,
    as_of: datetime | None = None,
    country: str | None = None,
) -> CabinetTemperatureTimeseriesResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    return build_cabinet_temperature_timeseries(
        data,
        station_id=station_id,
        hours=hours,
        as_of=as_of,
        country=c_norm,
        settings=resolve_settings(request),
        profiles=profs,
    )


@router.get(
    "/timeseries/ready-batteries",
    response_model=ReadyBatteriesTimeseriesResponse,
)
async def api_ready_batteries_timeseries(
    request: Request,
    profile: str = "dev",
    hours: int = 6,
    step_minutes: int = 15,
    as_of: datetime | None = None,
    country: str | None = None,
) -> ReadyBatteriesTimeseriesResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    return build_ready_batteries_timeseries(
        data,
        hours=hours,
        step_minutes=step_minutes,
        as_of=as_of,
        country=c_norm,
        settings=resolve_settings(request),
        profiles=profs,
    )


@router.get("/alarms", response_model=AlarmsResponse)
async def api_alarms(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> AlarmsResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    return build_alarms_page(
        data,
        as_of=as_of,
        country=c_norm,
        settings=resolve_settings(request),
        notifier=resolve_notifier(request),
        profiles=profs,
    )


@router.get("/stations", response_model=StationsListResponse)
async def api_stations(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> StationsListResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    return build_stations_list(
        data,
        as_of=as_of,
        country=c_norm,
        settings=resolve_settings(request),
        notifier=resolve_notifier(request),
        profiles=profs,
    )


@router.get("/stations/{station_id}", response_model=StationDetailResponse)
async def api_station_detail(
    request: Request,
    station_id: str,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> StationDetailResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    try:
        return build_station_detail(
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


@router.get("/sessions", response_model=SessionsPageResponse)
async def api_sessions(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
    window_hours: Annotated[int, Query(ge=1, le=720)] = 24,
    status: str | None = None,
    station_id: str | None = None,
    step: Step | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> SessionsPageResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    st_clean = status.strip() if status and status.strip() else None
    sid_clean = station_id.strip() if station_id and station_id.strip() else None
    return build_sessions_page(
        data,
        as_of=as_of,
        country=c_norm,
        window_hours=window_hours,
        status=st_clean,
        station_id=sid_clean,
        step=step,
        limit=limit,
        profiles=profs,
    )


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def api_session_detail(
    request: Request,
    session_id: str,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> SessionDetailResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    try:
        return build_session_detail(
            data,
            session_id,
            as_of=as_of,
            country=c_norm,
            profiles=profs,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy phiên {session_id}") from exc


@router.get("/payments", response_model=PaymentsPageResponse)
async def api_payments(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> PaymentsPageResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    return build_payments_page(
        data,
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )


@router.get("/batteries", response_model=BatteriesPageResponse)
async def api_batteries(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> BatteriesPageResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    return build_batteries_page(
        data,
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )


@router.get("/alerts", response_model=AlertsPageResponse)
async def api_alerts(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> AlertsPageResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    return build_alerts_page(
        data,
        resolve_notifier(request),
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )


@router.get("/incidents", response_model=IncidentsPlaceholderResponse)
async def api_incidents(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> IncidentsPlaceholderResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    return build_incidents_placeholder(
        data,
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )


@router.get("/metrics", response_model=MetricsPlaceholderResponse)
async def api_metrics(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> MetricsPlaceholderResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    return build_metrics_placeholder(
        data,
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )


@router.get("/ground-truth", response_model=GroundTruthPageResponse)
async def api_ground_truth(
    request: Request,
    profile: str = "dev",
    as_of: datetime | None = None,
    country: str | None = None,
) -> GroundTruthPageResponse:
    c_norm = normalize_country(country)
    data, profs = resolve_profile_data(request, profile)
    settings = resolve_settings(request)
    try:
        gt_df = load_ground_truth(data.profile, settings=settings)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return build_ground_truth_page(
        data,
        gt_df,
        as_of=as_of,
        country=c_norm,
        profiles=profs,
    )
