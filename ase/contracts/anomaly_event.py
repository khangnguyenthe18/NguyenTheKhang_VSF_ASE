"""Schema sự kiện bất thường CHUNG giữa ASE (Khang) và phần của Võ Doanh Nhân, v0.1.

Một schema cho cả hai chiều (mentor yêu cầu ở đánh giá Gate 0):
- `transaction_incident` (ASE → Nhân): sự cố gộp từ Danh sách giao dịch đổi pin.
- `baseline` (ASE → Nhân): mức nền theo tỉnh × giờ hoặc trạm × ngày (số giao dịch, tỷ lệ lỗi,
  health score) để Nhân làm ngưỡng cho cảnh báo "sụt giảm theo khung giờ" (v0.2: trạm × giờ quá
  thưa ở mật độ thật).
- `behavior` (Nhân → ASE): hành vi bất thường trên log (VinSOC scan, request lặp lại, sụt traffic,
  latency tăng dần sau deploy). ASE dùng làm bằng chứng `BHV-*` cho sự cố và open point.
Quy tắc: thêm field = không phá vỡ (consumer bỏ qua field lạ); đổi/xoá field = tăng major, cả hai
đồng ý. Thời gian ISO-8601 UTC. Mã lỗi (`codes`) là mã của bảng mã lỗi chuẩn, giữ nguyên văn.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Literal["0.1"] = "0.1"
SCHEMA_NAME = "ase.anomaly.event"


class Window(BaseModel):
    model_config = ConfigDict(extra="allow")

    start: datetime
    end: datetime


class Scope(BaseModel):
    model_config = ConfigDict(extra="allow")

    country: str = "VN"
    # Mã tỉnh trong mã trạm (vd `HCM`). Thêm ở contract v0.2 (trường tuỳ chọn, không phá vỡ):
    # mật độ thật ~0,5 GD/giờ/trạm nên mức nền và cảnh báo sụt traffic tính theo tỉnh × giờ.
    province: str | None = None
    station_id: str | None = None  # mã trạm vận hành, vd B.HCM01234
    cabinet_id: str | None = None  # mã tủ, vd B.HCM01234.002
    connector: str | None = None
    service: str | None = None  # dịch vụ critical (kpi.yaml) hoặc tên service trên log


class Metric(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str  # vd fail_rate, txn_count, latency_p95_ms, health_score, request_repeat
    observed: float
    baseline: float | None = None
    unit: str | None = None
    z_score: float | None = None


class AnomalyEvent(BaseModel):
    model_config = ConfigDict(extra="allow")  # consumer bỏ qua field lạ (tương thích tiến)

    schema_version: Literal["0.1"] = SCHEMA_VERSION
    event_id: str  # ASE: INC-*/BASE-*; Nhân: BHV-* — khoá idempotency
    source: Literal["ase", "partner"]
    kind: Literal["transaction_incident", "baseline", "behavior"]
    detected_at: datetime
    window: Window
    scope: Scope
    codes: list[str] = Field(default_factory=list)  # mã bảng chuẩn (nếu có)
    # mức theo bảng chuẩn; behavior: Nhân quy đổi mức cao → High, các mức còn lại → Low
    level: Literal["High", "Low"] | None = None
    metrics: list[Metric] = Field(default_factory=list)
    related_txn_ids: list[str] = Field(default_factory=list, max_length=50)  # mã GD eMSP
    evidence: list[str] = Field(default_factory=list, max_length=10)  # INC-*/BHV-*/mã GD
    summary: str = Field(max_length=500)
    confidence: float = Field(ge=0, le=1)
    notify: bool = False  # Nhân đặt true khi cần báo người: ASE là kênh push DUY NHẤT
    # v0.2 (đề xuất của Nhân, trường tuỳ chọn, không phá vỡ):
    # loại hành vi trên log (chỉ dùng cho kind=behavior; KHÔNG phải nhóm lỗi của bảng mã chuẩn)
    category: str | None = None
    dashboard_url: str | None = None  # link log stream của Nhân, lọc sẵn phạm vi + thời gian

    @field_validator("detected_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("detected_at phải có múi giờ (UTC)")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> AnomalyEvent:
        expected = {"transaction_incident": "ase", "baseline": "ase", "behavior": "partner"}
        if expected[self.kind] != self.source:
            raise ValueError(f"kind={self.kind} phải do source={expected[self.kind]} phát")
        if self.window.end < self.window.start:
            raise ValueError("window.end < window.start")
        prefix = {"transaction_incident": "INC-", "baseline": "BASE-", "behavior": "BHV-"}
        if not self.event_id.startswith(prefix[self.kind]):
            raise ValueError(f"event_id của {self.kind} phải bắt đầu bằng {prefix[self.kind]}")
        return self


def json_schema() -> dict[str, object]:
    schema = AnomalyEvent.model_json_schema()
    schema["$id"] = f"{SCHEMA_NAME}.v{SCHEMA_VERSION}"
    schema["title"] = f"{SCHEMA_NAME} v{SCHEMA_VERSION}"
    return schema
