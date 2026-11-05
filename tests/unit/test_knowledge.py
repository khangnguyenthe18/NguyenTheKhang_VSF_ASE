from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ase.common.models import REAL_CATEGORIES, Category, Severity, Step
from ase.knowledge.loader import (
    ErrorCatalog,
    default_knowledge,
    load_knowledge,
)

# Danh sách đầy đủ mã trong CLAUDE.md §6.2 (kỳ vọng của test, không phải nguồn của code).
SPEC_CODES = """
APP_CRASH AUTH_FAILED TOKEN_EXPIRED ACCOUNT_LOCKED STATION_LIST_FAIL STATION_DATA_STALE
STATION_OFFLINE NO_CHARGED_BATTERY QR_INVALID QR_WRONG_STATION SESS_CREATE_5XX SESS_DUPLICATE
CABINET_NO_ACK CABINET_BUSY ELIG_PACKAGE_EXPIRED ELIG_DEBT ELIG_QUOTA_EXCEEDED
ELIG_BATTERY_NOT_OWNED ELIG_SERVICE_ERROR ELIG_FALSE_REJECT PAY_GATEWAY_TIMEOUT PAY_DECLINED
PAY_USER_CANCEL PAY_CALLBACK_LATE PAY_CALLBACK_LOST PAY_DOUBLE_CHARGE PAY_WRONG_AMOUNT
SLOT_DOOR_JAM SLOT_SENSOR_FAIL SLOT_LOCK_FAIL USR_RETURN_TIMEOUT RETURN_FOREIGN_BATTERY
BAT_RETURN_FAULT CABINET_DISCONNECT SLOT_OUT_DOOR_JAM BAT_LOW_SOC BAT_LOW_SOH BAT_BMS_FAULT
BAT_PAIRING_FAIL USR_NOT_COLLECTED ASSET_NOT_ASSIGNED STEP_TIMEOUT PAID_NOT_DELIVERED
DELIVERED_NOT_PAID SESSION_DANGLING ASSET_MISMATCH CABINET_OVERHEAT SWAP_SLOW USR_RETRY_STORM
FRAUD_PATTERN
""".split()


def test_knowledge_loads() -> None:
    k = default_knowledge()
    assert k.countries.countries.keys() == {"VN", "PH", "ID"}


def test_all_spec_codes_present() -> None:
    cat = default_knowledge().error_catalog
    assert set(SPEC_CODES) == set(cat.by_code)


def test_default_category_in_enum_and_special_codes() -> None:
    by = default_knowledge().error_catalog.by_code
    assert all(isinstance(e.default_category, Category) for e in by.values())
    assert by["STEP_TIMEOUT"].default_category is Category.UNKNOWN
    assert set(by["STEP_TIMEOUT"].possible_categories) == set(REAL_CATEGORIES)
    assert set(by["BAT_PAIRING_FAIL"].possible_categories) == {
        Category.BATTERY_QUALITY,
        Category.SYSTEM,
    }
    assert set(by["SWAP_SLOW"].possible_categories) == {Category.SYSTEM, Category.HARDWARE}
    assert by["SWAP_SLOW"].severity is Severity.P3
    assert by["BAT_RETURN_FAULT"].default_category is Category.BATTERY_QUALITY  # fmt: skip


def test_bat_return_fault_escalation() -> None:
    cat = default_knowledge().error_catalog
    t = cat.thresholds
    assert (t.temp_warn_1_c, t.temp_warn_2_c) == (55, 60)
    assert cat.severity_for("BAT_RETURN_FAULT") is Severity.P2
    assert cat.severity_for("BAT_RETURN_FAULT", 54.9) is Severity.P2
    assert cat.severity_for("BAT_RETURN_FAULT", 55.1) is Severity.P1
    assert cat.severity_for("SLOT_DOOR_JAM", 99) is Severity.P2


def test_every_category_has_route() -> None:
    r = default_knowledge().routing
    for c in Category:
        assert r.route_for(c) is not None
    assert r.route_for(Category.USER).alert is False
    assert r.route_for(Category.UNKNOWN).needs_human_review is True


def test_state_machine_sla_and_chain() -> None:
    sm = default_knowledge().state_machine
    assert [sm.sla(s) for s in Step] == [None, None, 15, 10, 90, 120, 120]
    assert sm.next_step(Step.OPEN_APP) is Step.SELECT_SERVICE
    assert sm.next_step(Step.RECEIVE_BATTERY) is None
    assert sm.status_allowed("PENDING", "OK")
    assert not sm.status_allowed("OK", "PENDING")


def test_countries() -> None:
    c = default_knowledge().countries
    assert abs(sum(x.traffic_weight for x in c.countries.values()) - 1.0) < 1e-9
    assert c.countries["ID"].timezones == ["Asia/Jakarta", "Asia/Makassar"]
    assert c.country_of_provider("PH_WALLET_1") == "PH"


def test_duplicate_code_rejected() -> None:
    k = default_knowledge().error_catalog
    dup = k.model_dump(mode="json")
    dup["errors"].append(dup["errors"][0])
    with pytest.raises(ValidationError):
        ErrorCatalog.model_validate(dup)


def test_country_override_routing(tmp_path: Path) -> None:
    import shutil

    from ase.knowledge.loader import KNOWLEDGE_DIR

    for f in KNOWLEDGE_DIR.glob("*.yaml"):
        shutil.copy(f, tmp_path / f.name)
    (tmp_path / "routing.yaml").write_text(
        (KNOWLEDGE_DIR / "routing.yaml")
        .read_text()
        .replace(
            "country_overrides: []",
            "country_overrides:\n  - {country: PH, category: PAYMENT, team: 'Payment Ops PH'}",
        )
    )
    r = load_knowledge(tmp_path).routing
    assert r.route_for(Category.PAYMENT, "PH").team == "Payment Ops PH"
    assert r.route_for(Category.PAYMENT, "VN").team == "Payment Ops"
