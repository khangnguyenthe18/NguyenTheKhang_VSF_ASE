"""Unit tests cho `ase.streaming` (STREAMING_PLAN §9) — không cần broker thật."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ase.common.models import CabinetTelemetry, EventStatus, Step, SwapEvent
from ase.common.transport import JsonlSource, SimClock
from ase.detector.main import run_detector
from ase.detector.state_machine import SessionTracker
from ase.knowledge.loader import default_knowledge
from ase.simulator.generator import generate
from ase.simulator.profiles import load_profiles
from ase.streaming.bridge import run_bridge
from ase.streaming.codec import decode_event, decode_telemetry, encode_event, encode_telemetry
from ase.streaming.kafka_io import KafkaSink, KafkaSource
from ase.streaming.mqtt_topics import (
    UNKNOWN_TOKEN,
    build_event_topic,
    build_telemetry_topic,
    parse_mqtt_topic,
    resolve_country,
)
from ase.streaming.publisher import CABINET_STEPS, run_publisher

K = default_knowledge()
T0 = datetime(2026, 11, 4, 10, 0, 0, tzinfo=UTC)


def _ev(
    event_id: str = "ev-1",
    session_id: str = "sess-1",
    station_id: str = "VN-ST-001",
    cabinet_id: str | None = "VN-ST-001-CAB-01",
    slot_id: str | None = "01",
    step: Step = Step.OPEN_APP,
    status: EventStatus = "OK",
    error_code: str | None = None,
    occurred_at: datetime = T0,
) -> SwapEvent:
    return SwapEvent(
        event_id=event_id,
        session_id=session_id,
        station_id=station_id,
        cabinet_id=cabinet_id,
        slot_id=slot_id,
        step=step,
        status=status,
        error_code=error_code,
        occurred_at=occurred_at,
    )


def _tel(
    cabinet_id: str = "VN-ST-001-CAB-01",
    heartbeat_at: datetime = T0,
) -> CabinetTelemetry:
    return CabinetTelemetry(
        cabinet_id=cabinet_id,
        slot_id="01",
        door_state="CLOSED",
        battery_detected=True,
        soc=95.0,
        temperature=32.5,
        heartbeat_at=heartbeat_at,
        hw_error_code=None,
    )


# --- 1. codec ----------------------------------------------------------------


def test_codec_round_trip_and_invalid_payloads() -> None:
    ev = _ev()
    raw_ev = encode_event(ev)
    assert raw_ev.endswith(b'"2026-11-04T10:00:00Z"}') or b"2026-11-04T10:00:00Z" in raw_ev
    decoded_ev, err_ev = decode_event(raw_ev)
    assert err_ev is None
    assert decoded_ev == ev

    tel = _tel()
    raw_tel = encode_telemetry(tel)
    decoded_tel, err_tel = decode_telemetry(raw_tel)
    assert err_tel is None
    assert decoded_tel == tel

    # Broken JSON
    assert decode_event(b"{not-json")[0] is None
    assert decode_telemetry(b"{not-json")[0] is None

    # Missing occurred_at
    missing_occ = (
        b'{"event_id":"e1","session_id":"s1","station_id":"VN-ST-001",'
        b'"step":"1_open_app","status":"OK"}'
    )
    res_missing, reason_missing = decode_event(missing_occ)
    assert res_missing is None and reason_missing is not None

    # Naive datetime must be rejected
    naive_ev = (
        b'{"event_id":"e1","session_id":"s1","station_id":"VN-ST-001",'
        b'"step":"1_open_app","status":"OK","occurred_at":"2026-11-04T10:00:00"}'
    )
    res_naive, reason_naive = decode_event(naive_ev)
    assert res_naive is None and reason_naive is not None

    naive_tel = b'{"cabinet_id":"CAB-01","heartbeat_at":"2026-11-04T10:00:00"}'
    res_naive_tel, reason_naive_tel = decode_telemetry(naive_tel)
    assert res_naive_tel is None and reason_naive_tel is not None


# --- 2. mqtt_topics ----------------------------------------------------------


def test_mqtt_topics_build_parse_and_unknown_station() -> None:
    ev_topic = build_event_topic("VN", "VN-ST-001", "VN-ST-001-CAB-01")
    assert ev_topic == "bss/VN/VN-ST-001/VN-ST-001-CAB-01/events"
    parsed_ev = parse_mqtt_topic(ev_topic)
    assert parsed_ev is not None
    assert (parsed_ev.country, parsed_ev.station_id, parsed_ev.cabinet_id, parsed_ev.kind) == (
        "VN",
        "VN-ST-001",
        "VN-ST-001-CAB-01",
        "events",
    )

    tel_topic = build_telemetry_topic("ID", "ID-ST-002", "ID-ST-002-CAB-01")
    assert tel_topic == "bss/ID/ID-ST-002/ID-ST-002-CAB-01/telemetry"
    parsed_tel = parse_mqtt_topic(tel_topic)
    assert parsed_tel is not None
    assert parsed_tel.kind == "telemetry"

    # Unknown station_id and None cabinet_id
    station_map = {"VN-ST-001": "VN"}
    assert resolve_country("VN-ST-001", station_map) == "VN"
    assert resolve_country("XX-ST-999", station_map) == UNKNOWN_TOKEN
    unknown_topic = build_event_topic(resolve_country("XX-ST-999", station_map), "XX-ST-999", None)
    assert unknown_topic == "bss/UNKNOWN/XX-ST-999/UNKNOWN/events"
    parsed_unk = parse_mqtt_topic(unknown_topic)
    assert parsed_unk is not None and parsed_unk.country == UNKNOWN_TOKEN

    # Invalid topics
    assert parse_mqtt_topic("other/VN/ST/CAB/events") is None
    assert parse_mqtt_topic("bss/VN/ST/CAB/other") is None
    assert parse_mqtt_topic("bss/+/ST/CAB/events") is None
    assert parse_mqtt_topic("bss/VN/ST/events") is None


# --- 3. KafkaSource & KafkaSink with fake clients ----------------------------


@dataclass
class _FakeKafkaRecord:
    value: bytes


class _FakeConsumer:
    def __init__(self, payloads: list[bytes], *, hang_at_end: bool = True) -> None:
        self._payloads = list(payloads)
        self._hang_at_end = hang_at_end
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def __aiter__(self) -> _FakeConsumer:
        return self

    async def __anext__(self) -> _FakeKafkaRecord:
        if self._payloads:
            return _FakeKafkaRecord(value=self._payloads.pop(0))
        if self._hang_at_end:
            await asyncio.sleep(10.0)
        raise StopAsyncIteration


class _FakeProducer:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.sent: list[tuple[str, bytes, bytes]] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_and_wait(self, topic: str, value: bytes, key: bytes) -> None:
        self.sent.append((topic, key, value))


async def test_kafka_source_yields_valid_skips_broken_and_stops_on_idle() -> None:
    ev1 = _ev(event_id="e1", session_id="s1", occurred_at=T0)
    ev2 = _ev(event_id="e2", session_id="s2", occurred_at=T0 + timedelta(seconds=5))
    fake_consumer = _FakeConsumer(
        [encode_event(ev1), b"invalid-json", encode_event(ev2)],
        hang_at_end=True,
    )
    src = KafkaSource(consumer=fake_consumer, max_idle_seconds=0.05)
    got = [e async for e in src.events()]
    assert got == [ev1, ev2]
    assert src.decode_errors == 1
    assert fake_consumer.started and fake_consumer.stopped


async def test_kafka_sink_uses_session_id_and_cabinet_id_as_keys() -> None:
    fake_prod = _FakeProducer()
    sink = KafkaSink(
        topic="ase.swap.events",
        telemetry_topic="ase.cabinet.telemetry",
        producer=fake_prod,
    )
    ev = _ev(session_id="SESS-KEY-01")
    tel = _tel(cabinet_id="VN-ST-001-CAB-02")
    await sink.write(ev)
    await sink.write_telemetry(tel)
    await sink.close()

    assert len(fake_prod.sent) == 2
    assert fake_prod.sent[0][0] == "ase.swap.events"
    assert fake_prod.sent[0][1] == b"SESS-KEY-01"
    assert decode_event(fake_prod.sent[0][2])[0] == ev

    assert fake_prod.sent[1][0] == "ase.cabinet.telemetry"
    assert fake_prod.sent[1][1] == b"VN-ST-001-CAB-02"
    assert decode_telemetry(fake_prod.sent[1][2])[0] == tel
    assert fake_prod.stopped


# --- 4. Publisher & Bridge with fake clients ---------------------------------


@dataclass
class _FakeMqttMsg:
    topic: str
    payload: bytes


class _FakeMqttClient:
    def __init__(self, incoming: list[_FakeMqttMsg] | None = None) -> None:
        self.published: list[tuple[str, bytes, int]] = []
        self.subscriptions: list[tuple[str, int]] = []
        self._incoming = list(incoming or [])

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        self.published.append((topic, payload, qos))

    async def subscribe(self, topic: str, qos: int = 1) -> None:
        self.subscriptions.append((topic, qos))

    @property
    def messages(self) -> Any:
        items = self._incoming

        async def _gen() -> Any:
            for it in items:
                yield it

        return _gen()


@pytest.fixture(scope="module")
def smoke_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("streaming_smoke")
    asyncio.run(generate("smoke", out, knowledge=K, profiles=load_profiles()))
    return out


async def test_publisher_routes_steps_1_5_to_kafka_and_6_7_and_telemetry_to_mqtt(
    smoke_dir: Path,
) -> None:
    fake_prod = _FakeProducer()
    sink = KafkaSink(producer=fake_prod)
    fake_mqtt = _FakeMqttClient()

    stats = await run_publisher(
        smoke_dir,
        speed=None,
        include_telemetry=True,
        kafka_sink=sink,
        mqtt_client=fake_mqtt,
    )
    await sink.close()

    assert stats.kafka_events == len(fake_prod.sent)
    mqtt_ev_msgs = [m for m in fake_mqtt.published if m[0].endswith("/events")]
    mqtt_tel_msgs = [m for m in fake_mqtt.published if m[0].endswith("/telemetry")]
    assert stats.mqtt_events == len(mqtt_ev_msgs) > 0
    assert stats.mqtt_telemetry == len(mqtt_tel_msgs) > 0

    # Verify exact line order within each path compared to ase.swap.events.jsonl
    expected_kafka_ids: list[str] = []
    expected_mqtt_ids: list[str] = []
    for line in (smoke_dir / "ase.swap.events.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = SwapEvent.model_validate_json(line)
        if ev.step in CABINET_STEPS:
            expected_mqtt_ids.append(ev.event_id)
        else:
            expected_kafka_ids.append(ev.event_id)

    actual_kafka_ids = [decode_event(m[2])[0].event_id for m in fake_prod.sent]  # type: ignore[union-attr]
    actual_mqtt_ids = [decode_event(m[1])[0].event_id for m in mqtt_ev_msgs]  # type: ignore[union-attr]
    assert actual_kafka_ids == expected_kafka_ids
    assert actual_mqtt_ids == expected_mqtt_ids


async def test_bridge_forwards_valid_and_counts_invalid_without_stopping() -> None:
    ev = _ev(event_id="ev-bridge-1", session_id="sess-b1", step=Step.RETURN_BATTERY)
    tel = _tel(cabinet_id="VN-ST-001-CAB-01")
    incoming = [
        _FakeMqttMsg("bss/VN/VN-ST-001/VN-ST-001-CAB-01/events", b"{bad-json"),
        _FakeMqttMsg("bss/VN/VN-ST-001/VN-ST-001-CAB-01/events", encode_event(ev)),
        _FakeMqttMsg("bss/invalid_topic", encode_event(ev)),
        _FakeMqttMsg("bss/VN/VN-ST-001/VN-ST-001-CAB-01/telemetry", encode_telemetry(tel)),
    ]
    fake_mqtt = _FakeMqttClient(incoming=incoming)
    fake_prod = _FakeProducer()
    sink = KafkaSink(
        topic="ase.swap.events",
        telemetry_topic="ase.cabinet.telemetry",
        producer=fake_prod,
    )

    stats = await run_bridge(kafka_sink=sink, mqtt_client=fake_mqtt)
    await sink.close()

    assert stats.events_forwarded == 1
    assert stats.telemetry_forwarded == 1
    assert stats.invalid_messages == 2
    assert len(fake_prod.sent) == 2
    forwarded_ev, _ = decode_event(fake_prod.sent[0][2])
    assert forwarded_ev is not None and forwarded_ev.event_id == "ev-bridge-1"


# --- 5. SessionTracker allowed_lateness_seconds ------------------------------


def test_session_tracker_allowed_lateness_prevents_premature_timeout() -> None:
    # Step 5 OK at T0 -> waiting for Step 6 (SLA = 120s -> deadline = T0 + 120s)
    clock = SimClock()
    tracker = SessionTracker(clock, knowledge=K, allowed_lateness_seconds=10.0)

    ev_s5 = _ev(
        event_id="s1-5",
        session_id="s1",
        step=Step.PAYMENT,
        status="OK",
        occurred_at=T0,
    )
    assert tracker.on_event(ev_s5) == []

    # Next step event arrives 6s after SLA deadline (T0 + 126s, within 10s lateness) -> NO timeout
    ev_s6 = _ev(
        event_id="s1-6",
        session_id="s1",
        step=Step.RETURN_BATTERY,
        status="OK",
        occurred_at=T0 + timedelta(seconds=126),
    )
    assert tracker.on_event(ev_s6) == []
    ev_s7 = _ev(
        event_id="s1-7",
        session_id="s1",
        step=Step.RECEIVE_BATTERY,
        status="OK",
        occurred_at=T0 + timedelta(seconds=130),
    )
    assert tracker.on_event(ev_s7) == []

    # Session s2: Step 5 OK at T0 + 200s -> deadline for Step 6 is T0 + 320s
    ev_s2_5 = _ev(
        event_id="s2-5",
        session_id="s2",
        step=Step.PAYMENT,
        status="OK",
        occurred_at=T0 + timedelta(seconds=200),
    )
    assert tracker.on_event(ev_s2_5) == []

    # Event arrives at T0 + 327s (7s after deadline, within 10s) -> still NO timeout
    ev_other_1 = _ev(
        event_id="other-1",
        session_id="s3",
        step=Step.OPEN_APP,
        status="OK",
        occurred_at=T0 + timedelta(seconds=327),
    )
    assert tracker.on_event(ev_other_1) == []

    # Event arrives at T0 + 331s (11s after deadline > 10s allowed lateness) -> TIMEOUT!
    ev_other_2 = _ev(
        event_id="other-2",
        session_id="s3",
        step=Step.SELECT_SERVICE,
        status="OK",
        occurred_at=T0 + timedelta(seconds=331),
    )
    findings = tracker.on_event(ev_other_2)
    assert len(findings) == 1
    f = findings[0]
    assert f.session_id == "s2"
    assert f.kind == "STEP_TIMEOUT"
    assert f.step == Step.RETURN_BATTERY
    assert f.occurred_at == T0 + timedelta(seconds=320)
    assert f.detected_at == T0 + timedelta(seconds=331)


# --- 6. Per-partition watermark & advance_clock=False (WATERMARK_PLAN §5) ----


class _FakePartitionedConsumer:
    """Consumer giả hỗ trợ `getmany()`, `assignment()`, `highwater(tp)`, `position(tp)`."""

    def __init__(
        self,
        batches_seq: list[dict[int, list[_FakeKafkaRecord]]],
        *,
        assigned: set[int],
        highwaters: dict[int, int],
        initial_positions: dict[int, int] | None = None,
    ) -> None:
        self._batches_seq = list(batches_seq)
        self._assigned = set(assigned)
        self._highwaters = dict(highwaters)
        self._positions: dict[int, int] = {p: 0 for p in assigned}
        if initial_positions:
            self._positions.update(initial_positions)
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def assignment(self) -> set[int]:
        return set(self._assigned)

    def highwater(self, tp: int) -> int | None:
        return self._highwaters.get(tp)

    async def position(self, tp: int) -> int:
        return self._positions.get(tp, 0)

    async def getmany(self, timeout_ms: int = 1000) -> dict[int, list[_FakeKafkaRecord]]:
        _ = timeout_ms
        if not self._batches_seq:
            return {}
        batch = self._batches_seq.pop(0)
        for tp, recs in batch.items():
            self._positions[tp] = self._positions.get(tp, 0) + len(recs)
        return batch


async def test_watermark_is_min_across_active_partitions() -> None:
    # 1. Watermark = min qua các partition (P0 ở 10:05, P1 ở 10:01 -> clock = 10:01)
    t_10_01 = T0 + timedelta(minutes=1)
    t_10_05 = T0 + timedelta(minutes=5)
    ev_p0 = _ev(event_id="p0-1", session_id="s-p0", occurred_at=t_10_05)
    ev_p1 = _ev(event_id="p1-1", session_id="s-p1", occurred_at=t_10_01)

    # Cả 2 partition vẫn còn backlog (highwater=5 > position=1 sau lô đầu)
    consumer = _FakePartitionedConsumer(
        [{0: [_FakeKafkaRecord(encode_event(ev_p0))], 1: [_FakeKafkaRecord(encode_event(ev_p1))]}],
        assigned={0, 1},
        highwaters={0: 5, 1: 5},
    )
    clock = SimClock()
    src = KafkaSource(consumer=consumer, clock=clock, max_idle_seconds=0.05)

    seen_clocks: list[datetime | None] = []
    async for _ in src.events():
        seen_clocks.append(clock._now)

    # Khi đọc ev_p0: P1 chưa có event nên watermark chưa tiến (None).
    # Khi đọc ev_p1: cả P0 (10:05) và P1 (10:01) đều đã có event -> watermark = min = 10:01.
    assert seen_clocks == [None, t_10_01]
    assert src.max_watermark_lag_seconds == 240.0


async def test_watermark_caught_up_partition_does_not_hold_back_and_all_caught_up_uses_max() -> (
    None
):
    # 2. Partition đã đọc hết (position >= highwater) không còn giữ watermark;
    #    tất cả partition đã đọc hết -> watermark = max.
    t_10_01 = T0 + timedelta(minutes=1)
    t_10_05 = T0 + timedelta(minutes=5)
    ev_p0 = _ev(event_id="p0-1", session_id="s-p0", occurred_at=t_10_01)
    ev_p1 = _ev(event_id="p1-1", session_id="s-p1", occurred_at=t_10_05)

    # Lô 1: P0 đọc hết (pos=1 >= hw=1). Lô 2: P1 đọc tiếp (pos=1 < hw=2), P0 không giữ watermark.
    consumer = _FakePartitionedConsumer(
        [
            {0: [_FakeKafkaRecord(encode_event(ev_p0))]},
            {1: [_FakeKafkaRecord(encode_event(ev_p1))]},
        ],
        assigned={0, 1},
        highwaters={0: 1, 1: 2},
    )
    clock = SimClock()
    src = KafkaSource(consumer=consumer, clock=clock, max_idle_seconds=0.05)

    seen_clocks: list[datetime | None] = []
    async for _ in src.events():
        seen_clocks.append(clock._now)

    # Ở ev_p0: P1 còn backlog (pos=0 < hw=2) và chưa có event -> None.
    # Ở ev_p1: P0 đã ở cuối (pos=1 >= hw=1), chỉ còn P1 active -> watermark = 10:05.
    assert seen_clocks == [None, t_10_05]
    assert clock.now() == t_10_05

    # Trường hợp cả 2 partition đều đọc tới cuối (pos >= hw): watermark tiến tới max(high_p)
    consumer_all_done = _FakePartitionedConsumer(
        [{0: [_FakeKafkaRecord(encode_event(ev_p1))], 1: [_FakeKafkaRecord(encode_event(ev_p0))]}],
        assigned={0, 1},
        highwaters={0: 1, 1: 1},
    )
    clock2 = SimClock()
    src2 = KafkaSource(consumer=consumer_all_done, clock=clock2, max_idle_seconds=0.05)
    _ = [e async for e in src2.events()]
    assert clock2.now() == t_10_05


async def test_watermark_does_not_advance_when_partition_with_backlog_has_no_events() -> None:
    # 3. Partition có backlog (position < highwater) nhưng chưa đọc được event nào -> chưa tiến
    t_10_05 = T0 + timedelta(minutes=5)
    ev_p0 = _ev(event_id="p0-1", session_id="s-p0", occurred_at=t_10_05)

    consumer = _FakePartitionedConsumer(
        [{0: [_FakeKafkaRecord(encode_event(ev_p0))]}],
        assigned={0, 1},
        highwaters={0: 2, 1: 3},  # P1 có backlog (0 < 3) nhưng chưa trả về event nào
    )
    clock = SimClock()
    src = KafkaSource(consumer=consumer, clock=clock, max_idle_seconds=0.05)

    seen_During_Batch: list[datetime | None] = []
    async for _ in src.events():
        seen_During_Batch.append(clock._now)

    assert seen_During_Batch == [None]
    # Chỉ khi kết thúc luồng (idle timeout) mới chốt clock tới max(high_p)
    assert clock.now() == t_10_05


async def test_watermark_never_moves_backward_on_older_event() -> None:
    # 4. Watermark không bao giờ lùi khi partition chậm gửi thêm event có occurred_at cũ hơn
    t_10_05 = T0 + timedelta(minutes=5)
    t_10_03 = T0 + timedelta(minutes=3)
    t_10_01 = T0 + timedelta(minutes=1)
    ev_p0 = _ev(event_id="p0-1", session_id="s-p0", occurred_at=t_10_05)
    ev_p1_a = _ev(event_id="p1-1", session_id="s-p1", occurred_at=t_10_03)
    ev_p1_b = _ev(event_id="p1-2", session_id="s-p1", occurred_at=t_10_01)  # cũ hơn 10:03

    consumer = _FakePartitionedConsumer(
        [
            {
                0: [_FakeKafkaRecord(encode_event(ev_p0))],
                1: [_FakeKafkaRecord(encode_event(ev_p1_a))],
            },
            {1: [_FakeKafkaRecord(encode_event(ev_p1_b))]},
        ],
        assigned={0, 1},
        highwaters={0: 5, 1: 5},
    )
    clock = SimClock()
    src = KafkaSource(consumer=consumer, clock=clock, max_idle_seconds=0.05)

    seen_clocks: list[datetime | None] = []
    async for _ in src.events():
        seen_clocks.append(clock._now)

    assert seen_clocks == [None, t_10_03, t_10_03]


async def test_two_sessions_on_two_partitions_no_false_timeout_when_p0_batch_ahead_of_p1() -> None:
    # 5. Hai phiên ở 2 partition, lô P0 (thời gian muộn) trả về trước lô P1 (thời gian sớm):
    #    KHÔNG phát sinh timeout giả cho phiên ở P1.
    # Phiên s_slow ở P1: bước 1 lúc T0, bước 2 lúc T0+10s .. bước 7 lúc T0+60s (hoàn toàn hợp lệ).
    # Phiên s_fast ở P0: bước 1 lúc T0+500s (vượt xa SLA 60s của s_slow nếu không có watermark!).
    p1_events = [
        _ev(
            event_id=f"slow-{i}",
            session_id="s_slow",
            step=step,
            status="OK",
            occurred_at=T0 + timedelta(seconds=10 * i),
        )
        for i, step in enumerate(
            [
                Step.OPEN_APP,
                Step.SELECT_SERVICE,
                Step.SCAN_QR,
                Step.CHECK_ELIGIBILITY,
                Step.PAYMENT,
                Step.RETURN_BATTERY,
                Step.RECEIVE_BATTERY,
            ]
        )
    ]
    ev_p0_late = _ev(
        event_id="fast-1",
        session_id="s_fast",
        step=Step.OPEN_APP,
        status="OK",
        occurred_at=T0 + timedelta(seconds=500),
    )

    # Lô 1 trả P0 (T0+500s) trước rồi mới tới P1 (T0..T0+60s)
    consumer = _FakePartitionedConsumer(
        [
            {
                0: [_FakeKafkaRecord(encode_event(ev_p0_late))],
                1: [_FakeKafkaRecord(encode_event(e)) for e in p1_events],
            }
        ],
        assigned={0, 1},
        highwaters={0: 2, 1: len(p1_events) + 1},
    )
    clock = SimClock()
    src = KafkaSource(consumer=consumer, clock=clock, max_idle_seconds=0.05)
    tracker = SessionTracker(
        clock,
        knowledge=K,
        allowed_lateness_seconds=10.0,
        advance_clock=False,
    )

    findings = []
    async for ev in src.events():
        findings.extend(tracker.on_event(ev))

    # Không có bất kỳ timeout giả nào cho s_slow!
    assert [f for f in findings if f.session_id == "s_slow"] == []


def test_session_tracker_advance_clock_false_caps_timer_pop_and_sets_detected_at_max() -> None:
    # 6. SessionTracker(advance_clock=False):
    #    - event có occurred_at > clock.now() không được pop timer vượt trước watermark
    #    - EXPLICIT_FAILURE có detected_at = max(now, occurred_at)
    clock = SimClock()
    clock.advance(T0 + timedelta(seconds=20))
    tracker = SessionTracker(
        clock,
        knowledge=K,
        allowed_lateness_seconds=10.0,
        advance_clock=False,
    )

    # s1 mở app lúc T0 -> hạn chờ bước 2 (SLA 60s) là T0+60s (+10s lateness = T0+70s)
    ev_s1 = _ev(
        event_id="s1-1",
        session_id="s1",
        step=Step.OPEN_APP,
        status="OK",
        occurred_at=T0,
    )
    assert tracker.on_event(ev_s1) == []

    # s2 gửi event ở T0+200s (> T0+70s), nhưng watermark (clock.now()) vẫn ở T0+20s:
    # tracker KHÔNG được tự tăng clock và KHÔNG được pop timer của s1!
    ev_s2_ahead = _ev(
        event_id="s2-1",
        session_id="s2",
        step=Step.OPEN_APP,
        status="OK",
        occurred_at=T0 + timedelta(seconds=200),
    )
    assert tracker.on_event(ev_s2_ahead) == []
    assert clock.now() == T0 + timedelta(seconds=20)

    # EXPLICIT_FAILURE ở s2 lúc T0+210s khi clock=T0+20s -> detected_at = max(now, occ) = T0+210s
    ev_s2_fail = _ev(
        event_id="s2-2",
        session_id="s2",
        step=Step.SELECT_SERVICE,
        status="FAILED",
        error_code="ERR_BACKEND_5XX",
        occurred_at=T0 + timedelta(seconds=210),
    )
    f_fail = tracker.on_event(ev_s2_fail)
    assert len(f_fail) == 1
    assert f_fail[0].kind == "EXPLICIT_FAILURE"
    assert f_fail[0].detected_at == T0 + timedelta(seconds=210)

    # EXPLICIT_FAILURE ở s3 lúc T0+10s khi clock=T0+20s -> detected_at = max(now, occ) = T0+20s
    ev_s3_fail = _ev(
        event_id="s3-1",
        session_id="s3",
        step=Step.OPEN_APP,
        status="FAILED",
        error_code="ERR_BACKEND_5XX",
        occurred_at=T0 + timedelta(seconds=10),
    )
    f_fail3 = tracker.on_event(ev_s3_fail)
    assert len(f_fail3) == 1
    assert f_fail3[0].detected_at == T0 + timedelta(seconds=20)
    assert tracker.max_lateness_vs_clock_seconds == 20.0  # s1 ở T0 so với clock T0+20s


# --- 7. Regression: run_detector(source=None) vs JsonlSource -----------------


def _sha_dir(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}


async def test_run_detector_default_vs_injected_jsonl_source_byte_identical(
    smoke_dir: Path, tmp_path: Path
) -> None:
    r_default = await run_detector(smoke_dir, out_dir=tmp_path / "default", knowledge=K)

    clock = SimClock()
    jsonl_src = JsonlSource(smoke_dir / "ase.swap.events.jsonl", clock)
    r_injected = await run_detector(
        smoke_dir,
        out_dir=tmp_path / "injected",
        knowledge=K,
        source=jsonl_src,
        clock=clock,
    )

    assert _sha_dir(r_default.out_dir) == _sha_dir(r_injected.out_dir)
    # 5 file JSONL của smoke phải giữ nguyên SHA256 tuyệt đối
    expected_smoke_jsonl_sha = {
        "alerts.jsonl": "7c6529d1499e4a4200910a3a5ec4af7e1497c76a5dae36b96f819a445d3fa32f",
        "ase.anomaly.transaction.v0.jsonl": (
            "f761d303b8d33183ca52281d6ba20d66d41cccf3af56bc91725ddab64d1ab1a7"
        ),
        "batches.jsonl": "f074597ed2e8a62f3c4f6724e0d94f784cf49a8cd496060249ce34dace2cf0f0",
        "detections.jsonl": "8fb59e489f8d7b68db8773d889b9719e81298396d023cd52b652e96e5d206eae",
        "incidents.jsonl": "4669fee58ed6fd78d42635eefd4711220638ceba2f67ede6032d49cee00f0ebf",
    }
    actual_sha = _sha_dir(r_default.out_dir)
    for fname, sha in expected_smoke_jsonl_sha.items():
        assert actual_sha[fname] == sha
