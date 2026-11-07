"""Integration E2E tests cho luồng MQTT + Kafka (STREAMING_PLAN §9).

Tự động skip khi Redpanda (`127.0.0.1:19092`) hoặc Mosquitto (`127.0.0.1:1883`) chưa chạy.
"""

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from pathlib import Path

import aiomqtt
import pytest

from ase.common.config import Settings
from ase.common.models import SwapEvent
from ase.detector.main import run_detector
from ase.knowledge.loader import default_knowledge
from ase.simulator.generator import generate
from ase.simulator.profiles import load_profiles
from ase.streaming.bridge import BridgeStats, run_bridge
from ase.streaming.codec import encode_event
from ase.streaming.kafka_io import KafkaSink, KafkaSource
from ase.streaming.publisher import run_publisher

K = default_knowledge()


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _brokers_available() -> bool:
    return _port_open("127.0.0.1", 19092) and _port_open("127.0.0.1", 1883)


pytestmark = pytest.mark.skipif(
    not _brokers_available(),
    reason="Redpanda (19092) hoặc Mosquitto (1883) chưa chạy (chạy `make stream-up` trước)",
)


@pytest.fixture(scope="module")
def smoke_e2e_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("e2e_smoke")
    asyncio.run(generate("smoke", out, knowledge=K, profiles=load_profiles()))
    return out


async def test_e2e_smoke_kafka_matches_jsonl_and_dedups_and_handles_bad_mqtt(
    smoke_e2e_dir: Path, tmp_path: Path
) -> None:
    run_id = uuid.uuid4().hex[:8]
    ev_topic = f"ase.swap.events.e2e.{run_id}"
    tel_topic = f"ase.cabinet.telemetry.e2e.{run_id}"
    group_id = f"ase-detector-e2e-{run_id}"
    settings = Settings(
        data_dir=tmp_path,
        kafka_bootstrap="127.0.0.1:19092",
        kafka_events_topic=ev_topic,
        kafka_telemetry_topic=tel_topic,
        kafka_group_id=group_id,
        mqtt_host="127.0.0.1",
        mqtt_port=1883,
    )

    # 1. Baseline JSONL run
    jsonl_run = await run_detector(
        smoke_e2e_dir,
        out_dir=tmp_path / "jsonl_out",
        knowledge=K,
    )

    # 2. Start bridge in background forwarding MQTT -> test Kafka topics
    bridge_sink = KafkaSink(settings=settings, topic=ev_topic, telemetry_topic=tel_topic)
    await bridge_sink.start()
    bridge_stats = BridgeStats()
    bridge_task = asyncio.create_task(
        run_bridge(settings=settings, kafka_sink=bridge_sink, stats=bridge_stats)
    )
    await asyncio.sleep(0.5)

    try:
        # Publish 1 invalid MQTT payload -> bridge must count 1 error and keep running
        async with aiomqtt.Client(hostname="127.0.0.1", port=1883) as mqtt_client:
            await mqtt_client.publish(
                "bss/VN/VN-ST-001/VN-ST-001-CAB-01/events",
                payload=b"{corrupted-mqtt-payload",
                qos=1,
            )

        # Publish all smoke events (steps 1-5 -> Kafka, steps 6-7 -> MQTT)
        pub_sink = KafkaSink(settings=settings, topic=ev_topic, telemetry_topic=tel_topic)
        await pub_sink.start()
        try:
            await run_publisher(
                smoke_e2e_dir,
                settings=settings,
                speed=18000.0,
                include_telemetry=False,
                kafka_sink=pub_sink,
            )

            # Send 1 duplicate event through BOTH paths (Kafka directly + MQTT -> bridge)
            first_line = (
                (smoke_e2e_dir / "ase.swap.events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            dup_ev = SwapEvent.model_validate_json(first_line)
            await pub_sink.write(dup_ev)
            async with aiomqtt.Client(hostname="127.0.0.1", port=1883) as mqtt_client:
                await mqtt_client.publish(
                    f"bss/VN/{dup_ev.station_id}/{dup_ev.cabinet_id or 'UNKNOWN'}/events",
                    payload=encode_event(dup_ev),
                    qos=1,
                )
        finally:
            await pub_sink.close()

        # Wait briefly for bridge to finish forwarding to Kafka
        await asyncio.sleep(0.8)

        # 3. Run detector consuming from Kafka topic
        kafka_src = KafkaSource(
            settings=settings,
            topic=ev_topic,
            group_id=group_id,
            max_idle_seconds=2.5,
        )
        kafka_run = await run_detector(
            smoke_e2e_dir,
            out_dir=tmp_path / "kafka_out",
            knowledge=K,
            source=kafka_src,
            allowed_lateness_seconds=K.detector.stream.allowed_lateness_seconds,
            periodic_incidents=True,
        )
    finally:
        bridge_task.cancel()
        try:
            await bridge_task
        except asyncio.CancelledError:
            pass
        await bridge_sink.close()

    # Verify set of detected session_ids is identical to JSONL
    jsonl_sids = {
        json.loads(line)["session_id"]
        for line in (jsonl_run.out_dir / "detections.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    }
    kafka_sids = {
        json.loads(line)["session_id"]
        for line in (kafka_run.out_dir / "detections.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    }
    assert kafka_sids == jsonl_sids
    assert bridge_stats.invalid_messages == 1
    assert bridge_stats.events_forwarded > 0
    assert (
        kafka_run.report.coverage.duplicate_events_dropped
        == jsonl_run.report.coverage.duplicate_events_dropped + 2
    )
    assert (
        kafka_run.report.coverage.max_arrival_lag_seconds
        < K.detector.stream.allowed_lateness_seconds
    )
