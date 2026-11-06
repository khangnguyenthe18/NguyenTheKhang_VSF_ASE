"""Demo gửi 3 incident mẫu cố định qua NotifierService (`demo.py [--dry-run]`)."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import orjson
import polars as pl

from ase.common.config import Settings, get_settings
from ase.common.models import Category, Incident, Severity, Step
from ase.common.time import ensure_utc
from ase.common.transport import Clock, WallClock
from ase.notifier.service import NotifierService, NotifyReport


def sample_incidents() -> list[Incident]:
    """3 incident mẫu có dữ liệu cố định, ID khớp chuẩn simulator (NOTIFIER_PLAN §3.4)."""
    return [
        # (1) SYSTEM toàn cầu VN+PH+ID, SESS_CREATE_5XX bước 3 (deploy 503), P2
        Incident(
            incident_id="INC-20261104-0001",
            revision=1,
            category=Category.SYSTEM,
            severity=Severity.P2,
            error_code="SESS_CREATE_5XX",
            step=Step.SCAN_QR,
            station_id="VN-ST-001",
            cabinet_id="VN-ST-001-CAB-01",
            slot_id="03",
            country="GLOBAL",
            timezone="Asia/Ho_Chi_Minh",
            countries=["VN", "PH", "ID"],
            session_ids=[
                "SW20261104-000101",
                "SW20261104-000102",
                "SW20261104-000103",
            ],
            first_seen_at=datetime(2026, 11, 4, 3, 15, 0, tzinfo=UTC),
            last_seen_at=datetime(2026, 11, 4, 3, 20, 0, tzinfo=UTC),
            n_sessions=42,
            n_stations=5,
            window_minutes=5,
            confidence=0.92,
            probable_cause=(
                "Session-service trả lỗi 503 trên cả 3 nước (VN, PH, ID) sau đợt deploy"
                " lúc 10:14 (latency_ms tăng vọt trước khi fail)."
            ),
            evidence=["evt-SW20261104-000101-3-0", "evt-SW20261104-000102-3-0"],
            suggested_actions=[
                "Kiểm tra bản deploy gần nhất của session-service",
                "Rollback session-service nếu tỷ lệ 5xx tiếp tục cao",
            ],
            runbook_ids=["RB-SYS-SESS-5XX"],
            needs_human_review=False,
        ),
        # (2) PAYMENT chỉ PH, PH_WALLET_1, PAY_GATEWAY_TIMEOUT, trạm Asia/Manila -> GMT+8
        Incident(
            incident_id="INC-20261104-0002",
            revision=1,
            category=Category.PAYMENT,
            severity=Severity.P2,
            error_code="PAY_GATEWAY_TIMEOUT",
            step=Step.PAYMENT,
            station_id="PH-ST-001",
            cabinet_id="PH-ST-001-CAB-02",
            slot_id="05",
            country="PH",
            timezone="Asia/Manila",
            countries=["PH"],
            session_ids=[
                "SW20261104-000205",
                "SW20261104-000208",
            ],
            first_seen_at=datetime(2026, 11, 4, 4, 30, 0, tzinfo=UTC),
            last_seen_at=datetime(2026, 11, 4, 4, 45, 0, tzinfo=UTC),
            n_sessions=18,
            n_stations=2,
            window_minutes=15,
            confidence=0.89,
            probable_cause=(
                "Cổng thanh toán ví điện tử PH_WALLET_1 tại Philippines ngừng phản hồi"
                " callback (timeout) trong 15 phút."
            ),
            evidence=["evt-SW20261104-000205-5-0", "evt-SW20261104-000208-5-0"],
            suggested_actions=[
                "Kiểm tra trạng thái kết nối tới đối tác PH_WALLET_1",
                "Theo dõi hàng đợi đối soát callback trễ",
            ],
            runbook_ids=["RB-PAY-GATEWAY-TIMEOUT"],
            needs_human_review=False,
        ),
        # (3) PAYMENT P1 PAID_NOT_DELIVERED, VN (bước 1–5 OK, không có event bước 6/7)
        Incident(
            incident_id="INC-20261104-0003",
            revision=1,
            category=Category.PAYMENT,
            severity=Severity.P1,
            error_code="PAID_NOT_DELIVERED",
            step=Step.PAYMENT,
            station_id="VN-ST-002",
            cabinet_id="VN-ST-002-CAB-03",
            slot_id=None,
            country="VN",
            timezone="Asia/Ho_Chi_Minh",
            countries=["VN"],
            session_ids=["SW20261104-000312"],
            first_seen_at=datetime(2026, 11, 4, 5, 10, 30, tzinfo=UTC),
            last_seen_at=datetime(2026, 11, 4, 5, 12, 0, tzinfo=UTC),
            n_sessions=1,
            n_stations=1,
            window_minutes=5,
            confidence=0.98,
            probable_cause=(
                "Khách hàng đã bị trừ tiền (captured=true ở bước 5) nhưng phiên đổi pin"
                " dừng im lặng, không phát sinh sự kiện bước 6/7 (không nhả pin)."
            ),
            evidence=["evt-SW20261104-000312-5-0", "PAY-SW20261104-000312-0"],
            suggested_actions=[
                (
                    "Kiểm tra ngay bản ghi thanh toán PAY-SW20261104-000312-0"
                    " và trạng thái tủ VN-ST-002-CAB-03"
                ),
                "Hỗ trợ xuất pin thủ công hoặc hoàn tiền cho khách hàng",
            ],
            runbook_ids=["RB-PAY-PAID-NOT-DELIVERED"],
            needs_human_review=True,
        ),
    ]


_DEMO_NOTE = (
    " [Incident MẪU dựng từ ground truth của mock để demo luồng; chưa phải output detector.]"
)


def _evidence_ids(events_path: Path, session_ids: set[str], limit: int = 5) -> list[str]:
    """event_id CÓ THẬT trong luồng (SF9): event FAILED/TIMEOUT hoặc event cuối của các phiên."""
    found: list[str] = []
    last_of: dict[str, str] = {}
    with events_path.open("rb") as f:
        for line in f:
            ev = orjson.loads(line)
            sid = ev["session_id"]
            if sid not in session_ids:
                continue
            last_of[sid] = ev["event_id"]
            if ev["status"] in ("FAILED", "TIMEOUT") and ev["event_id"] not in found:
                found.append(ev["event_id"])
    for eid in last_of.values():
        if eid not in found:
            found.append(eid)
    return found[:limit]


def incidents_from_profile(profile_dir: Path) -> list[Incident]:
    """3 incident mẫu dựng từ phiên THẬT của một profile (mặc định `dev`), để demo luồng.

    Vì sao đọc ground truth: detector chưa có, nên demo cần incident trỏ đúng phiên/thời điểm có
    trong dữ liệu đang hiển thị trên dashboard. Đây là công cụ demo, có nhãn "MẪU"; code detector
    và các tab vận hành của dashboard không bao giờ đọc ground truth.
    """
    # Đọc injected_at dạng chuỗi rồi parse rõ ràng: CSV trộn có/không phần lẻ giây nên
    # suy kiểu tự động của polars không ổn định giữa các profile.
    gt = pl.read_csv(profile_dir / "ground_truth.csv", infer_schema=False)
    sessions = pl.read_parquet(profile_dir / "swap_sessions.parquet")
    stations = pl.read_parquet(profile_dir / "stations.parquet")
    tz_of = dict(zip(stations["station_id"], stations["timezone"], strict=True))
    country_of = dict(zip(stations["station_id"], stations["country"], strict=True))
    rows = gt.join(sessions.select("session_id", "station_id", "cabinet_id"), on="session_id")

    specs: list[tuple[str, Category, Severity, Step, str]] = [
        (
            "deploy_503",
            Category.SYSTEM,
            Severity.P2,
            Step.SCAN_QR,
            "Session-service trả 503 đồng loạt ở nhiều nước, latency tăng dần trước khi lỗi.",
        ),
        (
            "ph_wallet_outage",
            Category.PAYMENT,
            Severity.P2,
            Step.PAYMENT,
            "Ví PH_WALLET_1 ngừng phản hồi (timeout/callback trễ), chỉ ảnh hưởng PH.",
        ),
        (
            "paid_not_delivered",
            Category.PAYMENT,
            Severity.P1,
            Step.PAYMENT,
            "Đã trừ tiền (captured=true) nhưng phiên dừng sau bước 5, không có bước 6/7.",
        ),
    ]
    incidents: list[Incident] = []
    for idx, (scenario, cat, sev, step, cause) in enumerate(specs, start=1):
        sc = rows.filter(pl.col("scenario") == scenario).sort("injected_at")
        sc = sc.filter(pl.col("injected_at").is_not_null() & (pl.col("injected_at") != ""))
        if sc.height == 0:
            continue
        inst = sc["scenario_instance_id"][0]
        grp = sc.filter(pl.col("scenario_instance_id") == inst) if inst else sc.head(1)
        if scenario == "paid_not_delivered":
            grp = grp.head(1)  # mỗi phiên trừ tiền mà không nhả pin là 1 incident P1
        first = grp.row(0, named=True)
        st_id = str(first["station_id"])
        countries = sorted({country_of[s] for s in grp["station_id"]})
        codes = [c for c in grp["error_code"].to_list() if c]
        times = [ensure_utc(datetime.fromisoformat(str(t))) for t in grp["injected_at"]]
        first_seen, last_seen = min(times), max(times)
        sids = [str(x) for x in grp["session_id"].to_list()]
        incidents.append(
            Incident(
                incident_id=f"INC-DEMO-{first_seen:%Y%m%d}-{idx:04d}",
                category=cat,
                severity=sev,
                error_code=max(set(codes), key=codes.count) if codes else None,
                step=step,
                station_id=st_id,
                cabinet_id=first["cabinet_id"],
                country="GLOBAL" if len(countries) > 1 else countries[0],
                countries=countries,
                timezone=tz_of[st_id],
                session_ids=sids[:50],
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                n_sessions=len(sids),
                n_stations=grp["station_id"].n_unique(),
                window_minutes=max(1, int((last_seen - first_seen).total_seconds() // 60)),
                confidence=0.9,
                probable_cause=(cause + _DEMO_NOTE)[:500],
                evidence=_evidence_ids(profile_dir / "ase.swap.events.jsonl", set(sids)),
                suggested_actions=["Kiểm tra theo runbook tương ứng; xác nhận lại trên dashboard"],
                needs_human_review=True,
            )
        )
    return incidents


async def run_demo(
    dry_run: bool = False,
    live: bool | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
    service: NotifierService | None = None,
    incidents: list[Incident] | None = None,
) -> list[NotifyReport]:
    cfg = settings or get_settings()
    if dry_run or live is False:
        cfg = cfg.model_copy(update={"notify_mode": "dry_run"})
    elif live is True:
        cfg = cfg.model_copy(update={"notify_mode": "live"})
    svc = service or NotifierService(settings=cfg, clock=clock or WallClock())
    reports: list[NotifyReport] = []
    for inc in incidents if incidents is not None else sample_incidents():
        report = await svc.notify(inc)
        reports.append(report)
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description="Chạy demo gửi 3 cảnh báo mẫu của ASE")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ghi file preview ra data/outbox_preview/ thay vì gửi qua SMTP",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Gửi cảnh báo qua SMTP tới Mailpit (mặc định đã là live)",
    )
    parser.add_argument(
        "--from-profile",
        default=None,
        help="Dựng 3 incident mẫu từ phiên thật của profile (vd dev) thay vì dữ liệu cố định",
    )
    args = parser.parse_args()
    incs = None
    if args.from_profile:
        incs = incidents_from_profile(get_settings().profile_dir(args.from_profile))
        print(
            f"Dựng {len(incs)} incident mẫu từ data/{args.from_profile}: "
            + ", ".join(i.incident_id for i in incs)
        )
    reports = asyncio.run(run_demo(dry_run=bool(args.dry_run), incidents=incs))
    has_mailpit_fail = any(
        r.channel == "email" and r.status == "FAILED" and "Mailpit chưa chạy" in (r.detail or "")
        for rep in reports
        for r in rep.results
    )
    if has_mailpit_fail:
        print(
            "\n[LỖI] Mailpit chưa chạy? Hãy chạy 'make mail-up' để khởi động Mailpit"
            " (http://localhost:8025).\n"
        )


if __name__ == "__main__":
    main()
