"""报告落库与审计（日/周/月三份报告）。

从原 ``report_service.py`` 拆出（①，2026-10-26），行为不变。
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.config import Settings
from app.db.models import (
    DailyReportRow,
    MonthlyReportRow,
    RiskAssessmentRow,
    WeeklyReportRow,
)
from app.domain.risk import (
    blocks_generate,
    blocks_upload,
)
from app.schemas.api import MonthlyReport, WeeklyReport
from app.schemas.domain import (
    DailyReport,
)
from app.schemas.enums import (
    AuditCategory,
)
from app.storage.audit_store import record as audit


def _persist(session: Session, report: DailyReport, settings: Settings) -> None:
    payload = report.model_dump(mode="json")
    existing = session.exec(
        select(DailyReportRow).where(DailyReportRow.date == report.date)
    ).first()
    if existing is None:
        session.add(DailyReportRow(date=report.date, payload=payload, version=1))
    else:
        existing.payload = payload
        existing.version += 1
        session.add(existing)

    # 风险结论单独落库（便于趋势与审计）。每天必写一条，含 P4（Q15）。
    r = report.risk
    risk_row = session.exec(
        select(RiskAssessmentRow).where(RiskAssessmentRow.date == report.date)
    ).first()
    data = {
        "priority": r.priority.value,
        "data_state": r.data_state.value,
        "triggered_rules": {"rules": r.triggered_rules},
        "evidence": r.evidence,
        "missing_inputs": {"items": r.missing_inputs},
        "volume_halved": r.volume_halved,
        "intensity_reduced": r.intensity_reduced,
        "floored_at_recovery_template": r.floored_at_recovery_template,
        "rule_version": r.rule_version,
    }
    if risk_row is None:
        session.add(RiskAssessmentRow(date=report.date, **data))
    else:
        for k, v in data.items():
            setattr(risk_row, k, v)
        session.add(risk_row)
    session.commit()

    audit(
        session,
        AuditCategory.RISK,
        ref=report.date.isoformat(),
        detail={
            "priority": r.priority.value,
            "data_state": r.data_state.value,
            "triggered_rules": r.triggered_rules,
            "missing_inputs": r.missing_inputs,
            "rule_version": r.rule_version,
            "blocks_generate": blocks_generate(r),
            "blocks_upload": blocks_upload(r),
        },
        audit_dir=settings.audit_dir,
    )
    audit(
        session,
        AuditCategory.CALCULATION,
        ref=report.date.isoformat(),
        detail={"report_version": report.version, "quality": report.quality.grade.value},
        audit_dir=settings.audit_dir,
    )


def _persist_weekly(session: Session, report: WeeklyReport, settings: Settings) -> None:
    payload = report.model_dump(mode="json")
    existing = session.exec(
        select(WeeklyReportRow).where(WeeklyReportRow.week_start == report.week_start)
    ).first()
    if existing is None:
        session.add(WeeklyReportRow(week_start=report.week_start, payload=payload, version=1))
    else:
        existing.payload = payload
        existing.version += 1
        session.add(existing)
    session.commit()
    audit(
        session,
        AuditCategory.CALCULATION,
        ref=f"weekly:{report.week_start.isoformat()}",
        detail={"report": "weekly", "version": 1},
        audit_dir=settings.audit_dir,
    )


def _persist_monthly(session: Session, report: MonthlyReport, settings: Settings) -> None:
    payload = report.model_dump(mode="json")
    existing = session.exec(
        select(MonthlyReportRow).where(MonthlyReportRow.month == report.month)
    ).first()
    if existing is None:
        session.add(MonthlyReportRow(month=report.month, payload=payload, version=1))
    else:
        existing.payload = payload
        existing.version += 1
        session.add(existing)
    session.commit()
    audit(
        session,
        AuditCategory.CALCULATION,
        ref=f"monthly:{report.month}",
        detail={"report": "monthly"},
        audit_dir=settings.audit_dir,
    )
