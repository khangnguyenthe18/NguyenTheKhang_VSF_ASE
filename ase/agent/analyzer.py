"""Analyzer: gộp các giao dịch lỗi của một lần quét thành sự cố theo MẪU PHÂN BỐ.

Bảng mã lỗi chuẩn không có nhóm lỗi, nên sự cố được nhận ra bằng (a) mức của mã và (b) nơi lỗi
xảy ra so với mức nền:
- Lỗi "đáng chú ý" = mã mức High, hoặc số lỗi của mã đó trong lần quét vượt mức nền của chính nó
  (kiểm định Poisson với tỷ lệ lỗi 7 ngày gần nhất). Lỗi nền rải rác không thành sự cố.
- Phạm vi, xét từ rộng tới hẹp:
    1. WIDE    — cùng một mã vượt nền ở nhiều trạm cùng lúc (lỗi hệ thống / nhà cung cấp);
    2. STATION — lỗi đáng chú ý ở nhiều tủ của một trạm (vd mất điện trạm);
    3. CABINET — lỗi đáng chú ý tại một tủ.
- Lỗi không vào sự cố nào là "lỗi lẻ" (vẫn nằm trong thông báo).
- Sự cố tái diễn ở lần quét sau giữ nguyên `incident_id` và tăng revision (→ 1 ticket/sự cố).
Hoàn toàn tất định: không gọi LLM, không đọc đồng hồ.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ase.agent.models import Detection, IncidentKind, ScanIncident, ScanResult
from ase.common.models import AlertLevel
from ase.knowledge.loader import ScanSpec


def poisson_tail(k: int, lam: float) -> float:
    """P(X ≥ k) với X ~ Poisson(lam): xác suất thấy ≥ k lỗi nếu chỉ có mức nền."""
    if k <= 0:
        return 1.0
    term = cdf = math.exp(-lam)
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


class Baseline:
    """Tỷ lệ lỗi nền của từng mã trong `baseline_days` gần nhất, làm mượt bằng giá trị giả định.

    Vì sao cần prior: ngày đầu chưa có lịch sử; thiếu prior thì lỗi đầu tiên nào cũng "bất thường".
    """

    def __init__(self, spec: ScanSpec) -> None:
        self._spec = spec
        self._scans: deque[tuple[datetime, int, Counter[str]]] = deque()
        self._n = 0
        self._fails: Counter[str] = Counter()

    def rate(self, group: str) -> float:
        m = self._spec.baseline_prior_txns
        return (self._fails[group] + self._spec.baseline_prior_rate * m) / (self._n + m)

    def is_above(self, group: str, count: int, exposure: int) -> bool:
        lam = self.rate(group) * max(exposure, count)
        return poisson_tail(count, lam) < self._spec.baseline_alpha

    def update(self, scan_at: datetime, n_new: int, groups: Counter[str]) -> None:
        self._scans.append((scan_at, n_new, groups))
        self._n += n_new
        self._fails.update(groups)
        cutoff = scan_at - timedelta(days=self._spec.baseline_days)
        while self._scans and self._scans[0][0] < cutoff:
            _, n_old, old = self._scans.popleft()
            self._n -= n_old
            self._fails.subtract(old)


@dataclass
class _Open:
    incident_id: str
    kind: IncidentKind
    key: str
    first_seen_at: datetime
    last_seen_at: datetime
    revision: int = 0
    n_total: int = 0
    groups: list[str] = field(default_factory=list)
    station_ids: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    level: AlertLevel | None = None


def _max_level(a: AlertLevel | None, b: AlertLevel | None) -> AlertLevel | None:
    if AlertLevel.HIGH in (a, b):
        return AlertLevel.HIGH
    return a or b


class Analyzer:
    def __init__(self, spec: ScanSpec) -> None:
        self._spec = spec
        self.baseline = Baseline(spec)
        self._open: dict[str, _Open] = {}
        self._seq: dict[str, int] = defaultdict(int)  # ngày → số sự cố đã mở

    def _active(self, key: str, scan_at: datetime) -> _Open | None:
        inc = self._open.get(key)
        if inc is None:
            return None
        limit = timedelta(minutes=self._spec.continue_minutes[inc.kind])
        return inc if scan_at - inc.last_seen_at <= limit else None

    def analyze(self, scan_at: datetime, result: ScanResult) -> list[ScanIncident]:
        """Gán `incident_id` vào từng detection (tại chỗ) và trả các sự cố có giao dịch mới."""
        s = self._spec
        detections = result.detections
        n_new = sum(result.new_by_cabinet.values())
        new_by_station: Counter[str] = Counter()
        for (station, _cab), n in result.new_by_cabinet.items():
            new_by_station[station] += n

        assigned: dict[str, list[Detection]] = defaultdict(list)
        kinds: dict[str, IncidentKind] = {}
        rest = list(detections)

        def take(key: str, kind: IncidentKind, items: list[Detection]) -> None:
            assigned[key].extend(items)
            kinds[key] = kind
            taken = {d.emsp_txn_id for d in items}
            rest[:] = [d for d in rest if d.emsp_txn_id not in taken]

        def joins(key: str, d: Detection) -> bool:
            """Lỗi mới nối vào sự cố đang mở nếu cùng mã đã gặp, hoặc là mã mức High."""
            inc = self._active(key, scan_at)
            return inc is not None and (d.group in inc.groups or d.level is AlertLevel.HIGH)

        # 1. WIDE: cùng một mã vượt nền ở nhiều trạm.
        by_group: dict[str, list[Detection]] = defaultdict(list)
        for d in rest:
            by_group[d.group].append(d)
        for group, items in sorted(by_group.items()):
            key = f"WIDE|{group}"
            wide = (
                len({d.station_id for d in items}) >= s.wide_min_stations
                and len(items) >= s.wide_min_txns
                and self.baseline.is_above(group, len(items), n_new)
            )
            if wide or self._active(key, scan_at):
                take(key, "WIDE", items)

        # Lỗi đáng chú ý ở mức trạm / tủ: mã High, hoặc mã vượt nền tại đúng trạm / tủ đó.
        def notable(items: list[Detection], exposure: int, min_txns: int) -> list[Detection]:
            counts = Counter(d.group for d in items)
            above = {
                g
                for g, n in counts.items()
                if n >= min_txns and self.baseline.is_above(g, n, exposure)
            }
            return [d for d in items if d.level is AlertLevel.HIGH or d.group in above]

        # 2. STATION: lỗi đáng chú ý ở nhiều tủ của một trạm.
        by_station: dict[str, list[Detection]] = defaultdict(list)
        for d in rest:
            by_station[d.station_id].append(d)
        for station, items in sorted(by_station.items()):
            key = f"STATION|{station}"
            hot = notable(items, new_by_station[station], s.station_min_txns)
            spread = (
                len({d.cabinet_id for d in hot}) >= s.station_min_cabinets
                and len(hot) >= s.station_min_txns
            )
            picked = hot if spread else [d for d in items if joins(key, d)]
            if picked:
                take(key, "STATION", picked)

        # 3. CABINET: lỗi đáng chú ý tại một tủ.
        by_cabinet: dict[tuple[str, str], list[Detection]] = defaultdict(list)
        for d in rest:
            if d.cabinet_id:
                by_cabinet[(d.station_id, d.cabinet_id)].append(d)
        for (station, cabinet), items in sorted(by_cabinet.items()):
            key = f"CABINET|{cabinet}"
            exposure = result.new_by_cabinet.get((station, cabinet), 0)
            hot_ids = {d.emsp_txn_id for d in notable(items, exposure, s.cabinet_min_txns)}
            picked = [d for d in items if d.emsp_txn_id in hot_ids or joins(key, d)]
            if picked:
                take(key, "CABINET", picked)

        self.baseline.update(scan_at, n_new, Counter(d.group for d in detections))
        return [self._record(scan_at, key, kinds[key], assigned[key]) for key in sorted(assigned)]

    def _record(
        self, scan_at: datetime, key: str, kind: IncidentKind, found: list[Detection]
    ) -> ScanIncident:
        items = sorted(found, key=lambda d: (d.event_at, d.emsp_txn_id))
        inc = self._active(key, scan_at)
        if inc is None:
            day = f"{scan_at:%Y%m%d}"
            self._seq[day] += 1
            inc = _Open(
                incident_id=f"INC-{day}-{self._seq[day]:04d}",
                kind=kind,
                key=key,
                first_seen_at=items[0].event_at,
                last_seen_at=items[0].event_at,
            )
            self._open[key] = inc
        inc.revision += 1
        inc.n_total += len(items)
        inc.last_seen_at = max(inc.last_seen_at, items[-1].event_at)
        for d in items:
            d.incident_id = inc.incident_id
            inc.level = _max_level(inc.level, d.level)
            for bucket, value in (
                (inc.groups, d.group),
                (inc.station_ids, d.station_id),
                (inc.countries, d.country),
            ):
                if value not in bucket:
                    bucket.append(value)
        return ScanIncident(
            incident_id=inc.incident_id,
            revision=inc.revision,
            kind=inc.kind,
            key=key,
            scope=_scope(inc),
            groups=list(inc.groups),
            level=inc.level,
            station_ids=sorted(inc.station_ids),
            countries=sorted(inc.countries),
            txn_ids=[d.emsp_txn_id for d in items],
            n_total=inc.n_total,
            first_seen_at=inc.first_seen_at,
            last_seen_at=inc.last_seen_at,
        )


def _scope(inc: _Open) -> str:
    if inc.kind != "WIDE":
        return inc.key.split("|", 1)[1]
    countries = sorted(inc.countries)
    return countries[0] if len(countries) == 1 else f"GLOBAL ({', '.join(countries)})"
