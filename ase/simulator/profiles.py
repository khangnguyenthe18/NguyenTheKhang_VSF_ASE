"""Loader Pydantic cho `profiles.yaml`."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ase.common.models import Step

PROFILES_PATH = Path(__file__).resolve().parent / "profiles.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BackgroundFailure(_Strict):
    code: str
    weight: float = Field(gt=0)
    status: Literal["FAILED", "TIMEOUT"] = "FAILED"
    omit_code_allowed: bool = False


class CommonSpec(_Strict):
    cabinets_per_station: tuple[int, int]
    slots_per_cabinet: int = Field(ge=2)
    battery_lots: int = Field(ge=1)
    app_versions: list[str]
    package_mix: dict[str, float]
    users_per_station: int = Field(ge=1)
    debt_user_share: float = Field(ge=0, lt=1)
    expired_user_share: float = Field(ge=0, lt=1)
    pricing_mode: Literal["standard_fee"]
    peak_hours_local: list[tuple[int, int]]
    peak_weight: float = Field(gt=0)
    night_hours_local: tuple[int, int]
    night_weight: float = Field(gt=0)
    background_fail_rate: float = Field(ge=0, le=1)
    missing_error_code_rate: float = Field(ge=0, le=1)
    step_seconds: dict[Step, tuple[int, int]]
    latency_median_ms: dict[Step, int]
    ambient_temp_c: tuple[float, float]
    background_failures: list[BackgroundFailure]

    @model_validator(mode="after")
    def _complete(self) -> CommonSpec:
        for name in ("step_seconds", "latency_median_ms"):
            if set(getattr(self, name)) != set(Step):
                raise ValueError(f"{name} phải có đủ 7 bước")
        return self


class SplitRule(_Strict):
    baseline_days: int = Field(ge=0)
    after: str

    def split_for(self, day_index: int) -> str:
        return "baseline" if day_index < self.baseline_days else self.after


class ProfileSpec(_Strict):
    seed: int
    test_seed: int | None = None
    start_date: date
    days: int = Field(ge=1)
    sessions_per_day: int = Field(ge=1)
    stations: dict[str, int]
    split: SplitRule
    # Trạm VN lấy toạ độ từ tủ đổi pin VinFast thật (real_sites.py). Chỉ cho trình diễn.
    real_sites: bool = False
    # Bản JSONL của cabinet_telemetry (không module nào đọc; mọi nơi đọc parquet). Tắt ở profile
    # lớn để giảm dung lượng: heartbeat vẫn 1 phút vì detector (gap 120 s) và dashboard
    # (online 2 phút) giả định chu kỳ này.
    telemetry_jsonl: bool = True
    # Danh sách giao dịch (agent quét 30 phút): chỉ VN (góp ý Gate 0), trạm đặt tại điểm đổi pin
    # VinFast thật, lấy mẫu phân tầng theo tỉnh; lưu lượng mỗi trạm theo `txn_traffic` (đúng
    # mật độ thực tế). Pipeline event 7 bước cũ (đóng băng) vẫn dùng `stations` 3 nước.
    txn_sites: int | None = Field(default=None, ge=1)  # None = toàn bộ điểm thật (profile scale)
    txn_heartbeat_minutes: int = Field(default=10, ge=1)
    txn_telemetry: bool = True
    txn_only: bool = False  # profile chỉ có danh sách giao dịch (không sinh dữ liệu pipeline cũ)
    # Che mã điểm đổi pin thật (bộ dữ liệu dùng chung với đối tác): mã trạm giữ tỉnh, 5 số giả;
    # toạ độ làm tròn ~5 km. Mật độ, phân bổ theo tỉnh, số đo không đổi.
    mask_site_codes: bool = False

    def seed_for_day(self, day_index: int) -> int:
        """Ngày thuộc tập test dùng `test_seed` (khác seed dev) để tránh overfit."""
        if self.test_seed is not None and day_index >= self.split.baseline_days:
            return self.test_seed
        return self.seed

    def for_transactions(self, n_sites: int) -> ProfileSpec:
        """Bản profile cho simulator danh sách giao dịch: `n_sites` trạm VN đặt tại điểm thật."""
        return self.model_copy(update={"stations": {"VN": n_sites}, "real_sites": True})


class TxnTraffic(_Strict):
    """Lưu lượng thực tế (chủ dự án, 11/11): ≈ 2,8–3 triệu GD/tháng cho toàn bộ điểm đổi pin VN.

    Mỗi trạm (một điểm trong file tủ thật) có tốc độ = `base × (số điểm của tỉnh / bình quân số
    điểm mỗi tỉnh) ^ province_alpha`: tỉnh/thành nhiều điểm thì mỗi điểm cũng đông giao dịch hơn.
    `base` tính sao cho tổng toàn quốc = monthly_total / days_per_month. `province_alpha` là GIẢ
    ĐỊNH (chưa có số liệu theo tỉnh), cần OPS xác nhận.
    """

    monthly_total: int = Field(gt=0)
    days_per_month: int = Field(default=30, gt=0)
    province_alpha: float = Field(ge=0)
    cabinets_per_site: tuple[int, int] = (1, 2)
    users_per_site: int = Field(default=10, ge=1)


class ProfilesFile(_Strict):
    common: CommonSpec
    txn_traffic: TxnTraffic
    profiles: dict[str, ProfileSpec]


def load_profiles(path: Path = PROFILES_PATH) -> ProfilesFile:
    with path.open(encoding="utf-8") as f:
        return ProfilesFile.model_validate(yaml.safe_load(f))
