"""Traffic theo GIỜ ĐỊA PHƯƠNG của trạm (CLAUDE.md §10): cao điểm 7–9h và 17–19h local."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ase.knowledge.loader import Knowledge
from ase.simulator.entities import World
from ase.simulator.profiles import CommonSpec, ProfileSpec
from ase.simulator.rng import substream


@dataclass(frozen=True)
class Arrival:
    station_id: str
    day_index: int  # ngày địa phương thứ n của trạm (0-based)
    started_at: datetime  # UTC


def hourly_weights(common: CommonSpec) -> list[float]:
    """Trọng số 24 giờ local: cao điểm > bình thường > đêm."""
    weights: list[float] = []
    for h in range(24):
        w = 1.0
        if common.night_hours_local[0] <= h < common.night_hours_local[1]:
            w = common.night_weight
        if any(lo <= h < hi for lo, hi in common.peak_hours_local):
            w = common.peak_weight
        weights.append(w)
    return weights


def local_day_start_utc(profile: ProfileSpec, timezone: str, day_index: int) -> datetime:
    """00:00 giờ địa phương của ngày thứ `day_index`, quy ra UTC."""
    d = profile.start_date + timedelta(days=day_index)
    return datetime.combine(d, time(0), tzinfo=ZoneInfo(timezone)).astimezone(UTC)


def station_shares(profile: ProfileSpec, knowledge: Knowledge) -> dict[str, float]:
    """Tỷ lệ lưu lượng mỗi nước chia đều cho các trạm của nước đó, chuẩn hoá về tổng 1."""
    weights = {c: knowledge.countries.countries[c].traffic_weight for c in profile.stations}
    total = sum(weights.values())
    return {c: weights[c] / total / n for c, n in profile.stations.items()}


def generate_arrivals(
    profile: ProfileSpec, common: CommonSpec, world: World, knowledge: Knowledge
) -> list[Arrival]:
    hours = list(range(24))
    weights = hourly_weights(common)
    share_by_country = station_shares(profile, knowledge)
    arrivals: list[Arrival] = []
    for station in world.stations:
        share = share_by_country[station.country]
        for day in range(profile.days):
            rng = substream(profile.seed_for_day(day), "traffic", station.station_id, day)
            count = max(1, round(profile.sessions_per_day * share * rng.uniform(0.9, 1.1)))
            midnight = local_day_start_utc(profile, station.timezone, day)
            for _ in range(count):
                hour = rng.choices(hours, weights)[0]
                offset = timedelta(hours=hour, seconds=rng.randrange(3600))
                arrivals.append(Arrival(station.station_id, day, midnight + offset))
    arrivals.sort(key=lambda a: (a.started_at, a.station_id))
    return arrivals
