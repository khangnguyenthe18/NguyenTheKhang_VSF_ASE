"""Master data: trạm, tủ, khoang, pin, user. Sinh deterministic theo profile + seed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from ase.common import ids
from ase.common.models import Battery, Cabinet, Slot, Station, User
from ase.knowledge.loader import Knowledge
from ase.simulator.profiles import CommonSpec, ProfileSpec
from ase.simulator.rng import substream

USER_NORMAL = "normal"
USER_DEBT = "debt"
USER_EXPIRED = "expired"


@dataclass(frozen=True)
class CabinetInfo:
    cabinet_id: str
    station_id: str
    slot_ids: tuple[str, ...]
    battery_by_slot: dict[str, str]  # pin đang nằm sẵn trong khoang (khoang xuất pin)


@dataclass
class World:
    stations: list[Station] = field(default_factory=list)
    cabinets: list[Cabinet] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)
    batteries: list[Battery] = field(default_factory=list)
    users: list[User] = field(default_factory=list)
    station_by_id: dict[str, Station] = field(default_factory=dict)
    cabinets_of_station: dict[str, list[CabinetInfo]] = field(default_factory=dict)
    cabinet_info: dict[str, CabinetInfo] = field(default_factory=dict)
    battery_by_id: dict[str, Battery] = field(default_factory=dict)
    healthy_batteries: list[Battery] = field(default_factory=list)
    low_soh_batteries: list[Battery] = field(default_factory=list)
    user_by_id: dict[str, User] = field(default_factory=dict)
    users_by_country_state: dict[tuple[str, str], list[User]] = field(default_factory=dict)
    user_state: dict[str, str] = field(default_factory=dict)


def sim_start(profile: ProfileSpec) -> datetime:
    """Mốc UTC 00:00 của `start_date` (dùng cho install_date/package_expiry)."""
    return datetime.combine(profile.start_date, time(0), tzinfo=UTC)


def build_world(profile: ProfileSpec, common: CommonSpec, knowledge: Knowledge) -> World:
    seed = profile.seed
    start = sim_start(profile)
    world = World()

    lot_ids = [f"LOT-{chr(ord('A') + i)}" for i in range(common.battery_lots)]
    # LOT-F (lô cuối) là lô SOH thấp (< 70%) để phục vụ scenario `low_soh_lot`.
    low_lot = lot_ids[-1]
    lot_soh_mean = {
        lot: (63.0 if lot == low_lot else 96.0 - 1.5 * i) for i, lot in enumerate(lot_ids)
    }

    battery_seq = 0
    for country, n_stations in profile.stations.items():
        spec = knowledge.countries.countries[country]
        tz_city_idx: dict[str, int] = {}
        for n in range(1, n_stations + 1):
            rng = substream(seed, "entities", country, n)
            station_id = f"{country}-ST-{n:03d}"
            tz = spec.timezones[(n - 1) % len(spec.timezones)]  # xoay vòng múi giờ (ID: WIB/WITA)
            model_gen = rng.choice(["G1", "G2"])
            install = start - timedelta(days=rng.randint(120, 600))
            geo_rng = substream(seed, "geo", station_id)
            tz_cities = spec.cities_for_timezone(tz)
            if tz_cities:
                c_idx = tz_city_idx.get(tz, 0)
                tz_city_idx[tz] = c_idx + 1
                city = tz_cities[c_idx % len(tz_cities)]
                lat = round(city.lat + geo_rng.uniform(-0.02, 0.02), 6)
                lon = round(city.lon + geo_rng.uniform(-0.02, 0.02), 6)
            else:
                lat, lon = 0.0, 0.0
            station = Station(
                station_id=station_id,
                country=country,
                timezone=tz,
                model_gen=model_gen,
                install_date=install,
                lat=lat,
                lon=lon,
            )
            world.stations.append(station)
            world.station_by_id[station_id] = station
            world.cabinets_of_station[station_id] = []

            lo, hi = common.cabinets_per_station
            n_cabinets = rng.randint(lo, hi)
            if country == "VN" and n == 1:
                n_cabinets = max(3, n_cabinets)  # đảm bảo luôn có CAB-03 cho scenario slot_door_jam
            for c in range(1, n_cabinets + 1):
                cabinet_id = f"{station_id}-CAB-{c:02d}"
                world.cabinets.append(
                    Cabinet(
                        cabinet_id=cabinet_id,
                        station_id=station_id,
                        country=country,
                        timezone=tz,
                        model_gen=model_gen,
                        install_date=install,
                    )
                )
                slot_ids = tuple(f"{s:02d}" for s in range(1, common.slots_per_cabinet + 1))
                battery_by_slot: dict[str, str] = {}
                for slot_id in slot_ids:
                    world.slots.append(
                        Slot(
                            slot_id=slot_id,
                            cabinet_id=cabinet_id,
                            station_id=station_id,
                            country=country,
                            timezone=tz,
                            model_gen=model_gen,
                            install_date=install,
                        )
                    )
                    battery_seq += 1
                    # Pin trong khoang mặc định thuộc các lô khỏe (LOT-A..LOT-E, SOH >= 80%)
                    lot = rng.choice(lot_ids[:-1])
                    soh = min(100.0, max(80.0, rng.gauss(lot_soh_mean[lot], 2.0)))
                    battery = Battery(
                        battery_id=f"BAT-{battery_seq:05d}",
                        lot_id=lot,
                        soh=round(soh, 1),
                        cycle_count=int((100 - soh) * 25 + rng.randint(0, 80)),
                    )
                    world.batteries.append(battery)
                    world.healthy_batteries.append(battery)
                    world.battery_by_id[battery.battery_id] = battery
                    battery_by_slot[slot_id] = battery.battery_id
                info = CabinetInfo(cabinet_id, station_id, slot_ids, battery_by_slot)
                world.cabinets_of_station[station_id].append(info)
                world.cabinet_info[cabinet_id] = info

    # Tạo lô pin SOH thấp (LOT-F, SOH < 70%) tách riêng chỉ dành cho scenario `low_soh_lot`
    low_rng = substream(seed, "entities", "low_soh_lot")
    for _ in range(max(12, len(world.cabinets) * 2)):
        battery_seq += 1
        soh = min(68.5, max(58.0, low_rng.gauss(lot_soh_mean[low_lot], 2.0)))
        low_bat = Battery(
            battery_id=f"BAT-{battery_seq:05d}",
            lot_id=low_lot,
            soh=round(soh, 1),
            cycle_count=int((100 - soh) * 25 + low_rng.randint(20, 100)),
        )
        world.batteries.append(low_bat)
        world.low_soh_batteries.append(low_bat)
        world.battery_by_id[low_bat.battery_id] = low_bat

    _build_users(world, profile, common, knowledge, start)
    return world


def _build_users(
    world: World,
    profile: ProfileSpec,
    common: CommonSpec,
    knowledge: Knowledge,
    start: datetime,
) -> None:
    for country, n_stations in profile.stations.items():
        spec = knowledge.countries.countries[country]
        packages = [p for p in spec.pricing.packages if p in common.package_mix]
        weights = [common.package_mix[p] for p in packages]
        rng = substream(profile.seed, "users", country)
        for i in range(n_stations * common.users_per_station):
            roll = rng.random()
            if roll < common.debt_user_share:
                state = USER_DEBT
            elif roll < common.debt_user_share + common.expired_user_share:
                state = USER_EXPIRED
            else:
                state = USER_NORMAL
            if state == USER_EXPIRED:
                expiry = start - timedelta(days=rng.randint(1, 60))
            else:
                expiry = start + timedelta(days=profile.days + rng.randint(30, 400))
            user = User(
                user_id=ids.hash_user_id(f"{country}-{i}"),
                country=country,
                package_type=rng.choices(packages, weights)[0],
                package_expiry=expiry,
                debt_flag=state == USER_DEBT,
            )
            world.users.append(user)
            world.user_by_id[user.user_id] = user
            world.user_state[user.user_id] = state
            world.users_by_country_state.setdefault((country, state), []).append(user)
