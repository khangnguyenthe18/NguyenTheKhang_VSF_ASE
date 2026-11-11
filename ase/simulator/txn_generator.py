"""Sinh mock "Danh sách giao dịch đổi pin": 1 dòng/giao dịch, đúng định dạng màn hình thật.

Vì sao sinh thẳng danh sách thay vì suy từ event 7 bước: hệ thống vận hành thật chỉ cho thấy danh
sách giao dịch với trạng thái tổng (Tạo mới → Đưa pin vào → Hoàn thành / Thất bại); agent quét
30 phút/lần làm việc trên đúng dữ liệu đó. File ghi **trạng thái cuối** của từng giao dịch;
`ase.agent.source.view_as_of` dựng lại danh sách tại một thời điểm bất kỳ.

Tất định theo seed: mỗi nguồn ngẫu nhiên một sub-stream riêng (`rng.substream`), không đọc đồng
hồ thật. Lỗi chỉ dùng mã có trong bảng mã lỗi chuẩn.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import numpy as np
import orjson
import polars as pl
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ase.common.models import Station, SwapTransaction, TxnGroundTruthRow, TxnStatus
from ase.knowledge.loader import ErrorTable, Knowledge, default_error_table, default_knowledge
from ase.simulator.checks import SimulationCheckError
from ase.simulator.entities import USER_NORMAL, CabinetInfo, World, build_world
from ase.simulator.generator import _frame, _write_table
from ase.simulator.profiles import (
    PROFILES_PATH,
    CommonSpec,
    ProfilesFile,
    ProfileSpec,
    TxnTraffic,
    load_profiles,
)
from ase.simulator.real_sites import RealSite, load_real_sites
from ase.simulator.rng import substream
from ase.simulator.traffic import Arrival, hourly_weights, local_day_start_utc

TXN_SCENARIOS_PATH = Path(__file__).resolve().parent / "txn_scenarios.yaml"
TRANSACTIONS_TABLE = "swap_transactions"
TXN_GROUND_TRUTH = "txn_ground_truth.csv"
TXN_SCENARIO_INSTANCES = "scenario_instances.csv"
TXN_MANIFEST = "txn_manifest.json"
STATION_CODES_TABLE = "station_codes"
TXN_STATIONS_TABLE = "txn_stations"
TXN_TELEMETRY_FILE = "txn_telemetry.parquet"

# Mã tỉnh giả cho mã trạm `B.<TỈNH><5 số>`; ID chọn theo múi giờ (WIB / WITA).
_PROVINCES: dict[str, list[str]] = {"VN": ["HNO", "HCM", "DNG", "HPG", "QNI"], "PH": ["MNL", "CEB"]}
_PROVINCE_BY_TZ = {"Asia/Jakarta": "JKT", "Asia/Makassar": "MKS"}
_CABINET_TYPES = {"CC_06_P": 6, "CC_12_P": 12}  # loại tủ → số connector
_PEAK_START_HOURS = (7, 8, 17, 18)  # kịch bản bắt đầu trong giờ cao điểm địa phương để có đủ case
_CABINET_TXN_BASE = 12_000_000

# drop: giao dịch không xảy ra (sụt traffic); repeat: một khách tạo giao dịch lặp lại liên tục.
Effect = Literal["fail", "stuck_created", "stuck_battery_in", "slow", "drop", "repeat"]
Scope = Literal["cabinet", "station", "province", "country", "global"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodeSpec(_Strict):
    code: str
    weight: float = Field(default=1.0, gt=0)
    after_battery_in: bool = False


class TimingSpec(_Strict):
    battery_in_seconds: tuple[int, int]
    battery_out_seconds: tuple[int, int]
    fail_seconds: tuple[int, int]


class BackgroundSpec(_Strict):
    fail_rate: float = Field(ge=0, le=1)
    failures: list[CodeSpec] = Field(min_length=1)


class TxnScenarioSpec(_Strict):
    name: str
    scope: Scope
    country: str | None = None
    duration_minutes: int = Field(ge=1)
    fail_prob: float = Field(gt=0, le=1)
    effect: Effect = "fail"
    sequence: bool = False
    codes: list[CodeSpec] = Field(default_factory=list)
    slow_seconds: tuple[int, int] | None = None
    overheat_temp_c: tuple[float, float] | None = None  # nhiệt độ tủ (telemetry) khi sự cố
    repeat_count: tuple[int, int] | None = None  # effect=repeat: số giao dịch lặp của một khách
    repeat_gap_seconds: tuple[int, int] | None = None  # effect=repeat: khoảng cách giữa 2 lần
    occurrences: dict[str, int]

    @model_validator(mode="after")
    def _consistent(self) -> TxnScenarioSpec:
        if (self.effect in ("fail", "repeat")) != bool(self.codes):
            raise ValueError(f"{self.name}: chỉ effect=fail/repeat mới có `codes`, và bắt buộc có")
        if (self.effect == "slow") != (self.slow_seconds is not None):
            raise ValueError(f"{self.name}: effect=slow cần `slow_seconds`")
        is_repeat = self.effect == "repeat"
        if is_repeat != (self.repeat_count is not None and self.repeat_gap_seconds is not None):
            raise ValueError(f"{self.name}: effect=repeat cần `repeat_count`, `repeat_gap_seconds`")
        if is_repeat and self.scope != "cabinet":
            raise ValueError(f"{self.name}: effect=repeat chỉ dùng scope=cabinet")
        if (self.effect == "drop") != (self.scope == "province"):
            raise ValueError(f"{self.name}: effect=drop đi với scope=province (và ngược lại)")
        return self

    @property
    def is_failure(self) -> bool:
        return self.effect not in ("slow", "drop")


class TxnScenariosFile(_Strict):
    timing: TimingSpec
    background: BackgroundSpec
    scenarios: list[TxnScenarioSpec]

    def all_codes(self) -> set[str]:
        codes = {c.code for c in self.background.failures}
        return codes | {c.code for s in self.scenarios for c in s.codes}


def load_txn_scenarios(path: Path = TXN_SCENARIOS_PATH) -> TxnScenariosFile:
    with path.open(encoding="utf-8") as f:
        return TxnScenariosFile.model_validate(yaml.safe_load(f))


@dataclass(frozen=True)
class ScenarioInstance:
    spec: TxnScenarioSpec
    instance_id: str
    start: datetime
    end: datetime
    station_id: str | None  # mã trạm nội bộ của World
    cabinet_id: str | None
    country: str | None
    province: str | None = None  # scope=province: mã tỉnh trong mã trạm, vd `HCM`
    day_index: int = 0

    def matches(
        self, t: datetime, station_id: str, cabinet_id: str, country: str, province: str = ""
    ) -> bool:
        if (
            self.spec.effect == "repeat"
        ):  # giao dịch lặp do simulator chèn riêng, không khớp theo giờ
            return False
        if not self.start <= t < self.end:
            return False
        if self.province is not None:
            return province == self.province
        if self.cabinet_id is not None:
            return cabinet_id == self.cabinet_id
        if self.station_id is not None:
            return station_id == self.station_id
        return self.country is None or country == self.country

    def codes_at(self, t: datetime) -> list[CodeSpec]:
        codes = self.spec.codes
        if not self.spec.sequence:
            return codes
        frac = (t - self.start) / (self.end - self.start)
        return [codes[min(len(codes) - 1, int(frac * len(codes)))]]


class TxnManifest(BaseModel):
    profile: str
    seed: int
    test_seed: int | None
    n_transactions: int
    n_failures: int
    sha256: dict[str, str]
    by_scenario: dict[str, int]  # "<kịch bản>|<split>" → số giao dịch lỗi ("background" = lỗi nền)
    instances: dict[str, int]  # scenario_instance_id → số giao dịch bị ảnh hưởng (drop: bị mất)


class CabinetCodeRow(BaseModel):
    """Ánh xạ mã nội bộ của simulator → mã vận hành như màn hình thật (1 dòng/tủ).

    Dashboard dùng bảng này để trang Bản đồ và Trạm & Tủ hiện cùng mã với Danh sách giao dịch.
    """

    station_id: str
    station_code: str
    cabinet_id: str
    cabinet_code: str
    cabinet_serial: str
    cabinet_type: str


@dataclass(frozen=True)
class TxnResult:
    transactions: list[SwapTransaction]
    ground_truth: list[TxnGroundTruthRow]
    instances: list[ScenarioInstance]
    cabinet_codes: list[CabinetCodeRow]
    stations: list[Station]  # trạm của thế giới danh sách giao dịch, mã nội bộ
    telemetry: pl.DataFrame  # schema của `CabinetTelemetry`, mã tủ vận hành
    dropped: dict[str, int] = field(default_factory=dict)  # kịch bản sụt traffic: số GD bị mất


def _hex(seed: int, *keys: object, n: int) -> str:
    return hashlib.sha256(":".join([str(seed), *map(str, keys)]).encode()).hexdigest()[:n]


def station_codes(
    world: World, seed: int, mask: bool = False, reserved: frozenset[str] = frozenset()
) -> dict[str, str]:
    """Mã trạm dạng màn hình thật `B.<TỈNH><5 số>`; trạm lấy toạ độ tủ thật thì dùng mã tủ đó.

    `mask=True` (bộ dữ liệu dùng chung với đối tác): giữ tỉnh trong mã, thay 5 số bằng số giả
    tất định theo seed, không trùng mã thật nào trong `reserved` → không lộ mã điểm đổi pin thật.
    """
    out: dict[str, str] = {}
    used: set[str] = set(reserved) if mask else set()
    for st in world.stations:
        if st.site_code and not mask:
            code = st.site_code
        elif st.site_code:
            m = _PROVINCE_RE.match(st.site_code)
            province = m.group(1) if m else "VN"
            rng = substream(seed, "txn_masked_code", st.station_id)
            code = f"B.{province}{rng.randrange(1, 100_000):05d}"
            while code in used:
                code = f"B.{province}{rng.randrange(1, 100_000):05d}"
        else:
            rng = substream(seed, "txn_station_code", st.station_id)
            province = _PROVINCE_BY_TZ.get(st.timezone) or rng.choice(_PROVINCES[st.country])
            code = f"B.{province}{rng.randrange(1, 100_000):05d}"
            while code in used:
                code = f"B.{province}{rng.randrange(1, 100_000):05d}"
        used.add(code)
        out[st.station_id] = code
    return out


def _masked_station(st: Station, code: str, seed: int) -> Station:
    """Bản che của trạm: mã giả, toạ độ làm tròn 0,05° (~5 km) + lệch ngẫu nhiên tất định."""
    rng = substream(seed, "txn_masked_geo", st.station_id)
    lat = round(round(st.lat / 0.05) * 0.05 + rng.uniform(-0.02, 0.02), 4)
    lon = round(round(st.lon / 0.05) * 0.05 + rng.uniform(-0.02, 0.02), 4)
    return st.model_copy(update={"site_code": code, "lat": lat, "lon": lon})


def battery_serial(seed: int, battery_id: str) -> str:
    """S/N pin giả cùng độ dài với màn hình thật (`BAT` + 29 ký tự)."""
    return "BAT" + _hex(seed, "battery_serial", battery_id, n=29).upper()


def _after(rng: random.Random, t: datetime, bounds: tuple[int, int]) -> datetime:
    return t + timedelta(seconds=rng.randint(*bounds), milliseconds=rng.randrange(1000))


NATIONAL_SITES_FALLBACK = 43_297  # số điểm đổi pin VN trong file tủ thật (khi không có file)
_PROVINCE_RE = re.compile(r"^[A-Z]+\.([A-Z]+)\d+$")


def station_rates(
    world: World, traffic: TxnTraffic, sites: list[RealSite] | None
) -> dict[str, float]:
    """Số giao dịch kỳ vọng mỗi ngày của từng trạm, theo tỉnh/thành (xem `TxnTraffic`).

    Tổng của TOÀN QUỐC (mọi điểm thật, kể cả điểm không được lấy mẫu) = monthly_total / ngày, nên
    mỗi trạm mẫu có đúng mật độ thực tế; tỉnh nhiều điểm → mỗi điểm đông hơn.
    """
    daily = traffic.monthly_total / traffic.days_per_month
    if not sites:
        return {st.station_id: daily / NATIONAL_SITES_FALLBACK for st in world.stations}
    per_province = Counter(s.province for s in sites)
    mean = len(sites) / len(per_province)

    def factor(province: str) -> float:
        return float((per_province.get(province, mean) / mean) ** traffic.province_alpha)

    base = daily / sum(n * factor(p) for p, n in per_province.items())
    out: dict[str, float] = {}
    for st in world.stations:
        m = _PROVINCE_RE.match(st.site_code or "")
        out[st.station_id] = base * factor(m.group(1) if m else "")
    return out


def province_of(station: Station) -> str:
    """Mã tỉnh trong mã điểm đổi pin thật (`B.HCM01234` → `HCM`); trạm không có mã → ``."""
    m = _PROVINCE_RE.match(station.site_code or "")
    return m.group(1) if m else ""


def _repeat_arrivals(
    instances: list[ScenarioInstance], seed: int
) -> dict[tuple[str, datetime], ScenarioInstance]:
    """Giao dịch lặp của kịch bản `repeat`: một khách tạo liên tục ở một tủ, cách nhau vài phút."""
    out: dict[tuple[str, datetime], ScenarioInstance] = {}
    for inst in instances:
        spec = inst.spec
        if spec.effect != "repeat" or inst.station_id is None:
            continue
        assert spec.repeat_count is not None and spec.repeat_gap_seconds is not None
        rng = substream(seed, "txn_repeat", inst.instance_id)
        t = inst.start
        for _ in range(rng.randint(*spec.repeat_count)):
            out[(inst.station_id, t)] = inst
            t = _after(rng, t, spec.repeat_gap_seconds)
    return out


def _poisson(rng: random.Random, lam: float) -> int:
    """Poisson nhỏ (λ ≲ 30) theo Knuth; dùng RNG tất định của simulator."""
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def generate_txn_arrivals(
    profile: ProfileSpec, common: CommonSpec, world: World, rates: dict[str, float]
) -> list[Arrival]:
    """Giờ bắt đầu giao dịch: số GD/ngày ~ Poisson(tốc độ trạm), phân bố theo giờ địa phương."""
    hours = list(range(24))
    weights = hourly_weights(common)
    out: list[Arrival] = []
    for st in world.stations:
        for day in range(profile.days):
            rng = substream(profile.seed_for_day(day), "txn_traffic", st.station_id, day)
            midnight = local_day_start_utc(profile, st.timezone, day)
            for _ in range(_poisson(rng, rates[st.station_id] * rng.uniform(0.85, 1.15))):
                hour = rng.choices(hours, weights)[0]
                out.append(
                    Arrival(
                        st.station_id,
                        day,
                        midnight + timedelta(hours=hour, seconds=rng.randrange(3600)),
                    )
                )
    out.sort(key=lambda a: (a.started_at, a.station_id))
    return out


def _plan_instances(
    profile_name: str,
    profile: ProfileSpec,
    world: World,
    scenarios: TxnScenariosFile,
    knowledge: Knowledge,
    rates: dict[str, float],
) -> list[ScenarioInstance]:
    base, days = profile.split.baseline_days, profile.days
    windows = [(0, base), (base, days)] if 0 < base < days else [(0, days)]
    instances: list[ScenarioInstance] = []
    for k, spec in enumerate(scenarios.scenarios):
        if spec.country is not None and spec.country not in profile.stations:
            continue  # vd `smoke` chỉ có VN
        n = spec.occurrences.get(profile_name, 0)
        per_window = [len(range(w, n, len(windows))) for w in range(len(windows))]
        for i in range(n):
            lo, hi = windows[i % len(windows)]
            j, m = i // len(windows), per_window[i % len(windows)]
            # Rải đều trong tập, lệch `k` ngày theo từng kịch bản để các kịch bản không trùng ngày.
            day = lo + (int((j + 0.5) * (hi - lo) / m) + k) % (hi - lo)
            rng = substream(profile.seed_for_day(day), "txn_scenario", spec.name, i)
            pool = [s for s in world.stations if spec.country in (None, s.country)]
            # Trạm xảy ra sự cố chọn theo trọng số lưu lượng: sự cố ở trạm gần như không có
            # giao dịch thì danh sách giao dịch không thể thấy (giới hạn này ghi trong báo cáo).
            station = rng.choices(pool, weights=[rates[s.station_id] for s in pool])[0]
            cabinet = rng.choice(world.cabinets_of_station[station.station_id])
            if spec.scope == "province":
                # Sụt traffic chỉ nhìn thấy ở tỉnh đủ đông: chọn trong 5 tỉnh nhiều giao dịch nhất.
                by_prov: dict[str, float] = {}
                for s in pool:
                    by_prov[province_of(s)] = by_prov.get(province_of(s), 0.0) + rates[s.station_id]
                top = sorted(by_prov, key=lambda p: (-by_prov[p], p))[:5]
                prov = rng.choices(top, weights=[by_prov[p] for p in top])[0]
                station = next(s for s in pool if province_of(s) == prov)
            if spec.scope in ("cabinet", "station", "province"):
                tz = station.timezone
            else:  # country / global: neo theo múi giờ đầu tiên của nước (global: VN nếu có)
                anchor = spec.country or ("VN" if "VN" in profile.stations else station.country)
                tz = knowledge.countries.countries[anchor].timezones[0]
            start = (
                local_day_start_utc(profile, tz, day)
                + timedelta(hours=rng.choice(_PEAK_START_HOURS))
                + timedelta(minutes=rng.randrange(0, 20))
            )
            instances.append(
                ScenarioInstance(
                    spec=spec,
                    instance_id=f"{spec.name}#{i + 1}",
                    start=start,
                    end=start + timedelta(minutes=spec.duration_minutes),
                    station_id=station.station_id if spec.scope in ("cabinet", "station") else None,
                    cabinet_id=cabinet.cabinet_id if spec.scope == "cabinet" else None,
                    country=spec.country if spec.scope == "country" else None,
                    province=province_of(station) if spec.scope == "province" else None,
                    day_index=day,
                )
            )
    # Nhiều kịch bản trùng lúc: phạm vi hẹp hơn (tủ → trạm → tỉnh → nước → toàn mạng) áp trước.
    order = {"cabinet": 0, "station": 1, "province": 2, "country": 3, "global": 4}
    return sorted(instances, key=lambda i: order[i.spec.scope])


def simulate_transactions(
    profile_name: str,
    knowledge: Knowledge | None = None,
    profiles: ProfilesFile | None = None,
    scenarios: TxnScenariosFile | None = None,
    error_table: ErrorTable | None = None,
) -> TxnResult:
    knowledge = knowledge or default_knowledge()
    profiles = profiles or load_profiles(PROFILES_PATH)
    scenarios = scenarios or load_txn_scenarios()
    table = error_table or default_error_table()
    unknown = scenarios.all_codes() - table.codes
    if unknown:
        raise SimulationCheckError(f"mã không có trong bảng mã lỗi chuẩn: {sorted(unknown)}")

    base_profile = profiles.profiles[profile_name]
    traffic = profiles.txn_traffic
    sites = load_real_sites()
    n_sites = base_profile.txn_sites or (len(sites) if sites else NATIONAL_SITES_FALLBACK)
    profile = base_profile.for_transactions(n_sites)
    common = profiles.common.model_copy(
        update={
            "cabinets_per_station": traffic.cabinets_per_site,
            "users_per_station": traffic.users_per_site,
            "slots_per_cabinet": 2,  # pin trong world chỉ dùng để lấy S/N; connector theo loại tủ
        }
    )
    seed = profile.seed
    world = build_world(profile, common, knowledge)
    reserved = frozenset(s.code for s in sites) if sites else frozenset()
    codes = station_codes(world, seed, mask=profile.mask_site_codes, reserved=reserved)
    rates = station_rates(world, traffic, sites)
    instances = _plan_instances(profile_name, profile, world, scenarios, knowledge, rates)
    timing = scenarios.timing

    cabinet_type: dict[str, str] = {}
    cabinet_code: dict[str, str] = {}
    cabinet_sn: dict[str, str] = {}
    for st in world.stations:
        for idx, info in enumerate(world.cabinets_of_station[st.station_id], start=1):
            rng = substream(seed, "txn_cabinet", info.cabinet_id)
            cabinet_type[info.cabinet_id] = rng.choice(sorted(_CABINET_TYPES))
            cabinet_code[info.cabinet_id] = f"{codes[st.station_id]}.{idx:03d}"
            cabinet_sn[info.cabinet_id] = f"BSS-{rng.randrange(10**13, 10**14)}"

    txns: list[SwapTransaction] = []
    truth: list[TxnGroundTruthRow] = []
    dropped: Counter[str] = Counter()
    arrivals = generate_txn_arrivals(profile, common, world, rates)
    repeats = _repeat_arrivals(instances, seed)
    if repeats:  # chỉ profile có kịch bản `repeat` (vd `pair`); profile khác giữ nguyên thứ tự
        users = world.users_by_country_state
        arrivals += [
            Arrival(st_id, inst.day_index, t) for (st_id, t), inst in sorted(repeats.items())
        ]
        arrivals.sort(key=lambda a: (a.started_at, a.station_id))
    provinces = {st.station_id: province_of(st) for st in world.stations}
    seq = 0
    for arr in arrivals:
        station = world.station_by_id[arr.station_id]
        rng = substream(profile.seed_for_day(arr.day_index), "txn", seq + 1)
        cab: CabinetInfo = rng.choice(world.cabinets_of_station[arr.station_id])
        ctype = cabinet_type[cab.cabinet_id]
        slot_in, slot_out = rng.sample(range(1, _CABINET_TYPES[ctype] + 1), 2)
        bat_in, bat_out = rng.sample(world.healthy_batteries, 2)
        user = rng.choice(world.users_by_country_state[(station.country, USER_NORMAL)])
        t0 = arr.started_at

        repeat = repeats.get((arr.station_id, t0))
        hit = repeat or next(
            (
                i
                for i in instances
                if i.matches(
                    t0, arr.station_id, cab.cabinet_id, station.country, provinces[arr.station_id]
                )
            ),
            None,
        )
        if hit is not None and hit.spec.effect == "drop" and rng.random() < hit.spec.fail_prob:
            dropped[hit.instance_id] += 1
            continue  # sụt traffic: giao dịch này không xảy ra
        seq += 1
        if repeat is not None:
            assert repeat.cabinet_id is not None
            cab = world.cabinet_info[repeat.cabinet_id]
            ctype = cabinet_type[cab.cabinet_id]
            slot_in, slot_out = rng.sample(range(1, _CABINET_TYPES[ctype] + 1), 2)
            pool_u = users[(station.country, USER_NORMAL)]
            user = substream(seed, "txn_repeat_user", repeat.instance_id).choice(pool_u)
        effect: Effect | None = None
        code: CodeSpec | None = None
        if hit is not None and hit.spec.effect == "drop":
            hit = None
        if hit is not None and (repeat is not None or rng.random() < hit.spec.fail_prob):
            effect = "fail" if hit.spec.effect == "repeat" else hit.spec.effect
            if effect == "fail":
                pool = hit.codes_at(t0)
                code = rng.choices(pool, [c.weight for c in pool])[0]
        else:
            hit = None
            if rng.random() < scenarios.background.fail_rate:
                effect = "fail"
                bg = scenarios.background.failures
                code = rng.choices(bg, [c.weight for c in bg])[0]

        has_battery_in = effect in (None, "slow", "stuck_battery_in") or (
            effect == "fail" and code is not None and code.after_battery_in
        )
        in_bounds = hit.spec.slow_seconds if effect == "slow" and hit else None
        t_in = _after(rng, t0, in_bounds or timing.battery_in_seconds) if has_battery_in else None
        t_out: datetime | None = None
        if effect in (None, "slow"):
            assert t_in is not None
            t_out = _after(rng, t_in, timing.battery_out_seconds)
            status, updated = TxnStatus.COMPLETED, t_out
        elif effect == "fail":
            status, updated = TxnStatus.FAILED, _after(rng, t_in or t0, timing.fail_seconds)
        elif effect == "stuck_battery_in":
            assert t_in is not None
            status, updated = TxnStatus.BATTERY_IN, t_in
        else:
            status, updated = TxnStatus.CREATED, t0

        emsp = _hex(seed, "emsp", seq, n=24)
        txns.append(
            SwapTransaction(
                station_id=codes[arr.station_id],
                cabinet_id=cabinet_code[cab.cabinet_id],
                cabinet_serial=cabinet_sn[cab.cabinet_id],
                emsp_txn_id=emsp,
                cabinet_txn_id=str(_CABINET_TXN_BASE + seq),
                started_at=t0,
                status=status,
                error_code=code.code if code else None,
                battery_in_id=battery_serial(seed, bat_in.battery_id) if t_in else None,
                battery_out_id=battery_serial(seed, bat_out.battery_id) if t_out else None,
                slot_in=str(slot_in) if t_in else None,
                soh_in=bat_in.soh if t_in else None,
                soc_in=float(rng.randint(5, 40)) if t_in else None,
                slot_out=str(slot_out) if t_out else None,
                soh_out=bat_out.soh if t_out else None,
                soc_out=float(rng.randint(90, 100)) if t_out else None,
                battery_in_at=t_in,
                battery_out_at=t_out,
                cabinet_type=ctype,
                country=station.country,
                timezone=station.timezone,
                updated_at=updated,
                user_id=user.user_id,
            )
        )
        is_failure = effect is not None and effect != "slow"
        row = table.lookup(code.code, ctype) if code else None
        truth.append(
            TxnGroundTruthRow(
                emsp_txn_id=emsp,
                is_failure=is_failure,
                kind=("FAILED" if effect == "fail" else "STUCK") if is_failure else None,
                error_code=code.code if code else None,
                level=row.level if row else None,
                scenario=hit.spec.name if hit else None,
                scenario_instance_id=hit.instance_id if hit else None,
                failure_at=updated if is_failure else None,
                country=station.country,
                split=profile.split.split_for(arr.day_index),
            )
        )
    cabinet_codes = [
        CabinetCodeRow(
            station_id=st.station_id,
            station_code=codes[st.station_id],
            cabinet_id=c.cabinet_id,
            cabinet_code=cabinet_code[c.cabinet_id],
            cabinet_serial=cabinet_sn[c.cabinet_id],
            cabinet_type=cabinet_type[c.cabinet_id],
        )
        for st in world.stations
        for c in world.cabinets_of_station[st.station_id]
    ]
    return TxnResult(
        transactions=txns,
        ground_truth=truth,
        instances=instances,
        cabinet_codes=cabinet_codes,
        stations=(
            [_masked_station(st, codes[st.station_id], seed) for st in world.stations]
            if profile.mask_site_codes
            else list(world.stations)
        ),
        telemetry=build_telemetry(
            profile, common.ambient_temp_c, world, cabinet_code, instances, txns
        ),
        dropped=dict(dropped),
    )


def slot_id_of(connector: int | str) -> str:
    """Connector 1..12 → mã khoang 2 chữ số (`01`..`12`) dùng trong telemetry và trang trạm."""
    return f"{int(connector):02d}"


def build_telemetry(
    profile: ProfileSpec,
    ambient: tuple[float, float],
    world: World,
    cabinet_code: dict[str, str],
    instances: list[ScenarioInstance],
    txns: list[SwapTransaction],
) -> pl.DataFrame:
    """Telemetry khớp với danh sách giao dịch: heartbeat mỗi `txn_heartbeat_minutes`/tủ + khoang
    quanh từng giao dịch. Heartbeat dựng bằng numpy (hàng triệu dòng ở profile lớn).
    Kịch bản có `overheat_temp_c` làm nhiệt độ tủ tăng dần trong đúng khoảng thời gian sự cố.
    """
    cab_ids: list[str] = []
    slot_ids: list[str | None] = []
    doors: list[str | None] = []
    detected: list[bool | None] = []
    socs: list[float | None] = []
    temps: list[float] = []
    times: list[datetime] = []
    base_temp: dict[str, float] = {}
    step = profile.txn_heartbeat_minutes
    n = profile.days * 24 * 60 // step
    offsets = np.arange(n, dtype="int64") * step * 60_000_000  # micro giây
    hb_cab: list[np.ndarray] = []
    hb_temp: list[np.ndarray] = []
    hb_time: list[np.ndarray] = []
    for cab in world.cabinets:
        code = cabinet_code[cab.cabinet_id]
        rng = substream(profile.seed, "txn_heartbeat", cab.cabinet_id)
        base = rng.uniform(*ambient)
        base_temp[code] = base
        if not profile.txn_telemetry:
            continue
        start = local_day_start_utc(profile, cab.timezone, 0)
        t_us = int(start.timestamp() * 1_000_000) + offsets
        gen = np.random.default_rng(rng.getrandbits(64))
        temp = base + gen.normal(0.0, 0.8, n)
        for i in instances:
            if i.cabinet_id != cab.cabinet_id or not i.spec.overheat_temp_c:
                continue
            lo, hi = i.spec.overheat_temp_c
            a, b = int(i.start.timestamp() * 1e6), int(i.end.timestamp() * 1e6)
            mask = (t_us >= a) & (t_us < b)
            frac = (t_us[mask] - a) / (b - a)
            temp[mask] = lo + (hi - lo) * frac + gen.normal(0.0, 0.5, int(mask.sum()))
        hb_cab.append(np.full(n, code, dtype=object))
        hb_temp.append(np.round(temp, 1))
        hb_time.append(t_us)
    for txn in txns:
        assert txn.cabinet_id is not None
        events: list[tuple[str | None, datetime | None, bool, float | None]] = [
            (txn.slot_in, txn.battery_in_at, True, txn.soc_in),  # pin cũ vào khoang, bắt đầu sạc
            (txn.slot_out, txn.battery_out_at, False, None),  # pin mới rời khoang
        ]
        for slot, at, has_battery, soc in events:
            if slot is None or at is None:
                continue
            cab_ids.append(txn.cabinet_id)
            slot_ids.append(slot_id_of(slot))
            doors.append("CLOSED")
            detected.append(has_battery)
            socs.append(soc)
            temps.append(round(base_temp[txn.cabinet_id], 1))
            times.append(at)
    schema: dict[str, pl.DataType] = {
        "cabinet_id": pl.String(),
        "slot_id": pl.String(),
        "door_state": pl.String(),
        "battery_detected": pl.Boolean(),
        "soc": pl.Float64(),
        "temperature": pl.Float64(),
        "heartbeat_at": pl.Datetime("us", "UTC"),
        "hw_error_code": pl.String(),
    }
    slots_df = pl.DataFrame(
        {
            "cabinet_id": cab_ids,
            "slot_id": slot_ids,
            "door_state": doors,
            "battery_detected": detected,
            "soc": socs,
            "temperature": temps,
            "heartbeat_at": times,
            "hw_error_code": [None] * len(times),
        },
        schema=schema,
    )
    if hb_cab:
        hb = (
            pl.DataFrame(
                {
                    "cabinet_id": np.concatenate(hb_cab).astype(str),
                    "temperature": np.concatenate(hb_temp),
                    "heartbeat_at": np.concatenate(hb_time),
                }
            )
            .with_columns(
                pl.col("heartbeat_at").cast(pl.Datetime("us")).dt.replace_time_zone("UTC"),
                pl.lit(None, dtype=pl.String).alias("slot_id"),
                pl.lit(None, dtype=pl.String).alias("door_state"),
                pl.lit(None, dtype=pl.Boolean).alias("battery_detected"),
                pl.lit(None, dtype=pl.Float64).alias("soc"),
                pl.lit(None, dtype=pl.String).alias("hw_error_code"),
            )
            .select(list(schema))
        )
        slots_df = pl.concat([hb, slots_df])
    return slots_df.sort(["heartbeat_at", "cabinet_id", "slot_id"], nulls_last=False)


def scenario_instances_frame(result: TxnResult, profile: ProfileSpec) -> pl.DataFrame:
    """Đáp án mức lần lặp kịch bản (cho đối tác ở I1): thời gian, phạm vi, mã vận hành.

    `n_affected`: số giao dịch thuộc lần lặp (kịch bản `drop`: số giao dịch bị mất).
    """
    st_code = {c.station_id: c.station_code for c in result.cabinet_codes}
    cab_code = {c.cabinet_id: c.cabinet_code for c in result.cabinet_codes}
    affected = Counter(
        g.scenario_instance_id for g in result.ground_truth if g.scenario_instance_id
    )
    failures = Counter(
        g.scenario_instance_id
        for g in result.ground_truth
        if g.scenario_instance_id and g.is_failure
    )
    rows = sorted(result.instances, key=lambda i: (i.start, i.instance_id))
    return pl.DataFrame(
        {
            "scenario_instance_id": [i.instance_id for i in rows],
            "scenario": [i.spec.name for i in rows],
            "scope": [i.spec.scope for i in rows],
            "effect": [i.spec.effect for i in rows],
            "is_failure": [i.spec.is_failure for i in rows],
            "start": [i.start for i in rows],
            "end": [i.end for i in rows],
            "station_id": [st_code.get(i.station_id or "") for i in rows],
            "cabinet_id": [cab_code.get(i.cabinet_id or "") for i in rows],
            "province": [i.province for i in rows],
            "country": [i.country or "VN" for i in rows],
            "split": [profile.split.split_for(i.day_index) for i in rows],
            "n_affected": [
                affected[i.instance_id] + result.dropped.get(i.instance_id, 0) for i in rows
            ],
            "n_failures": [failures[i.instance_id] for i in rows],
        },
        schema_overrides={"province": pl.String, "station_id": pl.String, "cabinet_id": pl.String},
    )


def check_transactions(profile_name: str, result: TxnResult, table: ErrorTable) -> None:
    """Bất biến của mock; `eval` còn phải đủ số lần lặp trong tập test (CLAUDE.md §10)."""
    ids = [t.emsp_txn_id for t in result.transactions]
    if len(set(ids)) != len(ids):
        raise SimulationCheckError("emsp_txn_id bị trùng")
    outside = {t.error_code for t in result.transactions if t.error_code} - table.codes
    if outside:
        raise SimulationCheckError(f"giao dịch dùng mã ngoài bảng chuẩn: {sorted(outside)}")
    for t in result.transactions:
        if (t.status is TxnStatus.FAILED) != (t.error_code is not None):
            raise SimulationCheckError(f"{t.emsp_txn_id}: Thất bại ⇔ có mã lỗi")
        if t.status is TxnStatus.COMPLETED and not (t.battery_in_at and t.battery_out_at):
            raise SimulationCheckError(f"{t.emsp_txn_id}: Hoàn thành phải có pin vào và pin ra")
    if profile_name != "eval":
        return
    hits: Counter[str] = Counter()
    for g in result.ground_truth:
        if g.split == "test" and g.scenario and g.scenario_instance_id:
            hits[g.scenario_instance_id] += 1
    per_scenario: Counter[str] = Counter(i.split("#")[0] for i in hits)
    short = sorted(
        s.name
        for s in {i.spec.name: i.spec for i in result.instances}.values()
        if per_scenario[s.name] < 2
    )
    if short:
        raise SimulationCheckError(f"eval: kịch bản có < 2 lần lặp trong tập test: {short}")


async def generate_transactions(
    profile_name: str,
    out_dir: Path,
    *,
    knowledge: Knowledge | None = None,
    profiles: ProfilesFile | None = None,
    scenarios: TxnScenariosFile | None = None,
) -> TxnManifest:
    profiles = profiles or load_profiles(PROFILES_PATH)
    table = default_error_table()
    result = await asyncio.to_thread(
        simulate_transactions, profile_name, knowledge, profiles, scenarios, table
    )
    check_transactions(profile_name, result, table)

    def write() -> dict[str, str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = _write_table(out_dir, TRANSACTIONS_TABLE, result.transactions, jsonl=True)
        _write_table(out_dir, STATION_CODES_TABLE, result.cabinet_codes, jsonl=False)
        _write_table(out_dir, TXN_STATIONS_TABLE, result.stations, jsonl=False)
        result.telemetry.write_parquet(out_dir / TXN_TELEMETRY_FILE)
        gt_path = out_dir / TXN_GROUND_TRUTH
        _frame(result.ground_truth).write_csv(gt_path)
        inst_path = out_dir / TXN_SCENARIO_INSTANCES
        scenario_instances_frame(result, profile).write_csv(inst_path)
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (*paths, gt_path, inst_path)
            if p.suffix != ".parquet"  # parquet không ổn định từng byte giữa các phiên bản
        }

    profile = profiles.profiles[profile_name]
    sha = await asyncio.to_thread(write)
    failures = [g for g in result.ground_truth if g.is_failure]
    by_scenario = Counter(f"{g.scenario or 'background'}|{g.split}" for g in failures)
    affected = Counter(
        g.scenario_instance_id for g in result.ground_truth if g.scenario_instance_id
    )
    manifest = TxnManifest(
        profile=profile_name,
        seed=profile.seed,
        test_seed=profile.test_seed,
        n_transactions=len(result.transactions),
        n_failures=len(failures),
        sha256=sha,
        by_scenario=dict(sorted(by_scenario.items())),
        instances={
            i.instance_id: affected.get(i.instance_id, 0) + result.dropped.get(i.instance_id, 0)
            for i in result.instances
        },
    )
    (out_dir / TXN_MANIFEST).write_bytes(
        orjson.dumps(manifest.model_dump(mode="json"), option=orjson.OPT_INDENT_2) + b"\n"
    )
    return manifest
