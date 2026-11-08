"""Field ablation: bảng "tập trường → SF2/SF4" cho 4 preset (EVALUATOR_PLAN §6).

Kiểm chứng giả định rủi ro nhất: *với các trường mà hệ thống thật có, rule + correlator đạt
SF4 ≥ 85%*. Đọc output detector ở `data/<profile>/` (ALL) và `data/<profile>/ablation/<preset>/`,
ghi `data/<profile>/ablation/report.md` + `report.json`. Chỉ đọc, không chạy lại detector
(`make ablation` chạy `detect-ablation` trước).
"""

from __future__ import annotations

import argparse
import itertools
from collections import Counter
from pathlib import Path

from pydantic import BaseModel

from ase.common.config import get_settings
from ase.common.models import REAL_CATEGORIES
from ase.evaluator.loader import RunData, load_run
from ase.evaluator.metrics import SF2Result, SF4Result, compute_sf2, compute_sf4
from ase.evaluator.report import git_sha, pct
from ase.knowledge.loader import Knowledge, default_knowledge

PRESETS: tuple[str, ...] = ("T0", "T0+T1", "T0+T1+T2", "ALL")
TIER_OF_PRESET: dict[str, str] = {"T0+T1": "T1", "T0+T1+T2": "T2", "ALL": "T3"}
CAT_ABBR: dict[str, str] = {
    "SYSTEM": "SYS",
    "HARDWARE": "HW",
    "PAYMENT": "PAY",
    "BUSINESS": "BIZ",
    "USER": "USR",
    "BATTERY_QUALITY": "BAT",
}
# Nguồn dự kiến theo luồng mentor xác nhận (ĐỀ XUẤT, cần mentor xác nhận):
# thiết bị → MQTT; microservice (app, thanh toán, eligibility) → Kafka; ERP/MES.
FIELD_SOURCE: dict[str, str] = {
    "cabinet_id": "Thiết bị (MQTT)",
    "slot_id": "Thiết bị (MQTT)",
    "door_state": "Thiết bị (MQTT)",
    "battery_detected": "Thiết bị (MQTT)",
    "heartbeat_at": "Thiết bị (MQTT)",
    "temperature": "Thiết bị (MQTT)",
    "hw_error_code": "Thiết bị (MQTT)",
    "soc": "Thiết bị (MQTT)",
    "battery_id": "Thiết bị (MQTT)",
    "error_code": "Microservice (Kafka)",
    "payment.captured": "Microservice (Kafka)",
    "payment_provider": "Microservice (Kafka)",
    "app_version": "Microservice (Kafka)",
    "user_id": "Microservice (Kafka)",
    "latency_ms": "Microservice (Kafka)",
    "lot_id": "ERP/MES",
    "soh": "ERP/MES",
}
SF2_MIN = 0.95
SF4_MIN = 0.85


class AblationRow(BaseModel):
    preset: str
    sf2: SF2Result
    sf4: SF4Result
    n_unknown: int
    disabled_rules: list[str]


class AblationReport(BaseModel):
    config: str = "rule-only"
    profile: str
    git_sha: str
    rows: list[AblationRow]
    minimal_preset: str | None  # preset nhỏ nhất đạt cả SF2 ≥ 95% và SF4 ≥ 85%
    first_ok_by_category: dict[str, str | None]  # preset đầu tiên đạt ≥ 85% cho từng nhóm
    tier_gain_sf4: dict[str, float | None]  # tăng SF4 accuracy khi thêm tầng T1/T2/T3
    remaining_errors: dict[str, int]  # "scenario / rule_id" → số phiên còn sai ở ALL


def preset_run_dir(profile_dir: Path, preset: str) -> Path:
    return profile_dir if preset == "ALL" else profile_dir / "ablation" / preset


def _row(data: RunData) -> AblationRow:
    cov = data.run_report.coverage
    return AblationRow(
        preset=data.preset,
        sf2=compute_sf2(data),
        sf4=compute_sf4(data),
        n_unknown=sum(1 for d in data.detections.values() if d.category.value == "UNKNOWN"),
        disabled_rules=[r.rule_id for r in cov.disabled_rules],
    )


def build_report(profile_dir: Path) -> AblationReport:
    rows: list[AblationRow] = []
    last: RunData | None = None
    for preset in PRESETS:
        data = load_run(profile_dir, run_dir=preset_run_dir(profile_dir, preset))
        rows.append(_row(data))
        last = data
    assert last is not None  # preset cuối là ALL

    minimal: str | None = None
    for r in rows:
        if (
            r.sf2.recall is not None
            and r.sf2.recall >= SF2_MIN
            and r.sf4.accuracy is not None
            and r.sf4.accuracy >= SF4_MIN
        ):
            minimal = r.preset
            break

    first_ok: dict[str, str | None] = {}
    for cat in REAL_CATEGORIES:
        first_ok[cat.value] = None
        for r in rows:
            acc = r.sf4.per_category_accuracy.get(cat.value)
            if acc is not None and acc >= SF4_MIN:
                first_ok[cat.value] = r.preset
                break

    gains: dict[str, float | None] = {}
    for prev, cur in itertools.pairwise(rows):
        a, b = prev.sf4.accuracy, cur.sf4.accuracy
        gains[TIER_OF_PRESET[cur.preset]] = (b - a) if a is not None and b is not None else None

    wrong = Counter(
        f"{last.gt[s].scenario or '(lỗi nền)'} / {last.detections[s].rule_id}"
        for s in rows[-1].sf4.misclassified
    )
    return AblationReport(
        profile=profile_dir.name,
        git_sha=git_sha(),
        rows=rows,
        minimal_preset=minimal,
        first_ok_by_category=first_ok,
        tier_gain_sf4=gains,
        remaining_errors=dict(wrong.most_common()),
    )


