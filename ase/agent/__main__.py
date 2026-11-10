"""CLI: `python -m ase.agent --profile dev [--notify] [--max-notify N]`.

Phát lại danh sách giao dịch mock bằng `SimClock`, quét tại các mốc 30 phút, ghi kết quả vào
`data/<profile>/agent/` (digests.jsonl + summary.json). `--notify` gửi mail + card cho từng lần
quét có lỗi (theo `ASE_NOTIFY_MODE`: `dry_run` ghi bản xem trước, `live` gửi Mailpit/SMTP).
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from pathlib import Path

import orjson
from pydantic import BaseModel

from ase.agent.models import ScanDigest
from ase.agent.runner import ScanAgent
from ase.agent.source import MockTransactionSource
from ase.common.config import get_settings
from ase.common.transport import SimClock
from ase.knowledge.loader import default_error_table, default_knowledge
from ase.notifier.scan_digest import ScanNotifier

AGENT_DIR = "agent"
DIGESTS = "digests.jsonl"
SUMMARY = "summary.json"


class RunSummary(BaseModel):
    profile: str
    n_transactions: int  # số dòng của danh sách giao dịch
    n_seen: int  # số giao dịch agent đã đọc được qua các lần quét (SF1)
    n_scans: int
    n_scans_with_detections: int
    n_detections: int
    n_incidents: int
    notifications: dict[str, int]  # "<kênh>:<trạng thái>" → số lần


async def run(profile: str, out_dir: Path, *, notify: bool, max_notify: int | None) -> RunSummary:
    settings = get_settings()
    if settings.dashboard_link_profile is None:
        settings = settings.model_copy(update={"dashboard_link_profile": profile})
    if max_notify is not None:
        settings = settings.model_copy(update={"max_alerts_per_run": max_notify})
    source = MockTransactionSource(settings.profile_dir(profile))
    spec = default_knowledge().detector.scan
    agent = ScanAgent(source, default_error_table(), spec)
    clock = SimClock()
    notifier = ScanNotifier(settings, clock=clock, spec=spec) if notify else None

    sent: Counter[str] = Counter()
    incident_ids: set[str] = set()
    n_scans = n_with = n_det = 0
    with (out_dir / DIGESTS).open("wb") as f:
        for digest in agent.run_replay(*source.time_range, clock):
            n_scans += 1
            n_with += bool(digest.detections)
            n_det += len(digest.detections)
            incident_ids.update(i.incident_id for i in digest.incidents)
            f.write(orjson.dumps(digest.model_dump(mode="json")) + b"\n")
            if notifier is not None:
                for res in await notifier.notify(digest):
                    sent[f"{res.channel}:{res.status}"] += 1
    summary = RunSummary(
        profile=profile,
        n_transactions=source.n_transactions,
        n_seen=len(agent.scanner.seen),
        n_scans=n_scans,
        n_scans_with_detections=n_with,
        n_detections=n_det,
        n_incidents=len(incident_ids),
        notifications=dict(sorted(sent.items())),
    )
    (out_dir / SUMMARY).write_bytes(
        orjson.dumps(summary.model_dump(mode="json"), option=orjson.OPT_INDENT_2) + b"\n"
    )
    return summary


def load_digests(path: Path) -> list[ScanDigest]:
    with path.open("rb") as f:
        return [ScanDigest.model_validate_json(line) for line in f if line.strip()]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m ase.agent", description=__doc__)
    p.add_argument("--profile", default="dev")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--notify", action="store_true", help="gửi mail + card cho lần quét có lỗi")
    p.add_argument("--max-notify", type=int, default=None, help="giới hạn số lần quét được gửi")
    args = p.parse_args(argv)
    out = args.out or get_settings().profile_dir(args.profile) / AGENT_DIR
    out.mkdir(parents=True, exist_ok=True)
    s = asyncio.run(run(args.profile, out, notify=args.notify, max_notify=args.max_notify))
    print(f"=== Agent quét 30 phút · {s.profile} ===")
    print(f"giao dịch đọc được {s.n_seen}/{s.n_transactions} · {s.n_scans} lần quét")
    print(
        f"lần quét có lỗi {s.n_scans_with_detections} · giao dịch lỗi/kẹt {s.n_detections} · "
        f"sự cố {s.n_incidents}"
    )
    if s.notifications:
        print("thông báo: " + ", ".join(f"{k}={v}" for k, v in s.notifications.items()))
    print(f"kết quả: {out}")


if __name__ == "__main__":
    main()
