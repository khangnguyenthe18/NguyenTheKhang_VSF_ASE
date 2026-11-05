from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ase.common.models import GroundTruthRow, SwapEvent

FIXTURES = Path(__file__).resolve().parents[2] / "ase" / "schemas" / "fixtures"


def test_valid_fixture_parses() -> None:
    ev = SwapEvent.model_validate_json((FIXTURES / "swap_event.json").read_text())
    assert ev.occurred_at.tzinfo is not None
    assert ev.step.value == "3_scan_qr"


def test_naive_occurred_at_rejected() -> None:
    with pytest.raises(ValidationError):
        SwapEvent.model_validate(
            {
                "event_id": "e1",
                "session_id": "s1",
                "station_id": "st1",
                "step": "1_open_app",
                "status": "OK",
                "occurred_at": "2026-11-04T10:00:00",
            }
        )


def test_swap_event_has_exactly_11_fields() -> None:
    assert set(SwapEvent.model_fields) == {
        "event_id", "session_id", "station_id", "cabinet_id", "slot_id", "step",
        "status", "error_code", "error_message", "latency_ms", "occurred_at",
    }  # fmt: skip


def test_ground_truth_never_unknown() -> None:
    base = {"session_id": "s", "is_failure": True, "country": "VN", "split": "smoke"}
    with pytest.raises(ValidationError):
        GroundTruthRow.model_validate({**base, "category": "UNKNOWN"})
    row = GroundTruthRow.model_validate(
        {**base, "category": "SYSTEM", "scenario_start": datetime(2026, 1, 1, tzinfo=UTC)}
    )
    assert row.category is not None
