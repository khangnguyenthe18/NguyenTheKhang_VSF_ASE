"""Contract của agent quét: giao dịch lỗi phát hiện được, sự cố, và kết quả một lần quét."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ase.common.models import AlertLevel, TxnStatus

DetectionKind = Literal["FAILED", "STUCK"]
IncidentKind = Literal["WIDE", "STATION", "CABINET"]


class Detection(BaseModel):
    """Một giao dịch Thất bại hoặc kẹt, kèm thông tin tra từ bảng mã lỗi chuẩn."""

    emsp_txn_id: str
    cabinet_txn_id: str | None = None
    kind: DetectionKind
    status: TxnStatus
    station_id: str
    cabinet_id: str | None = None
    cabinet_type: str | None = None
    connector: str | None = None
    country: str
    timezone: str
    error_code: str | None = None
    error_name: str | None = None  # "Tên cảnh báo" của bảng chuẩn; None nếu mã ngoài bảng/không mã
    level: AlertLevel | None = None
    in_table: bool = False
    started_at: datetime
    event_at: datetime  # FAILED: lúc báo lỗi; STUCK: lúc vượt ngưỡng kẹt
    detected_at: datetime  # mốc quét phát hiện
    battery_in_id: str | None = None
    returned_not_received: bool = False  # khách đã trả pin cũ nhưng chưa có pin mới
    user_id: str | None = None
    incident_id: str | None = None

    @property
    def group(self) -> str:
        """Khoá gộp theo lỗi: mã lỗi, hoặc `STUCK` / `NO_CODE` khi không có mã."""
        return self.error_code or ("STUCK" if self.kind == "STUCK" else "NO_CODE")


class ScanResult(BaseModel):
    """Đầu ra của Scanner cho một mốc quét."""

    n_scanned: int  # số giao dịch trong cửa sổ nhìn lại
    detections: list[Detection]
    # Giao dịch LẦN ĐẦU thấy ở lần quét này, đếm theo (mã trạm, mã tủ): mẫu số để so với mức nền.
    new_by_cabinet: dict[tuple[str, str | None], int]


class ScanIncident(BaseModel):
    """Nhiều giao dịch lỗi cùng mẫu phân bố. Tái diễn → cùng `incident_id`, tăng revision."""

    incident_id: str
    revision: int = Field(ge=1)
    kind: IncidentKind
    key: str  # khoá ổn định qua các lần quét, vd `CABINET|B.HCM01234.002`
    scope: str  # hiển thị: mã tủ, mã trạm, mã nước hoặc `GLOBAL (ID, PH, VN)`
    groups: list[str]  # mã lỗi / STUCK / NO_CODE đã gặp, theo thứ tự xuất hiện
    level: AlertLevel | None = None  # mức cao nhất trong các mã đã gặp
    station_ids: list[str]
    countries: list[str]
    txn_ids: list[str]  # giao dịch MỚI của lần quét này
    n_total: int = Field(ge=1)  # tổng giao dịch từ khi mở sự cố
    first_seen_at: datetime
    last_seen_at: datetime


class ScanDigest(BaseModel):
    """Kết quả một lần quét: nguồn của thông báo (1 mail + 1 card) và của evaluator."""

    scan_id: str  # scan:{window_start:%Y%m%dT%H%MZ} — khoá idempotency khi gửi
    window_start: datetime
    scan_at: datetime
    n_scanned: int  # số giao dịch trong cửa sổ nhìn lại
    detections: list[Detection]
    incidents: list[ScanIncident]