def _by_cat_cell(sf4: SF4Result) -> str:
    return " · ".join(
        f"{CAT_ABBR[c.value]} {pct(sf4.per_category_accuracy.get(c.value), 0)}"
        for c in REAL_CATEGORIES
    )


def render_markdown(rep: AblationReport, k: Knowledge | None = None) -> str:
    kn = k or default_knowledge()
    tiers = kn.detector.field_tiers
    L: list[str] = [
        f"# Field ablation — {rep.config} — profile `{rep.profile}` (toàn bộ phiên)",
        "",
        f"git `{rep.git_sha}`. Giả định kiểm chứng: *với các trường mà hệ thống thật có, "
        "rule + correlator đạt SF4 ≥ 85%*. Rule thiếu trường **tự tắt** (không crash) và được "
        "ghi ở cột cuối.",
        "",
        "## 1. Bảng tập trường → SF2/SF4",
        "",
        "| Preset | SF2 tổng | SF2 lỗi im lặng | SF4 accuracy | SF4 macro-F1 | "
        "SF4 theo nhóm (recall) | `UNKNOWN` | Rule bị tắt |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rep.rows:
        off = ", ".join(f"`{x}`" for x in r.disabled_rules) or "—"
        L.append(
            f"| `{r.preset}` | {pct(r.sf2.recall)} | {pct(r.sf2.silent.recall)} | "
            f"{pct(r.sf4.accuracy)} | {pct(r.sf4.macro_f1)} | {_by_cat_cell(r.sf4)} | "
            f"{r.n_unknown} | {off} |"
        )

    L += ["", "## 2. Kết luận", ""]
    if rep.minimal_preset is None:
        L.append(
            "1. **Không preset nào đạt đồng thời SF2 ≥ 95% và SF4 ≥ 85%** — xem mục 4 về nguyên "
            "nhân; đưa vào backlog, không chỉnh dữ liệu."
        )
    else:
        row = next(r for r in rep.rows if r.preset == rep.minimal_preset)
        L.append(
            f"1. **Tầng tối thiểu đạt ngưỡng: `{rep.minimal_preset}`** "
            f"(SF2 {pct(row.sf2.recall)}, SF4 {pct(row.sf4.accuracy)})."
        )
    L.append("2. **Nhóm lỗi phụ thuộc trường nào** (preset đầu tiên đạt recall nhóm ≥ 85%):")
    for cat, p in rep.first_ok_by_category.items():
        L.append(f"   - {cat}: " + (f"`{p}`" if p else "không preset nào đạt (hoặc không có mẫu)"))
    L += [
        "",
        "## 3. Trường cần xin trong dữ liệu masked (xếp theo mức tăng SF4 của tầng chứa nó)",
        "",
        "> Cột nguồn là **đề xuất**, cần mentor xác nhận.",
        "",
        "| Thứ tự | Tầng | Tăng SF4 khi thêm tầng | Trường | Nguồn dự kiến |",
        "|---|---|---|---|---|",
    ]
    order = sorted(rep.tier_gain_sf4, key=lambda t: (-(rep.tier_gain_sf4[t] or 0.0), t))
    n = 0
    for tier in order:
        gain = rep.tier_gain_sf4[tier]
        for f in tiers[tier]:
            n += 1
            L.append(
                f"| {n} | {tier} | "
                f"{'—' if gain is None else f'{gain * 100:+.1f} điểm %'} | `{f}` | "
                f"{FIELD_SOURCE.get(f, '?')} |"
            )
    L += [
        "",
        "Trường bắt buộc (tầng T0, không có thì không làm được gì): "
        + ", ".join(f"`{f}`" for f in tiers["T0"])
        + ".",
        "",
        "## 4. Phiên còn sai ở `ALL`",
        "",
    ]
    if rep.remaining_errors:
        L += ["| Scenario / rule | Số phiên |", "|---|---|"]
        L += [f"| {k2} | {v} |" for k2, v in rep.remaining_errors.items()]
    else:
        L.append("Không còn phiên nào bị phân loại sai.")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m ase.evaluator.ablation", description=__doc__)
    p.add_argument("--profile", default="dev")
    args = p.parse_args(argv)
    profile_dir = get_settings().profile_dir(args.profile)
    rep = build_report(profile_dir)
    out = profile_dir / "ablation"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(rep.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (out / "report.md").write_text(render_markdown(rep), encoding="utf-8")
    for r in rep.rows:
        print(
            f"{r.preset:<10} SF2 {pct(r.sf2.recall):>7}  SF4 {pct(r.sf4.accuracy):>7}  "
            f"UNKNOWN {r.n_unknown}"
        )
    print(f"tầng tối thiểu: {rep.minimal_preset}  → {out / 'report.md'}")


if __name__ == "__main__":
    main()
