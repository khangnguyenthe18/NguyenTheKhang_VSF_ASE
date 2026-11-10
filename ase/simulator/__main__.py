"""`python -m ase.simulator --profile smoke [--out data/smoke]`."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ase.common.config import get_settings
from ase.simulator.generator import Manifest, generate
from ase.simulator.txn_generator import TxnManifest, generate_transactions


def _print_summary(m: Manifest) -> None:
    print(f"profile={m.profile} seed={m.seed} git={m.git_sha[:8]}")
    for name, info in m.files.items():
        print(f"  {name:<28} {info.rows:>8} rows  {info.sha256[:12]}")
    print("category x scenario x country (số phiên):")
    for c in m.counts:
        print(f"  {c.category or '-':<16} {c.scenario or '-':<22} {c.country:<3} {c.count:>7}")


def _print_txn_summary(m: TxnManifest) -> None:
    print(f"danh sách giao dịch: {m.n_transactions} giao dịch, {m.n_failures} lỗi/kẹt")
    for key, n in m.by_scenario.items():
        print(f"  {key:<34} {n:>6}")
    empty = sorted(i for i, n in m.instances.items() if n == 0)
    if empty:
        print(f"  [cảnh báo] lần lặp kịch bản không có giao dịch nào: {', '.join(empty)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="ase.simulator")
    parser.add_argument("--profile", default="smoke")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--only-transactions",
        action="store_true",
        help="chỉ sinh swap_transactions + txn_ground_truth (không sinh lại dữ liệu event 7 bước)",
    )
    args = parser.parse_args()
    out = args.out or get_settings().profile_dir(args.profile)
    if not args.only_transactions:
        _print_summary(asyncio.run(generate(args.profile, out)))
    _print_txn_summary(asyncio.run(generate_transactions(args.profile, out)))


if __name__ == "__main__":
    main()
