"""`Classifier` — phân loại `Finding` thành `Classification` bằng rule deterministic (§4.2).

Thứ tự ưu tiên rule:
1. `R_PAID_NOT_DELIVERED`: `payment.captured=true` (tới `now`), bước >= 6 -> `PAYMENT/P1`.
2. `R_CATALOG`: có `error_code` trong `ErrorCatalog` (trừ `STEP_TIMEOUT`) -> `default_category` +
   `severity_for(code, temperature)`.
3. Suy luận khi thiếu mã (`error_code=None`) hoặc `STEP_TIMEOUT`, theo dữ liệu `<= now`:
   - `R_INF_SLOT_DOOR_JAM`: bước 6, `door_state` vẫn `CLOSED` -> `HARDWARE`, `SLOT_DOOR_JAM`.
   - `R_INF_SLOT_SENSOR_FAIL`: bước 6, cửa đóng mà `battery_detected=false` -> `SLOT_SENSOR_FAIL`.
   - `R_INF_CABINET_DISCONNECT`: mất heartbeat > `heartbeat_gap_seconds` -> `CABINET_DISCONNECT`.
   - `R_INF_BAT_LOW_SOH`: bước 7, pin xuất `soh < low_soh_pct` -> `BATTERY_QUALITY`, `BAT_LOW_SOH`.
   - `R_INF_BAT_LOW_SOC`: bước 7, SOC xuất `< low_soc_pct` -> `BATTERY_QUALITY`, `BAT_LOW_SOC`.
   - `R_INF_ELIG_DEBT`: bước 4, user có `debt_flag=true` -> `BUSINESS`, `ELIG_DEBT`.
   - `R_INF_ELIG_PACKAGE_EXPIRED`: bước 4, user có `package_expiry < now` -> `ELIG_PACKAGE_EXPIRED`.
4. `R_UNKNOWN`: không khớp rule nào -> `UNKNOWN`, `needs_human_review=True`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from ase.common.config import get_settings
from ase.common.models import Category, Severity, Step
from ase.common.time import ensure_utc
from ase.common.transport import Clock, JsonlSource, SimClock
from ase.detector.context import ContextStore
from ase.detector.fields import CoverageReport, DisabledRule, FieldView
from ase.detector.state_machine import Finding, FindingKind, SessionTracker
from ase.knowledge.loader import Knowledge, default_knowledge


@dataclass(frozen=True)
class RuleDefinition:
    rule_id: str
    required_fields: tuple[str, ...]
    reason: str


RULE_DEFINITIONS: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        rule_id="R_PAID_NOT_DELIVERED",
        required_fields=("payment.captured",),
        reason="Thiếu payment.captured (T1) để kiểm tra giao dịch đã trừ tiền nhưng chưa nhả pin",
    ),
    RuleDefinition(
        rule_id="R_CATALOG",
        required_fields=("error_code",),
        reason="Thiếu trường error_code (T1) để tra cứu ErrorCatalog",
    ),
    RuleDefinition(
        rule_id="R_INF_SLOT_DOOR_JAM",
        required_fields=("cabinet_id", "slot_id", "door_state"),
        reason="Thiếu cabinet_id/slot_id (T1) hoặc door_state (T3) để suy luận kẹt cửa khoang",
    ),
    RuleDefinition(
        rule_id="R_INF_SLOT_SENSOR_FAIL",
        required_fields=("cabinet_id", "slot_id", "door_state", "battery_detected"),
        reason="Thiếu cabinet_id/slot_id (T1) hoặc door_state/battery_detected (T3)",
    ),
    RuleDefinition(
        rule_id="R_INF_CABINET_DISCONNECT",
        required_fields=("cabinet_id", "heartbeat_at"),
        reason="Thiếu trường cabinet_id (T1) hoặc heartbeat_at (T3) để suy luận mất kết nối tủ",
    ),
    RuleDefinition(
        rule_id="R_INF_BAT_LOW_SOH",
        required_fields=("battery_id", "lot_id", "soh"),
        reason="Thiếu trường battery_id/lot_id/soh (T2) để suy luận lô pin SOH thấp",
    ),
    RuleDefinition(
        rule_id="R_INF_BAT_LOW_SOC",
        required_fields=("cabinet_id", "slot_id", "soc"),
        reason="Thiếu trường cabinet_id/slot_id (T1) hoặc soc (T2) để suy luận pin xuất SOC thấp",
    ),
    RuleDefinition(
        rule_id="R_INF_ELIG_DEBT",
        required_fields=("user_id",),
        reason="Thiếu trường user_id (T2) để tra cứu cờ nợ cước của người dùng",
    ),
    RuleDefinition(
        rule_id="R_INF_ELIG_PACKAGE_EXPIRED",
        required_fields=("user_id",),
        reason="Thiếu trường user_id (T2) để tra cứu hạn gói thuê của người dùng",
    ),
)


class Classification(Finding):
    """Kết quả phân loại rule-based cho một `Finding` (DETECTOR_PLAN §3)."""

    category: Category
    severity: Severity
    rule_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    needs_human_review: bool = False

    @property
    def finding(self) -> Finding:
        return Finding(
            session_id=self.session_id,
            station_id=self.station_id,
            cabinet_id=self.cabinet_id,
            slot_id=self.slot_id,
            step=self.step,
            kind=self.kind,
            error_code=self.error_code,
            status=self.status,
            occurred_at=self.occurred_at,
            detected_at=self.detected_at,
            event_ids=list(self.event_ids),
        )


class Classifier:
    """Bộ phân loại deterministic theo thứ tự ưu tiên rule và `FieldView`."""

    def __init__(
        self,
        clock: Clock,
        field_view: FieldView,
        *,
        knowledge: Knowledge | None = None,
    ) -> None:
        self.clock = clock
        self.field_view = field_view
        self.knowledge = knowledge or default_knowledge()
        self.catalog = self.knowledge.error_catalog
        self.thresholds = self.catalog.thresholds
        self.cfg = self.knowledge.detector.classifier

        self._enabled_rules: set[str] = set()
        self._disabled_rules: list[DisabledRule] = []
        for rdef in RULE_DEFINITIONS:
            missing = self.field_view.missing_fields(rdef.required_fields)
            if not missing:
                self._enabled_rules.add(rdef.rule_id)
            else:
                self._disabled_rules.append(
                    DisabledRule(
                        rule_id=rdef.rule_id,
                        missing_fields=missing,
                        reason=rdef.reason,
                    )
                )

    @property
    def disabled_rules(self) -> list[DisabledRule]:
        return list(self._disabled_rules)

    def is_rule_enabled(self, rule_id: str) -> bool:
        return rule_id in self._enabled_rules

    def _build(
        self,
        finding: Finding,
        *,
        kind: FindingKind | None = None,
        cabinet_id: str | None = None,
        slot_id: str | None = None,
        category: Category,
        severity: Severity,
        error_code: str | None,
        rule_id: str,
        confidence: float,
        needs_human_review: bool = False,
    ) -> Classification:
        return Classification(
            session_id=finding.session_id,
            station_id=finding.station_id,
            cabinet_id=cabinet_id if cabinet_id is not None else finding.cabinet_id,
            slot_id=slot_id if slot_id is not None else finding.slot_id,
            step=finding.step,
            kind=kind or finding.kind,
            error_code=error_code,
            status=finding.status,
            occurred_at=finding.occurred_at,
            detected_at=finding.detected_at,
            event_ids=list(finding.event_ids),
            category=category,
            severity=severity,
            rule_id=rule_id,
            confidence=confidence,
            needs_human_review=needs_human_review,
        )

    def classify(self, finding: Finding) -> Classification:
        now = ensure_utc(self.clock.now())
        # Ghi nhận tra cứu quốc gia/timezone của trạm cho báo cáo coverage
        self.field_view.station_country_timezone(finding.station_id, finding.session_id)

        sess = self.field_view.context.get_session(finding.session_id, now)
        cab_id: str | None = None
        if self.field_view.has_field("cabinet_id"):
            cab_id = finding.cabinet_id or (sess.cabinet_id if sess is not None else None)

        slot_id: str | None = None
        if self.field_view.has_field("slot_id"):
            slot_id = finding.slot_id
            if slot_id is None and sess is not None:
                if finding.step is Step.RETURN_BATTERY:
                    slot_id = sess.slot_in
                elif finding.step is Step.RECEIVE_BATTERY:
                    slot_id = sess.slot_out

        # 1. R_PAID_NOT_DELIVERED (ưu tiên cao nhất, CLAUDE.md §6.3)
        if self.is_rule_enabled("R_PAID_NOT_DELIVERED") and finding.step in (
            Step.RETURN_BATTERY,
            Step.RECEIVE_BATTERY,
        ):
            if self.field_view.payment_captured(finding.session_id, now) is True:
                return self._build(
                    finding,
                    kind="PAID_NOT_DELIVERED",
                    cabinet_id=cab_id,
                    slot_id=slot_id,
                    category=Category.PAYMENT,
                    severity=self.catalog.severity_for("PAID_NOT_DELIVERED"),
                    error_code="PAID_NOT_DELIVERED",
                    rule_id="R_PAID_NOT_DELIVERED",
                    confidence=self.cfg.paid_not_delivered_confidence,
                    needs_human_review=False,
                )

        # 2. R_CATALOG (có error_code cụ thể, trừ STEP_TIMEOUT đa nhóm)
        code = finding.error_code if self.field_view.has_field("error_code") else None
        if self.is_rule_enabled("R_CATALOG") and code is not None and self.catalog.has(code):
            spec = self.catalog.by_code[code]
            if spec.default_category is not Category.UNKNOWN:
                temp = self.field_view.latest_temperature(cab_id, slot_id, now)
                sev = self.catalog.severity_for(code, temperature=temp)
                return self._build(
                    finding,
                    cabinet_id=cab_id,
                    slot_id=slot_id,
                    category=spec.default_category,
                    severity=sev,
                    error_code=code,
                    rule_id="R_CATALOG",
                    confidence=self.cfg.catalog_confidence,
                    needs_human_review=False,
                )

        # 3. Suy luận khi thiếu error_code hoặc STEP_TIMEOUT
        if finding.step is Step.RETURN_BATTERY:
            tel_win = self.field_view.slot_telemetry_window(
                cab_id, slot_id, now, lookback_seconds=120.0
            )
            if tel_win:
                latest = tel_win[-1]
                saw_open = any(t.door_state == "OPEN" for t in tel_win[:-1])
                if (
                    self.is_rule_enabled("R_INF_SLOT_DOOR_JAM")
                    and latest.door_state == "CLOSED"
                    and not saw_open
                    and latest.battery_detected is not True
                ):
                    return self._build(
                        finding,
                        cabinet_id=cab_id,
                        slot_id=slot_id,
                        category=Category.HARDWARE,
                        severity=self.catalog.severity_for("SLOT_DOOR_JAM"),
                        error_code="SLOT_DOOR_JAM",
                        rule_id="R_INF_SLOT_DOOR_JAM",
                        confidence=self.cfg.inferred_confidence,
                    )
                if (
                    self.is_rule_enabled("R_INF_SLOT_SENSOR_FAIL")
                    and latest.door_state == "CLOSED"
                    and latest.battery_detected is False
                ):
                    return self._build(
                        finding,
                        cabinet_id=cab_id,
                        slot_id=slot_id,
                        category=Category.HARDWARE,
                        severity=self.catalog.severity_for("SLOT_SENSOR_FAIL"),
                        error_code="SLOT_SENSOR_FAIL",
                        rule_id="R_INF_SLOT_SENSOR_FAIL",
                        confidence=self.cfg.inferred_confidence,
                    )

            temp = self.field_view.latest_temperature(cab_id, slot_id, now)
            if temp is not None:
                if temp > self.thresholds.temp_warn_2_c:
                    return self._build(
                        finding,
                        cabinet_id=cab_id,
                        slot_id=slot_id,
                        category=Category.HARDWARE,
                        severity=self.catalog.severity_for("CABINET_OVERHEAT"),
                        error_code="CABINET_OVERHEAT",
                        rule_id="R_INF_CABINET_DISCONNECT",
                        confidence=self.cfg.inferred_confidence,
                    )
                if temp > self.thresholds.temp_warn_1_c:
                    return self._build(
                        finding,
                        cabinet_id=cab_id,
                        slot_id=slot_id,
                        category=Category.BATTERY_QUALITY,
                        severity=self.catalog.severity_for("BAT_RETURN_FAULT", temperature=temp),
                        error_code="BAT_RETURN_FAULT",
                        rule_id="R_INF_SLOT_SENSOR_FAIL",
                        confidence=self.cfg.inferred_confidence,
                    )

        if self.is_rule_enabled("R_INF_CABINET_DISCONNECT") and cab_id is not None:
            gap = self.field_view.cabinet_heartbeat_gap_seconds(cab_id, finding.occurred_at, now)
            if gap is not None and gap > float(self.cfg.heartbeat_gap_seconds):
                return self._build(
                    finding,
                    cabinet_id=cab_id,
                    slot_id=slot_id,
                    category=Category.HARDWARE,
                    severity=self.catalog.severity_for("CABINET_DISCONNECT"),
                    error_code="CABINET_DISCONNECT",
                    rule_id="R_INF_CABINET_DISCONNECT",
                    confidence=self.cfg.inferred_confidence,
                )

        if finding.step is Step.RECEIVE_BATTERY:
            if self.is_rule_enabled("R_INF_BAT_LOW_SOH"):
                soh = self.field_view.battery_out_soh(finding.session_id, now)
                lot_id = self.field_view.battery_out_lot_id(finding.session_id, now)
                if soh is not None and lot_id is not None and soh < self.thresholds.low_soh_pct:
                    return self._build(
                        finding,
                        cabinet_id=cab_id,
                        slot_id=slot_id,
                        category=Category.BATTERY_QUALITY,
                        severity=self.catalog.severity_for("BAT_LOW_SOH"),
                        error_code="BAT_LOW_SOH",
                        rule_id="R_INF_BAT_LOW_SOH",
                        confidence=self.cfg.inferred_confidence,
                    )
            if self.is_rule_enabled("R_INF_BAT_LOW_SOC"):
                tel_win7 = self.field_view.slot_telemetry_window(
                    cab_id, slot_id, now, lookback_seconds=120.0
                )
                socs = [t.soc for t in tel_win7 if t.soc is not None]
                if socs and socs[-1] < self.thresholds.low_soc_pct:
                    return self._build(
                        finding,
                        cabinet_id=cab_id,
                        slot_id=slot_id,
                        category=Category.BATTERY_QUALITY,
                        severity=self.catalog.severity_for("BAT_LOW_SOC"),
                        error_code="BAT_LOW_SOC",
                        rule_id="R_INF_BAT_LOW_SOC",
                        confidence=self.cfg.inferred_confidence,
                    )

        if finding.step is Step.CHECK_ELIGIBILITY:
            usr = self.field_view.user(finding.session_id, now)
            if usr is not None:
                if self.is_rule_enabled("R_INF_ELIG_DEBT") and usr.debt_flag:
                    return self._build(
                        finding,
                        cabinet_id=cab_id,
                        slot_id=slot_id,
                        category=Category.BUSINESS,
                        severity=self.catalog.severity_for("ELIG_DEBT"),
                        error_code="ELIG_DEBT",
                        rule_id="R_INF_ELIG_DEBT",
                        confidence=self.cfg.inferred_confidence,
                    )
                if (
                    self.is_rule_enabled("R_INF_ELIG_PACKAGE_EXPIRED")
                    and ensure_utc(usr.package_expiry) < now
                ):
                    return self._build(
                        finding,
                        cabinet_id=cab_id,
                        slot_id=slot_id,
                        category=Category.BUSINESS,
                        severity=self.catalog.severity_for("ELIG_PACKAGE_EXPIRED"),
                        error_code="ELIG_PACKAGE_EXPIRED",
                        rule_id="R_INF_ELIG_PACKAGE_EXPIRED",
                        confidence=self.cfg.inferred_confidence,
                    )

        # 4. Không khớp rule nào -> UNKNOWN + needs_human_review
        fallback_code = "STEP_TIMEOUT" if finding.kind == "STEP_TIMEOUT" else code
        fallback_sev = (
            self.catalog.severity_for("STEP_TIMEOUT")
            if finding.kind == "STEP_TIMEOUT"
            else Severity.P2
        )
        return self._build(
            finding,
            cabinet_id=cab_id,
            slot_id=slot_id,
            category=Category.UNKNOWN,
            severity=fallback_sev,
            error_code=fallback_code,
            rule_id="R_UNKNOWN",
            confidence=self.cfg.unknown_confidence,
            needs_human_review=True,
        )


def build_coverage_report(
    field_view: FieldView,
    tracker: SessionTracker,
    classifier: Classifier,
) -> CoverageReport:
    return CoverageReport(
        preset=field_view.preset,
        available_fields=sorted(field_view.available_fields),
        disabled_rules=classifier.disabled_rules,
        default_country_sessions=field_view.default_country_sessions_count,
        duplicate_events_dropped=tracker.duplicate_events_dropped,
        late_events_handled=tracker.late_events_handled,
        backward_transitions_ignored=tracker.backward_transitions_ignored,
        total_events_processed=tracker.total_events_processed,
        total_sessions_seen=tracker.total_sessions_seen,
    )


async def run_core_detector(
    profile_dir: Path,
    *,
    preset: str = "ALL",
    knowledge: Knowledge | None = None,
    speed: float | None = None,
) -> tuple[list[Classification], CoverageReport]:
    """Chạy `JsonlSource` -> `SessionTracker` -> `Classifier` trên thư mục profile."""
    k = knowledge or default_knowledge()
    clock = SimClock()
    ctx = ContextStore.from_profile_dir(profile_dir)
    view = FieldView(ctx, preset=preset, knowledge=k)
    tracker = SessionTracker(clock, knowledge=k, field_view=view)
    classifier = Classifier(clock, view, knowledge=k)

    source = JsonlSource(profile_dir / "ase.swap.events.jsonl", clock, speed=speed)
    classifications: list[Classification] = []
    async for ev in source.events():
        for finding in tracker.on_event(ev):
            classifications.append(classifier.classify(finding))

    for finding in tracker.flush():
        classifications.append(classifier.classify(finding))

    coverage = build_coverage_report(view, tracker, classifier)
    return classifications, coverage


def format_core_summary(
    profile: str,
    classifications: list[Classification],
    coverage: CoverageReport,
) -> str:
    by_cat = Counter(c.category.value for c in classifications)
    lines = [
        f"=== Detector Core Summary (profile={profile}, fields={coverage.preset}) ===",
        f"Events processed : {coverage.total_events_processed} "
        f"(duplicates={coverage.duplicate_events_dropped}, late={coverage.late_events_handled}, "
        f"backward={coverage.backward_transitions_ignored})",
        f"Sessions seen    : {coverage.total_sessions_seen}",
        f"Detected failures: {len(classifications)}",
        "By category      :",
    ]
    for cat in Category:
        lines.append(f"  - {cat.value:<16}: {by_cat.get(cat.value, 0)}")
    if coverage.disabled_rules:
        disabled_str = ", ".join(
            f"{r.rule_id} (missing: {','.join(r.missing_fields)})" for r in coverage.disabled_rules
        )
        lines.append(f"Disabled rules   : {disabled_str}")
    else:
        lines.append("Disabled rules   : none")
    return "\n".join(lines)


async def run_profile_summary(profile: str = "dev", *, preset: str = "ALL") -> str:
    settings = get_settings()
    p_dir = settings.profile_dir(profile)
    classifications, coverage = await run_core_detector(p_dir, preset=preset)
    return format_core_summary(profile, classifications, coverage)
