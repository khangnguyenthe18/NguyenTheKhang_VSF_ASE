"""`python -m ase.simulator --profile smoke [--out data/smoke]`."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ase.common.config import get_settings
from ase.simulator.generator import Manifest, generate


def _print_summary(m: Manifest) -> None:
    print(f"profile={m.profile} seed={m.seed} git={m.git_sha[:8]}")
    for name, info in m.files.items():
        print(f"  {name:<28} {info.rows:>8} rows  {info.sha256[:12]}")
    print("category x scenario x country (số phiên):")
    for c in m.counts:
        print(f"  {c.category or '-':<16} {c.scenario or '-':<22} {c.country:<3} {c.count:>7}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="ase.simulator")
    parser.add_argument("--profile", default="smoke")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    out = args.out or get_settings().profile_dir(args.profile)
    _print_summary(asyncio.run(generate(args.profile, out)))


if __name__ == "__main__":
    main()
