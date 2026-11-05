"""Ground truth: đúng 1 dòng cho MỌI phiên (kể cả không lỗi). Chỉ ghi nhóm thật, không UNKNOWN."""

from __future__ import annotations

from ase.common.models import GroundTruthRow
from ase.simulator.profiles import SplitRule
from ase.simulator.session_fsm import SessionResult


def build_row(result: SessionResult, split_rule: SplitRule) -> GroundTruthRow:
    label = result.label
    return GroundTruthRow(
        session_id=result.session.session_id,
        is_failure=label is not None,
        category=label.category if label else None,
        scenario=label.scenario if label else result.non_failure_scenario,
        scenario_start=label.scenario_start if label else None,
        injected_at=label.injected_at if label else None,
        severity=label.severity if label else None,
        error_code=label.error_code if label else None,
        scenario_instance_id=label.scenario_instance_id if label else None,
        country=result.country,
        split=split_rule.split_for(result.day_index),
    )
