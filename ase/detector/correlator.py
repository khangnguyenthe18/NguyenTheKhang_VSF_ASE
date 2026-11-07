"""`Correlator` — gộp `Classification` (mức phiên) thành `Incident` (DETECTOR_PLAN §4.3).

Quyết định thiết kế:
- Khoá gộp theo phạm vi tự nhiên của nhóm lỗi (CLAUDE.md §6.3): SYSTEM theo `error_code` toàn
  mạng (nhiều nước cùng lúc → 1 incident GLOBAL); PAYMENT theo provider; HARDWARE theo tủ;
  BATTERY_QUALITY theo lô; BUSINESS theo trạm; UNKNOWN theo `(step, station)`.
- USER không tạo incident (đi tin gom CSKH). Chỉ khi lỗi USER tăng đột biến tại 1 trạm mới mở
  1 incident `UNKNOWN` cho IT Ops (`user_spike_*` trong `detector.yaml`).
- Cửa sổ: incident nhận thêm phiên nếu `occurred_at` cách `last_seen_at` ≤ `window_minutes`.
- `R_CLUSTER`: phiên `UNKNOWN` (thường do thiếu `error_code`) trùng bước + phạm vi + thời gian
  với incident có mã đang mở thì được gắn vào incident đó. Cả hai chiều: phiên UNKNOWN đến sau
  incident, hoặc incident có mã mở sau và "hút" incident UNKNOWN chưa được gửi đi.
  Chỉ áp cho `UNKNOWN`, không áp cho phiên suy luận (đã có mã cụ thể, đo trên `dev` đều đúng).
- Revision chỉ tăng khi severity tăng (CLAUDE.md §8) để giữ SF8 ≤ 1,2.
- Evidence chỉ là `event_id` có thật trong luồng (SF9).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field

from ase.common.models import Category, Incident, Severity, Step
from ase.common.time import ensure_utc
from ase.detector.classifier import Classification
from ase.detector.fields import FieldView
from ase.knowledge.loader import Knowledge, default_knowledge

SEVERITY_RANK: dict[Severity, int] = {Severity.P1: 0, Severity.P2: 1, Severity.P3: 2}
GLOBAL_COUNTRY = "GLOBAL"

ScopeKey = tuple[str, ...]


def max_severity(a: Severity, b: Severity) -> Severity:
    return a if SEVERITY_RANK[a] <= SEVERITY_RANK[b] else b


class Detection(Classification):
    """1 dòng `detections.jsonl`: phân loại cuối của 1 phiên + incident chứa nó."""

    incident_id: str | None = None
    country: str
    timezone: str
    payment_provider: str | None = None
    lot_id: str | None = None
    original_rule_id: str | None = None  # rule trước khi bị R_CLUSTER gán lại


class IncidentRecord(Incident):
    """1 dòng `incidents.jsonl` (1 dòng / revision). Thêm metadata gửi, không đổi `Incident`."""

    detected_at: datetime  # thời điểm (mô phỏng) incident mở
    revision_at: datetime  # thời điểm (mô phỏng) revision này được tạo
    scope: str
    rule_ids: list[str] = Field(default_factory=list)
    team: str | None = None
    dispatch: Literal["IMMEDIATE", "BATCH", "NONE"] = "NONE"
    alert_sent_at: datetime | None = None  # P1: lúc gửi ngay; P2/P3: cuối chu kỳ tin gom đầu
    batch_ids: list[str] = Field(default_factory=list)
    is_user_spike: bool = False
    merged_into: str | None = None  # incident UNKNOWN đã bị R_CLUSTER gộp vào incident có mã


@dataclass
class _Inc:
    incident_id: str
    key: ScopeKey
    category: Category
    error_code: str | None
    step: Step
    severity: Severity
    detected_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    provider: str | None = None
    is_user_spike: bool = False
    revision: int = 1
    session_ids: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    revision_times: list[datetime] = field(default_factory=list)
    revision_snapshots: list[IncidentRecord] = field(default_factory=list)
    dispatched: bool = False
    absorbed_into: str | None = None


@dataclass(frozen=True)
class CorrelationEvent:
    """Kết quả của `Correlator.add`: incident mới mở / tăng severity (để dispatcher xử lý)."""

    detection: Detection
    incident_id: str | None
    opened: bool = False
    escalated: bool = False
    spike_incident_id: str | None = None


class Correlator:
    def __init__(self, field_view: FieldView, *, knowledge: Knowledge | None = None) -> None:
        self.fv = field_view
        self.k = knowledge or default_knowledge()
        self.cfg = self.k.detector.correlator
        self.spike_window = timedelta(minutes=self.cfg.user_spike_window_minutes)

        self.detections: dict[str, Detection] = {}
        self.incidents: dict[str, _Inc] = {}  # mọi incident (kể cả đã đóng)
        self._current: dict[ScopeKey, str] = {}  # khoá → incident mới nhất của khoá
        self._seq_by_day: dict[str, int] = {}
        self._user_recent: dict[str, deque[tuple[datetime, str]]] = {}
        self._spike_until: dict[str, datetime] = {}

    # --- tiện ích ---------------------------------------------------------------------------

    def _new_id(self, at: datetime) -> str:
        day = f"{ensure_utc(at):%Y%m%d}"
        seq = self._seq_by_day.get(day, 0) + 1
        self._seq_by_day[day] = seq
        return f"INC-{day}-{seq:04d}"

    def _is_open(self, inc: _Inc, occurred_at: datetime) -> bool:
        window = timedelta(minutes=self.cfg.window_for(inc.category))
        return (
            inc.absorbed_into is None
            and occurred_at - inc.last_seen_at <= window
            and inc.first_seen_at - occurred_at <= window
        )

    def _scope_key(self, det: Detection) -> ScopeKey | None:
        code = det.error_code or "-"
        cat = det.category
        if cat is Category.USER:
            return None
        if cat is Category.SYSTEM:
            return ("SYSTEM", code)
        if cat is Category.PAYMENT:
            if det.payment_provider is not None:
                return ("PAYMENT", code, "provider", det.payment_provider)
            return ("PAYMENT", code, "country", det.country)
        if cat is Category.HARDWARE:
            if det.cabinet_id is not None:
                return ("HARDWARE", code, "cabinet", det.cabinet_id)
            return ("HARDWARE", code, "station", det.station_id)
        if cat is Category.BATTERY_QUALITY:
            if det.lot_id is not None:
                return ("BATTERY_QUALITY", code, "lot", det.lot_id)
            return ("BATTERY_QUALITY", code, "station", det.station_id)
        if cat is Category.BUSINESS:
            return ("BUSINESS", code, det.station_id)
        return ("UNKNOWN", det.step.value, det.station_id)

    def _scope_matches(self, inc: _Inc, det: Detection) -> bool:
        """Phiên `det` (UNKNOWN) có nằm trong phạm vi của incident có mã `inc` không."""
        if inc.step is not det.step:
            return False
        kind = inc.key[2] if len(inc.key) > 2 else None
        val = inc.key[3] if len(inc.key) > 3 else None
        if inc.category is Category.SYSTEM:
            return True
        if inc.category is Category.PAYMENT:
            if kind == "provider":
                return det.payment_provider == val
            return det.country == val
        if inc.category is Category.HARDWARE:
            if kind == "cabinet":
                return det.cabinet_id == val
            return det.station_id == val
        if inc.category is Category.BATTERY_QUALITY:
            if kind == "lot":
                return det.lot_id == val
            return det.station_id == val
        if inc.category is Category.BUSINESS:
            return det.station_id == inc.key[2]
        return False

    def _enrich(self, cls: Classification) -> Detection:
        now = cls.detected_at
        country, tz = self.fv.station_country_timezone(cls.station_id, cls.session_id)
        lot: str | None = None
        if cls.step is Step.RECEIVE_BATTERY:
            lot = self.fv.battery_out_lot_id(cls.session_id, now)
        return Detection(
            **cls.model_dump(),
            country=country,
            timezone=tz,
            payment_provider=self.fv.payment_provider(cls.session_id, now),
            lot_id=lot,
        )

    # --- incident ---------------------------------------------------------------------------

    def _open(self, det: Detection, key: ScopeKey) -> _Inc:
        inc = _Inc(
            incident_id=self._new_id(det.detected_at),
            key=key,
            category=det.category,
            error_code=det.error_code,
            step=det.step,
            severity=det.severity,
            detected_at=det.detected_at,
            first_seen_at=det.occurred_at,
            last_seen_at=det.occurred_at,
            provider=det.payment_provider,
            revision_times=[det.detected_at],
        )
        self.incidents[inc.incident_id] = inc
        self._current[key] = inc.incident_id
        return inc

    def _attach(self, inc: _Inc, det: Detection) -> bool:
        """Gắn phiên vào incident. Trả về True nếu severity tăng (→ revision mới)."""
        new_sev = max_severity(inc.severity, det.severity)
        escalated = bool(inc.session_ids) and new_sev is not inc.severity
        if escalated:
            # Chốt nội dung revision cũ (trước phiên này) rồi mới tăng revision
            inc.revision_snapshots.append(self.snapshot(inc.incident_id))
            inc.revision += 1
            inc.revision_times.append(det.detected_at)
        inc.severity = new_sev
        det.incident_id = inc.incident_id
        inc.session_ids.append(det.session_id)
        if det.event_ids and len(inc.evidence) < self.cfg.max_evidence_ids:
            inc.evidence.append(det.event_ids[0])
        inc.first_seen_at = min(inc.first_seen_at, det.occurred_at)
        inc.last_seen_at = max(inc.last_seen_at, det.occurred_at)
        return escalated

    def _cluster_into(self, det: Detection, inc: _Inc) -> None:
        det.original_rule_id = det.rule_id
        det.rule_id = "R_CLUSTER"
        det.category = inc.category
        det.error_code = inc.error_code
        det.severity = (
            self.k.error_catalog.severity_for(inc.error_code)
            if inc.error_code is not None and self.k.error_catalog.has(inc.error_code)
            else inc.severity
        )
        det.confidence = self.k.detector.classifier.inferred_confidence
        det.needs_human_review = True

    def _find_cluster_target(self, det: Detection) -> _Inc | None:
        best: _Inc | None = None
        for iid in self._current.values():
            inc = self.incidents[iid]
            if inc.category in (Category.UNKNOWN, Category.USER) or inc.is_user_spike:
                continue
            if not self._is_open(inc, det.occurred_at) or not self._scope_matches(inc, det):
                continue
            if best is None or (inc.last_seen_at, inc.incident_id) > (
                best.last_seen_at,
                best.incident_id,
            ):
                best = inc
        return best

    def _absorb_unknown(self, target: _Inc, trigger: Detection) -> None:
        """Incident có mã vừa nhận phiên: hút các incident UNKNOWN cùng bước + phạm vi.

        Dùng cửa sổ của nhóm đích (vd SYSTEM 15'). Incident UNKNOWN có thể đã nằm trong 1 tin
        gom trước đó: vẫn gộp, nhưng giữ bản ghi cũ với `merged_into` để tin gom đã gửi không
        trỏ tới ID không tồn tại (SF7/SF9).
        """
        window = timedelta(minutes=self.cfg.window_for(target.category))
        for key, iid in list(self._current.items()):
            if key[0] != "UNKNOWN" or key[1] != target.step.value:
                continue
            unk = self.incidents[iid]
            if unk.absorbed_into is not None or unk.is_user_spike:
                continue
            if trigger.occurred_at - unk.last_seen_at > window:
                continue
            if unk.first_seen_at - trigger.occurred_at > window:
                continue
            members = [self.detections[s] for s in unk.session_ids]
            if not all(self._scope_matches(target, d) for d in members):
                continue
            for d in members:
                self._cluster_into(d, target)
                self._attach(target, d)
            unk.absorbed_into = target.incident_id
            del self._current[key]

    def add(self, cls: Classification) -> CorrelationEvent:
        det = self._enrich(cls)
        self.detections[det.session_id] = det

        spike_id = self._track_user_spike(det) if det.category is Category.USER else None
        if det.category is Category.USER:
            return CorrelationEvent(detection=det, incident_id=None, spike_incident_id=spike_id)

        if det.category is Category.UNKNOWN:
            target = self._find_cluster_target(det)
            if target is not None:
                self._cluster_into(det, target)
                escalated = self._attach(target, det)
                return CorrelationEvent(
                    detection=det, incident_id=target.incident_id, escalated=escalated
                )

        key = self._scope_key(det)
        assert key is not None
        cur_id = self._current.get(key)
        cur = self.incidents.get(cur_id) if cur_id is not None else None
        opened = False
        if cur is None or not self._is_open(cur, det.occurred_at):
            cur = self._open(det, key)
            opened = True
        escalated = self._attach(cur, det)
        if det.category is not Category.UNKNOWN:
            self._absorb_unknown(cur, det)
        return CorrelationEvent(
            detection=det, incident_id=cur.incident_id, opened=opened, escalated=escalated
        )

    # --- USER đột biến ----------------------------------------------------------------------

    def _track_user_spike(self, det: Detection) -> str | None:
        q = self._user_recent.setdefault(det.station_id, deque())
        q.append((det.occurred_at, det.session_id))
        while q and det.occurred_at - q[0][0] > self.spike_window:
            q.popleft()
        until = self._spike_until.get(det.station_id)
        if until is not None and det.occurred_at <= until:
            return None
        if len(q) < self.cfg.user_spike_min_sessions:
            return None
        sids = [s for _, s in q]
        first = self.detections[sids[0]]
        inc = _Inc(
            incident_id=self._new_id(det.detected_at),
            key=("USER_SPIKE", det.station_id, det.detected_at.isoformat()),
            category=Category.UNKNOWN,
            error_code=None,
            step=first.step,
            severity=Severity.P2,
            detected_at=det.detected_at,
            first_seen_at=min(self.detections[s].occurred_at for s in sids),
            last_seen_at=max(self.detections[s].occurred_at for s in sids),
            is_user_spike=True,
            revision_times=[det.detected_at],
        )
        inc.session_ids = sids
        inc.evidence = [
            e for s in sids[: self.cfg.max_evidence_ids] for e in self.detections[s].event_ids[:1]
        ]
        self.incidents[inc.incident_id] = inc
        self._spike_until[det.station_id] = det.occurred_at + self.spike_window
        return inc.incident_id

    # --- xuất -------------------------------------------------------------------------------

    def get(self, incident_id: str) -> _Inc:
        return self.incidents[incident_id]

    def live_incidents(self) -> list[_Inc]:
        return [i for i in self.incidents.values() if i.absorbed_into is None]

    def reportable_incidents(self) -> list[_Inc]:
        """Incident sống + incident đã gộp nhưng đã được gửi đi (cần giữ để tham chiếu)."""
        return [i for i in self.incidents.values() if i.absorbed_into is None or i.dispatched]

    def scope_label(self, inc: _Inc) -> str:
        dets = [self.detections[s] for s in inc.session_ids]
        countries = sorted({d.country for d in dets})
        stations = sorted({d.station_id for d in dets})
        cabinets = sorted({d.cabinet_id for d in dets if d.cabinet_id})
        if inc.category is Category.SYSTEM and len(countries) > 1:
            return f"{GLOBAL_COUNTRY} ({', '.join(countries)})"
        if len(inc.key) > 3 and inc.key[2] == "provider":
            return f"{inc.key[3]} ({', '.join(countries)})"
        if len(inc.key) > 3 and inc.key[2] == "lot":
            return f"Lô {inc.key[3]} ({len(stations)} trạm)"
        if len(stations) == 1:
            return f"{stations[0]} / {cabinets[0]}" if len(cabinets) == 1 else stations[0]
        return f"{len(stations)} trạm ({', '.join(countries)})"

    def _probable_cause(self, inc: _Inc, n_stations: int, scope: str) -> str:
        n = len(inc.session_ids)
        if inc.is_user_spike:
            return (
                f"[Rule] Lỗi người dùng tăng đột biến: {n} lỗi USER trong "
                f"{self.cfg.user_spike_window_minutes} phút tại {scope}. Kiểm tra có phải lỗi "
                "hệ thống (app/QR/thanh toán) không."
            )
        cat = self.k.error_catalog
        if inc.category is Category.UNKNOWN or inc.error_code is None:
            return (
                f"[Rule] Không khớp rule nào ở bước {inc.step.value}: {n} giao dịch tại {scope}."
                " Cần OPS kiểm tra."
            )
        desc = (
            cat.by_code[inc.error_code].description if cat.has(inc.error_code) else inc.error_code
        )
        return f"[Rule] {desc}: {n} giao dịch tại {n_stations} trạm, phạm vi {scope}."[:500]

    def snapshot(self, incident_id: str) -> IncidentRecord:
        inc = self.incidents[incident_id]
        dets = [self.detections[s] for s in inc.session_ids]
        first = dets[0]
        countries = sorted({d.country for d in dets})
        stations = {d.station_id for d in dets}
        cabinets = {d.cabinet_id for d in dets}
        slots = {(d.cabinet_id, d.slot_id) for d in dets}
        is_global = inc.category is Category.SYSTEM and len(countries) > 1
        scope = self.scope_label(inc)
        conf = sum(d.confidence for d in dets) / len(dets) if not inc.is_user_spike else 0.5
        span_min = math.ceil((inc.last_seen_at - inc.first_seen_at).total_seconds() / 60)
        rule_ids = sorted({d.rule_id for d in dets}) if not inc.is_user_spike else ["R_USER_SPIKE"]
        return IncidentRecord(
            incident_id=inc.incident_id,
            revision=inc.revision,
            category=inc.category,
            severity=inc.severity,
            error_code=inc.error_code,
            step=inc.step,
            station_id=first.station_id,
            cabinet_id=first.cabinet_id if len(cabinets) == 1 else None,
            slot_id=first.slot_id if len(slots) == 1 else None,
            country=GLOBAL_COUNTRY if is_global else first.country,
            timezone=first.timezone,
            countries=countries,
            session_ids=list(inc.session_ids),
            first_seen_at=inc.first_seen_at,
            last_seen_at=inc.last_seen_at,
            n_sessions=len(inc.session_ids),
            n_stations=len(stations),
            window_minutes=span_min,
            confidence=round(conf, 2),
            probable_cause=self._probable_cause(inc, len(stations), scope),
            evidence=list(inc.evidence[: self.cfg.max_evidence_ids]),
            needs_human_review=inc.is_user_spike
            or inc.category is Category.UNKNOWN
            or any(d.needs_human_review for d in dets),
            detected_at=inc.detected_at,
            revision_at=inc.revision_times[-1],
            scope=scope,
            rule_ids=rule_ids,
            is_user_spike=inc.is_user_spike,
            merged_into=inc.absorbed_into,
        )
