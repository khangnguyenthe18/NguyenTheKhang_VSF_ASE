from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ase.common import ids
from ase.common.models import Step, SwapEvent
from ase.common.time import format_local, format_offset_for
from ase.common.transport import JsonlSink, JsonlSource, SimClock

T0 = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)


def test_simclock_never_goes_back() -> None:
    c = SimClock()
    c.advance(T0 + timedelta(seconds=10))
    c.advance(T0)  # event đến trễ
    assert c.now() == T0 + timedelta(seconds=10)


def test_simclock_uninitialised_and_naive() -> None:
    c = SimClock()
    with pytest.raises(RuntimeError):
        c.now()
    with pytest.raises(ValueError):
        c.advance(datetime(2026, 1, 1))  # noqa: DTZ001


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        ("Asia/Ho_Chi_Minh", "GMT+7"),
        ("Asia/Manila", "GMT+8"),
        ("Asia/Jakarta", "GMT+7"),
        ("Asia/Makassar", "GMT+8"),
        ("Asia/Kolkata", "GMT+5:30"),
    ],
)
def test_format_offset(tz: str, expected: str) -> None:
    assert format_offset_for(T0, tz) == expected


def test_format_local() -> None:
    assert format_local(T0, "Asia/Manila") == "18:00:00 04/11/2026 (GMT+8)"


def test_ids_deterministic() -> None:
    assert ids.session_id(T0, 123) == "SW20261104-000123"
    assert ids.hash_user_id("a") == ids.hash_user_id("a")
    assert ids.event_id("S", 3) == ids.event_id("S", 3)


def _ev(i: int, t: datetime) -> SwapEvent:
    return SwapEvent(
        event_id=f"e{i}", session_id="s", station_id="st", step=Step.OPEN_APP,
        status="OK", occurred_at=t,
    )  # fmt: skip


async def test_jsonl_roundtrip_keeps_order_and_clock_monotonic(tmp_path: Path) -> None:
    p = tmp_path / "ase.swap.events.jsonl"
    # cố ý sai thứ tự thời gian + trùng event
    evs = [
        _ev(1, T0 + timedelta(seconds=30)),
        _ev(2, T0),
        _ev(2, T0),
        _ev(3, T0 + timedelta(seconds=60)),
    ]
    sink = JsonlSink(p)
    for e in evs:
        await sink.write(e)
    await sink.close()

    clock = SimClock()
    seen: list[str] = []
    last: datetime | None = None
    async for e in JsonlSource(p, clock).events():
        seen.append(e.event_id)
        now = clock.now()
        assert last is None or now >= last
        last = now
    assert seen == ["e1", "e2", "e2", "e3"]  # giữ nguyên thứ tự dòng
    assert clock.now() == T0 + timedelta(seconds=60)


def test_simclock_is_set_reflects_whether_clock_was_initialised() -> None:
    from datetime import UTC, datetime

    from ase.common.transport import SimClock

    clock = SimClock()
    assert clock.is_set() is False
    clock.advance(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    assert clock.is_set() is True


def test_blank_llm_settings_in_env_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env` chép từ `.env.example` có các ô LLM để trống: không được làm hỏng cấu hình."""
    from ase.common.config import Settings

    for key in ("ASE_LLM_PROVIDER", "ASE_LLM_MAX_OUTPUT_TOKENS", "ASE_LLM_BASE_URL",
                "ASE_LLM_MODEL", "ASE_LLM_API_KEY", "ASE_LLM_TIMEOUT_SECONDS"):  # fmt: skip
        monkeypatch.setenv(key, "")
    s = Settings(_env_file=None)
    assert s.llm_provider == "none" and s.llm_max_output_tokens == 800
    assert s.llm_base_url is None and s.llm_api_key is None and s.llm_timeout_seconds == 30
    monkeypatch.setenv("ASE_LLM_PROVIDER", "anthropic")
    with pytest.warns(UserWarning, match="V-LLM"):
        assert Settings(_env_file=None).llm_provider == "none"
