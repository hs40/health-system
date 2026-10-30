"""数据质量分级（REQ-DATA-006 / REQ-DATA-002）。

四档对应不同使用场景，关键指标完整性要求不同：
- report（日报分析）：静息心率 + 睡眠
- capability（运动能力评估）：静息心率 + HRV
- adjustment（普通训练调整）：静息心率 + HRV + 睡眠
- highRisk（高风险决策）：静息心率 + HRV + 睡眠

关键安全数据不足 → 不得生成依赖这些指标的确定性结论（含当天训练计划）。
"""

from __future__ import annotations

from datetime import date

from app.schemas.domain import DataConflict, DataQualityReport
from app.schemas.enums import ConflictSeverity, QualityGrade, QualityLevel

REQUIRED_METRICS: dict[QualityLevel, frozenset[str]] = {
    QualityLevel.REPORT: frozenset({"resting_heart_rate", "sleep_duration_sec"}),
    QualityLevel.CAPABILITY: frozenset({"resting_heart_rate", "hrv_last_night_avg"}),
    QualityLevel.ADJUSTMENT: frozenset(
        {"resting_heart_rate", "hrv_last_night_avg", "sleep_duration_sec"}
    ),
    QualityLevel.HIGH_RISK: frozenset(
        {"resting_heart_rate", "hrv_last_night_avg", "sleep_duration_sec"}
    ),
}


def grade(
    on_date: date,
    level: QualityLevel,
    present_metrics: set[str],
    conflicts: list[DataConflict] | None = None,
    notes: list[str] | None = None,
) -> DataQualityReport:
    """按档位评估数据质量，返回可复现的质量报告。"""
    conflicts = conflicts or []
    note_list = list(notes or [])
    required = REQUIRED_METRICS[level]
    missing = sorted(required - set(present_metrics))

    if not missing:
        grade_value = QualityGrade.OK
    elif len(missing) < len(required):
        grade_value = QualityGrade.PARTIAL
    else:
        grade_value = QualityGrade.INSUFFICIENT

    # 影响安全决策的冲突 → 直接降级为不足
    if any(c.severity == ConflictSeverity.AFFECTS_SAFETY for c in conflicts):
        grade_value = QualityGrade.INSUFFICIENT
        note_list.append("存在影响安全决策的关键冲突，已降级为「不足」")

    if missing:
        note_list.append(f"缺失关键指标：{'、'.join(missing)}")

    return DataQualityReport(
        date=on_date,
        level=level,
        missing=missing,
        conflicts=conflicts,
        grade=grade_value,
        notes=note_list,
    )


def may_generate_plan(report: DataQualityReport) -> bool:
    """关键安全数据不足 → 不生成当天训练计划（REQ-RISK-003 / REQ-DATA-006）。"""
    return report.grade != QualityGrade.INSUFFICIENT
