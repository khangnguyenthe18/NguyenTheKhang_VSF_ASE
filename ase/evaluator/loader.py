"""Đọc output của detector và ground truth thành model Pydantic (EVALUATOR_PLAN §2, §3).

Evaluator là nơi DUY NHẤT (ngoài test) được đọc `ground_truth.csv`.
Dòng JSONL không parse được không làm dừng chương trình: chúng được đếm vào `parse_errors` để
SF7 (hợp lệ schema) phản ánh đúng.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson
import polars as pl
from pydantic import BaseModel, ValidationError

from ase.common.models import GroundTruthRow, TeamBatch
from ase.detector.correlator import Detection, IncidentRecord
from ase.detector.dispatch import P1Alert
from ase.detector.output import RunReport

DETECTIONS = "detections.jsonl"
INCIDENTS = "incidents.jsonl"
ALERTS = "alerts.jsonl"
BATCHES = "batches.jsonl"
COVERAGE = "coverage.json"
EVENTS = "ase.swap.events.jsonl"
GROUND_TRUTH = "ground_truth.csv"


@dataclass(frozen=True)
class RunData:
    """Mọi thứ evaluator cần cho 1 lần chạy detector (1 profile, 1 preset, 1 split)."""

    profile: str
    split: str | None
    gt: dict[str, GroundTruthRow]  # mọi phiên của profile
    eval_ids: frozenset[str]  # phiên thuộc split được đánh giá
    detections: dict[str, Detection]
    incidents: list[IncidentRecord]
    alerts: list[P1Alert]
    batches: list[TeamBatch]
    run_report: RunReport
    n_distinct_event_ids: int
    parse_errors: dict[str, int] = field(default_factory=dict)
    lines_read: dict[str, int] = field(default_factory=dict)

    @property
    def preset(self) -> str:
        return self.run_report.coverage.preset


def _parse_gt_row(raw: dict[str, Any]) -> GroundTruthRow:
    clean: dict[str, Any] = {k: (v if v not in ("", None) else None) for k, v in raw.items()}
    clean["is_failure"] = str(clean["is_failure"]).lower() == "true"
    for col in ("scenario_start", "injected_at"):
        if clean.get(col) is not None:
            clean[col] = datetime.fromisoformat(str(clean[col]))
    return GroundTruthRow.model_validate(clean)


def load_ground_truth(path: Path) -> dict[str, GroundTruthRow]:
    df = pl.read_csv(path, infer_schema_length=0)
    return {r["session_id"]: _parse_gt_row(r) for r in df.iter_rows(named=True)}


def load_jsonl[M: BaseModel](path: Path, model: type[M]) -> tuple[list[M], int, int]:
    """Trả về (các dòng hợp lệ, số dòng lỗi, tổng số dòng khác rỗng)."""
    items: list[M] = []
    errors = 0
    total = 0
    if not path.exists():
        return items, errors, total
    with path.open("rb") as f:
        for line in f:
            if not line.strip():
                continue
            total += 1
            try:
                items.append(model.model_validate_json(line))
            except ValidationError:
                errors += 1
    return items, errors, total


def count_distinct_event_ids(path: Path) -> int:
    seen: set[str] = set()
    with path.open("rb") as f:
        for line in f:
            if line.strip():
                seen.add(str(orjson.loads(line)["event_id"]))
    return len(seen)


def load_run(
    profile_dir: Path,
    *,
    run_dir: Path | None = None,
    split: str | None = None,
) -> RunData:
    """Nạp ground truth + output detector.

    `profile_dir` chứa dữ liệu nguồn (events, ground truth); `run_dir` chứa output detector
    (mặc định cùng thư mục, hoặc `ablation/<preset>/`).
    """
    out = run_dir or profile_dir
    gt = load_ground_truth(profile_dir / GROUND_TRUTH)
    eval_ids = frozenset(sid for sid, g in gt.items() if split is None or g.split == split)

    dets, e_det, n_det = load_jsonl(out / DETECTIONS, Detection)
    incs, e_inc, n_inc = load_jsonl(out / INCIDENTS, IncidentRecord)
    alerts, e_alr, n_alr = load_jsonl(out / ALERTS, P1Alert)
    batches, e_bat, n_bat = load_jsonl(out / BATCHES, TeamBatch)
    report = RunReport.model_validate_json((out / COVERAGE).read_bytes())

    return RunData(
        profile=profile_dir.name,
        split=split,
        gt=gt,
        eval_ids=eval_ids,
        detections={d.session_id: d for d in dets},
        incidents=incs,
        alerts=alerts,
        batches=batches,
        run_report=report,
        n_distinct_event_ids=count_distinct_event_ids(profile_dir / EVENTS),
        parse_errors={"detections": e_det, "incidents": e_inc, "alerts": e_alr, "batches": e_bat},
        lines_read={"detections": n_det, "incidents": n_inc, "alerts": n_alr, "batches": n_bat},
    )
