"""Encode/decode `SwapEvent` và `CabinetTelemetry` bằng `orjson` + Pydantic (§5)."""

from __future__ import annotations

import orjson

from ase.common.models import CabinetTelemetry, SwapEvent


def encode_event(event: SwapEvent) -> bytes:
    return orjson.dumps(event.model_dump(mode="json"), option=orjson.OPT_UTC_Z)


def decode_event(raw: bytes | str) -> tuple[SwapEvent | None, str | None]:
    try:
        return SwapEvent.model_validate_json(raw), None
    except Exception as exc:
        return None, str(exc)


def encode_telemetry(telemetry: CabinetTelemetry) -> bytes:
    return orjson.dumps(telemetry.model_dump(mode="json"), option=orjson.OPT_UTC_Z)


def decode_telemetry(raw: bytes | str) -> tuple[CabinetTelemetry | None, str | None]:
    try:
        return CabinetTelemetry.model_validate_json(raw), None
    except Exception as exc:
        return None, str(exc)
