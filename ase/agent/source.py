"""Nguồn "Danh sách giao dịch đổi pin" cho agent quét (chỉ đọc).

Agent chỉ phụ thuộc `TransactionSource`: mock đọc file do simulator sinh; nguồn thật (tệp "Xuất
dữ liệu", API) thêm sau cùng interface mà không sửa scanner.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

import polars as pl

from ase.common.models import SwapTransaction, TxnStatus

TRANSACTIONS_FILE = "swap_transactions.parquet"
_FINAL = [TxnStatus.COMPLETED.value, TxnStatus.FAILED.value]
_IN_COLS = ["battery_in_id", "slot_in", "soh_in", "soc_in", "battery_in_at"]
_OUT_COLS = ["battery_out_id", "slot_out", "soh_out", "soc_out", "battery_out_at"]


def view_as_of(df: pl.DataFrame, as_of: datetime) -> pl.DataFrame:
    """Danh sách giao dịch như màn hình hiện tại thời điểm `as_of`.

    File mock ghi trạng thái CUỐI của từng giao dịch; hàm này che mọi thứ xảy ra sau `as_of`:
    giao dịch chưa bắt đầu bị loại, giao dịch chưa xong hiện là Tạo mới / Đưa pin vào, lý do lỗi
    và thông tin pin chỉ hiện khi đã xảy ra. Nhờ vậy replay không "nhìn thấy tương lai".
    """
    done = pl.col("status").is_in(_FINAL) & (pl.col("updated_at") <= as_of)
    has_in = pl.col("battery_in_at").is_not_null() & (pl.col("battery_in_at") <= as_of)
    has_out = pl.col("battery_out_at").is_not_null() & (pl.col("battery_out_at") <= as_of)
    status = (
        pl.when(done)
        .then(pl.col("status"))
        .when(has_in)
        .then(pl.lit(TxnStatus.BATTERY_IN.value))
        .otherwise(pl.lit(TxnStatus.CREATED.value))
    )
    updated = (
        pl.when(done)
        .then(pl.col("updated_at"))
        .when(has_in)
        .then(pl.col("battery_in_at"))
        .otherwise(pl.col("started_at"))
    )
    failed = done & (pl.col("status") == TxnStatus.FAILED.value)
    return df.filter(pl.col("started_at") <= as_of).with_columns(
        *[pl.when(has_in).then(pl.col(c)).otherwise(None).alias(c) for c in _IN_COLS],
        *[pl.when(has_out).then(pl.col(c)).otherwise(None).alias(c) for c in _OUT_COLS],
        pl.when(failed).then(pl.col("error_code")).otherwise(None).alias("error_code"),
        updated.alias("updated_at"),
        status.alias("status"),
    )


def load_transactions(profile_dir: Path) -> pl.DataFrame:
    path = profile_dir / TRANSACTIONS_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"Chưa có {path}. Chạy `make sim PROFILE=<profile>` để sinh danh sách giao dịch."
        )
    return pl.read_parquet(path).sort(["started_at", "emsp_txn_id"])


class TransactionSource(Protocol):
    def fetch(self, as_of: datetime, since: datetime) -> list[SwapTransaction]:
        """Giao dịch bắt đầu trong `(since, as_of]`, theo trạng thái tại `as_of`."""
        ...


class MockTransactionSource:
    def __init__(self, profile_dir: Path) -> None:
        self._df = load_transactions(profile_dir)

    @property
    def time_range(self) -> tuple[datetime, datetime]:
        """(giao dịch đầu tiên bắt đầu, lần đổi trạng thái cuối cùng)."""
        lo, hi = self._df["started_at"].min(), self._df["updated_at"].max()
        assert isinstance(lo, datetime) and isinstance(hi, datetime)
        return lo, hi

    @property
    def frame(self) -> pl.DataFrame:
        """Toàn bộ danh sách (trạng thái cuối). Người dùng phải xem qua `view_as_of`."""
        return self._df

    @property
    def n_transactions(self) -> int:
        return self._df.height

    def fetch(self, as_of: datetime, since: datetime) -> list[SwapTransaction]:
        window = self._df.filter((pl.col("started_at") > since) & (pl.col("started_at") <= as_of))
        return [SwapTransaction.model_validate(r) for r in view_as_of(window, as_of).to_dicts()]
