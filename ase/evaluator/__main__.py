"""CLI: `python -m ase.evaluator --profile dev [--split test] [--run-dir PATH]`."""

from __future__ import annotations

import argparse
from pathlib import Path

from ase.common.config import get_settings
from ase.evaluator.loader import load_run
from ase.evaluator.report import evaluate, pct, secs, write_report


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m ase.evaluator", description=__doc__)
    p.add_argument("--profile", default="dev")
    p.add_argument("--split", default=None, help="chỉ đánh giá phiên thuộc split này (vd test)")
    p.add_argument("--run-dir", type=Path, default=None, help="thư mục output của detector")
    p.add_argument("--out", type=Path, default=None, help="thư mục ghi báo cáo")
    args = p.parse_args(argv)

    profile_dir = get_settings().profile_dir(args.profile)
    data = load_run(profile_dir, run_dir=args.run_dir, split=args.split)
    report = evaluate(data)
    out_dir = args.out or (args.run_dir or profile_dir) / "eval"
    write_report(report, out_dir)

    print(f"=== Evaluator rule-only · {args.profile} · split={args.split or 'all'} ===")
    print(f"SF1 events {pct(report.sf1.events_pct)} · phiên {pct(report.sf1.sessions_pct)}")
    print(f"SF2 recall {pct(report.sf2.recall)} · lỗi im lặng {pct(report.sf2.silent.recall)}")
    print(f"SF3 precision {pct(report.sf3.precision)} · bẫy bị báo {report.sf3.n_trap_detected}")
    print(f"SF4 accuracy {pct(report.sf4.accuracy)} · macro-F1 {pct(report.sf4.macro_f1)}")
    print(
        f"SF5 P1 p95 {secs(report.sf5.p1.overall.p95)} · "
        f"tin gom trong chu kỳ {pct(report.sf5.batch.pct_within_interval)}"
    )
    print(
        f"SF7 team {pct(report.sf7.team_pct)} · schema {pct(report.sf7.schema_pct)} · "
        f"render {pct(report.sf7.render_pct)}"
    )
    ratio = "—" if report.sf8.ratio is None else f"{report.sf8.ratio:.2f}"
    print(f"SF8 alert/incident P1 {ratio}")
    print(f"→ {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
