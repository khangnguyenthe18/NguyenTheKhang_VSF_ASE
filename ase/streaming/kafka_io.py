"""`KafkaSource` và `KafkaSink` sau `EventSource`/`EventSink` (STREAMING_PLAN §5).

- `KafkaSource`: consumer `aiokafka`, decode `SwapEvent`, **không** tự `clock.advance`, dừng khi
  idle quá `max_idle_seconds` (đồng hồ thật).
- `KafkaSink`: producer `aiokafka`, key = `session_id`, `acks="all"`.
- Cả hai nhận client qua constructor để unit test bằng client giả mà không cần broker.
"""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from ase.common.config import Settings, get_settings
from ase.common.models import CabinetTelemetry, SwapEvent
from ase.common.time import ensure_utc
from ase.streaming.codec import decode_event, encode_event, encode_telemetry

log = structlog.get_logger(__name__)


class KafkaSource:
    """`EventSource` đọc `SwapEvent` từ Kafka topic, tự dừng khi idle quá `max_idle_seconds`."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        topic: str | None = None,
        group_id: str | None = None,
        max_idle_seconds: float = 10.0,
        consumer: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.topic = topic or self.settings.kafka_events_topic
        self.group_id = group_id if group_id is not None else self.settings.kafka_group_id
        self.max_idle_seconds = max_idle_seconds
        self._consumer = consumer
        self.decode_errors: int = 0
        self.messages_read: int = 0

    def _build_consumer(self) -> Any:
        return AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.settings.kafka_bootstrap,
            group_id=self.group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
        )

    def _decode_raw(self, raw: Any) -> SwapEvent | None:
        self.messages_read += 1
        if not isinstance(raw, (bytes, str)):
            self.decode_errors += 1
            log.warning("kafka_invalid_payload_type", type=type(raw).__name__)
            return None
        ev, err = decode_event(raw)
        if ev is None:
            self.decode_errors += 1
            log.warning("kafka_decode_event_failed", reason=err)
            return None
        return ev

    async def events(self) -> AsyncIterator[SwapEvent]:
        consumer = self._consumer if self._consumer is not None else self._build_consumer()
        if hasattr(consumer, "start"):
            await consumer.start()
        try:
            getmany_fn = getattr(consumer, "getmany", None)
            if callable(getmany_fn):
                timeout_ms = max(1, int(self.max_idle_seconds * 1000))
                while True:
                    batches: dict[Any, list[Any]] = await getmany_fn(timeout_ms=timeout_ms)
                    if not batches or not any(batches.values()):
                        break
                    # Giữ nguyên thứ tự offset trong từng partition, chỉ trộn giữa các partition
                    # có sẵn trong đợt fetch để không xả cạn 1 partition trước partition khác.
                    part_queues: list[list[SwapEvent]] = []
                    for records in batches.values():
                        q: list[SwapEvent] = []
                        for msg in records:
                            ev = self._decode_raw(getattr(msg, "value", msg))
                            if ev is not None:
                                q.append(ev)
                        if q:
                            part_queues.append(q)
                    if len(part_queues) == 1:
                        for ev in part_queues[0]:
                            yield ev
                    elif len(part_queues) > 1:
                        heap: list[tuple[datetime, int, int, SwapEvent]] = []
                        for p_idx, q in enumerate(part_queues):
                            heapq.heappush(heap, (ensure_utc(q[0].occurred_at), p_idx, 0, q[0]))
                        while heap:
                            _, p_idx, r_idx, ev = heapq.heappop(heap)
                            yield ev
                            nxt_idx = r_idx + 1
                            q = part_queues[p_idx]
                            if nxt_idx < len(q):
                                nxt_ev = q[nxt_idx]
                                heapq.heappush(
                                    heap,
                                    (ensure_utc(nxt_ev.occurred_at), p_idx, nxt_idx, nxt_ev),
                                )
                return

            aiter = consumer.__aiter__()
            while True:
                try:
                    msg = await asyncio.wait_for(aiter.__anext__(), timeout=self.max_idle_seconds)
                except (TimeoutError, StopAsyncIteration):
                    break
                ev = self._decode_raw(getattr(msg, "value", msg))
                if ev is not None:
                    yield ev
        finally:
            if hasattr(consumer, "stop"):
                await consumer.stop()


class KafkaSink:
    """`EventSink` ghi `SwapEvent` (key=`session_id`) và telemetry (key=`cabinet_id`) vào Kafka."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        topic: str | None = None,
        telemetry_topic: str | None = None,
        producer: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.topic = topic or self.settings.kafka_events_topic
        self.telemetry_topic = telemetry_topic or self.settings.kafka_telemetry_topic
        self._producer = producer
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        if self._producer is None:
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self.settings.kafka_bootstrap,
                acks="all",
            )
        if hasattr(self._producer, "start"):
            await self._producer.start()
        self._started = True

    async def write(self, event: SwapEvent) -> None:
        if not self._started:
            await self.start()
        assert self._producer is not None
        await self._producer.send_and_wait(
            self.topic,
            value=encode_event(event),
            key=event.session_id.encode("utf-8"),
        )

    async def write_telemetry(self, telemetry: CabinetTelemetry) -> None:
        if not self._started:
            await self.start()
        assert self._producer is not None
        send_fn = getattr(self._producer, "send", None)
        if callable(send_fn):
            await send_fn(
                self.telemetry_topic,
                value=encode_telemetry(telemetry),
                key=telemetry.cabinet_id.encode("utf-8"),
            )
        else:
            await self._producer.send_and_wait(
                self.telemetry_topic,
                value=encode_telemetry(telemetry),
                key=telemetry.cabinet_id.encode("utf-8"),
            )

    async def close(self) -> None:
        if self._producer is not None and self._started:
            if hasattr(self._producer, "stop"):
                await self._producer.stop()
            self._started = False
