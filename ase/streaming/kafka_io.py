"""`KafkaSource` và `KafkaSink` sau `EventSource`/`EventSink` (WATERMARK_PLAN §3.2).

- `KafkaSource`: consumer `aiokafka`, decode `SwapEvent`, tiến `SimClock` theo per-partition
  watermark (khi truyền `clock`), dừng khi idle quá `max_idle_seconds` (đồng hồ thật).
- `KafkaSink`: producer `aiokafka`, key = `session_id`, `acks="all"`.
- Cả hai nhận client qua constructor để unit test bằng client giả mà không cần broker.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol

import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from ase.common.config import Settings, get_settings
from ase.common.models import CabinetTelemetry, SwapEvent
from ase.common.time import ensure_utc
from ase.common.transport import SimClock
from ase.streaming.codec import decode_event, encode_event, encode_telemetry

log = structlog.get_logger(__name__)


class PartitionProgress(Protocol):
    """Giao diện hỏi tiến độ từng partition để tính watermark và dễ mock trong unit test."""

    def assigned(self) -> set[Any]: ...
    def highwater(self, tp: Any) -> int | None: ...
    async def position(self, tp: Any) -> int: ...


class _ConsumerPartitionProgress:
    """Adapter đọc tiến độ partition trực tiếp từ `AIOKafkaConsumer` (hoặc fake consumer)."""

    def __init__(self, consumer: Any) -> None:
        self._consumer = consumer

    def assigned(self) -> set[Any]:
        fn = getattr(self._consumer, "assignment", None)
        return set(fn()) if callable(fn) else set()

    def highwater(self, tp: Any) -> int | None:
        fn = getattr(self._consumer, "highwater", None)
        return fn(tp) if callable(fn) else None

    async def position(self, tp: Any) -> int:
        fn = getattr(self._consumer, "position", None)
        if not callable(fn):
            return 0
        res = fn(tp)
        return int(await res) if inspect.isawaitable(res) else int(res)


class KafkaSource:
    """`EventSource` đọc `SwapEvent` từ Kafka topic, tự dừng khi idle quá `max_idle_seconds`.

    Trước message đầu tiên chờ tối đa `startup_timeout_seconds` (đồng hồ thật): publisher có thể
    mất hàng chục giây để nạp dữ liệu profile lớn (`demo`) trước khi phát; nếu dùng luôn ngưỡng
    idle thì detector tưởng hết dữ liệu và thoát với 0 event.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        topic: str | None = None,
        group_id: str | None = None,
        max_idle_seconds: float = 10.0,
        startup_timeout_seconds: float = 0.0,
        consumer: Any | None = None,
        clock: SimClock | None = None,
        progress: PartitionProgress | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.topic = topic or self.settings.kafka_events_topic
        self.group_id = group_id if group_id is not None else self.settings.kafka_group_id
        self.max_idle_seconds = max_idle_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self._consumer = consumer
        self.clock = clock
        self._progress = progress
        self.decode_errors: int = 0
        self.messages_read: int = 0
        self.max_watermark_lag_seconds: float = 0.0

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

    async def _compute_watermark(
        self,
        prog: PartitionProgress,
        high_p: dict[Any, datetime],
        known_partitions: set[Any],
        rem_in_batch: dict[Any, int],
    ) -> datetime | None:
        if not high_p:
            return None
        partitions = set(prog.assigned()) | known_partitions
        active: list[Any] = []
        for q in partitions:
            if rem_in_batch.get(q, 0) > 0:
                active.append(q)
                continue
            hw = prog.highwater(q)
            pos = await prog.position(q)
            at_end = hw is not None and pos >= hw
            if not at_end:
                active.append(q)
        for q in active:
            if q not in high_p:
                return None  # còn partition có backlog nhưng chưa đọc được event đầu tiên
        if active:
            return min(high_p[q] for q in active)
        return max(high_p.values())

    async def _advance_watermark(
        self,
        prog: PartitionProgress,
        high_p: dict[Any, datetime],
        known_partitions: set[Any],
        rem_in_batch: dict[Any, int],
    ) -> None:
        if self.clock is None:
            return
        wm = await self._compute_watermark(prog, high_p, known_partitions, rem_in_batch)
        if wm is not None:
            lag = (max(high_p.values()) - wm).total_seconds()
            if lag > self.max_watermark_lag_seconds:
                self.max_watermark_lag_seconds = round(lag, 6)
            self.clock.advance(wm)

    async def events(self) -> AsyncIterator[SwapEvent]:
        consumer = self._consumer if self._consumer is not None else self._build_consumer()
        prog = self._progress or _ConsumerPartitionProgress(consumer)
        high_p: dict[Any, datetime] = {}
        known_partitions: set[Any] = set()
        rem_in_batch: dict[Any, int] = {}
        if hasattr(consumer, "start"):
            await consumer.start()
        try:
            getmany_fn = getattr(consumer, "getmany", None)
            if callable(getmany_fn):
                timeout_ms = max(1, int(self.max_idle_seconds * 1000))
                started = time.monotonic()
                got_any = False
                while True:
                    batches: dict[Any, list[Any]] = await getmany_fn(timeout_ms=timeout_ms)
                    if not batches or not any(batches.values()):
                        waited = time.monotonic() - started
                        if not got_any and waited < self.startup_timeout_seconds:
                            log.info("kafka_waiting_first_message", waited_s=round(waited, 1))
                            continue
                        break
                    got_any = True
                    known_partitions.update(batches.keys())
                    for q, recs in batches.items():
                        rem_in_batch[q] = len(recs)
                    # Đúng thứ tự consumer nhận: theo offset trong từng partition, các partition
                    # nối tiếp nhau như aiokafka trả về. Không sắp xếp lại theo occurred_at.
                    for tp, records in batches.items():
                        for msg in records:
                            ev = self._decode_raw(getattr(msg, "value", msg))
                            if ev is None:
                                rem_in_batch[tp] = max(0, rem_in_batch.get(tp, 1) - 1)
                                if rem_in_batch[tp] == 0:
                                    await self._advance_watermark(
                                        prog, high_p, known_partitions, rem_in_batch
                                    )
                                continue
                            occ = ensure_utc(ev.occurred_at)
                            prev = high_p.get(tp)
                            if prev is None or occ > prev:
                                high_p[tp] = occ
                            await self._advance_watermark(
                                prog, high_p, known_partitions, rem_in_batch
                            )
                            yield ev
                            rem_in_batch[tp] = max(0, rem_in_batch.get(tp, 1) - 1)
                            if rem_in_batch[tp] == 0:
                                await self._advance_watermark(
                                    prog, high_p, known_partitions, rem_in_batch
                                )
                if self.clock is not None and high_p:
                    self.clock.advance(max(high_p.values()))
                return

            aiter = consumer.__aiter__()
            started = time.monotonic()
            got_first = False
            while True:
                try:
                    msg = await asyncio.wait_for(aiter.__anext__(), timeout=self.max_idle_seconds)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    waited = time.monotonic() - started
                    if not got_first and waited < self.startup_timeout_seconds:
                        log.info("kafka_waiting_first_message", waited_s=round(waited, 1))
                        continue
                    break
                got_first = True
                ev = self._decode_raw(getattr(msg, "value", msg))
                if ev is not None:
                    if self.clock is not None:
                        occ = ensure_utc(ev.occurred_at)
                        tp = getattr(msg, "partition", 0)
                        known_partitions.add(tp)
                        prev = high_p.get(tp)
                        if prev is None or occ > prev:
                            high_p[tp] = occ
                        await self._advance_watermark(prog, high_p, known_partitions, rem_in_batch)
                    yield ev
            if self.clock is not None and high_p:
                self.clock.advance(max(high_p.values()))
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
