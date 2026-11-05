"""Telemetry tủ (T3): heartbeat 1 phút/lần + dòng quanh phiên (mở/đóng cửa, đọc BMS).

Dấu vết lỗi phần cứng thể hiện qua `door_state` / `battery_detected` (không chỉ gắn nhãn).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from ase.common.models import CabinetTelemetry

HEARTBEAT_INTERVAL = timedelta(minutes=1)


def heartbeat_rows(
    cabinet_id: str,
    start: datetime,
    end: datetime,
    ambient: tuple[float, float],
    rng: random.Random,
) -> list[CabinetTelemetry]:
    """Heartbeat mỗi phút trong [start, end); nhiệt độ quanh mức môi trường của tủ."""
    base = rng.uniform(*ambient)
    rows: list[CabinetTelemetry] = []
    t = start
    while t < end:
        rows.append(
            CabinetTelemetry(
                cabinet_id=cabinet_id,
                temperature=round(base + rng.gauss(0, 0.8), 1),
                heartbeat_at=t,
            )
        )
        t += HEARTBEAT_INTERVAL
    return rows


def session_rows(
    *,
    cabinet_id: str,
    slot_in: str | None,
    slot_out: str | None,
    t6: tuple[datetime, datetime] | None,
    t7: tuple[datetime, datetime] | None,
    failure_code: str | None,
    soc_in: float,
    soc_out: float,
    temperature: float,
) -> list[CabinetTelemetry]:
    """Dòng telemetry của các bước đã đi tới (t6/t7 = (bắt đầu, kết thúc) hoặc None)."""
    rows: list[CabinetTelemetry] = []

    def row(
        slot: str, at: datetime, door: str, detected: bool, soc: float | None = None
    ) -> CabinetTelemetry:
        return CabinetTelemetry(
            cabinet_id=cabinet_id,
            slot_id=slot,
            door_state=door,
            battery_detected=detected,
            soc=soc,
            temperature=temperature,
            heartbeat_at=at,
        )

    if t6 is not None and slot_in is not None:
        opened = t6[0] + timedelta(seconds=2)
        if failure_code == "SLOT_DOOR_JAM":
            rows.append(row(slot_in, t6[1], "CLOSED", False))  # cửa không đổi trạng thái
        elif failure_code == "USR_RETURN_TIMEOUT":
            rows.append(row(slot_in, t6[1], "OPEN", False))  # khách không đặt pin, cảm biến ổn
        elif failure_code == "SLOT_SENSOR_FAIL":
            rows.append(row(slot_in, opened, "OPEN", False))
            rows.append(row(slot_in, t6[1], "CLOSED", False))  # đóng cửa nhưng không nhận pin
        else:
            rows.append(row(slot_in, opened, "OPEN", False))
            rows.append(row(slot_in, t6[1], "CLOSED", True, soc_in))

    if t7 is not None and slot_out is not None:
        opened = t7[0] + timedelta(seconds=2)
        if failure_code == "SLOT_OUT_DOOR_JAM":
            rows.append(row(slot_out, t7[1], "CLOSED", True, soc_out))
        elif failure_code == "USR_NOT_COLLECTED":
            rows.append(row(slot_out, t7[1], "OPEN", True, soc_out))  # cửa mở, pin còn
        else:
            rows.append(row(slot_out, opened, "OPEN", True, soc_out))
            rows.append(row(slot_out, t7[1], "CLOSED", False))
    return rows
