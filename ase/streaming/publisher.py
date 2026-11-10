"""Publisher replay `ase.swap.events.jsonl` và `cabinet_telemetry.parquet` (STREAMING_PLAN §5).

- Đọc `ase.swap.events.jsonl` **đúng thứ tự dòng**:
  - Bước 1–5 (`1_open_app` .. `5_payment`) → ghi thẳng vào Kafka (`ase.swap.events`).
  - Bước 6–7 (`6_return_battery`, `7_receive_battery`) → publish vào MQTT
    (`bss/{country}/{station_id}/{cabinet_id}/events`).
- Đọc `cabinet_telemetry.parquet` (sort theo `heartbeat_at`), trộn theo thời gian cùng luồng event
  và publish vào MQTT (`bss/{country}/{station_id}/{cabinet_id}/telemetry`).
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import aiomqtt
import polars as pl

from ase.common.config import Settings, get_settings
from ase.common.models import CabinetTelemetry, Step, SwapEvent
from ase.common.time import ensure_utc
from ase.streaming.codec import encode_event, encode_telemetry
from ase.streaming.kafka_io import KafkaSink
from ase.streaming.mqtt_topics import (
    UNKNOWN_TOKEN,
    build_event_topic,
    build_telemetry_topic,
    load_cabinet_location_map,
    load_station_country_map,
    resolve_country,
)

CABINET_STEPS: frozenset[Step] = frozenset({Step.RETURN_BATTERY, Step.RECEIVE_BATTERY})
DEFAULT_SPEED: float = 18000.0
DEFAULT_PATH_YIELD_MS: float = 25.0


@dataclass
class PublishStats:
    kafka_events: int = 0
    mqtt_events: int = 0
    mqtt_telemetry: int = 0


_TELEMETRY_BATCH_ROWS = 20_000


def _iter_sorted_telemetry(profile_dir: Path) -> Iterator[CabinetTelemetry]:
    """Telemetry sắp theo `heartbeat_at`, tạo model theo từng lô.

    Vì sao không nạp hết thành list: profile `demo` có hàng triệu dòng heartbeat; tạo trước toàn
    bộ model Pydantic mất ~17 s và ~5 GB RAM trước khi phát được message đầu tiên.
    """
    path = profile_dir / "cabinet_telemetry.parquet"
    if not path.exists():
        return
    df = pl.read_parquet(path).sort("heartbeat_at", maintain_order=True)
    for chunk in df.iter_slices(_TELEMETRY_BATCH_ROWS):
        for row in chunk.iter_rows(named=True):
            yield CabinetTelemetry.model_validate(row)


async def run_publisher(
    profile_dir: Path,
    *,
    settings: Settings | None = None,
    speed: float | None = DEFAULT_SPEED,
    path_yield_ms: float = DEFAULT_PATH_YIELD_MS,
    include_telemetry: bool = True,
    kafka_sink: KafkaSink | None = None,
    mqtt_client: Any | None = None,
) -> PublishStats:
    cfg = settings or get_settings()
    station_countries = load_station_country_map(profile_dir)
    cabinet_locations = load_cabinet_location_map(profile_dir)
    tel_iter = _iter_sorted_telemetry(profile_dir) if include_telemetry else iter(())
    next_tel: CabinetTelemetry | None = next(tel_iter, None)

    owns_sink = kafka_sink is None
    sink = kafka_sink if kafka_sink is not None else KafkaSink(settings=cfg)
    await sink.start()

    stats = PublishStats()
    prev_ts: datetime | None = None
    last_was_mqtt_event = False

    async def _pace(
        ts: datetime,
        *,
        is_kafka_after_mqtt: bool = False,
    ) -> None:
        nonlocal prev_ts
        t_utc = ensure_utc(ts)
        sleep_s = 0.0
        if speed is not None and speed > 0 and prev_ts is not None and t_utc > prev_ts:
            sleep_s = (t_utc - prev_ts).total_seconds() / speed
        if is_kafka_after_mqtt and speed is not None and speed > 0 and path_yield_ms > 0:
            # Nhường tối thiểu path_yield_ms khi chuyển từ bước 6–7 (MQTT) sang bước 1–5 (Kafka)
            # để bản tin MQTT kịp qua bridge vào Kafka trước khi phiên sau đẩy SimClock.
            sleep_s = max(sleep_s, path_yield_ms / 1000.0)
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        prev_ts = t_utc if prev_ts is None else max(prev_ts, t_utc)

    async def _publish_telemetry(client: Any, tel: CabinetTelemetry) -> None:
        await _pace(tel.heartbeat_at)
        loc = cabinet_locations.get(tel.cabinet_id)
        if loc is not None:
            country, station_id = loc
        else:
            country, station_id = UNKNOWN_TOKEN, UNKNOWN_TOKEN
        topic = build_telemetry_topic(country, station_id, tel.cabinet_id)
        await client.publish(topic, payload=encode_telemetry(tel), qos=0)
        stats.mqtt_telemetry += 1

    async def _publish_all(
        ev_client: Any,
        tel_client: Any,
    ) -> None:
        nonlocal next_tel, last_was_mqtt_event
        events_path = profile_dir / "ase.swap.events.jsonl"
        with events_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                ev = SwapEvent.model_validate_json(line)
                ev_utc = ensure_utc(ev.occurred_at)

                while next_tel is not None and ensure_utc(next_tel.heartbeat_at) <= ev_utc:
                    await _publish_telemetry(tel_client, next_tel)
                    next_tel = next(tel_iter, None)

                if ev.step in CABINET_STEPS:
                    await _pace(ev_utc)
                    country = resolve_country(ev.station_id, station_countries)
                    topic = build_event_topic(country, ev.station_id, ev.cabinet_id)
                    await ev_client.publish(topic, payload=encode_event(ev), qos=cfg.mqtt_qos)
                    stats.mqtt_events += 1
                    last_was_mqtt_event = True
                else:
                    await _pace(ev_utc, is_kafka_after_mqtt=last_was_mqtt_event)
                    await sink.write(ev)
                    stats.kafka_events += 1
                    last_was_mqtt_event = False

        while next_tel is not None:
            await _publish_telemetry(tel_client, next_tel)
            next_tel = next(tel_iter, None)

    try:
        if mqtt_client is not None:
            await _publish_all(mqtt_client, mqtt_client)
        else:
            async with (
                aiomqtt.Client(hostname=cfg.mqtt_host, port=cfg.mqtt_port) as ev_client,
                aiomqtt.Client(hostname=cfg.mqtt_host, port=cfg.mqtt_port) as tel_client,
            ):
                await _publish_all(ev_client, tel_client)
    finally:
        if owns_sink:
            await sink.close()

    return stats


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m ase.streaming.publisher", description=__doc__)
    p.add_argument("--profile", default="smoke")
    p.add_argument(
        "--speed",
        type=float,
        default=DEFAULT_SPEED,
        help="Hệ số tốc độ phát theo occurred_at (0 = không sleep)",
    )
    p.add_argument(
        "--path-yield-ms",
        type=float,
        default=DEFAULT_PATH_YIELD_MS,
        help="Nhường tối thiểu (ms) khi chuyển từ bước MQTT sang Kafka (0 = tắt)",
    )
    p.add_argument("--no-telemetry", action="store_true", help="Không phát cabinet_telemetry")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = get_settings()
    profile_dir = settings.profile_dir(args.profile)
    eff_speed = None if args.speed is not None and args.speed <= 0 else args.speed
    stats = asyncio.run(
        run_publisher(
            profile_dir,
            settings=settings,
            speed=eff_speed,
            path_yield_ms=args.path_yield_ms,
            include_telemetry=not args.no_telemetry,
        )
    )
    print(
        f"[publisher] profile={args.profile} speed={args.speed} "
        f"path_yield_ms={args.path_yield_ms}: "
        f"kafka_events={stats.kafka_events}, "
        f"mqtt_events={stats.mqtt_events}, "
        f"mqtt_telemetry={stats.mqtt_telemetry}"
    )


if __name__ == "__main__":
    main()
