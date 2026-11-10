"""Điều phối: profile → world → traffic → phiên → ghi file + manifest.

Thứ tự event trong JSONL = thứ tự phát sinh (sort theo `occurred_at` ở đây chỉ mô phỏng thứ tự
đến *trước* khi xáo trộn). Từ bước disorder trở đi file được giữ nguyên thứ tự, không sort lại.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import orjson
import polars as pl
from pydantic import BaseModel

from ase.common import ids
from ase.common.models import CabinetTelemetry, GroundTruthRow, SwapEvent
from ase.common.transport import JsonlSink
from ase.knowledge.loader import Knowledge, default_knowledge
from ase.simulator.checks import check_invariants
from ase.simulator.disorder import apply_disorder
from ase.simulator.entities import World, build_world
from ase.simulator.ground_truth import build_row
from ase.simulator.profiles import PROFILES_PATH, ProfilesFile, load_profiles
from ase.simulator.rng import substream
from ase.simulator.scenarios import ScenariosFile, apply_scenarios, load_scenarios
from ase.simulator.session_fsm import SessionResult, SimContext, run_session
from ase.simulator.telemetry import heartbeat_rows
from ase.simulator.traffic import generate_arrivals, local_day_start_utc


class FileInfo(BaseModel):
    rows: int
    sha256: str


class CountRow(BaseModel):
    category: str
    scenario: str
    country: str
    count: int


class Manifest(BaseModel):
    profile: str
    seed: int
    test_seed: int | None = None
    git_sha: str
    files: dict[str, FileInfo]
    counts: list[CountRow]


async def _git_sha(cwd: Path) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except OSError:
        return "unknown"
    return out.decode().strip() or "unknown" if proc.returncode == 0 else "unknown"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_lines(path: Path) -> int:
    with path.open("rb") as f:
        return sum(1 for _ in f)


def _frame(models: Sequence[BaseModel]) -> pl.DataFrame:
    return pl.DataFrame([m.model_dump(mode="python") for m in models], infer_schema_length=None)


def _write_jsonl(path: Path, models: Sequence[BaseModel]) -> None:
    with path.open("wb") as f:
        for m in models:
            f.write(orjson.dumps(m.model_dump(mode="json")) + b"\n")


def _write_table(out: Path, name: str, models: Sequence[BaseModel], *, jsonl: bool) -> list[Path]:
    paths = []
    if jsonl:
        _write_jsonl(out / f"{name}.jsonl", models)
        paths.append(out / f"{name}.jsonl")
    _frame(models).write_parquet(out / f"{name}.parquet")
    paths.append(out / f"{name}.parquet")
    return paths


def build_heartbeats(
    world: World, knowledge: Knowledge, profiles: ProfilesFile, profile_name: str
) -> list[CabinetTelemetry]:
    profile, common = profiles.profiles[profile_name], profiles.common
    rows: list[CabinetTelemetry] = []
    for cab in world.cabinets:
        start = local_day_start_utc(profile, cab.timezone, 0)
        end = start + timedelta(days=profile.days)
        rng = substream(profile.seed, "heartbeat", cab.cabinet_id)
        rows.extend(heartbeat_rows(cab.cabinet_id, start, end, common.ambient_temp_c, rng))
    return rows


def simulate(
    profile_name: str,
    knowledge: Knowledge | None = None,
    profiles: ProfilesFile | None = None,
    *,
    inject_scenarios: bool = True,
    scenarios_file: ScenariosFile | None = None,
) -> tuple[World, list[SessionResult], list[CabinetTelemetry]]:
    """Phần tính toán thuần (không I/O): dùng trong test và trong `generate`."""
    knowledge = knowledge or default_knowledge()
    profiles = profiles or load_profiles(PROFILES_PATH)
    profile, common = profiles.profiles[profile_name], profiles.common
    world = build_world(profile, common, knowledge)
    ctx = SimContext(knowledge, common, profile, world)
    arrivals = generate_arrivals(profile, common, world, knowledge)

    counters: Counter[str] = Counter()
    results: list[SessionResult] = []
    for arrival in arrivals:  # arrivals đã sort theo thời gian UTC
        day = arrival.started_at
        counters[f"{day:%Y%m%d}"] += 1
        sid = ids.session_id(day, counters[f"{day:%Y%m%d}"])
        results.append(run_session(ctx, arrival, sid))

    telemetry = build_heartbeats(world, knowledge, profiles, profile_name)
    if inject_scenarios:
        telemetry = apply_scenarios(
            profile_name,
            profile,
            common,
            world,
            knowledge,
            results,
            telemetry,
            scenarios_file=scenarios_file,
        )
    for r in results:
        telemetry.extend(r.telemetry)
    telemetry.sort(key=lambda x: (x.heartbeat_at, x.cabinet_id, x.slot_id or ""))
    return world, results, telemetry


def ordered_events(results: Sequence[SessionResult]) -> list[SwapEvent]:
    """Thứ tự phát sinh: theo thời gian, hoà thì theo session/bước (ổn định, deterministic)."""
    events = [e for r in results for e in r.events]
    events.sort(key=lambda e: (e.occurred_at, e.event_id))
    return events


async def generate(
    profile_name: str,
    out_dir: Path,
    *,
    knowledge: Knowledge | None = None,
    profiles: ProfilesFile | None = None,
    scenarios_file: ScenariosFile | None = None,
    repo_root: Path | None = None,
) -> Manifest:
    knowledge = knowledge or default_knowledge()
    profiles = profiles or load_profiles(PROFILES_PATH)
    scenarios_file = scenarios_file or load_scenarios()
    profile = profiles.profiles[profile_name]
    world, results, telemetry = await asyncio.to_thread(
        simulate,
        profile_name,
        knowledge,
        profiles,
        inject_scenarios=True,
        scenarios_file=scenarios_file,
    )
    await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)

    stream_events = apply_disorder(
        ordered_events(results), profile.seed, state_machine=knowledge.state_machine
    )
    gt_rows: list[GroundTruthRow] = [build_row(r, profile.split) for r in results]
    check_invariants(
        profile_name=profile_name,
        events=stream_events,
        sessions=[r.session for r in results],
        ground_truth=gt_rows,
        cabinets=world.cabinets,
        knowledge=knowledge,
        scenarios_file=scenarios_file,
    )

    paths: list[Path] = []
    sink = JsonlSink(out_dir / "ase.swap.events.jsonl")
    for ev in stream_events:
        await sink.write(ev)
    await sink.close()
    paths.append(sink.path)

    gt_path = out_dir / "ground_truth.csv"
    await asyncio.to_thread(_frame(gt_rows).write_csv, gt_path)
    paths.append(gt_path)

    def write_all() -> list[Path]:
        written: list[Path] = []
        for name, models in (
            ("stations", world.stations),
            ("cabinets", world.cabinets),
            ("slots", world.slots),
            ("batteries", world.batteries),
            ("users", world.users),
        ):
            written += _write_table(out_dir, name, models, jsonl=False)
        written += _write_table(out_dir, "swap_sessions", [r.session for r in results], jsonl=True)
        written += _write_table(
            out_dir, "payments", [r.payment for r in results if r.payment is not None], jsonl=True
        )
        written += _write_table(
            out_dir, "cabinet_telemetry", telemetry, jsonl=profile.telemetry_jsonl
        )
        return written

    paths += await asyncio.to_thread(write_all)

    files: dict[str, FileInfo] = {}
    for p in sorted(paths, key=lambda x: x.name):
        if p.suffix == ".parquet":
            rows = pl.scan_parquet(p).select(pl.len()).collect().item()
        elif p.suffix == ".csv":
            rows = _count_lines(p) - 1
        else:
            rows = _count_lines(p)
        files[p.name] = FileInfo(rows=rows, sha256=await asyncio.to_thread(_sha256, p))

    counter: Counter[tuple[str, str, str]] = Counter(
        (
            r.category.value if r.category else "",
            r.scenario or "",
            r.country,
        )
        for r in gt_rows
    )
    manifest = Manifest(
        profile=profile_name,
        seed=profile.seed,
        test_seed=profile.test_seed,
        git_sha=await _git_sha(repo_root or Path.cwd()),
        files=files,
        counts=[
            CountRow(category=c, scenario=s, country=k, count=n)
            for (c, s, k), n in sorted(counter.items())
        ],
    )
    (out_dir / "manifest.json").write_bytes(
        orjson.dumps(manifest.model_dump(mode="json"), option=orjson.OPT_INDENT_2) + b"\n"
    )
    return manifest
