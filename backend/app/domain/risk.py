"""风险引擎入口：三态数据关卡 + 四级优先级（2026-10-22 全量替换）。

旧三级体系（R-001 ~ R-008，normal / needsAdjustment / high）**已整体退役**
（Q1/Q8/Q12）。本模块只做「组装输入 → 调优先级判定 → 包装结果」，
判定逻辑与阈值分别在 ``domain/priority.py`` 与 ``domain/rule_registry.py``。

设计约束：
- REQ-RISK-002：判定为**纯程序逻辑**，模型不得覆盖（LLM 只负责措辞包装，Q4）。
- REQ-RISK-003：P1 不生成计划、P2 不允许上传（Q26）。
- REQ-RISK-007：``rule_version`` 随评估结果落库与审计。

已删除的旧机制（Q8）：
- 症状独立通道（``domain/symptom_alert.py``）—— 与自动判定无关，一并退役；
- 背离判定器（``domain/divergence.py``）—— 旧 R-006 专用，一并退役；
- 「恢复日」作为 R-003 的例外分支 —— 现为 P1 的标准输出（Q6）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from app.domain import rule_registry as rr
from app.domain.priority import DataStateInput, judge_data_state, judge_priority
from app.schemas.domain import RiskAssessment
from app.schemas.enums import PriorityLevel

__all__ = [
    "RECOVERY_ADVISORY",
    "RiskInput",
    "allows_recovery_day",
    "blocks_generate",
    "blocks_plan",
    "blocks_upload",
    "evaluate",
]

RECOVERY_ADVISORY = rr.RECOVERY_TEMPLATE_NOTE


@dataclass(slots=True)
class RiskInput:
    """风险判定输入（全部来自标准化层与基线层，不含原始数据）。

    字段与 ``DataStateInput`` 一一对应，额外携带展示用的上下文。
    """

    on_date: date
    # —— 负荷 ——
    acwr_ratio: float | None = None
    acwr_stale: bool = False
    acwr_source_date: date | None = None
    # —— HRV ——
    hrv_status: str | None = None
    hrv_last_night_avg: float | None = None
    hrv_baseline: float | None = None
    hrv_balanced_streak: int = 0
    # —— RHR ——
    resting_heart_rate: int | None = None
    rhr_baseline: float | None = None
    # —— 睡眠 / 电量 ——
    sleep_score: int | None = None
    body_battery_morning: int | None = None
    # —— 展示上下文（不参与判定）——
    hrv_low_upper: float | None = None
    hrv_balanced_high: float | None = None
    conflicts: list[str] = field(default_factory=list)

    def to_state_input(self) -> DataStateInput:
        return DataStateInput(
            on_date=self.on_date,
            acwr_ratio=self.acwr_ratio,
            acwr_stale=self.acwr_stale,
            acwr_source_date=self.acwr_source_date,
            hrv_status=self.hrv_status,
            hrv_last_night_avg=self.hrv_last_night_avg,
            hrv_baseline=self.hrv_baseline,
            rhr_today=self.resting_heart_rate,
            rhr_baseline=self.rhr_baseline,
            sleep_score=self.sleep_score,
            body_battery_morning=self.body_battery_morning,
            hrv_balanced_streak=self.hrv_balanced_streak,
        )


def evaluate(inp: RiskInput) -> RiskAssessment:
    """执行三态关卡与四级优先级判定，返回可复现的评估结果。"""
    state_inp = inp.to_state_input()
    state, missing = judge_data_state(state_inp)
    result = judge_priority(state_inp, state)

    return RiskAssessment(
        date=inp.on_date,
        priority=result.priority,
        data_state=state,
        triggered_rules=list(result.triggered_rules),
        reasons=list(result.reasons),
        evidence={
            "acwr_ratio": inp.acwr_ratio,
            "acwr_stale": inp.acwr_stale,
            "acwr_source_date": (
                inp.acwr_source_date.isoformat() if inp.acwr_source_date else None
            ),
            "hrv_status": inp.hrv_status,
            "hrv_last_night_avg": inp.hrv_last_night_avg,
            "hrv_baseline": inp.hrv_baseline,
            "hrv_balanced_streak": inp.hrv_balanced_streak,
            "resting_heart_rate": inp.resting_heart_rate,
            "rhr_baseline": inp.rhr_baseline,
            "sleep_score": inp.sleep_score,
            "body_battery_morning": inp.body_battery_morning,
            "hrv_low_upper": inp.hrv_low_upper,
            "hrv_balanced_high": inp.hrv_balanced_high,
            "conflicts": inp.conflicts,
            "reasons": list(result.reasons),
        },
        missing_inputs=missing,
        volume_halved=result.volume_halved,
        intensity_reduced=result.intensity_reduced,
        floored_at_recovery_template=result.floored_at_recovery_template,
        progress_duration_pct=list(result.progress_duration_pct or []),
        progress_strength_pct=list(result.progress_strength_pct or []),
        progress_strength_reps=list(result.progress_strength_reps or []),
        rule_version=rr.RULE_VERSION,
        decided_at=datetime.now(),
    )


# ============================================================
# 硬拦截（Q26）
# ============================================================
def blocks_generate(assessment: RiskAssessment) -> bool:
    """P1 → 不生成计划（只输出 15–20min Z1 恢复日模板）。"""
    return assessment.priority is PriorityLevel.REST


def blocks_upload(assessment: RiskAssessment) -> bool:
    """P1 / P2 → 不允许上传到 Tredict（P2 的减量计划需人工确认）。"""
    return assessment.priority in (PriorityLevel.REST, PriorityLevel.REDUCE)


def blocks_plan(assessment: RiskAssessment) -> bool:
    """是否拦截「生成并上传」—— 等价于 P1。

    保留此函数名以兼容 ``strategy_validator`` 的安全断言语义。
    """
    return blocks_generate(assessment)


def allows_recovery_day(assessment: RiskAssessment) -> bool:
    """P1 → 输出恢复日模板（Q6：15–20min Z1 取代旧的 28min 恢复计划）。"""
    return assessment.priority is PriorityLevel.REST