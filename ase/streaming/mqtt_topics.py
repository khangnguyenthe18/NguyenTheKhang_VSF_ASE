"""Quy ước topic MQTT: `bss/{country}/{station_id}/{cabinet_id}/(events|telemetry)` (§5)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import polars as pl

MqttTopicKind = Literal["events", "telemetry"]

EVENTS_SUBSCRIBE_TOPIC = "bss/+/+/+/events"
TELEMETRY_SUBSCRIBE_TOPIC = "bss/+/+/+/telemetry"
UNKNOWN_TOKEN = "UNKNOWN"


@dataclass(frozen=True)
class ParsedMqttTopic:
    country: str
    station_id: str
    cabinet_id: str
    kind: MqttTopicKind


def _clean_segment(val: str | None) -> str:
    if not val:
        return UNKNOWN_TOKEN
    s = val.strip()
    if not s or "/" in s or "+" in s or "#" in s:
        raise ValueError(f"invalid MQTT topic segment: {val!r}")
    return s


def build_event_topic(country: str | None, station_id: str, cabinet_id: str | None) -> str:
    c = _clean_segment(country)
    st = _clean_segment(station_id)
    cab = _clean_segment(cabinet_id)
    return f"bss/{c}/{st}/{cab}/events"


def build_telemetry_topic(country: str | None, station_id: str | None, cabinet_id: str) -> str:
    c = _clean_segment(country)
    st = _clean_segment(station_id)
    cab = _clean_segment(cabinet_id)
    return f"bss/{c}/{st}/{cab}/telemetry"


def parse_mqtt_topic(topic: str) -> ParsedMqttTopic | None:
    parts = topic.strip().split("/")
    if len(parts) != 5 or parts[0] != "bss":
        return None
    _, country, station_id, cabinet_id, kind = parts
    if kind not in ("events", "telemetry"):
        return None
    for seg in (country, station_id, cabinet_id):
        if not seg or "+" in seg or "#" in seg:
            return None
    typed_kind: MqttTopicKind = "events" if kind == "events" else "telemetry"
    return ParsedMqttTopic(
        country=country,
        station_id=station_id,
        cabinet_id=cabinet_id,
        kind=typed_kind,
    )


def load_station_country_map(profile_dir: Path) -> dict[str, str]:
    path = profile_dir / "stations.parquet"
    if not path.exists():
        return {}
    df = pl.read_parquet(path, columns=["station_id", "country"])
    return {str(r["station_id"]): str(r["country"]) for r in df.iter_rows(named=True)}


def load_cabinet_location_map(profile_dir: Path) -> dict[str, tuple[str, str]]:
    """Map `cabinet_id -> (country, station_id)` từ `cabinets.parquet`."""
    path = profile_dir / "cabinets.parquet"
    if not path.exists():
        return {}
    df = pl.read_parquet(path, columns=["cabinet_id", "country", "station_id"])
    return {
        str(r["cabinet_id"]): (str(r["country"]), str(r["station_id"]))
        for r in df.iter_rows(named=True)
    }


def resolve_country(station_id: str, station_countries: Mapping[str, str]) -> str:
    if station_id in station_countries:
        return station_countries[station_id]
    return UNKNOWN_TOKEN
