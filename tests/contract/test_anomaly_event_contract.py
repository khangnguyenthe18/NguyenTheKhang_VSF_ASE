"""Contract test cho schema sự kiện bất thường chung v0.1 (pair contract với Võ Doanh Nhân)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ase.contracts.anomaly_event import AnomalyEvent, json_schema

ROOT = Path(__file__).resolve().parents[2] / "contracts"


@pytest.mark.parametrize(
    "name", ["transaction_incident", "baseline", "baseline_province", "behavior"]
)
def test_fixtures_validate(name: str) -> None:
    ev = AnomalyEvent.model_validate_json((ROOT / "fixtures" / f"{name}.json").read_bytes())
    assert ev.kind == name.split("_province")[0]


def test_committed_schema_matches_model() -> None:
    """Đổi model mà quên cập nhật file schema chia sẻ với đối tác → test đỏ."""
    committed = json.loads((ROOT / "schemas" / "ase.anomaly.event.v0.1.json").read_text())
    assert committed == json_schema()


def test_unknown_fields_are_ignored_but_rules_enforced() -> None:
    raw = json.loads((ROOT / "fixtures" / "behavior.json").read_text())
    AnomalyEvent.model_validate({**raw, "field_moi_cua_doi_tac": 1})  # tương thích tiến
    for bad in (
        {**raw, "source": "ase"},  # behavior phải do đối tác phát
        {**raw, "event_id": "INC-1"},  # sai tiền tố
        {**raw, "detected_at": "2026-10-26T11:18:03"},  # thiếu múi giờ
        {**raw, "confidence": 1.5},
    ):
        with pytest.raises(ValidationError):
            AnomalyEvent.model_validate(bad)
