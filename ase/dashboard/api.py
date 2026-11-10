"""APIRouter `/api/dashboard/...` trả JSON theo Pydantic response models (DASHBOARD_PLAN §3, §5)."""

from __future__ import annotations

import functools
import gzip
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ase.common.config import Settings, get_settings
from ase.dashboard.data import (
    NotifierSnapshot,
    ProfileData,
    available_profiles,
    load_profile_data,
    read_notifier_sqlite,
)
from ase.dashboard.metrics import (
    build_cabinet_temperature_timeseries,
    build_overview,
    build_station_detail,
    build_stations_list,
)
from ase.dashboard.models import (
    CabinetTemperatureTimeseriesResponse,
    OverviewResponse,
    StationDetailResponse,
    StationsListResponse,
    TransactionsPageResponse,
)
from ase.dashboard.transactions import (
    INPUT_FMT,
    PAGE_SIZES,
    TxnFilters,
    TxnStatus,
    build_transactions_page,
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


def resolve_profile_data(request: Request, profile: str | None) -> tuple[ProfileData, list[str]]:
    """`profile=None` (URL không chỉ định) → `settings.dashboard_profile`, rồi `dev`, rồi profile
    đầu tiên có dữ liệu. Profile chỉ định rõ mà không có thì báo 422."""
    settings = resolve_settings(request)
    profs = available_profiles(settings)
    target = profile
    if target is None:
        preferred = [settings.dashboard_profile, "dev"]
        target = next((p for p in preferred if p in profs), profs[0] if profs else "dev")
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


def _parse_local_dt(raw: str | None, name: str) -> datetime | None:
    if raw is None or not raw.strip():
        return None
    try:
        # naive có chủ đích: giờ địa phương của từng trạm, so với cột giờ địa phương
        return datetime.strptime(raw.strip()[:16], INPUT_FMT)  # noqa: DTZ007
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name} không hợp lệ: {raw!r}") from exc


def txn_filters(
    q: str = "",
    station_id: str = "",
    cabinet_id: str = "",
    cabinet_serial: str = "",
    emsp_txn_id: str = "",
    cabinet_txn_id: str = "",
    battery_serial: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    status: Annotated[list[str] | None, Query()] = None,
) -> TxnFilters:
    """Dependency dùng chung cho trang, API và xuất dữ liệu: cùng một bộ lọc như màn hình thật.

    `date_from`/`date_to` là giờ địa phương của trạm (`YYYY-MM-DDTHH:MM`), `status` lặp lại được.
    """
    statuses: set[TxnStatus] = set()
    for raw in status or []:
        if not raw.strip():
            continue
        try:
            statuses.add(TxnStatus(raw.strip().upper()))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Trạng thái không hợp lệ: {raw}") from exc
    return TxnFilters(
        q=q.strip(),
        station_id=station_id.strip(),
        cabinet_id=cabinet_id.strip(),
        cabinet_serial=cabinet_serial.strip(),
        emsp_txn_id=emsp_txn_id.strip(),
        cabinet_txn_id=cabinet_txn_id.strip(),
        battery_serial=battery_serial.strip(),
        date_from=_parse_local_dt(date_from, "Ngày thực hiện từ"),
        date_to=_parse_local_dt(date_to, "Ngày thực hiện đến"),
        statuses=frozenset(statuses),
    )


def check_page_size(page_size: int) -> int:
    if page_size not in PAGE_SIZES:
        raise HTTPException(status_code=422, detail=f"page_size phải thuộc {list(PAGE_SIZES)}")
    return page_size


@router.get("/transactions", response_model=TransactionsPageResponse)
async def api_transactions(
    request: Request,
    filters: Annotated[TxnFilters, Depends(txn_filters)],
    profile: str | None = None,
    as_of: datetime | None = None,
    country: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: int = PAGE_SIZES[0],
) -> TransactionsPageResponse:
    data, profs = resolve_profile_data(request, profile)
    return build_transactions_page(
        data,
        filters=filters,
        as_of=as_of,
        country=normalize_country(country),
        page=page,
        page_size=check_page_size(page_size),
        profiles=profs,
    )


@router.get("/overview", response_model=OverviewResponse)
async def api_overview(
    request: Request,
    profile: str | None = None,
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
    profile: str | None = None,
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


@router.get("/stations", response_model=StationsListResponse)
async def api_stations(
    request: Request,
    profile: str | None = None,
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
    profile: str | None = None,
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


_VINFAST_BSS_GZ_PATH = Path(__file__).resolve().parent / "static" / "vinfast_bss.json.gz"


@functools.lru_cache(maxsize=1)
def _load_vinfast_bss_payloads() -> tuple[bytes, bytes]:
    gz_bytes = _VINFAST_BSS_GZ_PATH.read_bytes()
    raw_bytes = gzip.decompress(gz_bytes)
    return gz_bytes, raw_bytes


@router.get("/vinfast-bss")
async def api_vinfast_bss(request: Request) -> Response:
    """Danh sách tọa độ tủ đổi pin `[lat, lon, code, name, addr, (country)]`."""
    if not _VINFAST_BSS_GZ_PATH.exists():
        raise HTTPException(status_code=404, detail="Chưa có dữ liệu vinfast_bss.json.gz")
    gz_bytes, raw_bytes = _load_vinfast_bss_payloads()
    accept_enc = request.headers.get("accept-encoding", "")
    if "gzip" in accept_enc.lower():
        return Response(
            content=gz_bytes,
            media_type="application/json",
            headers={
                "Content-Encoding": "gzip",
                "Cache-Control": "public, max-age=3600",
            },
        )
    return Response(
        content=raw_bytes,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=3600"},
    )
