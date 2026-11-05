"""Kiểm tra tính toàn vẹn dữ liệu sinh ra và ngưỡng của profile `eval` (MOCK_PLAN §6, §9)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from ase.common.models import (
    REAL_CATEGORIES,
    Cabinet,
    Category,
    GroundTruthRow,
    SwapEvent,
    SwapSession,
)
from ase.knowledge.loader import Knowledge
from ase.simulator.scenarios import ScenariosFile


class SimulationCheckError(RuntimeError):
    """Ném ra khi dữ liệu sinh ra vi phạm ràng buộc hoặc không đạt ngưỡng profile."""


def check_invariants(
    *,
    profile_name: str,
    events: Sequence[SwapEvent],
    sessions: Sequence[SwapSession],
    ground_truth: Sequence[GroundTruthRow],
    cabinets: Sequence[Cabinet],
    knowledge: Knowledge,
    scenarios_file: ScenariosFile,
) -> None:
    session_ids = {s.session_id for s in sessions}
    event_session_ids = {e.session_id for e in events}
    gt_session_ids = {g.session_id for g in ground_truth}

    if len(ground_truth) != len(sessions) or gt_session_ids != session_ids:
        raise SimulationCheckError("ground_truth và swap_sessions lệch tập session_id")
    if event_session_ids != session_ids:
        raise SimulationCheckError("swap_events và ground_truth lệch tập session_id")

    catalog = knowledge.error_catalog
    for ev in events:
        if ev.occurred_at.tzinfo is None:
            raise SimulationCheckError(f"event {ev.event_id} thiếu tzinfo")
        if ev.error_code is not None and not catalog.has(ev.error_code):
            raise SimulationCheckError(f"event error_code lạ: {ev.error_code}")

    for g in ground_truth:
        if g.category is Category.UNKNOWN:
            raise SimulationCheckError(f"ground_truth {g.session_id} có category=UNKNOWN")
        if g.error_code is not None and not catalog.has(g.error_code):
            raise SimulationCheckError(f"ground_truth error_code lạ: {g.error_code}")
        if not g.is_failure:
            if any(
                x is not None
                for x in (g.category, g.severity, g.error_code, g.scenario_instance_id)
            ):
                raise SimulationCheckError(
                    f"phiên bình thường {g.session_id} phải để rỗng category/severity/error_code"
                )

    if profile_name == "eval":
        check_eval_thresholds(
            sessions=sessions,
            ground_truth=ground_truth,
            cabinets=cabinets,
            scenarios_file=scenarios_file,
        )


def check_eval_thresholds(
    *,
    sessions: Sequence[SwapSession],
    ground_truth: Sequence[GroundTruthRow],
    cabinets: Sequence[Cabinet],
    scenarios_file: ScenariosFile,
    min_cases_per_category: int = 50,
    min_scenario_occurrences: int = 2,
    min_cabinet_baseline_days: int = 7,
) -> None:
    """Ngưỡng bắt buộc của `eval` (MOCK_PLAN §6):
    1. Mỗi category trong 6 nhóm thật >= 50 case lỗi trong `split == "test"`.
    2. Mỗi scenario xuất hiện >= 2 lần (distinct instance hoặc >= 2 đợt) trong `split == "test"`.
    3. Mỗi tủ có >= 7 ngày lịch sử trong `split == "baseline"`.
    """
    test_rows = [g for g in ground_truth if g.split == "test"]
    cat_counts: Counter[Category] = Counter(
        g.category for g in test_rows if g.is_failure and g.category is not None
    )
    for cat in REAL_CATEGORIES:
        if cat_counts[cat] < min_cases_per_category:
            raise SimulationCheckError(
                f"eval thiếu case {cat.value}: {cat_counts[cat]} < {min_cases_per_category}"
            )

    # Đếm số đợt (instance) hoặc ngày xuất hiện của từng scenario trong tập test
    scn_instances: dict[str, set[str]] = {s.name: set() for s in scenarios_file.scenarios}
    session_by_id = {s.session_id: s for s in sessions}
    for g in test_rows:
        if g.scenario and g.scenario in scn_instances:
            key = g.scenario_instance_id or f"{session_by_id[g.session_id].started_at:%Y-%m-%d}"
            scn_instances[g.scenario].add(key)

    for scn_name, insts in scn_instances.items():
        if len(insts) < min_scenario_occurrences:
            raise SimulationCheckError(
                f"eval scenario {scn_name} xuất hiện {len(insts)} lần < {min_scenario_occurrences}"
            )

    baseline_ids = {g.session_id for g in ground_truth if g.split == "baseline"}
    cab_days: dict[str, set[str]] = {c.cabinet_id: set() for c in cabinets}
    for s in sessions:
        if s.session_id in baseline_ids and s.cabinet_id in cab_days:
            cab_days[s.cabinet_id].add(f"{s.started_at:%Y-%m-%d}")

    for cab_id, days in cab_days.items():
        if len(days) < min_cabinet_baseline_days:
            raise SimulationCheckError(
                f"eval tủ {cab_id} chỉ có {len(days)} ngày baseline < {min_cabinet_baseline_days}"
            )
