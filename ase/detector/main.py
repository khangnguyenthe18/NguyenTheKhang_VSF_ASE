"""Điều phối detector: EventSource → Tracker → Classifier → Correlator → Dispatcher → output.

CLI: `python -m ase.detector --profile dev [--fields ALL] [--notify] [--speed N]`.
- Preset `ALL` ghi vào `data/<profile>/`; preset khác ghi vào `data/<profile>/ablation/<preset>/`.
- Mặc định không gửi thật (vẫn tính lịch gửi để evaluator đo); `--notify` gửi qua notifier.
- `--source kafka` làm ở ngày 09/11 (GATE1_PLAN §4).
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from ase.common.config import get_settings
from ase.common.transport import EventSource, JsonlSource, SimClock
from ase.detector.classifier import Classifier, build_coverage_report
from ase.detector.context import ContextStore
from ase.detector.correlator import Correlator
from ase.detector.dispatch import Dispatcher
from ase.detector.fields import FieldView
from ase.detector.output import RunReport, write_incidents_atomic, write_outputs
from ase.detector.state_machine import Finding, SessionTracker
from ase.knowledge.loader import Knowledge, default_knowledge
from ase.notifier.service import NotifierService


@dataclass
class DetectorRun:
    report: RunReport
    out_dir: Path
    elapsed_s: float
    correlator: Correlator
    dispatcher: Dispatcher


def default_out_dir(profile_dir: Path, preset: str, *, stream: bool = False) -> Path:
    base = profile_dir / "stream" if stream else profile_dir
    return base if preset == "ALL" else base / "ablation" / preset


async def run_detector(
    profile_dir: Path,
    *,
    preset: str = "ALL",
    out_dir: Path | None = None,
    knowledge: Knowledge | None = None,
    notifier: NotifierService | None = None,
    speed: float | None = None,
    source: EventSource | None = None,
    clock: SimClock | None = None,
    allowed_lateness_seconds: float = 0.0,
    periodic_incidents: bool = False,
) -> DetectorRun:
    t0 = time.perf_counter()  # chỉ để in thông lượng, không dùng trong logic
    k = knowledge or default_knowledge()
    sim_clock = clock if clock is not None else SimClock()
    ctx = ContextStore.from_profile_dir(profile_dir)
    view = FieldView(ctx, preset=preset, knowledge=k)
    tracker = SessionTracker(
        sim_clock,
        knowledge=k,
        field_view=view,
        allowed_lateness_seconds=allowed_lateness_seconds,
    )
    classifier = Classifier(sim_clock, view, knowledge=k)
    corr = Correlator(view, knowledge=k)
    disp = Dispatcher(corr, knowledge=k, notifier=notifier)
    disp.start()

    target = out_dir or default_out_dir(profile_dir, preset)
    prev_p1 = 0
    prev_batches = 0

    def handle(findings: list[Finding]) -> None:
        for f in findings:
            disp.on_correlation(corr.add(classifier.classify(f)))

    ev_source: EventSource = (
        source
        if source is not None
        else JsonlSource(profile_dir / "ase.swap.events.jsonl", sim_clock, speed=speed)
    )
    async for ev in ev_source.events():
        handle(tracker.on_event(ev))
        disp.tick(sim_clock.now())
        if periodic_incidents and (
            len(disp.p1_alerts) > prev_p1 or len(disp.batches) > prev_batches
        ):
            write_incidents_atomic(target, corr=corr, disp=disp)
            prev_p1 = len(disp.p1_alerts)
            prev_batches = len(disp.batches)
        if notifier is not None and source is None:
            await asyncio.sleep(0)  # nhường worker gửi
    handle(tracker.flush())
    disp.flush_all()
    await disp.drain()

    coverage = build_coverage_report(view, tracker, classifier)
    report = write_outputs(
        target, corr=corr, disp=disp, coverage=coverage, notify=notifier is not None
    )
    return DetectorRun(
        report=report,
        out_dir=target,
        elapsed_s=time.perf_counter() - t0,
        correlator=corr,
        dispatcher=disp,
    )


def format_summary(profile: str, run: DetectorRun) -> str:
    r = run.report
    c = r.coverage
    eps = c.total_events_processed / run.elapsed_s if run.elapsed_s > 0 else 0.0
    lines = [
        f"=== ASE detector (profile={profile}, fields={c.preset}) → {run.out_dir} ===",
        f"Events     : {c.total_events_processed} (trùng={c.duplicate_events_dropped}, "
        f"đến trễ={c.late_events_handled}, lùi={c.backward_transitions_ignored})",
        f"Sessions   : {c.total_sessions_seen}",
        f"Detections : {r.n_detections} "
        + ", ".join(f"{k}={v}" for k, v in r.detections_by_category.items()),
        f"Incidents  : {r.n_incidents} "
        + ", ".join(f"{k}={v}" for k, v in r.incidents_by_category.items())
        + f" (USER đột biến={r.n_user_spike_incidents})",
        f"R_CLUSTER  : {r.n_cluster_reassigned} phiên được gán lại theo cụm",
        f"Gửi        : P1 ngay={r.n_p1_alerts}, tin gom={r.n_batches}"
        + (f", lỗi gửi={r.send_errors}" if r.notify else " (không gửi thật, chỉ lập lịch)"),
        "Rule tắt   : "
        + (
            ", ".join(f"{d.rule_id} (thiếu {','.join(d.missing_fields)})" for d in c.disabled_rules)
            or "không"
        ),
        f"Thời gian  : {run.elapsed_s:.1f}s ({eps:,.0f} event/giây)",
    ]
    return "\n".join(lines)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m ase.detector", description=__doc__)
    p.add_argument("--profile", default="smoke")
    p.add_argument("--fields", default="ALL", help="T0 | T0+T1 | T0+T1+T2 | ALL")
    p.add_argument("--source", default="jsonl", choices=["jsonl", "kafka"])
    p.add_argument("--max-idle-seconds", type=float, default=10.0)
    p.add_argument("--notify", action="store_true", help="gửi thật qua Mail + mock Teams")
    p.add_argument("--speed", type=float, default=None, help="replay theo thời gian (1 = thật)")
    p.add_argument("--out", type=Path, default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = get_settings()
    k = default_knowledge()
    profile_dir = settings.profile_dir(args.profile)
    notifier = NotifierService(settings=settings) if args.notify else None

    source: EventSource | None = None
    lateness = 0.0
    periodic = False
    target_out = args.out
    if args.source == "kafka":
        from ase.streaming.kafka_io import KafkaSource

        source = KafkaSource(settings=settings, max_idle_seconds=args.max_idle_seconds)
        lateness = k.detector.stream.allowed_lateness_seconds
        periodic = True
        if target_out is None:
            target_out = default_out_dir(profile_dir, args.fields, stream=True)

    run = asyncio.run(
        run_detector(
            profile_dir,
            preset=args.fields,
            out_dir=target_out,
            knowledge=k,
            notifier=notifier,
            speed=args.speed,
            source=source,
            allowed_lateness_seconds=lateness,
            periodic_incidents=periodic,
        )
    )
    print(format_summary(args.profile, run))
