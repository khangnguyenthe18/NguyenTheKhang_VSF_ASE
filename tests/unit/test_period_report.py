"""Báo cáo kỳ: KPI success factor, Top N lỗi, health score, open point, lịch gửi, SF10."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from ase.agent.source import load_transactions
from ase.common.models import AlertLevel, SwapTransaction, TxnStatus
from ase.evaluator.txn_eval import check_reports, load_ground_truth
from ase.knowledge.loader import default_error_table, default_knowledge, default_kpi
from ase.notifier.period_mail import render_period_report
from ase.report.period import build_period_report, due_reports, health_score
from ase.simulator.profiles import load_profiles
from ase.simulator.txn_generator import TXN_GROUND_TRUTH, generate_transactions

KPI = default_kpi()
SCAN = default_knowledge().detector.scan
TABLE = default_error_table()
T0 = datetime(2026, 10, 19, 3, 0, tzinfo=UTC)  # 10:00 thứ Hai 19/10 giờ VN


def _txn(i: int, status: TxnStatus = TxnStatus.COMPLETED, code: str | None = None,
         cabinet: str = "B.TST00001.001", day: int = 0) -> SwapTransaction:  # fmt: skip
    start = T0 + timedelta(days=day, minutes=i)
    end = start + timedelta(seconds=70)
    done = status is TxnStatus.COMPLETED
    return SwapTransaction(
        station_id=cabinet.rsplit(".", 1)[0],
        cabinet_id=cabinet,
        cabinet_type="CC_06_P",
        emsp_txn_id=f"{i:024x}",
        started_at=start,
        status=status,
        error_code=code,
        battery_in_id="BAT1",
        battery_in_at=start + timedelta(seconds=30),
        battery_out_id="BAT2" if done else None,
        battery_out_at=end if done else None,
        country="VN",
        timezone="Asia/Ho_Chi_Minh",
        updated_at=end,
        user_id=f"u{i % 7}",
    )


def _frame(txns: list[SwapTransaction]) -> pl.DataFrame:
    return pl.DataFrame([t.model_dump() for t in txns], infer_schema_length=None)


def test_top_n_percentages_and_kpis() -> None:
    txns = [_txn(i) for i in range(100)]
    codes = (
        ["D6031"] * 5 + ["C6029"] * 3 + ["D6036"] * 2 + ["X0000"]
    )  # X0000: mã giả, ngoài bảng chuẩn
    txns += [_txn(200 + k, TxnStatus.FAILED, c) for k, c in enumerate(codes)]
    r = build_period_report(
        _frame(txns),
        kind="weekly",
        start=date(2026, 10, 19),
        end=date(2026, 10, 25),
        generated_at=datetime(2026, 10, 26, 1, tzinfo=UTC),
        spec=KPI,
        scan=SCAN,
        table=TABLE,
        top_n=3,
    )
    assert r.report_id == "report:weekly:2026-10-19"
    (s,) = r.sections
    assert (s.n_total, s.n_failures, s.n_completed) == (111, 11, 100)
    assert [(c.code, c.n) for c in s.top] == [("D6031", 5), ("C6029", 3), ("D6036", 2)]
    assert s.top[0].pct == pytest.approx(45.45) and s.top_pct == pytest.approx(90.91, abs=0.01)
    assert s.top[1].name == "LockTimeoutError" and s.top[1].level is AlertLevel.HIGH
    outside = next(c for c in s.all_codes if c.code == "X0000")
    assert outside.name == "mã ngoài bảng chuẩn" and outside.level is None
    assert sum(c.n for c in s.all_codes) == s.n_failures
    assert s.success_rate == pytest.approx(100 / 111)
    assert s.swap_seconds_p50 == pytest.approx(70.0)
    assert s.n_returned_not_received == 11  # mọi giao dịch lỗi ở đây đều đã có pin cũ vào

    mail = render_period_report(r)
    assert (
        "Top 3 lỗi đang chiếm số lượng lớn 90.91% trong tổng số 11 các phiên lỗi" in mail.text_body
    )
    assert "1. D6031 – BatteryInTimeout (Low), chiếm 45.45% (5 case)" in mail.text_body
    assert "tuần 19/10–25/10/2026" in mail.subject
    assert mail.csv_text.startswith("﻿Hệ thống BSS,Mã lỗi")


def test_health_score_formula_and_labels() -> None:
    cab = next(s for s in KPI.services if s.id == "cabinet")
    assert health_score(1000, 40, 0, cab, KPI) == (100.0, "Tốt")  # dưới mục tiêu 5%
    assert health_score(1000, 80, 0, cab, KPI) == (85.0, "Cần theo dõi")  # 8%: −5 × 3
    assert health_score(1000, 80, 20, cab, KPI) == (65.0, "Kém")  # thêm 2% mã High: −10 × 2
    assert health_score(0, 0, 0, cab, KPI)[0] == 100.0
    assert health_score(10, 10, 10, cab, KPI)[0] == 0.0


def test_open_points_come_from_kpis() -> None:
    good = [_txn(i, cabinet=f"B.TST0000{1 + i % 4}.009") for i in range(400)]
    bad = [_txn(1000 + i, cabinet="B.TST00005.001") for i in range(40)]
    bad += [_txn(2000 + i, TxnStatus.FAILED, "C6029", cabinet="B.TST00005.001") for i in range(8)]
    r = build_period_report(
        _frame(good + bad), kind="custom", start=date(2026, 10, 19), end=date(2026, 10, 25),
        generated_at=datetime(2026, 10, 26, tzinfo=UTC), spec=KPI, scan=SCAN, table=TABLE,
    )  # fmt: skip
    ops = {o.id: o for o in r.sections[0].open_points}
    assert "OP-HEALTH-B.TST00005.001" in ops and "OP-REPEAT-B.TST00005.001-C6029" in ops
    assert all("B.TST00005" in k for k in ops)  # tủ tốt không có open point
    op = ops["OP-HEALTH-B.TST00005.001"]
    assert "C6029 – LockTimeoutError" in op.recommendations[0]
    assert op.evidence and all(e in {t.emsp_txn_id for t in bad} for e in op.evidence)
    assert op.priority_score == pytest.approx(8 * 3)  # 8 giao dịch × trọng số High 3


def test_due_reports_schedule() -> None:
    def at(d: int, h: int, m: int) -> datetime:
        return datetime(2026, 10 if d > 1 else 11, d, h, m, tzinfo=UTC)

    # 08:00 thứ Hai 26/10 giờ VN = 01:00 UTC → báo cáo tuần 19/10–25/10
    weekly = ("weekly", date(2026, 10, 19), date(2026, 10, 25))
    assert due_reports(at(26, 0, 30), at(26, 1, 0), KPI) == [weekly]
    assert due_reports(at(26, 1, 0), at(26, 1, 30), KPI) == []
    # 08:00 ngày 1/11 (Chủ nhật) → báo cáo tháng 10
    monthly = ("monthly", date(2026, 10, 1), date(2026, 10, 31))
    assert due_reports(at(1, 0, 30), at(1, 1, 0), KPI) == [monthly]


def test_transaction_profiles_are_vietnam_only() -> None:
    profiles = load_profiles()
    for name, p in profiles.profiles.items():
        assert set(p.for_transactions().stations) == {"VN"}, name


def test_sf10_report_counts_match_ground_truth(tmp_path: Path) -> None:
    out = tmp_path / "dev"
    asyncio.run(generate_transactions("dev", out))
    df = load_transactions(out)
    r = build_period_report(
        df, kind="weekly", start=date(2026, 10, 19), end=date(2026, 10, 25),
        generated_at=datetime(2026, 10, 26, 1, tzinfo=UTC), spec=KPI, scan=SCAN, table=TABLE,
    )  # fmt: skip
    truth = load_ground_truth(out / TXN_GROUND_TRUTH)
    started = df.select("emsp_txn_id", "started_at")
    n_ok, bad = check_reports([r], truth, started)
    assert (n_ok, bad) == (1, [])
    broken = r.model_copy(deep=True)
    broken.sections[0].all_codes[0].n += 1
    assert check_reports([broken], truth, started)[0] == 0
