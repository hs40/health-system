"""历史策略手动恢复（阶段④ T4.6；REQ §9.2~§9.5）。

**恢复前必须检查**（§9.2）：
- 目标版本是否完整；
- 目标版本是否已验证；
- 当前安全边界是否允许；
- 当前数据定义是否兼容；
- 当前策略依赖是否可用；
- 是否存在已知缺陷；
- 恢复后是否会影响关键训练决策。

**存在冲突 → 不得直接恢复**，并说明冲突内容、影响和不能恢复的原因（§9.2）。

**生效方式**（§9.3）：通过安全与兼容检查后立即生效，**不设统一观察期**；
创建**新的**策略版本记录，记录来源/发起人/时间/目标版本/校验结果/执行结果。

**版本关系**（§9.5）：
- 不删除后续已有版本；
- 不修改历史版本原来的生效时间；
- **不将历史版本伪装成从未中断的当前版本**；
- 将本次恢复记录为**新的版本变化事件**；
- 影响未来决策和未来报告；
- **不自动重算旧报告**（§11.5）。
"""

from __future__ import annotations

from app.domain.strategy_validator import BOUNDARY_CASES, validate_strategy

# 恢复前检查项（§9.2）
_REQUIRED_CHECKS = (
    "target_complete",  # 目标版本完整
    "target_verified",  # 目标版本已验证
    "safety_boundary_ok",  # 当前安全边界允许
    "data_definition_compatible",  # 当前数据定义兼容
    "dependencies_available",  # 当前策略依赖可用
    "no_known_defects",  # 无已知缺陷
    "decision_impact_acceptable",  # 恢复后不影响关键训练决策（超出安全边界）
)


def check_restorable(
    *,
    target: dict,
    available_metrics: set[str] | None,
    safety_boundary_ok: bool = True,
    known_defect_reasons: list[str] | None = None,
    replay_total: int = 0,
    replay_matched: int = 0,
    min_replay_match: float = 0.80,
) -> dict:
    """恢复前检查（§9.2）。返回逐项结果 + 是否允许恢复 + 冲突说明。

    :param target: 目标（历史）版本内容 ``payload``。
    :param available_metrics: 当前可用指标/数据定义集合。
    :param safety_boundary_ok: 当前安全边界是否允许（由上层按硬性环境/安全规则给出）。
    :param known_defect_reasons: 已知缺陷说明（非空即视为存在已知缺陷）。
    :param replay_total/replay_matched: 历史回放一致性样本（复用 §7.4 校验）。
    """
    defects = list(known_defect_reasons or [])

    # 完整性：payload 非空且含 season/focus
    payload = target or {}
    target_complete = bool(payload) and bool(payload.get("season") and payload.get("focus"))

    # 已验证：依赖校验器对目标 payload 做结构与语义校验
    report = validate_strategy(
        payload,
        cases=list(BOUNDARY_CASES),
        available_metrics=available_metrics,
        replay_total=replay_total,
        replay_matched=replay_matched,
        min_replay_match=min_replay_match,
    )

    # 依赖可用：校验器的依赖检查项
    deps_check = next((c for c in report.checks if c.key == "deps"), None)
    dependencies_available = bool(deps_check and deps_check.passed)

    # 安全边界：校验器的硬性安全检查
    safety_check = next((c for c in report.checks if c.key == "safety"), None)
    safety_semantic_ok = bool(safety_check and safety_check.passed)
    safety_boundary_ok = bool(safety_boundary_ok and safety_semantic_ok)

    # 数据定义兼容：依赖可用 + 结构校验通过
    data_definition_compatible = bool(report.passed and dependencies_available)

    # 无已知缺陷
    no_known_defects = not defects

    # 决策影响可接受：整体验证通过（语义正确 → 不会影响关键训练决策）
    decision_impact_acceptable = bool(report.passed)

    results = {
        "target_complete": target_complete,
        "target_verified": bool(report.passed),
        "safety_boundary_ok": safety_boundary_ok,
        "data_definition_compatible": data_definition_compatible,
        "dependencies_available": dependencies_available,
        "no_known_defects": no_known_defects,
        "decision_impact_acceptable": decision_impact_acceptable,
    }

    conflicts: list[str] = []
    if not target_complete:
        conflicts.append("目标版本内容不完整（缺少 season/focus），无法证明可恢复")
    if dependencies_available is False:
        conflicts.append(
            f"当前策略依赖不可用：{(deps_check.detail if deps_check else '依赖校验未执行')}"
        )
    if not safety_boundary_ok:
        conflicts.append(
            f"当前安全边界不允许或硬约束校验未通过："
            f"{(safety_check.detail if safety_check else '安全检查未执行')}"
        )
    if not data_definition_compatible:
        conflicts.append("当前数据定义与目标版本不兼容（结构/依赖校验未通过）")
    if not no_known_defects:
        conflicts.append("目标版本存在已知缺陷：" + "；".join(defects))
    if not decision_impact_acceptable:
        conflicts.append(
            "恢复后可能影响关键训练决策：" + "；".join(report.failures or ["运行校验未通过"])
        )

    allowed = all(results[k] for k in _REQUIRED_CHECKS)
    return {
        "allowed": bool(allowed),
        "checks": results,
        "conflicts": conflicts,
        "validation": report.to_dict(),
    }


__all__ = ["check_restorable"]
