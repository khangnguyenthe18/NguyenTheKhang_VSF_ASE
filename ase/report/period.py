"""Báo cáo kỳ (tuần / tháng / tuỳ chọn): KPI success factor, Top N lỗi, health score, open point.

Vì sao nằm riêng khỏi agent quét: báo cáo kỳ trả lời câu hỏi của đề bài ("success factor của luồng
đổi pin là gì, dịch vụ critical nào yếu, cần làm gì"), còn agent quét 30 phút trả lời "có gì vừa
hỏng". Mọi con số do code tính từ danh sách giao dịch; LLM (V-LLM nội bộ, sau này) chỉ được diễn
giải các con số này. Thời gian lưu UTC; ranh giới kỳ theo giờ của đội OPS (`report.timezone`).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import BaseModel, Field

from ase.agent.source import view_as_of
from ase.common.models import AlertLevel, TxnStatus
from ase.knowledge.loader import ErrorTable, KpiSpec, ScanSpec, ServiceSpec

ReportKind = Literal["weekly", "monthly", "custom"]
STUCK = "STUCK"
NO_CODE = "NO_CODE"
GROUP_NAMES = {STUCK: "Giao dịch kẹt (không có mã lỗi)", NO_CODE: "Thất bại không có mã lỗi"}
OUTSIDE_TABLE = "mã ngoài bảng chuẩn"


class CodeRow(BaseModel):
    code: str  # mã lỗi, hoặc STUCK / NO_CODE
    name: str  # "Tên cảnh báo" của bảng chuẩn, hoặc nhãn của nhóm không có mã
    level: AlertLevel | None = None
    in_table: bool = False
    n: int
    pct: float  # % trên tổng số phiên lỗi của kỳ


class HealthRow(BaseModel):
    service_id: str
    service_name: str
    entity: str  # mã tủ / mã trạm / "VN" (dịch vụ toàn mạng)
    n_total: int
    n_fail: int
    fail_pct: float
    n_high: int
    score: float = Field(ge=0, le=100)
    label: str


class OpenPointItem(BaseModel):
    """Open point nhóm OPERATION, sinh hoàn toàn từ KPI (CLAUDE.md §11)."""

    id: str  # ổn định giữa các kỳ
    kind: Literal["HEALTH", "CODE_INCREASE", "REPEAT_CABINET"]
    title: str
    scope: str
    impact: str
    evidence: list[str] = Field(max_length=5)  # mã giao dịch eMSP có thật trong kỳ
    recommendations: list[str]
    priority_score: float
    priority: Literal["Cao", "Trung bình", "Thấp"]


class CountrySection(BaseModel):
    country: str  # mã nước hoặc "ALL"
    n_total: int
    n_completed: int
    n_failed: int
    n_stuck: int
    n_failures: int  # = Thất bại + kẹt: mẫu số của "% trên tổng số phiên lỗi"
    success_rate: float | None
    failure_rate: float | None
    swap_seconds_p50: float | None
    swap_seconds_p95: float | None
    n_returned_not_received: int
    n_users_affected: int
    n_repeat_users: int
    top: list[CodeRow]
    top_pct: float  # tổng % của Top N
    all_codes: list[CodeRow]
    top_stations: list[HealthRow]
    services: list[HealthRow]  # dịch vụ toàn mạng (callback thanh toán, đồng bộ giao dịch)
    weak_cabinets: list[HealthRow]  # tủ có health score thấp nhất
    open_points: list[OpenPointItem]
    prev_n_failures: int | None = None
    prev_failure_rate: float | None = None


class PeriodReport(BaseModel):
    report_id: str  # khoá idempotency: report:weekly:2026-10-19 / report:monthly:2026-10 / …
    kind: ReportKind
    period_start: date  # ngày đầu kỳ (giờ OPS)
    period_end: date  # ngày cuối kỳ, tính cả ngày này
    timezone: str
    generated_at: datetime  # UTC
    data_start: date | None  # ngày có giao dịch đầu tiên trong kỳ (kỳ có thể thiếu dữ liệu)
    data_end: date | None
    top_n: int
    sections: list[CountrySection]


def report_id(kind: ReportKind, start: date, end: date) -> str:
    if kind == "weekly":
        return f"report:weekly:{start.isoformat()}"
    if kind == "monthly":
        return f"report:monthly:{start:%Y-%m}"
    return f"report:custom:{start.isoformat()}_{end.isoformat()}"


def _bounds(start: date, end: date, tz: str) -> tuple[datetime, datetime]:
    z = ZoneInfo(tz)
    lo = datetime.combine(start, time(0), tzinfo=z).astimezone(UTC)
    hi = datetime.combine(end + timedelta(days=1), time(0), tzinfo=z).astimezone(UTC)
    return lo, hi


def _pct(n: int, d: int) -> float:
    return round(100 * n / d, 2) if d else 0.0


def _rate(n: int, d: int) -> float | None:
    return n / d if d else None


def classify(df: pl.DataFrame, as_of: datetime, scan: ScanSpec) -> pl.DataFrame:
    """Trạng thái tại `as_of` + cột `group` (mã lỗi / STUCK / NO_CODE) cho giao dịch lỗi/kẹt."""
    view = view_as_of(df, as_of)
    stuck_cut = as_of - timedelta(minutes=scan.stuck_after_minutes)
    is_failed = pl.col("status") == TxnStatus.FAILED.value
    is_stuck = pl.col("status").is_in([TxnStatus.CREATED.value, TxnStatus.BATTERY_IN.value]) & (
        pl.col("updated_at") <= stuck_cut
    )
    return view.with_columns(
        is_failed.alias("is_failed"),
        is_stuck.alias("is_stuck"),
        pl.when(is_failed)
        .then(pl.col("error_code").fill_null(NO_CODE))
        .when(is_stuck)
        .then(pl.lit(STUCK))
        .otherwise(None)
        .alias("group"),
    )


def _code_rows(counts: Counter[str], total: int, table: ErrorTable) -> list[CodeRow]:
    rows: list[CodeRow] = []
    for code, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        row = table.lookup(code)
        if code in GROUP_NAMES:
            name = GROUP_NAMES[code]
        else:
            name = row.name if row else OUTSIDE_TABLE
        rows.append(
            CodeRow(
                code=code,
                name=name,
                level=row.level if row else None,
                in_table=row is not None,
                n=n,
                pct=_pct(n, total),
            )
        )
    return rows


def health_score(
    n_total: int, n_fail: int, n_high: int, svc: ServiceSpec, spec: KpiSpec
) -> tuple[float, str]:
    fail_pct = 100 * n_fail / n_total if n_total else 0.0
    high_pct = 100 * n_high / n_total if n_total else 0.0
    raw = (
        100
        - svc.penalty_per_pp * max(0.0, fail_pct - svc.target_fail_pct)
        - spec.health.high_penalty_per_pp * high_pct
    )
    score = round(min(100.0, max(0.0, raw)), 1)
    return score, spec.health.label(score)


def _health_by(
    part: pl.DataFrame, key: str, svc: ServiceSpec, spec: KpiSpec, table: ErrorTable
) -> list[HealthRow]:
    high_codes = [r.code for r in table.rows if r.level is AlertLevel.HIGH]
    agg = (
        part.filter(pl.col(key).is_not_null())
        .group_by(key)
        .agg(
            pl.len().alias("n_total"),
            pl.col("group").is_not_null().sum().alias("n_fail"),
            (pl.col("is_failed") & pl.col("error_code").is_in(high_codes)).sum().alias("n_high"),
        )
    )
    rows = []
    for r in agg.iter_rows(named=True):
        n_total, n_fail, n_high = int(r["n_total"]), int(r["n_fail"]), int(r["n_high"])
        score, label = health_score(n_total, n_fail, n_high, svc, spec)
        rows.append(
            HealthRow(
                service_id=svc.id,
                service_name=svc.name,
                entity=str(r[key]),
                n_total=n_total,
                n_fail=n_fail,
                fail_pct=_pct(n_fail, n_total),
                n_high=n_high,
                score=score,
                label=label,
            )
        )
    return sorted(rows, key=lambda x: (x.score, -x.n_fail, x.entity))


def _service_health(
    part: pl.DataFrame, country: str, svc: ServiceSpec, spec: KpiSpec, table: ErrorTable
) -> HealthRow:
    n_total = part.height
    hit = part.filter(pl.col("is_failed") & pl.col("error_code").is_in(svc.codes))
    n_high = sum(
        1
        for c in hit["error_code"].to_list()
        if (row := table.lookup(c)) and row.level is AlertLevel.HIGH
    )
    score, label = health_score(n_total, hit.height, n_high, svc, spec)
    return HealthRow(
        service_id=svc.id,
        service_name=svc.name,
        entity=country,
        n_total=n_total,
        n_fail=hit.height,
        fail_pct=_pct(hit.height, n_total),
        n_high=n_high,
        score=score,
        label=label,
    )


def _priority(score: float) -> Literal["Cao", "Trung bình", "Thấp"]:
    if score >= 30:
        return "Cao"
    return "Trung bình" if score >= 10 else "Thấp"


def _open_points(
    part: pl.DataFrame,
    prev: pl.DataFrame,
    cabinets: list[HealthRow],
    stations: list[HealthRow],
    services: list[HealthRow],
    spec: KpiSpec,
    table: ErrorTable,
) -> list[OpenPointItem]:
    """Open point từ KPI. Priority = số GD ảnh hưởng × trọng số mức × xu hướng (công thức)."""
    op = spec.open_points
    fails = part.filter(pl.col("group").is_not_null()).sort("started_at")

    def weight(code: str | None) -> float:
        row = table.lookup(code)
        return op.level_weight["none" if row is None else row.level.value]

    def evidence(expr: pl.Expr) -> list[str]:
        return fails.filter(expr)["emsp_txn_id"].head(5).to_list()

    def top_code(expr: pl.Expr) -> str:
        """Mã nên ưu tiên xử lý: mã mức High nhiều nhất; không có thì mã nhiều nhất."""
        sub = fails.filter(expr)["group"].value_counts(sort=True)
        if not sub.height:
            return NO_CODE
        ranked = sorted(
            zip(sub["group"].to_list(), sub["count"].to_list(), strict=True),
            key=lambda gc: (weight(str(gc[0])) <= 1, -int(gc[1]), str(gc[0])),
        )
        return str(ranked[0][0])

    def label(code: str) -> str:
        row = table.lookup(code)
        return (
            f"{code} – {row.name}"
            if row
            else (GROUP_NAMES.get(code) or f"{code} – {OUTSIDE_TABLE}")
        )

    out: dict[str, OpenPointItem] = {}
    for h, col in [*((c, "cabinet_id") for c in cabinets), *((s, "station_id") for s in stations)]:
        if h.score >= op.health_below:
            continue
        expr = pl.col(col) == h.entity
        code = top_code(expr)
        ps = h.n_fail * weight(code)
        what = "Tủ" if col == "cabinet_id" else "Trạm"
        out[f"OP-HEALTH-{h.entity}"] = OpenPointItem(
            id=f"OP-HEALTH-{h.entity}",
            kind="HEALTH",
            title=f"{what} {h.entity}: health score {h.score:g} ({h.label})",
            scope=h.entity,
            impact=f"{h.n_fail}/{h.n_total} giao dịch lỗi/kẹt ({h.fail_pct:g}%), "
            f"{h.n_high} giao dịch mã mức High",
            evidence=evidence(expr & (pl.col("group") == code)) or evidence(expr),
            recommendations=[
                f"Kiểm tra tại chỗ {what.lower()} {h.entity}, ưu tiên lỗi {label(code)}",
                "Đối chiếu lịch bảo trì / thay thế linh kiện của tủ trong kỳ",
            ],
            priority_score=round(ps, 1),
            priority=_priority(ps),
        )
    for svc in services:
        if svc.score >= op.health_below:
            continue
        svc_spec = next(s for s in spec.services if s.id == svc.service_id)
        expr = pl.col("error_code").is_in(svc_spec.codes)
        ps = svc.n_fail * max(weight(c) for c in svc_spec.codes)
        out[f"OP-HEALTH-{svc.service_id}"] = OpenPointItem(
            id=f"OP-HEALTH-{svc.service_id}",
            kind="HEALTH",
            title=f"{svc.service_name}: health score {svc.score:g} ({svc.label})",
            scope=svc.entity,
            impact=f"{svc.n_fail} giao dịch lỗi ({svc.fail_pct:g}% tổng giao dịch)",
            evidence=evidence(expr),
            recommendations=[
                f"Rà soát {svc.service_name.lower()} trong các khung giờ có lỗi",
                "Kiểm tra log phía hệ thống liên quan trong cùng thời điểm",
            ],
            priority_score=round(ps, 1),
            priority=_priority(ps),
        )

    now_counts = Counter(fails["group"].to_list())
    prev_counts = Counter(prev.filter(pl.col("group").is_not_null())["group"].to_list())
    for code, n in now_counts.items():
        before = prev_counts.get(code, 0)
        if n < op.code_increase_min_cases or before == 0:
            continue
        inc = 100 * (n - before) / before
        if inc < op.code_increase_pct:
            continue
        ps = n * weight(code) * (1 + min(inc, 300) / 100)
        out[f"OP-TREND-{code}"] = OpenPointItem(
            id=f"OP-TREND-{code}",
            kind="CODE_INCREASE",
            title=f"Lỗi {label(code)} tăng {inc:.0f}% so với kỳ trước",
            scope="toàn mạng",
            impact=f"{n} case (kỳ trước {before})",
            evidence=evidence(pl.col("group") == code),
            recommendations=[
                f"Xác định trạm/tủ đóng góp nhiều nhất vào {code}",
                "So sánh với thay đổi phần mềm / cấu hình tủ trong kỳ",
            ],
            priority_score=round(ps, 1),
            priority=_priority(ps),
        )

    # Lặp tại một tủ: đủ số lần VÀ tỷ lệ mã đó tại tủ ≥ `repeat_rate_ratio` × tỷ lệ toàn mạng
    # (lỗi nền như D6031 lặp ở tủ nào cũng có, không phải open point). Mã của dịch vụ toàn mạng
    # (callback thanh toán, đồng bộ giao dịch) đã có open point riêng ở mức dịch vụ.
    network_codes = {c for svc_spec in spec.services for c in svc_spec.codes}
    net_rate = {g: n / part.height for g, n in now_counts.items()} if part.height else {}
    cab_total = dict(part.group_by("cabinet_id").len().iter_rows())
    rep = (
        fails.filter(pl.col("cabinet_id").is_not_null() & ~pl.col("group").is_in(network_codes))
        .group_by(["cabinet_id", "group"])
        .len()
        .filter(pl.col("len") >= op.repeat_cabinet_code_min)
    )
    for r in rep.iter_rows(named=True):
        cab, code, n = str(r["cabinet_id"]), str(r["group"]), int(r["len"])
        if n / cab_total[cab] < op.repeat_rate_ratio * net_rate.get(code, 0.0):
            continue
        ps = n * weight(code)
        key = f"OP-REPEAT-{cab}-{code}"
        out[key] = OpenPointItem(
            id=key,
            kind="REPEAT_CABINET",
            title=f"Tủ {cab}: {label(code)} lặp {n} lần",
            scope=cab,
            impact=f"{n} giao dịch cùng một lỗi tại một tủ",
            evidence=evidence((pl.col("cabinet_id") == cab) & (pl.col("group") == code)),
            recommendations=[
                f"Kiểm tra connector / khoá khoang của tủ {cab}",
                "Nếu lặp ở cùng connector: tạm khoá connector và lập ticket bảo trì",
            ],
            priority_score=round(ps, 1),
            priority=_priority(ps),
        )
    return sorted(out.values(), key=lambda o: (-o.priority_score, o.id))


def _section(
    country: str,
    part: pl.DataFrame,
    prev: pl.DataFrame,
    spec: KpiSpec,
    table: ErrorTable,
    top_n: int,
) -> CountrySection:
    n_total = part.height
    n_completed = int((part["status"] == TxnStatus.COMPLETED.value).sum())
    n_failed = int(part["is_failed"].sum())
    n_stuck = int(part["is_stuck"].sum())
    n_failures = n_failed + n_stuck
    counts = Counter(part.filter(pl.col("group").is_not_null())["group"].to_list())
    codes = _code_rows(counts, n_failures, table)
    top = codes[:top_n]

    done = part.filter(pl.col("status") == TxnStatus.COMPLETED.value)
    secs = (done["updated_at"] - done["started_at"]).dt.total_seconds()
    fails = part.filter(pl.col("group").is_not_null())
    returned = part.filter(
        pl.col("is_failed")
        & pl.col("battery_in_at").is_not_null()
        & pl.col("battery_out_at").is_null()
    ).height + int((part["is_stuck"] & (part["status"] == TxnStatus.BATTERY_IN.value)).sum())
    tz = spec.report.timezone
    per_day = (
        fails.filter(pl.col("user_id").is_not_null())
        .with_columns(pl.col("started_at").dt.convert_time_zone(tz).dt.date().alias("_d"))
        .group_by(["user_id", "_d"])
        .len()
    )
    repeat_users = per_day.filter(pl.col("len") >= spec.report.repeat_user_min_failures)[
        "user_id"
    ].n_unique()

    svc_by_id = {s.id: s for s in spec.services}
    cabinets = (
        _health_by(part, "cabinet_id", svc_by_id["cabinet"], spec, table)
        if "cabinet" in svc_by_id
        else []
    )
    stations = (
        _health_by(part, "station_id", svc_by_id["station"], spec, table)
        if "station" in svc_by_id
        else []
    )
    services = [_service_health(part, country, s, spec, table) for s in spec.services if s.codes]
    prev_failures = int(prev["group"].is_not_null().sum()) if prev.height else None
    return CountrySection(
        country=country,
        n_total=n_total,
        n_completed=n_completed,
        n_failed=n_failed,
        n_stuck=n_stuck,
        n_failures=n_failures,
        success_rate=_rate(n_completed, n_total),
        failure_rate=_rate(n_failures, n_total),
        swap_seconds_p50=float(secs.quantile(0.5)) if done.height else None,  # type: ignore[arg-type]
        swap_seconds_p95=float(secs.quantile(0.95)) if done.height else None,  # type: ignore[arg-type]
        n_returned_not_received=returned,
        n_users_affected=fails["user_id"].drop_nulls().n_unique(),
        n_repeat_users=repeat_users,
        top=top,
        top_pct=_pct(sum(r.n for r in top), n_failures),
        all_codes=codes,
        top_stations=sorted(stations, key=lambda h: (-h.n_fail, h.entity))[:5],
        services=services,
        weak_cabinets=cabinets[:5],
        open_points=_open_points(part, prev, cabinets, stations, services, spec, table),
        prev_n_failures=prev_failures,
        prev_failure_rate=_rate(prev_failures, prev.height) if prev_failures is not None else None,
    )


def build_period_report(
    transactions: pl.DataFrame,
    *,
    kind: ReportKind,
    start: date,
    end: date,
    generated_at: datetime,
    spec: KpiSpec,
    scan: ScanSpec,
    table: ErrorTable,
    top_n: int | None = None,
) -> PeriodReport:
    """Giao dịch bắt đầu trong [start, end] (ngày theo giờ OPS), trạng thái tại `generated_at`."""
    tz = spec.report.timezone
    lo, hi = _bounds(start, end, tz)
    span = hi - lo
    df = classify(transactions.filter(pl.col("started_at") < hi), generated_at, scan)
    now = df.filter(pl.col("started_at") >= lo)
    prev = df.filter((pl.col("started_at") >= lo - span) & (pl.col("started_at") < lo))
    n = top_n or spec.report.top_n
    countries = sorted(now["country"].unique().to_list())
    sections = [
        _section(
            c,
            now.filter(pl.col("country") == c),
            prev.filter(pl.col("country") == c),
            spec,
            table,
            n,
        )
        for c in countries
    ]
    if len(countries) > 1:
        sections.append(_section("ALL", now, prev, spec, table, n))
    first, last = now["started_at"].min(), now["started_at"].max()
    z = ZoneInfo(tz)
    return PeriodReport(
        report_id=report_id(kind, start, end),
        kind=kind,
        period_start=start,
        period_end=end,
        timezone=tz,
        generated_at=generated_at,
        data_start=first.astimezone(z).date() if isinstance(first, datetime) else None,
        data_end=last.astimezone(z).date() if isinstance(last, datetime) else None,
        top_n=n,
        sections=sections,
    )


def due_reports(
    prev_tick: datetime, tick: datetime, spec: KpiSpec
) -> list[tuple[ReportKind, date, date]]:
    """Báo cáo đến hạn trong (prev_tick, tick]: tuần (thứ Hai 08:00), tháng (ngày 1 08:00)."""
    r = spec.report
    z = ZoneInfo(r.timezone)
    out: list[tuple[ReportKind, date, date]] = []
    day = prev_tick.astimezone(z).date()
    last = tick.astimezone(z).date()
    while day <= last:
        for kind, sched in (("weekly", r.weekly), ("monthly", r.monthly)):
            h, m = sched.hour_minute
            due = datetime.combine(day, time(h, m), tzinfo=z).astimezone(UTC)
            if not prev_tick < due <= tick:
                continue
            if kind == "weekly" and day.weekday() == sched.weekday:
                out.append(("weekly", day - timedelta(days=7), day - timedelta(days=1)))
            if kind == "monthly" and day.day == sched.day:
                end = day - timedelta(days=1)
                out.append(("monthly", end.replace(day=1), end))
        day += timedelta(days=1)
    return out


def counts_by_group(df: pl.DataFrame) -> dict[str, int]:
    """Đếm phiên lỗi theo nhóm (dùng cho kiểm chứng SF10)."""
    c: defaultdict[str, int] = defaultdict(int)
    for g in df.filter(pl.col("group").is_not_null())["group"].to_list():
        c[str(g)] += 1
    return dict(c)
