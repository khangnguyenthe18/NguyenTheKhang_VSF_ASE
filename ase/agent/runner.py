"""Vòng quét: mốc tròn theo `scan.interval_minutes`, mỗi mốc ra một `ScanDigest`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from ase.agent.analyzer import Analyzer
from ase.agent.models import ScanDigest
from ase.agent.scanner import Scanner
from ase.agent.source import TransactionSource
from ase.common.transport import SimClock
from ase.knowledge.loader import ErrorTable, ScanSpec


def scan_ticks(start: datetime, end: datetime, interval_minutes: int) -> Iterator[datetime]:
    """Các mốc quét tròn (vd :00, :30 UTC) từ mốc đầu tiên sau `start` tới mốc đầu tiên ≥ `end`."""
    step = timedelta(minutes=interval_minutes)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    tick = epoch + ((start - epoch) // step + 1) * step
    while True:
        yield tick
        if tick >= end:
            return
        tick += step


class ScanAgent:
    """Ghép Scanner + Analyzer. `run_replay` phát lại dữ liệu mock bằng `SimClock` (không ngủ)."""

    def __init__(self, source: TransactionSource, table: ErrorTable, spec: ScanSpec) -> None:
        self.spec = spec
        self.scanner = Scanner(source, table, spec)
        self.analyzer = Analyzer(spec)

    def scan_once(self, scan_at: datetime) -> ScanDigest:
        window_start = scan_at - timedelta(minutes=self.spec.interval_minutes)
        result = self.scanner.scan(scan_at)
        incidents = self.analyzer.analyze(scan_at, result)
        return ScanDigest(
            scan_id=f"scan:{window_start:%Y%m%dT%H%MZ}",
            window_start=window_start,
            scan_at=scan_at,
            n_scanned=result.n_scanned,
            detections=result.detections,
            incidents=incidents,
        )

    def run_replay(self, start: datetime, end: datetime, clock: SimClock) -> Iterator[ScanDigest]:
        """Quét mọi mốc trong `[start, end]`, thêm các mốc cuối để giao dịch kẹt kịp lộ ra."""
        tail = timedelta(minutes=self.spec.stuck_after_minutes + self.spec.interval_minutes)
        for tick in scan_ticks(start, end + tail, self.spec.interval_minutes):
            clock.advance(tick)
            yield self.scan_once(clock.now())
