"""APIRouter `/dashboard/...` trả trang HTML render qua Jinja2 (autoescape bắt buộc)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from pydantic import BaseModel

from ase.dashboard.api import (
    check_page_size,
    normalize_country,
    resolve_notifier,
    resolve_profile_data,
    resolve_settings,
    transactions_or_422,
    txn_filters,
)
from ase.dashboard.metrics import (
    build_overview,
    build_station_detail,
    build_stations_list,
    clamp_as_of,
)
from ase.dashboard.transactions import (
    PAGE_SIZES,
    TxnFilters,
    build_transactions_page,
    query_transactions,
    to_csv,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

router = APIRouter(prefix="/dashboard", tags=["dashboard-pages"])


def _to_safe_json(obj: object) -> Markup:
    """JSON nhúng vào `<script type="application/json">`.

    Trả `Markup` để Jinja2 autoescape KHÔNG đổi `"` thành `&#34;`: trình duyệt không giải mã entity
    bên trong thẻ script, nên JSON bị escape sẽ làm `JSON.parse` lỗi và mọi widget trống.
    An toàn nhờ mã hoá `<`, `>`, `&` thành `\\u003c`, `\\u003e`, `\\u0026`: vẫn là JSON hợp lệ,
    dữ liệu untrusted (vd `</untrusted_data>`, `</script>`, `<!--`) không thể thoát khỏi thẻ script.
    """
    if isinstance(obj, BaseModel):
        raw = json.dumps(obj.model_dump(mode="json"), ensure_ascii=False)
    else:
        raw = json.dumps(obj, ensure_ascii=False, default=str)
    raw = raw.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    # U+2028/U+2029 hợp lệ trong JSON nhưng một số engine JS cũ coi là xuống dòng
    raw = raw.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return Markup(raw)  # an toàn: đã tự mã hoá mọi ký tự nguy hiểm ở trên


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
async def page_transactions(
    request: Request,
    filters: Annotated[TxnFilters, Depends(txn_filters)],
    profile: str | None = None,
    as_of: datetime | None = None,
    country: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: int = PAGE_SIZES[0],
) -> HTMLResponse:
    """Trang chủ: Danh sách giao dịch đổi pin. Mặc định hiện tất cả giao dịch, mới nhất trước."""
    data, profs = resolve_profile_data(request, profile)
    c_norm, size = normalize_country(country), check_page_size(page_size)
    payload = transactions_or_422(
        lambda: build_transactions_page(
            data,
            filters=filters,
            as_of=as_of,
            country=c_norm,
            page=page,
            page_size=size,
            profiles=profs,
        )
    )
    return _render(
        "transactions.html.j2",
        active_tab="transactions",
        meta=payload.meta,
        data=payload,
        page_sizes=PAGE_SIZES,
    )


@router.get("/transactions/export")
async def export_transactions(
    request: Request,
    filters: Annotated[TxnFilters, Depends(txn_filters)],
    profile: str | None = None,
    as_of: datetime | None = None,
    country: str | None = None,
) -> Response:
    """Xuất dữ liệu: toàn bộ giao dịch theo bộ lọc hiện tại (không phân trang), CSV UTF-8 có BOM."""
    data, _ = resolve_profile_data(request, profile)
    eff_as_of, _ = clamp_as_of(as_of, data.min_time, data.max_time, data.default_as_of)
    c_norm = normalize_country(country)
    df = transactions_or_422(lambda: query_transactions(data, eff_as_of, filters, c_norm))
    filename = f"giao_dich_doi_pin_{data.profile}_{eff_as_of:%Y%m%dT%H%MZ}.csv"
    return Response(
        content=to_csv(df).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/map", response_class=HTMLResponse)
async def page_map(
    request: Request,
    profile: str | None = None,
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
    key = resolve_settings(request).vietmap_api_key
    return _render(
        "overview.html.j2",
        active_tab="map",
        meta=payload.meta,
        data=payload,
        vietmap_key=key.get_secret_value() if key is not None else "",
    )


@router.get("/stations", response_class=HTMLResponse)
async def page_stations(
    request: Request,
    profile: str | None = None,
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
    profile: str | None = None,
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
