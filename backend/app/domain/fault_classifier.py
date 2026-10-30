"""故障分类（阶段③ T3.9；REQ §8.1）。

**纯逻辑**：把故障区分为「一般技术故障」与「重大故障」。

重大故障（涉及安全/关键数据可信度/训练决策正确性，§8.1）：
- 安全策略故障；
- 关键数据可信度故障；
- 训练决策正确性故障；
- 关键策略依赖故障；
- 其他可能导致错误训练安排或安全拦截失效的故障。

一般技术故障：不影响安全判断与训练决策正确性的普通运行故障（修复后可按规则自动恢复）。
"""

from __future__ import annotations

from enum import StrEnum


class FaultKind(StrEnum):
    """故障类别（用于分级与提示）。"""

    SAFETY = "safety"  # 安全策略
    DATA_TRUST = "dataTrust"  # 关键数据可信度
    DECISION = "decision"  # 训练决策正确性
    STRATEGY_DEP = "strategyDependency"  # 关键策略依赖
    GENERAL_TECH = "generalTech"  # 一般技术


# 触发「重大故障」的类别
MAJOR_KINDS = {
    FaultKind.SAFETY,
    FaultKind.DATA_TRUST,
    FaultKind.DECISION,
    FaultKind.STRATEGY_DEP,
}


def classify_fault(kind: FaultKind | str) -> str:
    """返回故障等级：``major`` | ``general``（§8.1）。"""
    k = FaultKind(kind) if not isinstance(kind, FaultKind) else kind
    return "major" if k in MAJOR_KINDS else "general"


def needs_user_confirm(severity: str) -> bool:
    """重大故障修复后恢复优化必须等用户确认（§8.2）。"""
    return severity == "major"


# 重大故障恢复流程的 10 步（§8.2）
RECOVERY_STEPS: tuple[str, ...] = (
    "修复重大故障",
    "系统执行恢复前校验",
    "确认故障已解决",
    "确认原有季节策略及其依赖仍然有效",
    "确认硬性安全规则有效",
    "生成恢复校验报告",
    "报告校验结果、修复措施、剩余风险和恢复影响",
    "等待用户明确确认",
    "用户同意后，恢复季节策略优化",
    "记录确认人、确认时间、确认内容和恢复结果",
)
