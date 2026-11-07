"""Ghi output detector ra `data/<profile>/` (DETECTOR_PLAN §5).

Mọi file được sắp xếp ổn định để 2 lần chạy trên cùng dữ liệu ra sha256 giống hệt.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ase.common.models import Category, Severity, Step
from ase.detector.correlator import Correlator, Detection, IncidentRecord
from ase.detector.dispatch import Dispatcher, P1Alert
from ase.detector.fields import CoverageReport

DETECTIONS_FILE = "detections.jsonl"
INCIDENTS_FILE = "incidents.jsonl"
ALERTS_FILE = "alerts.jsonl"
BATCHES_FILE = "batches.jsonl"
TRANSACTION_TOPIC_FILE = "ase.anomaly.transaction.v0.jsonl"
COVERAGE_FILE = "coverage.json"


class AnomalyScope(BaseModel):
    station_id: str
    cabinet_id: str | None = None
    slot_id: str | None = None


class TransactionAnomalyV0(BaseModel):
    """Bản xuất `ase.anomaly.transaction.v0` theo CLAUDE.md §9 (chỉ ghi file, chưa tích hợp)."""

    schema_version: Literal["0.1"] = "0.1"
    incident_id: str
    revision: int
    session_ids: list[str]
    detected_at: datetime
    step: Step
    category: Category
    severity: Severity
    confidence: float
    scope: AnomalyScope
    probable_cause: str
    evidence: list[str]
    alert_status: Literal["SENT", "SCHEDULED", "NONE"]


class RunReport(BaseModel):
    """`coverage.json`: tầng trường, rule bị tắt, xử lý event + thống kê output."""

    coverage: CoverageReport
    detections_by_category: dict[str, int] = Field(default_factory=dict)
    incidents_by_category: dict[str, int] = Field(default_factory=dict)
    n_detections: int = 0
    n_incidents: int = 0
    n_user_spike_incidents: int = 0
    n_cluster_reassigned: int = 0
    n_merged_incidents: int = 0
    n_p1_alerts: int = 0
    n_batches: int = 0
    notify: bool = False
    send_errors: int = 0


def build_incident_records(corr: Correlator, disp: Dispatcher) -> list[IncidentRecord]:
    records: list[IncidentRecord] = []
    for inc in corr.reportable_incidents():
        final = corr.snapshot(inc.incident_id)
        for snap in [*inc.revision_snapshots, final]:
            records.append(
                snap.model_copy(
                    update={
                        "team": disp.state.team_of.get(inc.incident_id),
                        "dispatch": disp.state.dispatch_kind.get(inc.incident_id, "NONE"),
                        "alert_sent_at": disp.state.first_alert_at.get(inc.incident_id),
                        "batch_ids": list(disp.state.batch_ids.get(inc.incident_id, [])),
                    }
                )
            )
    records.sort(key=lambda r: (r.incident_id, r.revision))
    return records


def to_transaction_v0(rec: IncidentRecord, *, notify: bool) -> TransactionAnomalyV0:
    status: Literal["SENT", "SCHEDULED", "NONE"]
    if rec.dispatch == "NONE":
        status = "NONE"
    else:
        status = "SENT" if notify else "SCHEDULED"
    return TransactionAnomalyV0(
        incident_id=rec.incident_id,
        revision=rec.revision,
        session_ids=rec.session_ids,
        detected_at=rec.detected_at,
        step=rec.step,
        category=rec.category,
        severity=rec.severity,
        confidence=rec.confidence,
        scope=AnomalyScope(
            station_id=rec.station_id, cabinet_id=rec.cabinet_id, slot_id=rec.slot_id
        ),
        probable_cause=rec.probable_cause,
        evidence=rec.evidence,
        alert_status=status,
    )


def _write_jsonl(path: Path, items: list[BaseModel]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(it.model_dump_json())
            f.write("\n")


def write_incidents_atomic(
    out_dir: Path,
    *,
    corr: Correlator,
    disp: Dispatcher,
) -> None:
    """Ghi `incidents.jsonl` qua file tạm rồi `rename` nguyên tử (STREAMING_PLAN §7)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    records = build_incident_records(corr, disp)
    tmp_path = out_dir / f".{INCIDENTS_FILE}.tmp"
    _write_jsonl(tmp_path, list(records))
    tmp_path.replace(out_dir / INCIDENTS_FILE)


def write_outputs(
    out_dir: Path,
    *,
    corr: Correlator,
    disp: Dispatcher,
    coverage: CoverageReport,
    notify: bool,
) -> RunReport:
    out_dir.mkdir(parents=True, exist_ok=True)
    detections: list[Detection] = sorted(
        corr.detections.values(), key=lambda d: (d.detected_at, d.session_id)
    )
    records = build_incident_records(corr, disp)
    latest: dict[str, IncidentRecord] = {}
    for r in records:
        if r.merged_into is None:
            latest[r.incident_id] = r  # đã sort theo revision → giữ revision cuối
    alerts: list[P1Alert] = sorted(disp.p1_alerts, key=lambda a: (a.sent_at, a.incident_id))
    batches = sorted(disp.batches, key=lambda b: (b.window_start, b.team))

    _write_jsonl(out_dir / DETECTIONS_FILE, list(detections))
    _write_jsonl(out_dir / INCIDENTS_FILE, list(records))
    _write_jsonl(out_dir / ALERTS_FILE, list(alerts))
    _write_jsonl(out_dir / BATCHES_FILE, list(batches))
    _write_jsonl(
        out_dir / TRANSACTION_TOPIC_FILE,
        [to_transaction_v0(r, notify=notify) for r in latest.values() if not r.is_user_spike],
    )

    report = RunReport(
        coverage=coverage,
        detections_by_category=dict(sorted(Counter(d.category.value for d in detections).items())),
        incidents_by_category=dict(
            sorted(Counter(r.category.value for r in latest.values()).items())
        ),
        n_detections=len(detections),
        n_incidents=len(latest),
        n_user_spike_incidents=sum(1 for r in latest.values() if r.is_user_spike),
        n_cluster_reassigned=sum(1 for d in detections if d.rule_id == "R_CLUSTER"),
        n_merged_incidents=sum(1 for r in records if r.merged_into is not None),
        n_p1_alerts=len(alerts),
        n_batches=len(batches),
        notify=notify,
        send_errors=disp.send_errors,
    )
    (out_dir / COVERAGE_FILE).write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return report
