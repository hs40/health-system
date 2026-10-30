"""数据质量分级单元测试（REQ-DATA-006 / REQ-DATA-002）。"""

from __future__ import annotations

from datetime import date

from app.domain.quality import grade, may_generate_plan
from app.schemas.domain import DataConflict
from app.schemas.enums import (
    ConflictResolution,
    ConflictSeverity,
    QualityGrade,
    QualityLevel,
)

D = date(2026, 10, 14)


def test_report_ok_when_required_present():
    r = grade(D, QualityLevel.REPORT, {"resting_heart_rate", "sleep_duration_sec"})
    assert r.grade == QualityGrade.OK
    assert r.missing == []
    assert may_generate_plan(r) is True


def test_report_partial_when_one_missing():
    r = grade(D, QualityLevel.REPORT, {"resting_heart_rate"})
    assert r.grade == QualityGrade.PARTIAL
    assert r.missing == ["sleep_duration_sec"]


def test_insufficient_when_all_missing():
    r = grade(D, QualityLevel.ADJUSTMENT, set())
    assert r.grade == QualityGrade.INSUFFICIENT
    assert may_generate_plan(r) is False


def test_safety_conflict_downgrades_to_insufficient():
    conflict = DataConflict(
        metric="resting_heart_rate",
        values=[{"source": "garmin", "value": 50}, {"source": "tredict", "value": 70}],
        severity=ConflictSeverity.AFFECTS_SAFETY,
        resolution=ConflictResolution.BLOCKED,
    )
    r = grade(
        D,
        QualityLevel.REPORT,
        {"resting_heart_rate", "sleep_duration_sec"},
        conflicts=[conflict],
    )
    assert r.grade == QualityGrade.INSUFFICIENT
    assert may_generate_plan(r) is False
    assert any("关键冲突" in n for n in r.notes)


def test_high_risk_level_requires_three_metrics():
    r = grade(D, QualityLevel.HIGH_RISK, {"resting_heart_rate", "hrv_last_night_avg"})
    assert r.grade == QualityGrade.PARTIAL
    assert "sleep_duration_sec" in r.missing
