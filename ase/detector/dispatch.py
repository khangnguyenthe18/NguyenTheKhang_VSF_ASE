"""Lịch gửi thông báo (DETECTOR_PLAN §4.5, CLAUDE.md §8 "Lịch gửi thông báo").

- **P1:** gửi ngay khi incident mở (hoặc lên P1) → SF5 < 60s.
- **P2/P3 (trừ USER):** gom theo team, chu kỳ `notify.batch_interval_minutes` căn mốc tròn UTC
  theo `SimClock`; mỗi team ≤ 1 tin/chu kỳ, team rỗng không gửi.
- **USER:** vào tin gom của CSKH (theo `routing.yaml`), không gửi tức thời, không gọi LLM.
- Một lỗi được quyết định đi đường nào **tại thời điểm phát hiện**: incident đang là P1 → đã có
  alert P1, không vào tin gom; ngược lại → vào đúng 1 tin gom của chu kỳ chứa `detected_at`.
- Báo cáo ngày 18:00: Sprint 1 (GATE1_PLAN §3).

Dispatcher luôn tính lịch gửi (thời gian mô phỏng) để evaluator đo SF5/SF7/SF8, kể cả khi
không bật `--notify`. Khi bật, việc gửi thật chạy ở 1 worker async qua hàng đợi, không chặn
vòng xử lý event.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from pydantic import BaseModel

from ase.common.models import (
    BatchIncident,
    BatchRow,
    Category,
    Incident,
    Severity,
    TeamBatch,
)
from ase.common.time import ensure_utc
from ase.detector.correlator import CorrelationEvent, Correlator
from ase.knowledge.loader import Knowledge, default_knowledge
from ase.notifier.router import AlertRouter, team_to_channel
from ase.notifier.service import NotifierService

log = structlog.get_logger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class P1Alert(BaseModel):
    """1 alert P1 gửi ngay (1 dòng trong `alerts.jsonl`)."""

    incident_id: str
    revision: int
    team: str
    channel: str
    sent_at: datetime  # thời gian mô phỏng


@dataclass
class _Job:
    kind: Literal["incident", "batch"]
    incident: Incident | None = None
    batch: TeamBatch | None = None


@dataclass
class DispatchState:
    first_alert_at: dict[str, datetime] = field(default_factory=dict)
    dispatch_kind: dict[str, Literal["IMMEDIATE", "BATCH"]] = field(default_factory=dict)
    batch_ids: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    team_of: dict[str, str] = field(default_factory=dict)


class Dispatcher:
    def __init__(
        self,
        correlator: Correlator,
        *,
        knowledge: Knowledge | None = None,
        notifier: NotifierService | None = None,
    ) -> None:
        self.corr = correlator
        self.k = knowledge or default_knowledge()
        self.router = AlertRouter(self.k.routing)
        self.interval = timedelta(minutes=self.k.detector.notify.batch_interval_minutes)
        self.notifier = notifier

        self._pending_rows: dict[datetime, list[str]] = defaultdict(list)
        self._pending_spikes: dict[datetime, list[str]] = defaultdict(list)
        self._p1_sent: dict[str, int] = {}  # incident_id -> revision đã gửi P1
        self.p1_alerts: list[P1Alert] = []
        self.batches: list[TeamBatch] = []
        self.state = DispatchState()

        self._queue: asyncio.Queue[_Job | None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self.send_errors: int = 0

    # --- chu kỳ ----------------------------------------------------------------------------

    def window_start(self, t: datetime) -> datetime:
        secs = int(self.interval.total_seconds())
        elapsed = int((ensure_utc(t) - _EPOCH).total_seconds())
        return _EPOCH + timedelta(seconds=elapsed - elapsed % secs)

    # --- vào ------------------------------------------------------------------------------

    def on_correlation(self, ev: CorrelationEvent) -> None:
        det = ev.detection
        ws = self.window_start(det.detected_at)
        if ev.spike_incident_id is not None:
            self._pending_spikes[ws].append(ev.spike_incident_id)

        if det.incident_id is None:  # USER → tin gom CSKH
            self._pending_rows[ws].append(det.session_id)
            return

        inc = self.corr.get(det.incident_id)
        if inc.severity is Severity.P1:
            if self._p1_sent.get(inc.incident_id) != inc.revision:
                self._send_p1(inc.incident_id, det.detected_at)
            return
        self._pending_rows[ws].append(det.session_id)

    def _send_p1(self, incident_id: str, now: datetime) -> None:
        snap = self.corr.snapshot(incident_id)
        alert = self.router.route(snap)
        inc = self.corr.get(incident_id)
        inc.dispatched = True
        self._p1_sent[incident_id] = snap.revision
        if alert is None:
            return
        self.p1_alerts.append(
            P1Alert(
                incident_id=incident_id,
                revision=snap.revision,
                team=alert.team,
                channel=alert.channel,
                sent_at=now,
            )
        )
        self.state.first_alert_at.setdefault(incident_id, now)
        self.state.dispatch_kind.setdefault(incident_id, "IMMEDIATE")
        self.state.team_of.setdefault(incident_id, alert.team)
        self._enqueue(_Job(kind="incident", incident=snap))

    # --- tin gom ---------------------------------------------------------------------------

    def tick(self, now: datetime) -> None:
        """Gọi mỗi khi clock tiến: đóng mọi chu kỳ đã kết thúc (`window_end <= now`)."""
        due = sorted(
            ws
            for ws in set(self._pending_rows) | set(self._pending_spikes)
            if ws + self.interval <= now
        )
        for ws in due:
            self._flush_window(ws)

    def flush_all(self) -> None:
        for ws in sorted(set(self._pending_rows) | set(self._pending_spikes)):
            self._flush_window(ws)

    def _flush_window(self, ws: datetime) -> None:
        we = ws + self.interval
        rows_by_team: dict[str, list[BatchRow]] = defaultdict(list)
        incs_by_team: dict[str, dict[str, BatchIncident]] = defaultdict(dict)

        for sid in self._pending_rows.pop(ws, []):
            det = self.corr.detections[sid]
            route = self.k.routing.route_for(det.category, det.country)
            if not route.alert or route.team is None:
                continue
            rows_by_team[route.team].append(
                BatchRow(
                    session_id=det.session_id,
                    incident_id=det.incident_id,
                    category=det.category,
                    severity=det.severity,
                    error_code=det.error_code,
                    step=det.step,
                    station_id=det.station_id,
                    cabinet_id=det.cabinet_id,
                    slot_id=det.slot_id,
                    country=det.country,
                    timezone=det.timezone,
                    occurred_at=det.occurred_at,
                    detected_at=det.detected_at,
                )
            )

        for team, rows in rows_by_team.items():
            counts: dict[str, int] = defaultdict(int)
            for r in rows:
                if r.incident_id is not None:
                    counts[r.incident_id] += 1
            for iid, n in counts.items():
                incs_by_team[team][iid] = self._batch_incident(iid, n)

        for iid in self._pending_spikes.pop(ws, []):
            route = self.k.routing.route_for(Category.UNKNOWN, None)
            if route.team is None:
                continue
            incs_by_team[route.team][iid] = self._batch_incident(iid, None)

        for team in sorted(set(rows_by_team) | set(incs_by_team)):
            rows = sorted(rows_by_team.get(team, []), key=lambda r: (r.occurred_at, r.session_id))
            incs = sorted(
                incs_by_team.get(team, {}).values(),
                key=lambda i: (i.first_seen_at, i.incident_id),
            )
            if not rows and not incs:
                continue
            channel = team_to_channel(team)
            batch = TeamBatch(
                batch_id=f"batch:{channel}:{ws:%Y%m%dT%H%MZ}",
                team=team,
                channel=channel,
                window_start=ws,
                window_end=we,
                rows=rows,
                incidents=incs,
            )
            self.batches.append(batch)
            for i in incs:
                inc = self.corr.get(i.incident_id)
                inc.dispatched = True
                self.state.first_alert_at.setdefault(i.incident_id, we)
                self.state.dispatch_kind.setdefault(i.incident_id, "BATCH")
                self.state.team_of.setdefault(i.incident_id, team)
                self.state.batch_ids[i.incident_id].append(batch.batch_id)
            self._enqueue(_Job(kind="batch", batch=batch))

    def _batch_incident(self, incident_id: str, n_rows: int | None) -> BatchIncident:
        snap = self.corr.snapshot(incident_id)
        return BatchIncident(
            incident_id=incident_id,
            category=snap.category,
            severity=snap.severity,
            error_code=snap.error_code,
            step=snap.step,
            scope=snap.scope,
            n_sessions=snap.n_sessions if n_rows is None else n_rows,
            n_stations=snap.n_stations,
            first_seen_at=snap.first_seen_at,
            needs_human_review=snap.needs_human_review,
        )

    # --- gửi thật (tuỳ chọn) ---------------------------------------------------------------

    def start(self) -> None:
        if self.notifier is None or self._worker is not None:
            return
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._run_worker())

    def _enqueue(self, job: _Job) -> None:
        if self._queue is not None:
            self._queue.put_nowait(job)

    async def _run_worker(self) -> None:
        assert self._queue is not None and self.notifier is not None
        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    return
                if job.kind == "incident" and job.incident is not None:
                    results = (await self.notifier.notify(job.incident)).results
                elif job.batch is not None:
                    results = (await self.notifier.notify_batch(job.batch)).results
                else:
                    results = []
                self.send_errors += sum(1 for r in results if r.status == "FAILED")
            except Exception as exc:  # gửi lỗi không được làm dừng detector
                self.send_errors += 1
                log.error("dispatch_send_failed", kind=job.kind if job else None, error=str(exc))
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        if self._queue is None or self._worker is None:
            return
        await self._queue.put(None)
        await self._worker
        self._worker = None
