"""Danh sách giao dịch đổi pin: bảng 1 dòng/giao dịch theo màn hình hệ thống vận hành thật.

Đọc `swap_transactions.parquet` do simulator sinh (cùng nguồn với agent quét 30 phút) và dùng
chung `ase.agent.source.view_as_of`, nên dashboard và agent luôn thấy cùng một danh sách tại
cùng một `as_of`: phiên chưa xong hiện là `Tạo mới` / `Đưa pin vào`. Lọc ngày theo **giờ địa
phương của trạm**, đúng như giờ hiển thị trên bảng.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import polars as pl

from ase.agent.source import TRANSACTIONS_FILE, load_transactions, view_as_of
from ase.common.models import TxnStatus
from ase.dashboard.data import ProfileData
from ase.dashboard.metrics import _build_meta
from ase.dashboard.models import TransactionFiltersView, TransactionRow, TransactionsPageResponse
from ase.knowledge.loader import Knowledge, default_knowledge

LOCAL_FMT = "%d/%m/%Y %H:%M:%S"
INPUT_FMT = "%Y-%m-%dT%H:%M"  # giá trị của <input type="datetime-local">
PAGE_SIZES = (20, 50, 100)


STATUS_LABELS: dict[TxnStatus, str] = {
    TxnStatus.CREATED: "Tạo mới",
    TxnStatus.BATTERY_IN: "Đưa pin vào",
    TxnStatus.COMPLETED: "Hoàn thành",
    TxnStatus.FAILED: "Thất bại",
}

# (khoá cột, tiêu đề như màn hình thật) — thứ tự cột khi hiển thị và khi xuất CSV
COLUMNS: list[tuple[str, str]] = [
    ("station_id", "Mã trạm"),
    ("cabinet_id", "Mã tủ"),
    ("cabinet_serial", "S/N tủ pin"),
    ("emsp_txn_id", "Mã giao dịch trên eMSP"),
    ("cabinet_txn_id", "Mã giao dịch trên tủ"),
    ("started_local", "Ngày thực hiện"),
    ("status_label", "Trạng thái"),
    ("error_code", "Lý do lỗi"),
    ("battery_in_id", "S/N pin cũ"),
    ("battery_out_id", "S/N pin mới"),
    ("slot_in", "Connector ID (pin cũ)"),
    ("soh_in", "SOH pin cũ"),
    ("soc_in", "SOC pin cũ"),
    ("slot_out", "Connector ID (pin mới)"),
    ("soh_out", "SOH pin mới"),
    ("soc_out", "SOC pin mới"),
    ("battery_in_local", "Thời gian pin vào"),
    ("battery_out_local", "Thời gian pin ra"),
]


@dataclass(frozen=True)
class TxnFilters:
    """Bộ lọc như màn hình thật. Chuỗi rỗng = không lọc; khớp chứa, không phân biệt hoa thường."""

    q: str = ""  # ô tìm kiếm: mã trạm, tủ, giao dịch eMSP
    station_id: str = ""
    cabinet_id: str = ""
    cabinet_serial: str = ""
    emsp_txn_id: str = ""
    cabinet_txn_id: str = ""
    battery_serial: str = ""  # khớp S/N pin cũ hoặc mới
    date_from: datetime | None = None  # giờ địa phương trạm, naive
    date_to: datetime | None = None
    statuses: frozenset[TxnStatus] = field(default_factory=frozenset)

    @property
    def active_count(self) -> int:
        texts = (
            self.station_id,
            self.cabinet_id,
            self.cabinet_serial,
            self.emsp_txn_id,
            self.cabinet_txn_id,
            self.battery_serial,
        )
        return (
            sum(1 for t in texts if t)
            + (self.date_from is not None)
            + (self.date_to is not None)
            + (1 if self.statuses else 0)
        )

    def view(self) -> TransactionFiltersView:
        return TransactionFiltersView(
            q=self.q,
            station_id=self.station_id,
            cabinet_id=self.cabinet_id,
            cabinet_serial=self.cabinet_serial,
            emsp_txn_id=self.emsp_txn_id,
            cabinet_txn_id=self.cabinet_txn_id,
            battery_serial=self.battery_serial,
            date_from=self.date_from.strftime(INPUT_FMT) if self.date_from else "",
            date_to=self.date_to.strftime(INPUT_FMT) if self.date_to else "",
            statuses=[s.value for s in TxnStatus if s in self.statuses],
            active_count=self.active_count,
        )


_BASE_CACHE: dict[Path, tuple[float, pl.DataFrame]] = {}


def clear_cache() -> None:
    _BASE_CACHE.clear()


def _base(data: ProfileData) -> pl.DataFrame:
    """Danh sách giao dịch (trạng thái cuối) kèm giờ địa phương; cache theo mtime của file."""
    path = data.profile_dir / TRANSACTIONS_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"Chưa có {TRANSACTIONS_FILE} cho profile '{data.profile}'. "
            f"Chạy `make sim PROFILE={data.profile}`."
        )
    mtime = path.stat().st_mtime
    cached = _BASE_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    df = load_transactions(data.profile_dir)
    for src, out in (
        ("started_at", "started_local_dt"),
        ("battery_in_at", "battery_in_local_dt"),
        ("battery_out_at", "battery_out_local_dt"),
    ):
        df = _to_local_naive(df, src, out)
    df = df.sort(["started_at", "emsp_txn_id"], descending=True)
    _BASE_CACHE.clear()  # chỉ giữ profile đang xem: bảng demo lớn
    _BASE_CACHE[path] = (mtime, df)
    return df


def _to_local_naive(df: pl.DataFrame, col: str, out: str) -> pl.DataFrame:
    """Đổi cột UTC sang giờ địa phương (naive) theo cột `timezone` của từng dòng."""
    expr: pl.Expr = pl.lit(None, dtype=pl.Datetime("us"))
    for tz in df["timezone"].unique().drop_nulls().to_list():
        expr = (
            pl.when(pl.col("timezone") == tz)
            .then(pl.col(col).dt.convert_time_zone(str(tz)).dt.replace_time_zone(None))
            .otherwise(expr)
        )
    return df.with_columns(expr.alias(out))


def _contains(col: str, needle: str) -> pl.Expr:
    return pl.col(col).fill_null("").str.to_lowercase().str.contains(needle.lower(), literal=True)


def query_transactions(
    data: ProfileData, as_of: datetime, filters: TxnFilters, country: str | None = None
) -> pl.DataFrame:
    """Các giao dịch đã bắt đầu tại `as_of`, đã lọc, mới nhất trước, kèm cột hiển thị."""
    df = _base(data)
    if country:
        df = df.filter(pl.col("country") == country)
    df = view_as_of(df, as_of)
    # Giờ pin vào/ra chỉ hiện khi đã xảy ra tại `as_of` (view_as_of đã che cột UTC tương ứng)
    df = df.with_columns(
        pl.when(pl.col("battery_in_at").is_not_null())
        .then(pl.col("battery_in_local_dt"))
        .otherwise(None)
        .alias("battery_in_local_dt"),
        pl.when(pl.col("battery_out_at").is_not_null())
        .then(pl.col("battery_out_local_dt"))
        .otherwise(None)
        .alias("battery_out_local_dt"),
    )

    f = filters
    if f.q:
        df = df.filter(
            _contains("station_id", f.q)
            | _contains("cabinet_id", f.q)
            | _contains("emsp_txn_id", f.q)
        )
    for col, needle in (
        ("station_id", f.station_id),
        ("cabinet_id", f.cabinet_id),
        ("cabinet_serial", f.cabinet_serial),
        ("emsp_txn_id", f.emsp_txn_id),
        ("cabinet_txn_id", f.cabinet_txn_id),
    ):
        if needle:
            df = df.filter(_contains(col, needle))
    if f.battery_serial:
        df = df.filter(
            _contains("battery_in_id", f.battery_serial)
            | _contains("battery_out_id", f.battery_serial)
        )
    if f.date_from is not None:
        df = df.filter(pl.col("started_local_dt") >= f.date_from)
    if f.date_to is not None:
        df = df.filter(pl.col("started_local_dt") <= f.date_to)
    if f.statuses:
        df = df.filter(pl.col("status").is_in([s.value for s in f.statuses]))

    labels = {s.value: lbl for s, lbl in STATUS_LABELS.items()}
    return df.with_columns(
        pl.col("status").replace_strict(labels).alias("status_label"),
        pl.col("started_local_dt").dt.strftime(LOCAL_FMT).alias("started_local"),
        pl.col("battery_in_local_dt").dt.strftime(LOCAL_FMT).alias("battery_in_local"),
        pl.col("battery_out_local_dt").dt.strftime(LOCAL_FMT).alias("battery_out_local"),
        pl.col("soh_in").round(1),
        pl.col("soh_out").round(1),
        pl.col("soc_in").round(0).cast(pl.Int64),
        pl.col("soc_out").round(0).cast(pl.Int64),
    )


def to_csv(df: pl.DataFrame) -> str:
    """CSV có BOM UTF-8 để Excel mở đúng tiếng Việt; tiêu đề cột như màn hình thật."""
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow(["STT", *[h for _, h in COLUMNS]])
    keys = [k for k, _ in COLUMNS]
    for i, row in enumerate(df.select(keys).iter_rows(), start=1):
        w.writerow([i, *["" if v is None else v for v in row]])
    return buf.getvalue()


def build_transactions_page(
    data: ProfileData,
    *,
    filters: TxnFilters,
    as_of: datetime | None = None,
    country: str | None = None,
    page: int = 1,
    page_size: int = PAGE_SIZES[0],
    profiles: list[str] | None = None,
    knowledge: Knowledge | None = None,
) -> TransactionsPageResponse:
    meta = _build_meta(
        data, as_of, country, profiles or [data.profile], knowledge or default_knowledge()
    )
    df = query_transactions(data, meta.as_of, filters, country)
    total = df.height
    total_pages = max(1, -(-total // page_size))
    page = min(max(page, 1), total_pages)
    offset = (page - 1) * page_size
    keys = ["status", *[k for k, _ in COLUMNS]]
    rows = [
        TransactionRow(stt=offset + i, **r)
        for i, r in enumerate(df.slice(offset, page_size).select(keys).iter_rows(named=True), 1)
    ]
    return TransactionsPageResponse(
        meta=meta,
        filters=filters.view(),
        status_choices=[(s.value, STATUS_LABELS[s]) for s in TxnStatus],
        columns=COLUMNS,
        rows=rows,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )
