"""Bảng phụ có điều kiện thời gian `<= clock.now()` (DETECTOR_PLAN §2.2, §4.4).

Nguyên tắc bắt buộc:
- Không bao giờ đọc `ground_truth.csv`.
- Không dùng `swap_sessions.final_status` hay `ended_at` (kết quả cuối mà lúc đang chạy chưa biết).
- `payments` chỉ lộ diện khi `callback_at is not None and callback_at <= now`.
- `cabinet_telemetry` chỉ lộ diện khi `heartbeat_at <= now`, tra cứu bằng `bisect`.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, field_validator

from ase.common.models import (
    Battery,
    Cabinet,
    CabinetTelemetry,
    Payment,
    Slot,
    Station,
    SwapSession,
    User,
)
from ase.common.time import ensure_utc

_SESSION_SAFE_COLUMNS: tuple[str, ...] = (
    "session_id",
    "user_id",
    "station_id",
    "cabinet_id",
    "slot_in",
    "slot_out",
    "battery_in_id",
    "battery_out_id",
    "app_version",
    "payment_provider",
    "started_at",
)


class SessionContext(BaseModel):
    """Thông tin phiên được phép biết khi phiên đã bắt đầu (bỏ `final_status` và `ended_at`)."""

    model_config = ConfigDict(extra="ignore")

    session_id: str
    user_id: str
    station_id: str
    cabinet_id: str | None = None
    slot_in: str | None = None
    slot_out: str | None = None
    battery_in_id: str | None = None
    battery_out_id: str | None = None
    app_version: str
    payment_provider: str | None = None
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        return ensure_utc(v)


class ContextStore:
    """Bộ nhớ tra cứu bảng tĩnh và bảng phụ có điều kiện thời gian `<= now`."""

    def __init__(
        self,
        *,
        stations: dict[str, Station] | None = None,
        cabinets: dict[str, Cabinet] | None = None,
        slots: dict[tuple[str, str], Slot] | None = None,
        batteries: dict[str, Battery] | None = None,
        users: dict[str, User] | None = None,
        session_raw: dict[str, dict[str, Any]] | None = None,
        payment_raw: dict[str, dict[str, Any]] | None = None,
        slot_tel_index: dict[tuple[str, str], tuple[list[datetime], list[dict[str, Any]]]]
        | None = None,
        cab_hb_index: dict[str, tuple[list[datetime], list[float | None]]] | None = None,
    ) -> None:
        self._stations = stations or {}
        self._cabinets = cabinets or {}
        self._slots = slots or {}
        self._batteries = batteries or {}
        self._users = users or {}
        self._session_raw = session_raw or {}
        self._session_cache: dict[str, SessionContext] = {}
        self._payment_raw = payment_raw or {}
        self._payment_cache: dict[str, Payment] = {}
        self._slot_tel_index = slot_tel_index or {}
        self._cab_hb_index = cab_hb_index or {}

    @classmethod
    def from_models(
        cls,
        *,
        stations: Sequence[Station] = (),
        cabinets: Sequence[Cabinet] = (),
        slots: Sequence[Slot] = (),
        batteries: Sequence[Battery] = (),
        users: Sequence[User] = (),
        sessions: Sequence[SwapSession | SessionContext] = (),
        payments: Sequence[Payment] = (),
        telemetry: Sequence[CabinetTelemetry] = (),
    ) -> ContextStore:
        st_map = {s.station_id: s for s in stations}
        cab_map = {c.cabinet_id: c for c in cabinets}
        slot_map = {(s.cabinet_id, s.slot_id): s for s in slots}
        bat_map = {b.battery_id: b for b in batteries}
        usr_map = {u.user_id: u for u in users}

        sess_raw: dict[str, dict[str, Any]] = {}
        for s in sessions:
            d = s.model_dump(mode="python")
            d.pop("final_status", None)
            d.pop("ended_at", None)
            sess_raw[s.session_id] = d

        pay_raw = {p.session_id: p.model_dump(mode="python") for p in payments}

        sorted_tel = sorted(telemetry, key=lambda t: ensure_utc(t.heartbeat_at))
        slot_groups: dict[tuple[str, str], tuple[list[datetime], list[dict[str, Any]]]] = {}
        cab_groups: dict[str, tuple[list[datetime], list[float | None]]] = {}

        for t in sorted_tel:
            hb = ensure_utc(t.heartbeat_at)
            if t.slot_id is not None:
                key = (t.cabinet_id, t.slot_id)
                if key not in slot_groups:
                    slot_groups[key] = ([], [])
                slot_groups[key][0].append(hb)
                row_dict = t.model_dump(mode="python")
                row_dict["heartbeat_at"] = hb
                slot_groups[key][1].append(row_dict)
            else:
                if t.cabinet_id not in cab_groups:
                    cab_groups[t.cabinet_id] = ([], [])
                cab_groups[t.cabinet_id][0].append(hb)
                cab_groups[t.cabinet_id][1].append(t.temperature)

        return cls(
            stations=st_map,
            cabinets=cab_map,
            slots=slot_map,
            batteries=bat_map,
            users=usr_map,
            session_raw=sess_raw,
            payment_raw=pay_raw,
            slot_tel_index=slot_groups,
            cab_hb_index=cab_groups,
        )

    @classmethod
    def from_profile_dir(cls, profile_dir: Path) -> ContextStore:
        """Nạp các bảng Parquet từ `data/<profile>/`. Tuyệt đối không đọc `ground_truth.csv`."""
        st_map: dict[str, Station] = {}
        st_path = profile_dir / "stations.parquet"
        if st_path.exists():
            for row in pl.read_parquet(st_path).to_dicts():
                st = Station.model_validate(row)
                st_map[st.station_id] = st

        cab_map: dict[str, Cabinet] = {}
        cab_path = profile_dir / "cabinets.parquet"
        if cab_path.exists():
            for row in pl.read_parquet(cab_path).to_dicts():
                cab = Cabinet.model_validate(row)
                cab_map[cab.cabinet_id] = cab

        slot_map: dict[tuple[str, str], Slot] = {}
        slot_path = profile_dir / "slots.parquet"
        if slot_path.exists():
            for row in pl.read_parquet(slot_path).to_dicts():
                sl = Slot.model_validate(row)
                slot_map[(sl.cabinet_id, sl.slot_id)] = sl

        bat_map: dict[str, Battery] = {}
        bat_path = profile_dir / "batteries.parquet"
        if bat_path.exists():
            for row in pl.read_parquet(bat_path).to_dicts():
                b = Battery.model_validate(row)
                bat_map[b.battery_id] = b

        usr_map: dict[str, User] = {}
        usr_path = profile_dir / "users.parquet"
        if usr_path.exists():
            for row in pl.read_parquet(usr_path).to_dicts():
                u = User.model_validate(row)
                usr_map[u.user_id] = u

        sess_raw: dict[str, dict[str, Any]] = {}
        sess_path = profile_dir / "swap_sessions.parquet"
        if sess_path.exists():
            # Chỉ đọc các cột an toàn, bỏ hoàn toàn `final_status` và `ended_at`
            df_sess = pl.read_parquet(sess_path, columns=list(_SESSION_SAFE_COLUMNS))
            for row in df_sess.to_dicts():
                sess_raw[str(row["session_id"])] = row

        pay_raw: dict[str, dict[str, Any]] = {}
        pay_path = profile_dir / "payments.parquet"
        if pay_path.exists():
            for row in pl.read_parquet(pay_path).to_dicts():
                pay_raw[str(row["session_id"])] = row

        slot_tel_index: dict[tuple[str, str], tuple[list[datetime], list[dict[str, Any]]]] = {}
        cab_hb_index: dict[str, tuple[list[datetime], list[float | None]]] = {}
        tel_path = profile_dir / "cabinet_telemetry.parquet"
        if tel_path.exists():
            df_tel = pl.read_parquet(tel_path)
            if df_tel.height > 0:
                slot_df = df_tel.filter(pl.col("slot_id").is_not_null()).sort("heartbeat_at")
                if slot_df.height > 0:
                    for key_tuple, sub_df in slot_df.partition_by(
                        ["cabinet_id", "slot_id"], as_dict=True
                    ).items():
                        c_id, s_id = str(key_tuple[0]), str(key_tuple[1])
                        times: list[datetime] = sub_df["heartbeat_at"].to_list()
                        slot_tel_index[(c_id, s_id)] = (times, sub_df.to_dicts())

                hb_df = df_tel.filter(pl.col("slot_id").is_null()).sort("heartbeat_at")
                if hb_df.height > 0:
                    for key_tuple, sub_df in hb_df.partition_by(
                        ["cabinet_id"], as_dict=True
                    ).items():
                        c_id = str(key_tuple[0])
                        hb_times: list[datetime] = sub_df["heartbeat_at"].to_list()
                        hb_temps: list[float | None] = sub_df["temperature"].to_list()
                        cab_hb_index[c_id] = (hb_times, hb_temps)

        return cls(
            stations=st_map,
            cabinets=cab_map,
            slots=slot_map,
            batteries=bat_map,
            users=usr_map,
            session_raw=sess_raw,
            payment_raw=pay_raw,
            slot_tel_index=slot_tel_index,
            cab_hb_index=cab_hb_index,
        )

    # --- Bảng tĩnh ----------------------------------------------------------

    def get_station(self, station_id: str) -> Station | None:
        return self._stations.get(station_id)

    def get_cabinet(self, cabinet_id: str) -> Cabinet | None:
        return self._cabinets.get(cabinet_id)

    def get_battery(self, battery_id: str) -> Battery | None:
        return self._batteries.get(battery_id)

    def get_user(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    # --- Bảng phụ có điều kiện thời gian <= now -----------------------------

    def get_session(self, session_id: str, now: datetime) -> SessionContext | None:
        now_utc = ensure_utc(now)
        cached = self._session_cache.get(session_id)
        if cached is None:
            raw = self._session_raw.get(session_id)
            if raw is None:
                return None
            cached = SessionContext.model_validate(raw)
            self._session_cache[session_id] = cached
        if cached.started_at <= now_utc:
            return cached
        return None

    def get_payment(self, session_id: str, now: datetime) -> Payment | None:
        """Trả về `Payment` chỉ khi `callback_at is not None and callback_at <= now`."""
        now_utc = ensure_utc(now)
        cached = self._payment_cache.get(session_id)
        if cached is None:
            raw = self._payment_raw.get(session_id)
            if raw is None:
                return None
            cached = Payment.model_validate(raw)
            self._payment_cache[session_id] = cached
        if cached.callback_at is not None and ensure_utc(cached.callback_at) <= now_utc:
            return cached
        return None

    def get_latest_slot_telemetry(
        self, cabinet_id: str, slot_id: str, now: datetime
    ) -> CabinetTelemetry | None:
        now_utc = ensure_utc(now)
        entry = self._slot_tel_index.get((cabinet_id, slot_id))
        if entry is None:
            return None
        times, rows = entry
        idx = bisect_right(times, now_utc) - 1
        if idx < 0:
            return None
        return CabinetTelemetry.model_validate(rows[idx])

    def get_slot_telemetry_window(
        self,
        cabinet_id: str,
        slot_id: str,
        now: datetime,
        *,
        lookback_seconds: float = 120.0,
    ) -> list[CabinetTelemetry]:
        """Lấy telemetry khoang trong `[latest - lookback_seconds, latest]` (`<= now`)."""
        now_utc = ensure_utc(now)
        entry = self._slot_tel_index.get((cabinet_id, slot_id))
        if entry is None:
            return []
        times, rows = entry
        hi = bisect_right(times, now_utc)
        if hi <= 0:
            return []
        latest_t = times[hi - 1]
        min_t = latest_t - timedelta(seconds=lookback_seconds)
        lo = hi - 1
        while lo > 0 and times[lo - 1] >= min_t:
            lo -= 1
        return [CabinetTelemetry.model_validate(rows[i]) for i in range(lo, hi)]

    def get_latest_temperature(
        self, cabinet_id: str, slot_id: str | None, now: datetime
    ) -> float | None:
        """Nhiệt độ gần nhất `<= now` (ưu tiên telemetry khoang, fallback heartbeat tủ)."""
        now_utc = ensure_utc(now)
        if slot_id is not None:
            slot_tel = self.get_latest_slot_telemetry(cabinet_id, slot_id, now_utc)
            if slot_tel is not None and slot_tel.temperature is not None:
                return slot_tel.temperature
        entry = self._cab_hb_index.get(cabinet_id)
        if entry is None:
            return None
        times, temps = entry
        idx = bisect_right(times, now_utc) - 1
        if idx < 0:
            return None
        return temps[idx]

    def get_cabinet_heartbeat_gap_seconds(
        self, cabinet_id: str, around_time: datetime, now: datetime
    ) -> float | None:
        """Khoảng trống heartbeat lớn nhất quanh `around_time` (chỉ xét `heartbeat_at <= now`)."""
        now_utc = ensure_utc(now)
        ev_utc = min(ensure_utc(around_time), now_utc)
        entry = self._cab_hb_index.get(cabinet_id)
        if entry is None:
            return None
        times, _ = entry
        upto = bisect_right(times, now_utc)
        if upto <= 0:
            return None
        idx = bisect_right(times, ev_utc, 0, upto) - 1
        if idx < 0:
            return None
        last_hb = times[idx]
        gap = (ev_utc - last_hb).total_seconds()
        if idx > 0 and ev_utc == last_hb:
            gap = max(gap, (last_hb - times[idx - 1]).total_seconds())
        if idx + 1 < upto:
            gap = max(gap, (times[idx + 1] - last_hb).total_seconds())
        return gap
