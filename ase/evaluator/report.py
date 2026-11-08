"""`EvalReport` và ghi `report.json` + `report.md` (EVALUATOR_PLAN §5)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel

from ase.common.models import REAL_CATEGORIES
from ase.evaluator.loader import RunData
from ase.evaluator.metrics import (
    LABELS_PRED,
    SF_THRESHOLDS,
    ExampleRow,
    LatencyStats,
    SF1Result,
    SF2Result,
    SF3Result,
    SF4Result,
    SF5Result,
    SF7Result,
    SF8Result,
    compute_sf1,
    compute_sf2,
    compute_sf3,
    compute_sf4,
    compute_sf5,
    compute_sf7,
    compute_sf8,
    pick_examples,
)
from ase.knowledge.loader import Knowledge, default_knowledge


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


class EvalReport(BaseModel):
    config: str = "rule-only"
    profile: str
    split: str | None
    preset: str
    git_sha: str
    n_sessions_total: int
    n_sessions_evaluated: int
    n_failures_evaluated: int
    sf1: SF1Result
    sf2: SF2Result
    sf3: SF3Result
    sf4: SF4Result
    sf5: SF5Result
    sf7: SF7Result
    sf8: SF8Result
    examples: list[ExampleRow]
    sf6_sf9: str = "chưa đo — Sprint 1 (cần LLM)"


def evaluate(data: RunData, knowledge: Knowledge | None = None) -> EvalReport:
    k = knowledge or default_knowledge()
    sf3 = compute_sf3(data)
    sf4 = compute_sf4(data)
    return EvalReport(
        profile=data.profile,
        split=data.split,
        preset=data.preset,
        git_sha=git_sha(),
        n_sessions_total=len(data.gt),
        n_sessions_evaluated=len(data.eval_ids),
        n_failures_evaluated=sum(1 for s in data.eval_ids if data.gt[s].is_failure),
        sf1=compute_sf1(data),
        sf2=compute_sf2(data),
        sf3=sf3,
        sf4=sf4,
        sf5=compute_sf5(data, k),
        sf7=compute_sf7(data, k),
        sf8=compute_sf8(data),
        examples=pick_examples(data, sf3, sf4),
    )


# --- Markdown ---------------------------------------------------------------------------------


def pct(x: float | None, digits: int = 1) -> str:
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def secs(x: float | None) -> str:
    return "—" if x is None else f"{x:.1f}s"


def ok(passed: bool) -> str:
    return "đạt" if passed else "**chưa đạt**"


def _lat(s: LatencyStats) -> str:
    return f"n={s.n}, p50={secs(s.p50)}, p95={secs(s.p95)}, max={secs(s.max)}"


def render_markdown(r: EvalReport) -> str:
    split = r.split or "toàn bộ"
    L: list[str] = [
        f"# Báo cáo đánh giá — {r.config} — profile `{r.profile}` (split: {split})",
        "",
        f"- Preset trường dữ liệu: `{r.preset}` · git `{r.git_sha}`",
        f"- Phiên đánh giá: {r.n_sessions_evaluated:,} / {r.n_sessions_total:,} "
        f"(lỗi thật: {r.n_failures_evaluated:,})",
        f"- SF6, SF9: {r.sf6_sf9}",
        "",
        "## 1. Bảng SF",
        "",
        "| SF | Chỉ số | Giá trị | Ngưỡng | Kết quả |",
        "|---|---|---|---|---|",
        f"| SF1 | event xử lý / event khác nhau | {pct(r.sf1.events_pct)} "
        f"({r.sf1.events_processed:,}/{r.sf1.events_distinct:,}) | {SF_THRESHOLDS['SF1']} | "
        f"{ok(r.sf1.passed)} |",
        f"| SF1 | phiên thấy / phiên ground truth | {pct(r.sf1.sessions_pct)} "
        f"({r.sf1.sessions_seen:,}/{r.sf1.sessions_gt:,}) | {SF_THRESHOLDS['SF1']} | "
        f"{ok(r.sf1.passed)} |",
        f"| SF2 | recall | {pct(r.sf2.recall)} ({r.sf2.n_detected}/{r.sf2.n_failures}) | "
        f"{SF_THRESHOLDS['SF2']} | {ok(r.sf2.passed)} |",
        f"| SF3 | precision | {pct(r.sf3.precision)} ({r.sf3.n_true}/{r.sf3.n_detected}) | "
        f"{SF_THRESHOLDS['SF3']} | {ok(r.sf3.passed)} |",
        f"| SF4 | accuracy | {pct(r.sf4.accuracy)} | {SF_THRESHOLDS['SF4']} | {ok(r.sf4.passed)} |",
        f"| SF4 | macro-F1 | {pct(r.sf4.macro_f1)} | — | — |",
        f"| SF5 | P1: p95 (lỗi → alert) | {secs(r.sf5.p1.overall.p95)} "
        f"({r.sf5.p1.n_alerted}/{r.sf5.p1.n_real_p1} incident P1 có alert) | < 60s | "
        f"{ok(r.sf5.p1.passed)} |",
        f"| SF5 | P2/P3/USER: trong 1 chu kỳ | {pct(r.sf5.batch.pct_within_interval)} "
        f"(max {secs(r.sf5.batch.latency.max)}) | ≤ {r.sf5.batch.interval_seconds}s | "
        f"{ok(r.sf5.batch.passed)} |",
        f"| SF7 | đúng team | {pct(r.sf7.team_pct)} | {SF_THRESHOLDS['SF7']} | "
        f"{ok(r.sf7.team_pct == 1.0)} |",
        f"| SF7 | hợp lệ schema | {pct(r.sf7.schema_pct)} | {SF_THRESHOLDS['SF7']} | "
        f"{ok(r.sf7.schema_pct == 1.0)} |",
        f"| SF7 | đủ mã GD + giờ địa phương | {pct(r.sf7.render_pct)} | {SF_THRESHOLDS['SF7']} | "
        f"{ok(r.sf7.render_pct == 1.0)} |",
        f"| SF8 | alert P1 / incident P1 thật | "
        f"{'—' if r.sf8.ratio is None else f'{r.sf8.ratio:.2f}'} "
        f"({r.sf8.n_alerts}/{r.sf8.n_real_p1}) | {SF_THRESHOLDS['SF8']} | {ok(r.sf8.passed)} |",
        "",
        "## 2. SF2 — recall theo nhóm và lỗi im lặng",
        "",
        "| Nhóm | Lỗi thật | Phát hiện | Recall |",
        "|---|---|---|---|",
    ]
    for cat, rs in r.sf2.by_category.items():
        L.append(f"| {cat} | {rs.n} | {rs.detected} | {pct(rs.recall)} |")
    s = r.sf2.silent
    L += [
        f"| **Lỗi im lặng** (`STEP_TIMEOUT`, `PAID_NOT_DELIVERED`) | {s.n} | {s.detected} | "
        f"{pct(s.recall)} |",
        "",
        "## 3. SF3 — phiên bẫy `pending_then_ok`",
        "",
        f"Bị báo nhầm: **{r.sf3.n_trap_detected}**"
        + (f" ({', '.join(r.sf3.trap_sessions[:10])})" if r.sf3.trap_sessions else "")
        + f". Tổng báo nhầm: {len(r.sf3.false_alarms)}.",
        "",
        "## 4. SF4 — phân loại nhóm (`UNKNOWN` tính là sai)",
        "",
        "| Nhóm | Precision | Recall | F1 | Số phiên |",
        "|---|---|---|---|---|",
    ]
    for lab in LABELS_PRED:
        c = r.sf4.per_class.get(lab)
        if c is not None:
            L.append(
                f"| {lab} | {pct(c.precision)} | {pct(c.recall)} | {pct(c.f1)} | {c.support} |"
            )
    L += [
        "",
        "Confusion matrix (hàng = nhóm thật, cột = nhóm dự đoán):",
        "",
        "| thật \\ dự đoán | " + " | ".join(r.sf4.confusion_labels) + " |",
        "|---|" + "---|" * len(r.sf4.confusion_labels),
    ]
    for cat, row in zip((c.value for c in REAL_CATEGORIES), r.sf4.confusion, strict=True):
        L.append(f"| {cat} | " + " | ".join(str(x) for x in row) + " |")

    p1 = r.sf5.p1
    b = r.sf5.batch
    L += [
        "",
        "## 5. SF5 — thời gian lỗi → thông báo",
        "",
        f"**P1** (incident P1 thật: {p1.n_real_p1}, có alert: {p1.n_alerted}): {_lat(p1.overall)}",
        "",
        "Mốc lỗi của incident P1 = `injected_at` của **phiên P1 đầu tiên** (alert P1 không thể đến "
        "trước khi lỗi P1 xảy ra; scenario `thermal_overload` mở đầu bằng các phiên P2 trước lúc "
        f"quá nhiệt). Tính từ phiên sớm nhất của cả incident: {_lat(p1.from_incident_start)}.",
        "",
        "| Scenario | Thống kê (giây) |",
        "|---|---|",
    ]
    for scn, ls in p1.by_scenario.items():
        L.append(f"| {scn} | {_lat(ls)} |")
    L += [
        "",
        f"**P2/P3/USER** (tin gom {b.interval_seconds // 60} phút): {_lat(b.latency)}; "
        f"{b.n_within_interval}/{b.latency.n} dòng trong 1 chu kỳ; "
        f"chưa tới tay team: {b.n_undelivered}; không đo được: {b.n_unmeasurable}.",
        "",
        "| Team | Thống kê (giây) |",
        "|---|---|",
    ]
    for team, tls in b.by_team.items():
        L.append(f"| {team} | {_lat(tls)} |")
    L += [
        "",
        f"> Mốc lỗi: dùng `injected_at` của ground truth. Phiên lỗi nền không có `injected_at` "
        f"({b.n_proxy_time} phiên) dùng `occurred_at` của phát hiện — với lỗi tường minh hai mốc "
        "này trùng nhau (đã kiểm trên các phiên scenario).",
        "",
        "## 6. SF7 và SF8",
        "",
        f"- Alert P1: {r.sf7.n_alerts}; tin gom: {r.sf7.n_batches} ({r.sf7.n_rows} dòng).",
        f"- Đúng team: alert {r.sf7.team_ok_alerts}/{r.sf7.n_alerts}, dòng "
        f"{r.sf7.team_ok_rows}/{r.sf7.n_rows}, tin gom đúng hoàn toàn "
        f"{r.sf7.team_ok_batches}/{r.sf7.n_batches}.",
        f"- Hợp lệ schema: {r.sf7.schema_ok}/{r.sf7.schema_total}; "
        f"đủ mã GD + giờ địa phương: {r.sf7.render_ok}/{r.sf7.render_total}.",
        "- Alert P1 / incident P1 thật theo scenario (correlator tách alert theo khoá gộp: nhóm "
        "lỗi + mã lỗi + provider/tủ/lô; incident thật theo ground truth là cả scenario): "
        + (
            ", ".join(f"{scn} {v.n_alerts}/{v.n_real}" for scn, v in r.sf8.by_scenario.items())
            or "—"
        )
        + ".",
        f"- Số tin gom mỗi team mỗi ngày ({r.sf8.n_days} ngày): "
        + (", ".join(f"{t} {v}" for t, v in r.sf8.batches_per_team_per_day.items()) or "—"),
    ]
    for ex in r.sf7.wrong_team_examples:
        L.append(f"  - sai team: {ex}")
    L += [
        "",
        "## 7. Giới hạn đã biết",
        "",
        "- **SF5 của `PAID_NOT_DELIVERED`:** lỗi im lặng chỉ lộ ra khi hết SLA 120s của bước 6, "
        'nên alert P1 đến sau khoảng 120s. Đã đo: không có ngưỡng "chờ bắt đầu bước 6" nào '
        "vừa an toàn vừa < 60s (event bước 6 đầu tiên đến tới 65s sau thanh toán). Câu hỏi cho "
        "mentor: hệ thống thật có phát event lúc mở khoang không.",
        "- Cửa sổ gộp SYSTEM là 15 phút (`detector.yaml`), khác 5 phút ở CLAUDE.md §13.",
        "",
        "## 8. Phiên báo sai / bỏ sót tiêu biểu",
        "",
        "| Loại | Phiên | Nhóm thật | Nhóm dự đoán | Rule | Scenario | Mã lỗi thật |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in r.examples:
        L.append(
            f"| {e.kind} | {e.session_id} | {e.true_category or '—'} | {e.pred_category or '—'} | "
            f"{e.rule_id or '—'} | {e.scenario or '—'} | {e.error_code or '—'} |"
        )
    return "\n".join(L) + "\n"


def write_report(report: EvalReport, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
