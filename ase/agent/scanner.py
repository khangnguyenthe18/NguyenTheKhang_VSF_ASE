"""Scanner: mỗi mốc quét tìm giao dịch Thất bại và giao dịch kẹt CHƯA báo."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from ase.agent.models import Detection, ScanResult
from ase.agent.source import TransactionSource
from ase.common.models import SwapTransaction, TxnStatus
from ase.knowledge.loader import ErrorTable, ScanSpec


class Scanner:
    """Giữ danh sách giao dịch đã báo để mỗi giao dịch chỉ xuất hiện trong MỘT thông báo.

    Vì sao nhìn lại `lookback_minutes` thay vì chỉ cửa sổ 30 phút vừa qua: giao dịch bắt đầu sát
    mốc quét có thể thất bại hoặc vượt ngưỡng kẹt sau mốc đó; lần quét kế tiếp phải thấy lại nó.
    Trạng thái đang giữ trong bộ nhớ (đủ cho replay); chạy thật cần lưu bền (backlog).
    """

    def __init__(self, source: TransactionSource, table: ErrorTable, spec: ScanSpec) -> None:
        self._source, self._table, self._spec = source, table, spec
        self._reported: set[str] = set()
        self.seen: set[str] = set()  # mọi giao dịch từng đọc được (SF1)

    def scan(self, scan_at: datetime) -> ScanResult:
        since = scan_at - timedelta(minutes=self._spec.lookback_minutes)
        stuck_after = timedelta(minutes=self._spec.stuck_after_minutes)
        txns = self._source.fetch(scan_at, since)
        found: list[Detection] = []
        new_by_cabinet: Counter[tuple[str, str | None]] = Counter()
        for t in txns:
            if t.emsp_txn_id not in self.seen:
                self.seen.add(t.emsp_txn_id)
                new_by_cabinet[(t.station_id, t.cabinet_id)] += 1
            if t.emsp_txn_id in self._reported:
                continue
            if t.status is TxnStatus.FAILED:
                found.append(self._detection(t, "FAILED", t.updated_at, scan_at))
            elif t.status is not TxnStatus.COMPLETED and scan_at - t.updated_at >= stuck_after:
                found.append(self._detection(t, "STUCK", t.updated_at + stuck_after, scan_at))
            else:
                continue
            self._reported.add(t.emsp_txn_id)
        found.sort(key=lambda d: (d.event_at, d.emsp_txn_id))
        return ScanResult(
            n_scanned=len(txns), detections=found, new_by_cabinet=dict(new_by_cabinet)
        )

    def _detection(
        self, t: SwapTransaction, kind: str, event_at: datetime, scan_at: datetime
    ) -> Detection:
        row = self._table.lookup(t.error_code, t.cabinet_type)
        return Detection(
            emsp_txn_id=t.emsp_txn_id,
            cabinet_txn_id=t.cabinet_txn_id,
            kind="FAILED" if kind == "FAILED" else "STUCK",
            status=t.status,
            station_id=t.station_id,
            cabinet_id=t.cabinet_id,
            cabinet_type=t.cabinet_type,
            connector=t.slot_in,
            country=t.country,
            timezone=t.timezone,
            error_code=t.error_code,
            error_name=row.name if row else None,
            level=row.level if row else None,
            in_table=row is not None,
            started_at=t.started_at,
            event_at=event_at,
            detected_at=scan_at,
            battery_in_id=t.battery_in_id,
            returned_not_received=t.battery_in_at is not None and t.battery_out_at is None,
            user_id=t.user_id,
        )
