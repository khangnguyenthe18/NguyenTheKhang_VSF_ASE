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
        return self.is_above_any([group], count, exposure)

    def is_above_any(self, groups: list[str], count: int, exposure: int) -> bool:
        """Kiểm định cho một nhóm nhiều mã (vd các mã của cùng một dịch vụ critical)."""
        lam = sum(self.rate(g) for g in groups) * max(exposure, count)
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


class Rolling:
    """Giao dịch lỗi CHƯA vào sự cố và số giao dịch mới, theo tủ/trạm, trong `window` gần nhất.

    Vì sao: mật độ thực tế chỉ 2–3 giao dịch/tủ/ngày nên lỗi của một tủ hỏng rải rác qua nhiều
    lần quét; chỉ xét trong một lần quét 30 phút thì không bao giờ đủ 2 lỗi để thành sự cố.
    """

    def __init__(self, window: timedelta) -> None:
        self._window = window
        self._lone: dict[str, deque[tuple[datetime, str]]] = defaultdict(deque)  # key → (t, group)
        self._exposure: dict[str, deque[tuple[datetime, int]]] = defaultdict(deque)

    def _trim(self, q: deque[tuple[datetime, object]], now: datetime) -> None:
        while q and q[0][0] < now - self._window:
            q.popleft()

    def counts(self, key: str, now: datetime) -> Counter[str]:
        q = self._lone[key]
        self._trim(q, now)  # type: ignore[arg-type]
        return Counter(g for _, g in q)

    def exposure(self, key: str, now: datetime) -> int:
        q = self._exposure[key]
        self._trim(q, now)  # type: ignore[arg-type]
        return sum(n for _, n in q)

    def add_exposure(self, key: str, now: datetime, n: int) -> None:
        if n:
            self._exposure[key].append((now, n))

    def add_lone(self, key: str, now: datetime, group: str) -> None:
        self._lone[key].append((now, group))

    def clear(self, key: str, group: str) -> None:
        self._lone[key] = deque((t, g) for t, g in self._lone[key] if g != group)


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
    """`wide_groups`: mã → khoá gộp lan rộng. Các mã của cùng một dịch vụ critical toàn mạng
    (`kpi.yaml`, vd D6036 + D6037 = callback thanh toán) gộp thành MỘT sự cố lan rộng, vì một
    sự cố của dịch vụ thường sinh nhiều mã cùng lúc. Mã khác gộp theo chính nó.
    """

    def __init__(self, spec: ScanSpec, wide_groups: dict[str, str] | None = None) -> None:
        self._spec = spec
        self._wide_groups = wide_groups or {}
        self.baseline = Baseline(spec)
        self.rolling = Rolling(timedelta(minutes=spec.local_window_minutes))
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

        # 1. WIDE: cùng một mã (hoặc các mã của cùng một dịch vụ critical) vượt nền ở nhiều trạm.
        by_group: dict[str, list[Detection]] = defaultdict(list)
        for d in rest:
            by_group[self._wide_groups.get(d.group, d.group)].append(d)
        for group, items in sorted(by_group.items()):
            key = f"WIDE|{group}"
            members = sorted({d.group for d in items})
            wide = (
                len({d.station_id for d in items}) >= s.wide_min_stations
                and len(items) >= s.wide_min_txns
                and self.baseline.is_above_any(members, len(items), n_new)
            )
            if wide or self._active(key, scan_at):
                take(key, "WIDE", items)

        # Lỗi đáng chú ý ở mức trạm / tủ: mã High, hoặc mã vượt nền tại đúng trạm / tủ đó.
        # Đếm cả lỗi chưa vào sự cố của các lần quét trước trong cửa sổ trượt (`Rolling`).
        for (station, cab), n in result.new_by_cabinet.items():
            self.rolling.add_exposure(f"STATION|{station}", scan_at, n)
            if cab:
                self.rolling.add_exposure(f"CABINET|{cab}", scan_at, n)

        def notable(key: str, items: list[Detection], min_txns: int) -> list[Detection]:
            counts = Counter(d.group for d in items) + self.rolling.counts(key, scan_at)
            exposure = self.rolling.exposure(key, scan_at)
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
            hot = notable(key, items, s.station_min_txns)
            spread = (
                len({d.cabinet_id for d in hot}) >= s.station_min_cabinets
                and len(hot) >= s.station_min_txns
            )
            picked = hot if spread else [d for d in items if joins(key, d)]
            if picked:
                take(key, "STATION", picked)
                for g in {d.group for d in picked}:
                    self.rolling.clear(key, g)

        # 3. CABINET: lỗi đáng chú ý tại một tủ.
        by_cabinet: dict[str, list[Detection]] = defaultdict(list)
        for d in rest:
            if d.cabinet_id:
                by_cabinet[d.cabinet_id].append(d)
        for cabinet, items in sorted(by_cabinet.items()):
            key = f"CABINET|{cabinet}"
            hot_ids = {d.emsp_txn_id for d in notable(key, items, s.cabinet_min_txns)}
            picked = [d for d in items if d.emsp_txn_id in hot_ids or joins(key, d)]
            if not picked:
                continue
            target: str = key
            kind: IncidentKind = "CABINET"
            if self._active(key, scan_at) is None and hot_ids:
                promoted = self._promote_to_station(items[0].station_id, key, scan_at)
                if promoted is not None:
                    target, kind = promoted, "STATION"
            take(target, kind, picked)
            for g in {d.group for d in picked}:
                self.rolling.clear(key, g)

        # Lỗi lẻ còn lại: nhớ trong cửa sổ trượt để lần quét sau cộng dồn.
        for d in rest:
            if d.cabinet_id:
                self.rolling.add_lone(f"CABINET|{d.cabinet_id}", scan_at, d.group)
            self.rolling.add_lone(f"STATION|{d.station_id}", scan_at, d.group)

        self.baseline.update(scan_at, n_new, Counter(d.group for d in detections))
        return [self._record(scan_at, key, kinds[key], assigned[key]) for key in sorted(assigned)]

    def _promote_to_station(self, station: str, cabinet_key: str, scan_at: datetime) -> str | None:
        """Tủ khác của cùng trạm đang có sự cố mở → nâng sự cố đó lên mức trạm và trả khoá trạm.

        Vì sao: sự cố cả trạm (vd mất điện) ở mật độ thật lộ ra lần lượt ở từng tủ, cách nhau vài
        giờ; mỗi tủ mở một sự cố riêng thì một sự cố thật bị tách thành nhiều cảnh báo.
        """
        prefix = f"CABINET|{station}."
        for key, inc in list(self._open.items()):
            if key.startswith(prefix) and key != cabinet_key and self._active(key, scan_at):
                station_key = f"STATION|{station}"
                inc.kind = "STATION"
                inc.key = station_key
                self._open[station_key] = inc
                self._open[cabinet_key] = inc
                return station_key
        return None

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
