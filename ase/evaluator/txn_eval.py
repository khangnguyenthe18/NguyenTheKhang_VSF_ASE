"""Evaluator cho agent quét danh sách giao dịch (rule-only): đo SF1–SF5, SF7, SF8 (CLAUDE.md §4).

`python -m ase.evaluator.txn_eval --profile dev [--split test]`

So kết quả agent (`agent/digests.jsonl`) với `txn_ground_truth.csv`. Chỉ evaluator đọc ground
truth; agent không bao giờ đọc. Định nghĩa từng chỉ tiêu nằm ở docstring của hàm tương ứng.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import orjson
import polars as pl
from pydantic import BaseModel

from ase.agent.__main__ import AGENT_DIR, DIGESTS, SUMMARY, RunSummary, load_digests
from ase.agent.models import Detection, ScanDigest
from ase.common.config import get_settings
from ase.common.models import TxnGroundTruthRow
from ase.evaluator.metrics import LatencyStats, latency_stats
from ase.evaluator.report import git_sha, ok, pct
from ase.knowledge.loader import ScanSpec, default_knowledge
from ase.notifier.scan_digest import render_scan_email
from ase.simulator.txn_generator import TXN_GROUND_TRUTH

BACKGROUND = "background"
THRESHOLDS = {"sf1": 1.0, "sf2": 0.95, "sf3": 0.90, "sf4": 0.85, "sf5_p95_s": 35 * 60, "sf7": 1.0}


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


class Recall(BaseModel):
    n: int
    n_detected: int
    recall: float | None


class InstanceRow(BaseModel):
    instance_id: str
    n_failures: int
    n_detected: int
    n_in_incident: int
    incident_ids: list[str]


class TxnEvalReport(BaseModel):
    profile: str
    split: str | None
    git_sha: str
    n_transactions: int
    n_scans: int
    # SF1
    n_seen: int
    sf1: float | None
    # SF2 / SF3
    n_gt_failures: int
    sf2: Recall
    sf2_by_kind: dict[str, Recall]
    sf2_by_scenario: dict[str, Recall]
    n_detected: int
    n_true_detected: int
    sf3: float | None
    n_trap: int
    n_trap_detected: int
    # SF4
    sf4_code_level: float | None
    n_instances: int
    n_instances_detected: int
    n_instances_one_incident: int
    sf4_one_incident: float | None
    n_incidents: int
    n_incidents_background_only: int
    instances: list[InstanceRow]
    # SF5
    sf5: LatencyStats
    sf5_by_kind: dict[str, LatencyStats]
    # SF7 / SF8
    n_notifications: int
    sf7: float | None
    max_notifications_per_scan: int
    n_scans_notified: int


def load_ground_truth(path: Path) -> dict[str, TxnGroundTruthRow]:
    rows = pl.read_csv(path, infer_schema_length=None, try_parse_dates=False).to_dicts()
    out: dict[str, TxnGroundTruthRow] = {}
    for raw in rows:
        clean = {k: (None if v == "" else v) for k, v in raw.items()}
        row = TxnGroundTruthRow.model_validate(clean)
        out[row.emsp_txn_id] = row
    return out


def _render_ok(digest: ScanDigest, spec: ScanSpec) -> bool:
    """SF7: thông báo dựng được, có đủ mã giao dịch và giờ địa phương kèm GMT của mọi dòng."""
    try:
        mail = render_scan_email(digest, spec=spec)
    except Exception:
        return False
    shown = digest.detections[: spec.max_rows_mail]
    in_body = all(
        d.emsp_txn_id in mail.text_body and d.emsp_txn_id in mail.html_body for d in shown
    )
    in_csv = all(d.emsp_txn_id in mail.csv_text for d in digest.detections)
    return (
        in_body and in_csv and "(GMT" in mail.subject and mail.text_body.count("(GMT") > len(shown)
    )


def evaluate(
    profile: str,
    digests: list[ScanDigest],
    summary: RunSummary,
    truth: dict[str, TxnGroundTruthRow],
    *,
    split: str | None = None,
    spec: ScanSpec | None = None,
) -> TxnEvalReport:
    """Mọi chỉ tiêu chỉ tính trên giao dịch thuộc `split` (nếu có), trừ SF1 và SF7/SF8.

    - SF2 recall = giao dịch lỗi/kẹt thật được đưa vào một thông báo.
    - SF3 precision = giao dịch trong thông báo là lỗi/kẹt thật; bẫy = `slow_but_completed`.
    - SF4: (a) mã + mức báo ra khớp ground truth; (b) mỗi lần lặp kịch bản nằm trong ĐÚNG 1 sự cố.
    - SF5 = mốc quét phát hiện − lúc lỗi (FAILED: lúc báo Thất bại; STUCK: lúc vượt ngưỡng kẹt).
    - SF8 = số thông báo mỗi lần quét (mỗi lần quét có lỗi sinh đúng 1 digest → 1 mail + 1 card).
    """
    spec = spec or default_knowledge().detector.scan
    in_split = {i for i, g in truth.items() if split is None or g.split == split}
    stuck_after = timedelta(minutes=spec.stuck_after_minutes)

    detected: dict[str, Detection] = {}
    for g in digests:
        for d in g.detections:
            if d.emsp_txn_id in in_split:
                detected[d.emsp_txn_id] = d

    failures = {i for i in in_split if truth[i].is_failure}
    true_det = failures & set(detected)

    def recall(ids: set[str]) -> Recall:
        hit = len(ids & set(detected))
        return Recall(n=len(ids), n_detected=hit, recall=_ratio(hit, len(ids)))

    by_kind: dict[str, set[str]] = defaultdict(set)
    by_scenario: dict[str, set[str]] = defaultdict(set)
    for i in failures:
        by_kind[truth[i].kind or "?"].add(i)
        by_scenario[truth[i].scenario or BACKGROUND].add(i)

    traps = {i for i in in_split if truth[i].scenario and not truth[i].is_failure}

    code_ok = sum(
        1
        for i in true_det
        if detected[i].error_code == truth[i].error_code and detected[i].level == truth[i].level
    )

    by_instance: dict[str, set[str]] = defaultdict(set)
    for i in failures:
        inst = truth[i].scenario_instance_id
        if inst:
            by_instance[inst].add(i)
    instances: list[InstanceRow] = []
    for inst, ids in sorted(by_instance.items()):
        hits = [detected[i] for i in ids if i in detected]
        inc = sorted({d.incident_id for d in hits if d.incident_id})
        instances.append(
            InstanceRow(
                instance_id=inst,
                n_failures=len(ids),
                n_detected=len(hits),
                n_in_incident=sum(d.incident_id is not None for d in hits),
                incident_ids=inc,
            )
        )
    incident_txns: dict[str, set[str]] = defaultdict(set)
    for d in detected.values():
        if d.incident_id:
            incident_txns[d.incident_id].add(d.emsp_txn_id)
    background_only = sum(
        1
        for ids in incident_txns.values()
        if not any(truth[i].scenario_instance_id and truth[i].is_failure for i in ids)
    )

    lat: dict[str, list[float]] = defaultdict(list)
    for i in true_det:
        g_row, d = truth[i], detected[i]
        assert g_row.failure_at is not None
        event_at: datetime = g_row.failure_at + (
            stuck_after if g_row.kind == "STUCK" else timedelta()
        )
        lat[g_row.kind or "?"].append((d.detected_at - event_at).total_seconds())

    notified = [g for g in digests if g.detections]
    per_scan = Counter(g.scan_id for g in notified)
    return TxnEvalReport(
        profile=profile,
        split=split,
        git_sha=git_sha(),
        n_transactions=summary.n_transactions,
        n_scans=summary.n_scans,
        n_seen=summary.n_seen,
        sf1=_ratio(summary.n_seen, summary.n_transactions),
        n_gt_failures=len(failures),
        sf2=recall(failures),
        sf2_by_kind={k: recall(v) for k, v in sorted(by_kind.items())},
        sf2_by_scenario={k: recall(v) for k, v in sorted(by_scenario.items())},
        n_detected=len(detected),
        n_true_detected=len(true_det),
        sf3=_ratio(len(true_det), len(detected)),
        n_trap=len(traps),
        n_trap_detected=len(traps & set(detected)),
        sf4_code_level=_ratio(code_ok, len(true_det)),
        n_instances=len(instances),
        n_instances_detected=sum(bool(r.incident_ids) for r in instances),
        n_instances_one_incident=sum(len(r.incident_ids) == 1 for r in instances),
        sf4_one_incident=_ratio(sum(len(r.incident_ids) == 1 for r in instances), len(instances)),
        n_incidents=len(incident_txns),
        n_incidents_background_only=background_only,
        instances=instances,
        sf5=latency_stats([x for v in lat.values() for x in v]),
        sf5_by_kind={k: latency_stats(v) for k, v in sorted(lat.items())},
        n_notifications=len(notified),
        sf7=_ratio(sum(_render_ok(g, spec) for g in notified), len(notified)),
        max_notifications_per_scan=max(per_scan.values(), default=0),
        n_scans_notified=len(per_scan),
    )


def _mins(x: float | None) -> str:
    return "—" if x is None else f"{x / 60:.1f} phút".replace(".", ",")


def render_markdown(r: TxnEvalReport) -> str:
    t = THRESHOLDS
    p95 = r.sf5.p95
    lines = [
        f"# Đánh giá agent quét danh sách giao dịch (rule-only) — {r.profile}",
        "",
        f"- Tập đánh giá: `{r.split or 'toàn bộ'}` · {r.n_transactions} giao dịch · "
        f"{r.n_scans} lần quét · commit `{r.git_sha[:8]}` · không dùng LLM",
        "",
        "| # | Chỉ tiêu | Kết quả | Ngưỡng | Đạt |",
        "|---|---|---|---|---|",
        f"| SF1 | Giao dịch được đọc | {pct(r.sf1)} ({r.n_seen}/{r.n_transactions}) | 100% | "
        f"{ok(r.sf1 is not None and r.sf1 >= t['sf1'])} |",
        f"| SF2 | Recall lỗi + kẹt | {pct(r.sf2.recall)} ({r.sf2.n_detected}/{r.sf2.n}) | ≥ 95% | "
        f"{ok(r.sf2.recall is not None and r.sf2.recall >= t['sf2'])} |",
        f"| SF3 | Precision | {pct(r.sf3)} ({r.n_true_detected}/{r.n_detected}); bẫy bị báo "
        f"{r.n_trap_detected}/{r.n_trap} | ≥ 90% | {ok(r.sf3 is not None and r.sf3 >= t['sf3'])} |",
        f"| SF4a | Đúng mã + mức theo bảng chuẩn | {pct(r.sf4_code_level)} | ≥ 85% | "
        f"{ok(r.sf4_code_level is not None and r.sf4_code_level >= t['sf4'])} |",
        f"| SF4b | Lần lặp kịch bản ↔ đúng 1 sự cố | {pct(r.sf4_one_incident)} "
        f"({r.n_instances_one_incident}/{r.n_instances}); có sự cố: {r.n_instances_detected} | "
        f"≥ 85% | {ok(r.sf4_one_incident is not None and r.sf4_one_incident >= t['sf4'])} |",
        f"| SF5 | Lỗi → thông báo, p95 | {_mins(p95)} (p50 {_mins(r.sf5.p50)}, max "
        f"{_mins(r.sf5.max)}) | ≤ 35 phút | {ok(p95 is not None and p95 <= t['sf5_p95_s'])} |",
        f"| SF7 | Thông báo hợp lệ (mã GD + giờ địa phương) | {pct(r.sf7)} "
        f"({r.n_notifications} thông báo) | 100% | {ok(r.sf7 is not None and r.sf7 >= t['sf7'])} |",
        f"| SF8 | Thông báo mỗi lần quét | tối đa {r.max_notifications_per_scan} "
        f"({r.n_scans_notified}/{r.n_scans} lần quét có thông báo) | ≤ 1 | "
        f"{ok(r.max_notifications_per_scan <= 1)} |",
        "",
        f"Sự cố: {r.n_incidents}, trong đó {r.n_incidents_background_only} sự cố chỉ gồm lỗi nền "
        "(không thuộc kịch bản nào).",
        "",
        "## SF2 theo kịch bản",
        "",
        "| Kịch bản | Lỗi thật | Phát hiện | Recall |",
        "|---|---|---|---|",
        *[
            f"| {name} | {x.n} | {x.n_detected} | {pct(x.recall)} |"
            for name, x in r.sf2_by_scenario.items()
        ],
        "",
        "## SF4b theo từng lần lặp kịch bản",
        "",
        "| Lần lặp | Lỗi thật | Phát hiện | Trong sự cố | Số sự cố |",
        "|---|---|---|---|---|",
        *[
            f"| {x.instance_id} | {x.n_failures} | {x.n_detected} | {x.n_in_incident} | "
            f"{len(x.incident_ids)} |"
            for x in r.instances
        ],
        "",
        "## SF5 theo loại",
        "",
        "| Loại | Số | p50 | p95 | max |",
        "|---|---|---|---|---|",
        *[
            f"| {k} | {v.n} | {_mins(v.p50)} | {_mins(v.p95)} | {_mins(v.max)} |"
            for k, v in r.sf5_by_kind.items()
        ],
        "",
    ]
    return "\n".join(lines)


async def _load(
    profile_dir: Path,
) -> tuple[list[ScanDigest], RunSummary, dict[str, TxnGroundTruthRow]]:
    def read() -> tuple[list[ScanDigest], RunSummary, dict[str, TxnGroundTruthRow]]:
        agent_dir = profile_dir / AGENT_DIR
        summary = RunSummary.model_validate_json((agent_dir / SUMMARY).read_bytes())
        truth = load_ground_truth(profile_dir / TXN_GROUND_TRUTH)
        return load_digests(agent_dir / DIGESTS), summary, truth

    return await asyncio.to_thread(read)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m ase.evaluator.txn_eval", description=__doc__)
    p.add_argument("--profile", default="dev")
    p.add_argument("--split", default=None, help="chỉ đánh giá giao dịch thuộc split này (vd test)")
    args = p.parse_args(argv)
    profile_dir = get_settings().profile_dir(args.profile)
    digests, summary, truth = asyncio.run(_load(profile_dir))
    report = evaluate(args.profile, digests, summary, truth, split=args.split)
    out = profile_dir / AGENT_DIR / "eval"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8")
    (out / "report.json").write_bytes(
        orjson.dumps(report.model_dump(mode="json"), option=orjson.OPT_INDENT_2) + b"\n"
    )
    print(render_markdown(report))
    print(f"báo cáo: {out / 'report.md'}")


if __name__ == "__main__":
    main()
