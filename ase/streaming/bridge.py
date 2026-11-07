"""Bridge MQTT → Kafka (`bss/+/+/+/events` và `bss/+/+/+/telemetry`) (STREAMING_PLAN §5).

- Subscribe `bss/+/+/+/events` và `bss/+/+/+/telemetry`.
- Decode + validate bằng Pydantic (`SwapEvent`, `CabinetTelemetry`), giữ nguyên `event_id`.
- Payload sai hoặc topic không hợp lệ: log + đếm + bỏ qua, không crash vòng lặp.
- Mất kết nối MQTT: exponential backoff + jitter rồi kết nối lại.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import aiomqtt
import structlog

from ase.common.config import Settings, get_settings
from ase.streaming.codec import decode_event, decode_telemetry
from ase.streaming.kafka_io import KafkaSink
from ase.streaming.mqtt_topics import (
    EVENTS_SUBSCRIBE_TOPIC,
    TELEMETRY_SUBSCRIBE_TOPIC,
    parse_mqtt_topic,
)

log = structlog.get_logger(__name__)


@dataclass
class BridgeStats:
    events_forwarded: int = 0
    telemetry_forwarded: int = 0
    invalid_messages: int = 0
    reconnects: int = 0


async def process_mqtt_message(msg: Any, sink: KafkaSink, stats: BridgeStats) -> None:
    topic_str = str(getattr(msg, "topic", ""))
    parsed = parse_mqtt_topic(topic_str)
    if parsed is None:
        stats.invalid_messages += 1
        log.warning("bridge_invalid_topic", topic=topic_str)
        return

    raw = getattr(msg, "payload", None)
    if not isinstance(raw, (bytes, str)):
        stats.invalid_messages += 1
        log.warning("bridge_invalid_payload_type", topic=topic_str, type=type(raw).__name__)
        return

    if parsed.kind == "events":
        ev, err = decode_event(raw)
        if ev is None:
            stats.invalid_messages += 1
            log.warning("bridge_invalid_event", topic=topic_str, reason=err)
            return
        await sink.write(ev)
        stats.events_forwarded += 1
    else:
        tel, err = decode_telemetry(raw)
        if tel is None:
            stats.invalid_messages += 1
            log.warning("bridge_invalid_telemetry", topic=topic_str, reason=err)
            return
        await sink.write_telemetry(tel)
        stats.telemetry_forwarded += 1


async def consume_mqtt_stream(
    messages: AsyncIterator[Any],
    sink: KafkaSink,
    stats: BridgeStats,
    *,
    max_messages: int | None = None,
) -> None:
    processed = 0
    async for msg in messages:
        await process_mqtt_message(msg, sink, stats)
        processed += 1
        if max_messages is not None and processed >= max_messages:
            break


async def run_bridge(
    *,
    settings: Settings | None = None,
    kafka_sink: KafkaSink | None = None,
    mqtt_client: Any | None = None,
    client_factory: Callable[[], Any] | None = None,
    max_messages: int | None = None,
    stop_event: asyncio.Event | None = None,
    stats: BridgeStats | None = None,
) -> BridgeStats:
    cfg = settings or get_settings()
    owns_sink = kafka_sink is None
    sink = kafka_sink if kafka_sink is not None else KafkaSink(settings=cfg)
    await sink.start()
    out_stats = stats if stats is not None else BridgeStats()

    try:
        if mqtt_client is not None:
            if hasattr(mqtt_client, "subscribe"):
                await mqtt_client.subscribe(EVENTS_SUBSCRIBE_TOPIC, qos=cfg.mqtt_qos)
                await mqtt_client.subscribe(TELEMETRY_SUBSCRIBE_TOPIC, qos=cfg.mqtt_qos)
            msgs = mqtt_client.messages.__aiter__()
            await consume_mqtt_stream(msgs, sink, out_stats, max_messages=max_messages)
            return out_stats

        attempt = 0
        while stop_event is None or not stop_event.is_set():
            try:
                factory = client_factory or (
                    lambda: aiomqtt.Client(hostname=cfg.mqtt_host, port=cfg.mqtt_port)
                )
                async with factory() as client:
                    attempt = 0
                    if client_factory is not None:
                        await client.subscribe(EVENTS_SUBSCRIBE_TOPIC, qos=cfg.mqtt_qos)
                        await client.subscribe(TELEMETRY_SUBSCRIBE_TOPIC, qos=cfg.mqtt_qos)
                        log.info(
                            "bridge_subscribed",
                            mqtt=f"{cfg.mqtt_host}:{cfg.mqtt_port}",
                            kafka=cfg.kafka_bootstrap,
                        )
                        await consume_mqtt_stream(
                            client.messages.__aiter__(),
                            sink,
                            out_stats,
                            max_messages=max_messages,
                        )
                    else:
                        async with aiomqtt.Client(
                            hostname=cfg.mqtt_host, port=cfg.mqtt_port
                        ) as tel_client:
                            await client.subscribe(EVENTS_SUBSCRIBE_TOPIC, qos=cfg.mqtt_qos)
                            await tel_client.subscribe(TELEMETRY_SUBSCRIBE_TOPIC, qos=0)
                            log.info(
                                "bridge_subscribed",
                                mqtt=f"{cfg.mqtt_host}:{cfg.mqtt_port}",
                                kafka=cfg.kafka_bootstrap,
                            )
                            ev_task = asyncio.create_task(
                                consume_mqtt_stream(
                                    client.messages.__aiter__(),
                                    sink,
                                    out_stats,
                                    max_messages=max_messages,
                                )
                            )
                            tel_task = asyncio.create_task(
                                consume_mqtt_stream(
                                    tel_client.messages.__aiter__(),
                                    sink,
                                    out_stats,
                                    max_messages=max_messages,
                                )
                            )
                            done, pending = await asyncio.wait(
                                {ev_task, tel_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for p in pending:
                                p.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await p
                            for d in done:
                                d.result()
                    if max_messages is not None:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                out_stats.reconnects += 1
                delay = min(10.0, 0.5 * (2 ** min(attempt, 5))) + random.uniform(0.0, 0.25)
                log.warning(
                    "bridge_disconnected_retrying",
                    error=str(exc),
                    retry_in_s=round(delay, 2),
                )
                await asyncio.sleep(delay)
    finally:
        if owns_sink:
            await sink.close()

    return out_stats


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m ase.streaming.bridge", description=__doc__)
    p.add_argument("--max-messages", type=int, default=None)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = get_settings()

    async def _runner() -> BridgeStats:
        sink = KafkaSink(settings=settings)
        await sink.start()
        try:
            return await run_bridge(
                settings=settings,
                kafka_sink=sink,
                max_messages=args.max_messages,
            )
        finally:
            await sink.close()

    stats = BridgeStats()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(_runner())
    try:
        stats = loop.run_until_complete(task)
    except KeyboardInterrupt:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(task)
    finally:
        loop.close()

    print(
        f"[bridge] events_forwarded={stats.events_forwarded}, "
        f"telemetry_forwarded={stats.telemetry_forwarded}, "
        f"invalid_messages={stats.invalid_messages}, "
        f"reconnects={stats.reconnects}"
    )


if __name__ == "__main__":
    main()
