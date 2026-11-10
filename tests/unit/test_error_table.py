"""Bảng mã lỗi chuẩn (CLAUDE.md §6): chỉ đọc, nguyên văn; và model SwapTransaction."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ase.common.models import AlertLevel, SwapTransaction, TxnStatus
from ase.knowledge.loader import (
    ERROR_TABLE_FILE,
    KNOWLEDGE_DIR,
    ErrorTable,
    ErrorTableRow,
    default_error_table,
    load_error_table,
)


def test_error_table_shape_matches_owner_table() -> None:
    before = hashlib.sha256((KNOWLEDGE_DIR / ERROR_TABLE_FILE).read_bytes()).hexdigest()
    t = load_error_table()
    assert t.n_source_rows == 136  # 66 mã × 2 loại tủ + 4 dòng lặp cuối bảng
    assert len(t.rows) == 132 and len(t.codes) == 66
    assert {r.cabinet_type for r in t.rows} == {"CC_06_P", "CC_12_P"}
    assert {r.manufacturer for r in t.rows} == {"Charge core"}
    by_level = {lv: {r.code for r in t.rows if r.level is lv} for lv in AlertLevel}
    assert len(by_level[AlertLevel.LOW]) == 12 and len(by_level[AlertLevel.HIGH]) == 54
    # Hai loại tủ có cùng bộ mã
    assert {r.code for r in t.rows if r.cabinet_type == "CC_06_P"} == t.codes
    assert {r.code for r in t.rows if r.cabinet_type == "CC_12_P"} == t.codes
    # Loader không ghi file
    after = hashlib.sha256((KNOWLEDGE_DIR / ERROR_TABLE_FILE).read_bytes()).hexdigest()
    assert before == after


def test_error_table_lookup() -> None:
    t = default_error_table()
    row = t.lookup("D6031", "CC_12_P")
    assert row == ErrorTableRow(
        code="D6031",
        level=AlertLevel.LOW,
        manufacturer="Charge core",
        cabinet_type="CC_12_P",
        name="BatteryInTimeout",
    )
    assert t.lookup("C6029", "CC_06_P") is not None
    assert t.lookup("C6029", "CC_06_P").level is AlertLevel.HIGH  # type: ignore[union-attr]
    # Không biết loại tủ / loại tủ lạ → vẫn tra được theo mã
    assert t.lookup("A0110") is not None and t.lookup("A0110", "CC_99_X") is not None
    # Tên giữ nguyên chính tả gốc
    assert t.lookup("B6070").name == "ASIC Short Circut"  # type: ignore[union-attr]
    # Mã ngoài bảng chuẩn và giao dịch không có mã
    for code in ("C6003", "400364", "B6086", "", None):
        assert t.lookup(code) is None
    assert not t.has("C6003") and t.has("D6038")


def test_error_table_rejects_malformed_file(tmp_path: Path) -> None:
    def write(text: str) -> None:
        (tmp_path / ERROR_TABLE_FILE).write_text(text, encoding="utf-8")

    cols = '["Mã Lỗi", "Mức Độ Cảnh Báo", "Nhà Sản Xuất", "Loại Tủ", "Tên cảnh báo"]'
    write(f'columns: {cols}\nrows:\n  - ["X1", "Medium", "Charge core", "CC_06_P", "n"]\n')
    with pytest.raises(ValueError):
        load_error_table(tmp_path)  # mức ngoài High/Low
    write(f'columns: {cols}\nrows:\n  - ["X1", "High", "Charge core", "CC_06_P"]\n')
    with pytest.raises(ValueError):
        load_error_table(tmp_path)  # thiếu ô
    write('columns: ["Mã Lỗi", "Nhóm"]\nrows:\n  - ["X1", "HARDWARE"]\n')
    with pytest.raises(ValueError):
        load_error_table(tmp_path)  # sai bộ cột (vd có người thêm cột)
    write(
        f"columns: {cols}\nrows:\n"
        '  - ["X1", "High", "Charge core", "CC_06_P", "a"]\n'
        '  - ["X1", "Low", "Charge core", "CC_06_P", "a"]\n'
    )
    with pytest.raises(ValidationError):
        load_error_table(tmp_path)  # cùng mã + loại tủ nhưng hai mức
    assert isinstance(default_error_table(), ErrorTable)


def test_swap_transaction_contract() -> None:
    t0 = datetime(2026, 11, 9, 10, 29, 4, tzinfo=UTC)
    txn = SwapTransaction(
        station_id="B.TST00001",
        cabinet_id="B.TST00001.002",
        emsp_txn_id="0123456789abcdef01234567",
        started_at=t0,
        status=TxnStatus.FAILED,
        error_code="D6038",
        battery_in_id="BAT-TEST-0001",
        slot_in="1",
        soh_in=100,
        soc_in=29,
        battery_in_at=t0,
        country="VN",
        timezone="Asia/Ho_Chi_Minh",
        updated_at=t0,
    )
    assert txn.battery_out_id is None and txn.cabinet_type is None
    assert SwapTransaction.model_validate_json(txn.model_dump_json()) == txn
    base = txn.model_dump()
    with pytest.raises(ValidationError):
        SwapTransaction.model_validate({**base, "started_at": datetime(2026, 11, 9, 10, 0)})  # noqa: DTZ001
    with pytest.raises(ValidationError):
        SwapTransaction.model_validate({**base, "soc_in": 129})
    with pytest.raises(ValidationError):
        SwapTransaction.model_validate({**base, "status": "DONE"})
