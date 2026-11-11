"""CLI báo cáo kỳ: `python -m ase.report --profile dev --from 2026-10-19 --to 2026-10-25 [--top 5]`.

Lập báo cáo KPI success factor + Top N lỗi + health score + open point cho khoảng ngày bất kỳ
(giờ OPS). `--notify` gửi mail tới đội OPS (theo `ASE_NOTIFY_MODE`). Báo cáo tuần / tháng tự
động do `python -m ase.agent` lập theo lịch trong `knowledge/kpi.yaml`.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import orjson

from ase.agent.source import load_transactions
from ase.common.config import get_settings
from ase.knowledge.loader import default_error_table, default_knowledge, default_kpi
from ase.notifier.period_mail import PeriodReportNotifier, render_period_report
from ase.report.period import build_period_report


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m ase.report", description=__doc__)
    p.add_argument("--profile", default="dev")
    p.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    p.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    p.add_argument("--top", type=int, default=None, help="Top N lỗi (mặc định theo kpi.yaml)")
    p.add_argument("--notify", action="store_true")
    args = p.parse_args(argv)

    settings = get_settings()
    kpi = default_kpi()
    profile_dir = settings.profile_dir(args.profile)
    # Lập ngay sau kỳ (00:00 ngày kế tiếp, giờ OPS): tái lập được, không theo đồng hồ thật.
    after = datetime.combine(args.end + timedelta(days=1), time(0), ZoneInfo(kpi.report.timezone))
    report = build_period_report(
        load_transactions(profile_dir),
        kind="custom",
        start=args.start,
        end=args.end,
        generated_at=after.astimezone(UTC),
        spec=kpi,
        scan=default_knowledge().detector.scan,
        table=default_error_table(),
        top_n=args.top,
    )
    out = profile_dir / "reports"
    out.mkdir(exist_ok=True)
    stem = report.report_id.replace(":", "-")
    (out / f"{stem}.json").write_bytes(
        orjson.dumps(report.model_dump(mode="json"), option=orjson.OPT_INDENT_2)
    )
    rendered = render_period_report(report, kpi)
    (out / f"{stem}.txt").write_text(rendered.text_body, encoding="utf-8")
    print(rendered.text_body)
    if args.notify:
        res = asyncio.run(PeriodReportNotifier(settings).send(report))
        print(f"gửi mail: {res.status} {res.detail or ''}")
    print(f"báo cáo: {out / stem}.txt")


if __name__ == "__main__":
    main()
