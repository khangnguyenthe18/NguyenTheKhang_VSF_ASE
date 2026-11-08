"""Tính SF1–SF5, SF7, SF8 cho cấu hình rule-only (EVALUATOR_PLAN §4).

Mỗi SF là 1 hàm thuần nhận `RunData` (đã nạp, đã lọc split) và trả model kết quả. Đơn vị là
**phiên**, trừ SF7/SF8 tính theo alert/tin gom. SF6, SF9 cần LLM nên không có ở đây.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sklearn.metrics import (  # type: ignore[import-untyped]
    classification_report,
    confusion_matrix,
    f1_score,
)

from ase.common.models import REAL_CATEGORIES, Category, Incident, Severity, TeamBatch
from ase.common.time import format_local, format_offset_for, to_local
from ase.detector.correlator import IncidentRecord
from ase.evaluator.loader import RunData
from ase.knowledge.loader import Knowledge, default_knowledge
from ase.notifier.batch import render_batch_email
from ase.notifier.render import render_email
from ase.notifier.router import Alert

SILENT_ERROR_CODES: frozenset[str] = frozenset({"STEP_TIMEOUT", "PAID_NOT_DELIVERED"})
SF_THRESHOLDS: dict[str, str] = {
    "SF1": "100%",
    "SF2": "≥ 95%",
    "SF3": "≥ 90%",
    "SF4": "≥ 85%",
    "SF5": "P1 p95 < 60s; P2/P3/USER ≤ 1 chu kỳ gom",
    "SF7": "100%",
    "SF8": "≤ 1,2",
}
P1_LATENCY_LIMIT_S = 60.0
LABELS_PRED: list[str] = [c.value for c in REAL_CATEGORIES] + [Category.UNKNOWN.value]
LABELS_TRUE: list[str] = [c.value for c in REAL_CATEGORIES]


def _ratio(num: int, den: int) -> float | None:
    return num / den if den > 0 else None


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank: phần tử thứ ceil(p/100 * n) của danh sách đã sắp xếp."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return ordered[rank - 1]


class LatencyStats(BaseModel):
    n: int = 0
    p50: float | None = None
    p95: float | None = None
    max: float | None = None


def latency_stats(values: list[float]) -> LatencyStats:
    return LatencyStats(
        n=len(values),
        p50=percentile(values, 50),
        p95=percentile(values, 95),
        max=max(values) if values else None,
    )


# --- incident thật ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RealIncident:
    """Incident thật: mỗi `scenario_instance_id` là 1; mỗi phiên lỗi nền là 1 (§4.1)."""

    key: str
    session_ids: tuple[str, ...]
    severity: Severity
    scenario: str | None
    t0: datetime | None  # mốc lỗi sớm nhất của cả incident (xem `failure_time`)
    p1_t0: datetime | None  # mốc lỗi sớm nhất trong các phiên có severity P1 của incident


_SEV_RANK: dict[Severity, int] = {Severity.P1: 0, Severity.P2: 1, Severity.P3: 2}


def failure_time(data: RunData, session_id: str) -> tuple[datetime | None, bool]:
    """Mốc lỗi xảy ra của 1 phiên → (thời điểm, có phải mốc thay thế).

    Ưu tiên `injected_at` của ground truth. Phiên lỗi nền không có `injected_at`; với lỗi tường
    minh thì `occurred_at` của phát hiện chính là thời điểm lỗi (đã kiểm: trùng `injected_at` ở
    mọi phiên scenario có lỗi tường minh, chênh 0,0s). Các trường hợp khác không đo được.
    """
    g = data.gt[session_id]
    if g.injected_at is not None:
        return g.injected_at, False
    det = data.detections.get(session_id)
    if det is not None and det.kind == "EXPLICIT_FAILURE":
        return det.occurred_at, True
    return None, False


def build_real_incidents(data: RunData) -> list[RealIncident]:
    groups: dict[str, list[str]] = defaultdict(list)
    for sid in sorted(data.eval_ids):
        g = data.gt[sid]
        if g.is_failure:
            groups[g.scenario_instance_id or sid].append(sid)
    out: list[RealIncident] = []
    for key in sorted(groups):
        sids = groups[key]
        sevs: list[Severity] = []
        for s in sids:
            sv = data.gt[s].severity
            if sv is not None:
                sevs.append(sv)
        severity = min(sevs, key=lambda v: _SEV_RANK[v]) if sevs else Severity.P3
        times = [t for s in sids if (t := failure_time(data, s)[0]) is not None]
        p1_times = [
            t
            for s in sids
            if data.gt[s].severity is Severity.P1 and (t := failure_time(data, s)[0]) is not None
        ]
        out.append(
            RealIncident(
                key=key,
                session_ids=tuple(sids),
                severity=severity,
                scenario=data.gt[sids[0]].scenario,
                t0=min(times) if times else None,
                p1_t0=min(p1_times) if p1_times else None,
            )
        )
    return out


def final_incidents(data: RunData) -> dict[str, IncidentRecord]:
    """Revision mới nhất của mỗi incident (file đã sort theo (incident_id, revision))."""
    latest: dict[str, IncidentRecord] = {}
    for rec in data.incidents:
        cur = latest.get(rec.incident_id)
        if cur is None or rec.revision >= cur.revision:
            latest[rec.incident_id] = rec
    return latest


# --- SF1 --------------------------------------------------------------------------------------


class SF1Result(BaseModel):
    events_processed: int
    events_distinct: int
    events_pct: float | None
    sessions_seen: int
    sessions_gt: int
    sessions_pct: float | None
    passed: bool


def compute_sf1(data: RunData) -> SF1Result:
    cov = data.run_report.coverage
    ev_pct = _ratio(cov.total_events_processed, data.n_distinct_event_ids)
    ss_pct = _ratio(cov.total_sessions_seen, len(data.gt))
    return SF1Result(
        events_processed=cov.total_events_processed,
        events_distinct=data.n_distinct_event_ids,
        events_pct=ev_pct,
        sessions_seen=cov.total_sessions_seen,
        sessions_gt=len(data.gt),
        sessions_pct=ss_pct,
        passed=ev_pct == 1.0 and ss_pct == 1.0,
    )


# --- SF2 / SF3 --------------------------------------------------------------------------------


class RecallStat(BaseModel):
    n: int
    detected: int
    recall: float | None


class SF2Result(BaseModel):
    n_failures: int
    n_detected: int
    recall: float | None
    by_category: dict[str, RecallStat]
    silent: RecallStat
    passed: bool


def _gt_failures(data: RunData) -> list[str]:
    return [s for s in sorted(data.eval_ids) if data.gt[s].is_failure]


def _detected_in_eval(data: RunData) -> list[str]:
    return [s for s in sorted(data.detections) if s in data.eval_ids]


def compute_sf2(data: RunData) -> SF2Result:
    fails = _gt_failures(data)
    det = set(data.detections)
    hit = [s for s in fails if s in det]
    by_cat: dict[str, RecallStat] = {}
    for cat in REAL_CATEGORIES:
        sub = [s for s in fails if data.gt[s].category is cat]
        d = sum(1 for s in sub if s in det)
        by_cat[cat.value] = RecallStat(n=len(sub), detected=d, recall=_ratio(d, len(sub)))
    silent = [s for s in fails if data.gt[s].error_code in SILENT_ERROR_CODES]
    sd = sum(1 for s in silent if s in det)
    recall = _ratio(len(hit), len(fails))
    return SF2Result(
        n_failures=len(fails),
        n_detected=len(hit),
        recall=recall,
        by_category=by_cat,
        silent=RecallStat(n=len(silent), detected=sd, recall=_ratio(sd, len(silent))),
        passed=recall is not None and recall >= 0.95,
    )


class SF3Result(BaseModel):
    n_detected: int
    n_true: int
    precision: float | None
    n_trap_detected: int
    trap_sessions: list[str]
    false_alarms: list[str]
    passed: bool


def compute_sf3(data: RunData) -> SF3Result:
    detected = _detected_in_eval(data)
    true_hits = [s for s in detected if data.gt[s].is_failure]
    false_alarms = [s for s in detected if not data.gt[s].is_failure]
    traps = [s for s in false_alarms if data.gt[s].scenario == "pending_then_ok"]
    precision = _ratio(len(true_hits), len(detected))
    return SF3Result(
        n_detected=len(detected),
        n_true=len(true_hits),
        precision=precision,
        n_trap_detected=len(traps),
        trap_sessions=traps,
        false_alarms=false_alarms,
        passed=precision is not None and precision >= 0.90,
    )


# --- SF4 --------------------------------------------------------------------------------------


class ClassStats(BaseModel):
    precision: float
    recall: float
    f1: float
    support: int


class SF4Result(BaseModel):
    n_evaluated: int
    accuracy: float | None
    macro_f1: float | None
    per_class: dict[str, ClassStats]
    per_category_accuracy: dict[
        str, float | None
    ]  # recall từng nhóm thật (trên phiên đã phát hiện)
    confusion_labels: list[str]  # nhãn dự đoán (cột); hàng = 6 nhóm thật
    confusion: list[list[int]]
    misclassified: list[str]
    passed: bool


def compute_sf4(data: RunData) -> SF4Result:
    sids = [s for s in _detected_in_eval(data) if data.gt[s].is_failure]
    y_true: list[str] = []
    y_pred: list[str] = []
    for s in sids:
        cat = data.gt[s].category
        assert cat is not None
        y_true.append(cat.value)
        y_pred.append(data.detections[s].category.value)
    n = len(sids)
    if n == 0:
        return SF4Result(
            n_evaluated=0,
            accuracy=None,
            macro_f1=None,
            per_class={},
            per_category_accuracy={c: None for c in LABELS_TRUE},
            confusion_labels=LABELS_PRED,
            confusion=[[0] * len(LABELS_PRED) for _ in LABELS_TRUE],
            misclassified=[],
            passed=False,
        )
    correct = sum(1 for a, b in zip(y_true, y_pred, strict=True) if a == b)
    present = [c for c in LABELS_TRUE if c in set(y_true)]
    macro = float(f1_score(y_true, y_pred, labels=present, average="macro", zero_division=0))
    rep = classification_report(
        y_true, y_pred, labels=LABELS_PRED, output_dict=True, zero_division=0
    )
    per_class = {
        lab: ClassStats(
            precision=float(rep[lab]["precision"]),
            recall=float(rep[lab]["recall"]),
            f1=float(rep[lab]["f1-score"]),
            support=int(rep[lab]["support"]),
        )
        for lab in LABELS_PRED
    }
    cm = confusion_matrix(y_true, y_pred, labels=LABELS_PRED)
    cm_rows = [[int(x) for x in cm[LABELS_PRED.index(lab)]] for lab in LABELS_TRUE]
    accuracy = correct / n
    return SF4Result(
        n_evaluated=n,
        accuracy=accuracy,
        macro_f1=macro,
        per_class=per_class,
        per_category_accuracy={
            lab: (per_class[lab].recall if per_class[lab].support > 0 else None)
            for lab in LABELS_TRUE
        },
        confusion_labels=LABELS_PRED,
        confusion=cm_rows,
        misclassified=[s for s, a, b in zip(sids, y_true, y_pred, strict=True) if a != b],
        passed=accuracy >= 0.85,
    )


# --- SF5 --------------------------------------------------------------------------------------


class P1Latency(BaseModel):
    n_real_p1: int
    n_alerted: int
    n_unmeasurable: int
    overall: LatencyStats  # tính từ phiên P1 đầu tiên của incident (số chính)
    from_incident_start: LatencyStats  # tính từ phiên sớm nhất của cả incident (bất kể mức)
    by_scenario: dict[str, LatencyStats]
    passed: bool


class BatchLatency(BaseModel):
    interval_seconds: int
    n_rows: int
    n_within_interval: int
    pct_within_interval: float | None
    latency: LatencyStats
    by_team: dict[str, LatencyStats]
    n_undelivered: int  # phiên được phát hiện nhưng không nằm trong tin gom nào, không có alert P1
    n_unmeasurable: int  # phiên không có mốc lỗi (không suy ra được)
    n_proxy_time: int  # số phiên dùng `occurred_at` của phát hiện thay `injected_at`
    passed: bool


class SF5Result(BaseModel):
    p1: P1Latency
    batch: BatchLatency
    passed: bool


def _session_to_incident(data: RunData) -> dict[str, str]:
    return {s: d.incident_id for s, d in data.detections.items() if d.incident_id is not None}


def compute_sf5(data: RunData, knowledge: Knowledge | None = None) -> SF5Result:
    k = knowledge or default_knowledge()
    interval_s = k.detector.notify.batch_interval_minutes * 60
    sess_inc = _session_to_incident(data)

    first_alert: dict[str, datetime] = {}
    for a in data.alerts:
        cur = first_alert.get(a.incident_id)
        if cur is None or a.sent_at < cur:
            first_alert[a.incident_id] = a.sent_at

    real = build_real_incidents(data)
    real_p1 = [r for r in real if r.severity is Severity.P1]
    lat_all: list[float] = []
    lat_literal: list[float] = []
    lat_by_scn: dict[str, list[float]] = defaultdict(list)
    n_alerted = 0
    n_unmeasurable = 0
    for r in real_p1:
        times: list[datetime] = []
        for s in r.session_ids:
            iid = sess_inc.get(s)
            if iid is not None and iid in first_alert:
                times.append(first_alert[iid])
        if not times:
            continue
        n_alerted += 1
        if r.t0 is None or r.p1_t0 is None:
            n_unmeasurable += 1
            continue
        first = min(times)
        lat = (first - r.p1_t0).total_seconds()
        lat_all.append(lat)
        lat_literal.append((first - r.t0).total_seconds())
        lat_by_scn[r.scenario or "(nền)"].append(lat)
    p1_stats = latency_stats(lat_all)
    p1 = P1Latency(
        n_real_p1=len(real_p1),
        n_alerted=n_alerted,
        n_unmeasurable=n_unmeasurable,
        overall=p1_stats,
        from_incident_start=latency_stats(lat_literal),
        by_scenario={s: latency_stats(v) for s, v in sorted(lat_by_scn.items())},
        passed=(
            len(real_p1) > 0
            and n_alerted == len(real_p1)
            and p1_stats.p95 is not None
            and p1_stats.p95 < P1_LATENCY_LIMIT_S
        ),
    )

    batch_lat: list[float] = []
    by_team: dict[str, list[float]] = defaultdict(list)
    in_batch: set[str] = set()
    unmeasurable = 0
    proxy = 0
    for b in data.batches:
        for row in b.rows:
            if row.session_id not in data.eval_ids:
                continue
            in_batch.add(row.session_id)
            t0, is_proxy = failure_time(data, row.session_id)
            if t0 is None:
                unmeasurable += 1
                continue
            proxy += int(is_proxy)
            lat = (b.window_end - t0).total_seconds()
            batch_lat.append(lat)
            by_team[b.team].append(lat)
    covered_by_p1 = {s for s, i in sess_inc.items() if i in first_alert}
    undelivered = [
        s for s in _detected_in_eval(data) if s not in in_batch and s not in covered_by_p1
    ]
    within = sum(1 for x in batch_lat if x <= interval_s)
    batch = BatchLatency(
        interval_seconds=interval_s,
        n_rows=len(batch_lat) + unmeasurable,
        n_within_interval=within,
        pct_within_interval=_ratio(within, len(batch_lat)),
        latency=latency_stats(batch_lat),
        by_team={t: latency_stats(v) for t, v in sorted(by_team.items())},
        n_undelivered=len(undelivered),
        n_unmeasurable=unmeasurable,
        n_proxy_time=proxy,
        passed=bool(batch_lat) and within == len(batch_lat) and not undelivered,
    )
    return SF5Result(p1=p1, batch=batch, passed=p1.passed and batch.passed)


# --- SF7 --------------------------------------------------------------------------------------


class SF7Result(BaseModel):
    n_alerts: int
    n_batches: int
    n_rows: int
    team_ok_alerts: int
    team_ok_rows: int
    team_pct: float | None  # (alert + dòng) đúng team / (alert + dòng)
    team_ok_batches: int  # tin gom mà mọi dòng đều đúng team
    schema_ok: int
    schema_total: int
    schema_pct: float | None
    render_ok: int  # alert + tin gom đủ mã GD + giờ địa phương
    render_total: int
    render_pct: float | None
    wrong_team_examples: list[str]
    passed: bool


def _expected_team(k: Knowledge, category: Category | None, country: str | None) -> str | None:
    if category is None:
        return None
    return k.routing.route_for(category, country).team


def _majority_category(data: RunData, session_ids: list[str]) -> Category | None:
    cats = [c for s in session_ids if s in data.gt and (c := data.gt[s].category) is not None]
    if not cats:
        return None
    return Counter(cats).most_common(1)[0][0]


def _incident_view(rec: IncidentRecord) -> Incident:
    return Incident.model_validate(rec.model_dump(include=set(Incident.model_fields)))


def _alert_render_ok(rec: IncidentRecord, alert: Alert, k: Knowledge) -> bool:
    text = render_email(alert, knowledge=k).text_body
    local = to_local(rec.first_seen_at, rec.timezone)
    offset = format_offset_for(rec.first_seen_at, rec.timezone)
    want_time = f"{local:%H:%M:%S} ngày {local:%d/%m/%Y} ({offset}"
    return rec.session_ids[0] in text and want_time in text


def _batch_render_ok(batch: TeamBatch, k: Knowledge) -> bool:
    rendered = render_batch_email(batch, knowledge=k)
    csv_lines = {line.split(",", 1)[0]: line for line in rendered.csv_text.splitlines()[1:]}
    for r in batch.rows:
        line = csv_lines.get(r.session_id)
        if line is None or format_local(r.occurred_at, r.timezone) not in line:
            return False
    shown = batch.rows[: k.detector.notify.max_rows_mail]
    return all(r.session_id in rendered.text_body for r in shown)


def compute_sf7(data: RunData, knowledge: Knowledge | None = None) -> SF7Result:
    k = knowledge or default_knowledge()
    final = final_incidents(data)
    by_rev = {(r.incident_id, r.revision): r for r in data.incidents}
    examples: list[str] = []

    n_alerts = team_ok_alerts = schema_ok = schema_total = 0
    render_ok = render_total = 0
    for a in data.alerts:
        rec = final.get(a.incident_id)
        if rec is None or not any(s in data.eval_ids for s in rec.session_ids):
            continue
        n_alerts += 1
        exp = _expected_team(
            k,
            _majority_category(data, rec.session_ids),
            None if rec.country == "GLOBAL" else rec.country,
        )
        if exp is not None and exp == a.team:
            team_ok_alerts += 1
        elif len(examples) < 10:
            examples.append(f"alert {a.incident_id}: gửi {a.team}, đúng phải là {exp}")
        schema_total += 1
        render_total += 1
        at_rev = by_rev.get((a.incident_id, a.revision))
        if at_rev is None:
            continue
        try:
            inc = _incident_view(at_rev)
        except ValueError:
            continue
        schema_ok += 1
        alert = Alert(incident=inc, team=a.team, channel=a.channel)
        render_ok += int(_alert_render_ok(at_rev, alert, k))

    n_batches = n_rows = team_ok_rows = team_ok_batches = 0
    for b in data.batches:
        rows = [r for r in b.rows if r.session_id in data.eval_ids]
        if not rows:
            continue
        n_batches += 1
        all_ok = True
        for r in rows:
            n_rows += 1
            g = data.gt[r.session_id]
            exp = _expected_team(k, g.category if g.is_failure else None, r.country)
            if exp is not None and exp == b.team:
                team_ok_rows += 1
            else:
                all_ok = False
                if len(examples) < 10:
                    examples.append(f"{r.session_id}: tin gom {b.team}, đúng phải là {exp}")
        team_ok_batches += int(all_ok)
        schema_total += 1
        render_total += 1
        schema_ok += 1  # đã parse được bằng `TeamBatch`; lỗi parse tính ở dưới
        render_ok += int(_batch_render_ok(b, k))

    bad_lines = sum(data.parse_errors.get(f, 0) for f in ("alerts", "batches", "incidents"))
    schema_total += bad_lines
    items = n_alerts + n_rows
    team_pct = _ratio(team_ok_alerts + team_ok_rows, items)
    schema_pct = _ratio(schema_ok, schema_total)
    render_pct = _ratio(render_ok, render_total)
    return SF7Result(
        n_alerts=n_alerts,
        n_batches=n_batches,
        n_rows=n_rows,
        team_ok_alerts=team_ok_alerts,
        team_ok_rows=team_ok_rows,
        team_pct=team_pct,
        team_ok_batches=team_ok_batches,
        schema_ok=schema_ok,
        schema_total=schema_total,
        schema_pct=schema_pct,
        render_ok=render_ok,
        render_total=render_total,
        render_pct=render_pct,
        wrong_team_examples=examples,
        passed=team_pct == 1.0 and schema_pct == 1.0 and render_pct == 1.0,
    )


# --- SF8 --------------------------------------------------------------------------------------


class ScenarioAlerts(BaseModel):
    n_real: int  # incident P1 thật của scenario
    n_alerts: int  # số alert P1 đã gửi cho các incident đó


class SF8Result(BaseModel):
    n_alerts: int
    n_real_p1: int
    ratio: float | None
    # Số phụ (định nghĩa cần mentor chốt): incident thật = (scenario, nhóm lỗi) của các phiên P1,
    # vì mỗi nhóm lỗi đi tới 1 team khác nhau (vd thermal_overload → Battery Ops + Field Ops).
    n_real_p1_by_category: int
    ratio_by_category: float | None
    by_scenario: dict[str, ScenarioAlerts]
    batches_per_team_per_day: dict[str, float]
    n_days: int
    passed: bool


def compute_sf8(data: RunData) -> SF8Result:
    final = final_incidents(data)
    n_alerts = sum(
        1
        for a in data.alerts
        if (rec := final.get(a.incident_id)) is not None
        and any(s in data.eval_ids for s in rec.session_ids)
    )
    real_p1 = [r for r in build_real_incidents(data) if r.severity is Severity.P1]
    n_real_p1 = len(real_p1)
    ratio = _ratio(n_alerts, n_real_p1)
    by_cat_keys = {
        (r.key, cat)
        for r in real_p1
        for s in r.session_ids
        if data.gt[s].severity is Severity.P1 and (cat := data.gt[s].category) is not None
    }

    # Mỗi scenario: số incident P1 thật và số alert đã gửi cho chúng (để thấy chỗ alert bị tách)
    sess_to_scn = {s: (r.scenario or "(nền)") for r in real_p1 for s in r.session_ids}
    alerts_of_scn: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for a in data.alerts:
        rec = final.get(a.incident_id)
        if rec is None:
            continue
        for s in rec.session_ids:
            if s in sess_to_scn:
                alerts_of_scn[sess_to_scn[s]].add((a.incident_id, a.revision))
    real_of_scn = Counter(r.scenario or "(nền)" for r in real_p1)
    by_scenario = {
        scn: ScenarioAlerts(n_real=n, n_alerts=len(alerts_of_scn.get(scn, set())))
        for scn, n in sorted(real_of_scn.items())
    }

    days: set[str] = set()
    per_team: Counter[str] = Counter()
    for b in data.batches:
        if any(r.session_id in data.eval_ids for r in b.rows):
            days.add(f"{b.window_start:%Y-%m-%d}")
            per_team[b.team] += 1
    n_days = len(days)
    return SF8Result(
        n_alerts=n_alerts,
        n_real_p1=n_real_p1,
        ratio=ratio,
        n_real_p1_by_category=len(by_cat_keys),
        ratio_by_category=_ratio(n_alerts, len(by_cat_keys)),
        by_scenario=by_scenario,
        batches_per_team_per_day={t: round(c / n_days, 2) for t, c in sorted(per_team.items())}
        if n_days
        else {},
        n_days=n_days,
        passed=ratio is not None and ratio <= 1.2,
    )


# --- tổng hợp ---------------------------------------------------------------------------------

ExampleKind = Literal["BỎ SÓT", "BÁO NHẦM", "SAI NHÓM"]


class ExampleRow(BaseModel):
    kind: ExampleKind
    session_id: str
    true_category: str | None
    pred_category: str | None
    rule_id: str | None
    scenario: str | None
    error_code: str | None


def pick_examples(
    data: RunData, sf3: SF3Result, sf4: SF4Result, limit: int = 20
) -> list[ExampleRow]:
    """Tối đa `limit` phiên báo sai / bỏ sót tiêu biểu, xen kẽ 3 loại để thấy đủ dạng lỗi."""
    missed = [s for s in _gt_failures(data) if s not in data.detections]
    queues: list[tuple[ExampleKind, list[str]]] = [
        ("BỎ SÓT", missed),
        ("BÁO NHẦM", sf3.false_alarms),
        ("SAI NHÓM", sf4.misclassified),
    ]
    out: list[ExampleRow] = []
    idx = 0
    while len(out) < limit and any(idx < len(q) for _, q in queues):
        for kind, q in queues:
            if idx < len(q) and len(out) < limit:
                sid = q[idx]
                g = data.gt[sid]
                d = data.detections.get(sid)
                out.append(
                    ExampleRow(
                        kind=kind,
                        session_id=sid,
                        true_category=g.category.value if g.category else None,
                        pred_category=d.category.value if d else None,
                        rule_id=d.rule_id if d else None,
                        scenario=g.scenario,
                        error_code=g.error_code,
                    )
                )
        idx += 1
    return out
