"""Nạp và cache dữ liệu vận hành cho Dashboard (DASHBOARD_PLAN §2, §3).

Nguyên tắc:
- Read-only: chỉ đọc Parquet, JSONL và SQLite (mode=ro), tuyệt đối không ghi file.
- Không đọc Ground Truth: dashboard chỉ hiển thị dữ liệu vận hành, không đọc `ground_truth.csv`.
- Cache theo mtime của `manifest.json` để mọi API trả kết quả dưới 1 giây sau lần nạp đầu.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from ase.common.config import Settings, get_settings

SWAP_EVENT_SCHEMA: dict[str, pl.DataType] = {
    "event_id": pl.String(),
    "session_id": pl.String(),
    "station_id": pl.String(),
    "cabinet_id": pl.String(),
    "slot_id": pl.String(),
    "step": pl.String(),
    "status": pl.String(),
    "error_code": pl.String(),
    "error_message": pl.String(),
    "latency_ms": pl.Int64(),
    "occurred_at": pl.String(),
}


@dataclass(frozen=True)
class ProfileData:
    profile: str
    profile_dir: Path
    manifest_mtime: float
    stations: pl.DataFrame
    cabinets: pl.DataFrame
    slots: pl.DataFrame
    batteries: pl.DataFrame
    users: pl.DataFrame
    swap_sessions: pl.DataFrame
    payments: pl.DataFrame
    cabinet_telemetry: pl.DataFrame
    swap_events: pl.DataFrame
    min_time: datetime
    max_time: datetime
    default_as_of: datetime


@dataclass(frozen=True)
class NotifierSnapshot:
    outbox: list[dict[str, object]]
    cards: list[dict[str, object]]
    latest_feedback: dict[str, tuple[str, str]]  # incident_id -> (action, at)


_PROFILE_CACHE: dict[Path, ProfileData] = {}


def clear_cache() -> None:
    _PROFILE_CACHE.clear()


def available_profiles(settings: Settings | None = None) -> list[str]:
    cfg = settings or get_settings()
    found: list[str] = []
    for name in ("smoke", "dev", "eval"):
        p_dir = cfg.profile_dir(name)
        if (p_dir / "stations.parquet").exists():
            found.append(name)
    if cfg.data_dir.exists():
        for child in sorted(cfg.data_dir.iterdir()):
            if child.is_dir() and child.name not in found and (child / "stations.parquet").exists():
                found.append(child.name)
    return found or ["dev"]


def _manifest_mtime(profile_dir: Path) -> float:
    manifest_path = profile_dir / "manifest.json"
    if manifest_path.exists():
        return manifest_path.stat().st_mtime
    stations_path = profile_dir / "stations.parquet"
    if stations_path.exists():
        return stations_path.stat().st_mtime
    raise FileNotFoundError(f"Không tìm thấy dữ liệu profile tại {profile_dir}")


def _ensure_utc_col(df: pl.DataFrame, col: str) -> pl.DataFrame:
    if col not in df.columns:
        return df
    dtype = df.schema[col]
    if isinstance(dtype, pl.String):
        return df.with_columns(pl.col(col).str.to_datetime(time_zone="UTC", strict=False))
    if isinstance(dtype, pl.Datetime):
        if dtype.time_zone is None:
            return df.with_columns(pl.col(col).dt.replace_time_zone("UTC"))
        if dtype.time_zone != "UTC":
            return df.with_columns(pl.col(col).dt.convert_time_zone("UTC"))
    return df


def load_profile_data(
    profile: str = "dev",
    *,
    settings: Settings | None = None,
    data_dir: Path | None = None,
) -> ProfileData:
    """Nạp dữ liệu vận hành (Parquet + events), cache theo mtime `manifest.json`.

    Tuyệt đối KHÔNG đọc `ground_truth.csv`.
    """
    cfg = settings or get_settings()
    base_dir = data_dir if data_dir is not None else cfg.data_dir
    profile_dir = (base_dir / profile).resolve()
    if base_dir.resolve() not in profile_dir.parents:
        raise FileNotFoundError(f"Profile '{profile}' nằm ngoài thư mục dữ liệu")
    if not profile_dir.exists() or not (profile_dir / "stations.parquet").exists():
        raise FileNotFoundError(f"Profile '{profile}' chưa có dữ liệu tại {profile_dir}")

    mtime = _manifest_mtime(profile_dir)
    cached = _PROFILE_CACHE.get(profile_dir)
    if cached is not None and cached.manifest_mtime == mtime:
        return cached

    stations = pl.read_parquet(profile_dir / "stations.parquet")
    if "lat" not in stations.columns:
        stations = stations.with_columns(
            pl.lit(0.0).alias("lat"),
            pl.lit(0.0).alias("lon"),
        )
    cabinets = pl.read_parquet(profile_dir / "cabinets.parquet")
    slots = pl.read_parquet(profile_dir / "slots.parquet")
    batteries = pl.read_parquet(profile_dir / "batteries.parquet")
    users = pl.read_parquet(profile_dir / "users.parquet")
    swap_sessions = _ensure_utc_col(
        _ensure_utc_col(pl.read_parquet(profile_dir / "swap_sessions.parquet"), "started_at"),
        "ended_at",
    )
    payments = _ensure_utc_col(pl.read_parquet(profile_dir / "payments.parquet"), "callback_at")
    cabinet_telemetry = _ensure_utc_col(
        pl.read_parquet(profile_dir / "cabinet_telemetry.parquet"), "heartbeat_at"
    ).sort("heartbeat_at")

    events_raw = pl.read_ndjson(
        profile_dir / "ase.swap.events.jsonl",
        schema=SWAP_EVENT_SCHEMA,
    )
    swap_events = (
        events_raw.unique(subset=["event_id"], keep="first", maintain_order=True)
        .with_columns(pl.col("occurred_at").str.to_datetime(time_zone="UTC"))
        .sort(["occurred_at", "event_id"])
    )

    min_dt_raw = swap_events["occurred_at"].min()
    max_dt_raw = swap_events["occurred_at"].max()
    if not isinstance(min_dt_raw, datetime) or not isinstance(max_dt_raw, datetime):
        raise ValueError(f"Không có occurred_at hợp lệ trong {profile_dir}")

    min_time = min_dt_raw.astimezone(UTC)
    max_time = max_dt_raw.astimezone(UTC)

    # Mặc định `as_of` = `occurred_at` lớn nhất trong cửa sổ dữ liệu chung mà mọi múi giờ
    # đều còn đang chạy (tránh 27 giây tràn qua nửa đêm ngày kế tiếp khi kết thúc mô phỏng).
    default_as_of = max_time
    hb_only = cabinet_telemetry.filter(pl.col("slot_id").is_null())
    if hb_only.height > 0 and cabinets.height > 0:
        min_tz_max_hb = (
            hb_only.join(cabinets.select(["cabinet_id", "timezone"]), on="cabinet_id")
            .group_by("timezone")
            .agg(pl.col("heartbeat_at").max().alias("max_hb"))
            .select(pl.col("max_hb").min())
            .item()
        )
        if isinstance(min_tz_max_hb, datetime):
            in_win_max = (
                swap_events.filter(pl.col("occurred_at") <= min_tz_max_hb)
                .select(pl.col("occurred_at").max())
                .item()
            )
            if isinstance(in_win_max, datetime):
                default_as_of = in_win_max.astimezone(UTC)

    loaded = ProfileData(
        profile=profile,
        profile_dir=profile_dir,
        manifest_mtime=mtime,
        stations=stations,
        cabinets=cabinets,
        slots=slots,
        batteries=batteries,
        users=users,
        swap_sessions=swap_sessions,
        payments=payments,
        cabinet_telemetry=cabinet_telemetry,
        swap_events=swap_events,
        min_time=min_time,
        max_time=max_time,
        default_as_of=default_as_of,
    )
    _PROFILE_CACHE[profile_dir] = loaded
    return loaded


def read_notifier_sqlite(db_path: Path) -> NotifierSnapshot:
    """Đọc read-only từ `notifier.sqlite` (nếu chưa có file thì trả rỗng, không tạo file)."""
    if not db_path.exists():
        return NotifierSnapshot(outbox=[], cards=[], latest_feedback={})

    uri = f"file:{db_path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return NotifierSnapshot(outbox=[], cards=[], latest_feedback={})

    try:
        conn.row_factory = sqlite3.Row
        tables = {
            str(r["name"])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        outbox_rows: list[dict[str, object]] = []
        if "outbox" in tables:
            cur = conn.execute("SELECT * FROM outbox ORDER BY created_at DESC, key ASC")
            outbox_rows = [dict(r) for r in cur.fetchall()]

        card_rows: list[dict[str, object]] = []
        if "cards" in tables:
            cur = conn.execute("SELECT * FROM cards ORDER BY id DESC")
            card_rows = [dict(r) for r in cur.fetchall()]

        latest_fb: dict[str, tuple[str, str]] = {}
        if "feedback" in tables:
            cur = conn.execute(
                """
                SELECT f.incident_id, f.action, f.at
                FROM feedback f
                INNER JOIN (
                    SELECT incident_id, MAX(id) AS max_id
                    FROM feedback
                    GROUP BY incident_id
                ) latest ON f.id = latest.max_id
                ORDER BY f.id DESC
                """
            )
            for r in cur.fetchall():
                latest_fb[str(r["incident_id"])] = (str(r["action"]), str(r["at"]))

        return NotifierSnapshot(outbox=outbox_rows, cards=card_rows, latest_feedback=latest_fb)
    finally:
        conn.close()


def extract_session_from_card_json(raw_json: str) -> tuple[str | None, str | None, str | None]:
    """Trích (first_session_id, category, severity) từ Adaptive Card JSON nếu có."""
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return None, None, None
    if not isinstance(parsed, dict):
        return None, None, None

    category = parsed.get("category") if isinstance(parsed.get("category"), str) else None
    severity = parsed.get("severity") if isinstance(parsed.get("severity"), str) else None
    first_sid = parsed.get("session_id") if isinstance(parsed.get("session_id"), str) else None

    if first_sid is None:
        # Tìm trong FactSet của Adaptive Card
        body = parsed.get("body")
        if isinstance(body, list):
            for block in body:
                if isinstance(block, dict) and block.get("type") == "FactSet":
                    facts = block.get("facts")
                    if isinstance(facts, list):
                        for fact in facts:
                            if isinstance(fact, dict) and "Mã giao dịch" in str(
                                fact.get("title", "")
                            ):
                                val = str(fact.get("value", "")).strip()
                                if val:
                                    first_sid = val.split()[0].strip(",")
    return first_sid, category, severity
