"""FSM 7 bước + payment + telemetry + lỗi nền cho từng phiên.

Phiên đi theo chuỗi bước trong `state_machine.yaml` (qua `StateMachine.next_step`), không hardcode
thứ tự ở đây. Lỗi nền chỉ dùng mã có trong `error_catalog.yaml`.

Quy ước thanh toán (giả định đã ghi trong MOCK_PLAN, cần chủ dự án xác nhận):
- Lỗi tường minh ở bước 6/7 → hệ thống huỷ giữ tiền (`VOIDED`, `captured=false`).
- `captured=true` mà phiên không có event bước 6/7 chỉ dành cho `PAID_NOT_DELIVERED` (scenario).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import BaseModel

from ase.common import ids
from ase.common.models import (
    CabinetTelemetry,
    Category,
    Payment,
    Severity,
    Step,
    SwapEvent,
    SwapSession,
)
from ase.knowledge.loader import Knowledge, StateMachine
from ase.simulator.entities import USER_DEBT, USER_EXPIRED, USER_NORMAL, World
from ase.simulator.profiles import BackgroundFailure, CommonSpec, ProfileSpec
from ase.simulator.rng import substream
from ase.simulator.telemetry import session_rows
from ase.simulator.traffic import Arrival


class FailureLabel(BaseModel):
    """Nhãn thật của phiên lỗi (đi vào ground truth, kể cả khi event bị bỏ error_code)."""

    category: Category
    severity: Severity
    error_code: str
    scenario: str | None = None
    scenario_instance_id: str | None = None
    scenario_start: datetime | None = None
    injected_at: datetime | None = None


class SessionResult(BaseModel):
    events: list[SwapEvent]
    session: SwapSession
    payment: Payment | None
    telemetry: list[CabinetTelemetry]
    label: FailureLabel | None
    country: str
    day_index: int
    non_failure_scenario: str | None = None


def step_chain(sm: StateMachine) -> list[Step]:
    chain = [Step.OPEN_APP]
    while (nxt := sm.next_step(chain[-1])) is not None:
        chain.append(nxt)
    return chain


@dataclass
class SimContext:
    knowledge: Knowledge
    common: CommonSpec
    profile: ProfileSpec
    world: World
    chain: list[Step] = field(init=False)
    failure_pool: list[BackgroundFailure] = field(init=False)
    failure_weights: list[float] = field(init=False)

    def __post_init__(self) -> None:
        self.chain = step_chain(self.knowledge.state_machine)
        self.failure_pool = list(self.common.background_failures)
        self.failure_weights = [f.weight for f in self.failure_pool]
        catalog = self.knowledge.error_catalog
        for f in self.failure_pool:
            if not catalog.has(f.code):
                raise ValueError(f"lỗi nền {f.code} không có trong error_catalog.yaml")
            if len(catalog.by_code[f.code].steps) != 1:
                raise ValueError(f"lỗi nền {f.code} phải gắn đúng 1 bước")


def _user_state_for(code: str | None) -> str:
    if code == "ELIG_DEBT":
        return USER_DEBT
    if code == "ELIG_PACKAGE_EXPIRED":
        return USER_EXPIRED
    return USER_NORMAL


def run_session(ctx: SimContext, arrival: Arrival, session_id: str) -> SessionResult:
    common, world, catalog = ctx.common, ctx.world, ctx.knowledge.error_catalog
    seed = ctx.profile.seed_for_day(arrival.day_index)
    rng = substream(seed, "session", session_id)
    station = world.station_by_id[arrival.station_id]
    country = station.country
    pricing = ctx.knowledge.countries.countries[country].pricing
    providers = ctx.knowledge.countries.countries[country].payment_providers

    failure: BackgroundFailure | None = None
    if rng.random() < common.background_fail_rate:
        failure = rng.choices(ctx.failure_pool, ctx.failure_weights)[0]
    code = failure.code if failure else None
    fail_idx = ctx.chain.index(catalog.by_code[code].steps[0]) if code else None
    last_idx = fail_idx if fail_idx is not None else len(ctx.chain) - 1

    pool = (
        world.users_by_country_state.get((country, _user_state_for(code)))
        or (world.users_by_country_state[(country, USER_NORMAL)])
    )
    user = rng.choice(pool)
    app_version = rng.choice(common.app_versions)
    cabinet = rng.choice(world.cabinets_of_station[station.station_id])
    slot_in, slot_out = rng.sample(cabinet.slot_ids, 2)
    battery_in = rng.choice(world.batteries)
    provider = rng.choice(providers)

    omit_code = bool(
        failure and failure.omit_code_allowed and rng.random() < common.missing_error_code_rate
    )

    events: list[SwapEvent] = []
    windows: dict[int, tuple[datetime, datetime]] = {}
    t = arrival.started_at
    for idx in range(last_idx + 1):
        step = ctx.chain[idx]
        lo, hi = common.step_seconds[step]
        t_end = t + timedelta(milliseconds=round(rng.uniform(lo, hi) * 1000))
        windows[idx] = (t, t_end)
        latency = max(1, round(rng.lognormvariate(0, 0.35) * common.latency_median_ms[step]))
        is_fail = idx == fail_idx
        step_slot = slot_in if idx == 5 else slot_out if idx == 6 else None
        events.append(
            SwapEvent(
                event_id=ids.event_id(session_id, idx + 1),
                session_id=session_id,
                station_id=station.station_id,
                cabinet_id=cabinet.cabinet_id if idx >= 2 else None,
                slot_id=step_slot,
                step=step,
                status=failure.status if is_fail and failure else "OK",
                error_code=None if (not is_fail or omit_code) else code,
                error_message=catalog.by_code[code].description if is_fail and code else None,
                latency_ms=latency,
                occurred_at=t_end,
            )
        )
        t = t_end

    # --- payment (bước 5 = index 4) ---
    payment: Payment | None = None
    if last_idx >= 4:
        pay_end = windows[4][1]
        callback: datetime | None = None
        if fail_idx == 4:
            status = {"PAY_DECLINED": "DECLINED", "PAY_USER_CANCEL": "CANCELLED"}.get(
                code or "", "FAILED"
            )
            captured = False
        else:  # thành công, hoặc lỗi ở bước 6/7 (huỷ giữ tiền)
            captured = fail_idx is None
            status = "SUCCESS" if captured else "VOIDED"
            callback = pay_end + timedelta(milliseconds=rng.randint(800, 3000))
        payment = Payment(
            payment_id=ids.payment_id(session_id),
            session_id=session_id,
            amount=pricing.standard_swap_amount(user.package_type),
            currency=ctx.knowledge.countries.countries[country].currency,
            status=status,
            captured=captured,
            callback_at=callback,
        )

    # --- telemetry quanh phiên ---
    soc_in, soc_out = rng.uniform(10, 40), rng.uniform(90, 100)
    ambient = rng.uniform(*common.ambient_temp_c)
    telemetry = session_rows(
        cabinet_id=cabinet.cabinet_id,
        slot_in=slot_in,
        slot_out=slot_out,
        t6=windows.get(5),
        t7=windows.get(6),
        failure_code=code,
        soc_in=round(soc_in, 1),
        soc_out=round(soc_out, 1),
        temperature=round(ambient, 1),
    )

    returned = last_idx > 5 or (fail_idx is None)
    session = SwapSession(
        session_id=session_id,
        user_id=user.user_id,
        station_id=station.station_id,
        cabinet_id=cabinet.cabinet_id if last_idx >= 2 else None,
        slot_in=slot_in if last_idx >= 5 else None,
        slot_out=slot_out if last_idx >= 6 else None,
        battery_in_id=battery_in.battery_id if returned else None,
        battery_out_id=cabinet.battery_by_slot[slot_out] if last_idx >= 6 else None,
        app_version=app_version,
        payment_provider=provider if last_idx >= 4 else None,
        final_status="COMPLETED" if fail_idx is None else "FAILED",
        started_at=arrival.started_at,
        ended_at=events[-1].occurred_at,
    )

    label: FailureLabel | None = None
    if code:
        label = FailureLabel(
            category=catalog.by_code[code].default_category,
            severity=catalog.severity_for(code),
            error_code=code,
        )
    return SessionResult(
        events=events,
        session=session,
        payment=payment,
        telemetry=telemetry,
        label=label,
        country=country,
        day_index=arrival.day_index,
    )
