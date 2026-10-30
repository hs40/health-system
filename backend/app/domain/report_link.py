"""报告与策略版本关联（阶段④ T4.1；REQ §11.1 / §11.5）。

**要求**：报告必须关联**生成时实际使用的有效策略版本**，且关联信息足以支持后续追溯。

关联块字段（§11.1）：
- ``report_generated_at``：报告生成时间；
- ``data_range``：报告使用的数据范围；
- ``rule_version``：报告使用的计算规则版本；
- ``model_version``：报告使用的模型版本；
- ``strategy_version_id`` / ``strategy_version_number``：报告使用的策略版本；
- ``risk_state``：报告生成时的风险状态；
- ``is_recalculation``：是否属于历史回顾性重算。

**硬性约束（§11.5）**：策略版本变化（优化/回退/恢复）**不自动触发**历史报告重算。
历史报告若要重算（数据修正触发），必须标注 ``is_recalculation=True``，
**不得伪装成当时生成的原始报告**。

本模块为纯逻辑 + 只读查询，供 report_service / plan_service 组装关联块。
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session

from app.schemas.enums import StrategyStatus, StrategyType
from app.storage.strategy_store import StrategyVersionStore


def build_strategy_link(
    session: Session,
    *,
    report_generated_at: dt.datetime,
    data_range: tuple[dt.date, dt.date],
    rule_version: str,
    model_version: str,
    risk_state: str = "",
    strategy_type: str = StrategyType.SEASON.value,
    is_recalculation: bool = False,
    recalc_of_generated_at: dt.datetime | None = None,
) -> dict:
    """组装报告与策略版本的关联块（§11.1）。

    :param session: 数据库会话（只读当前 active 策略）。
    :param report_generated_at: 报告生成时间。
    :param data_range: (起, 止) 数据范围（含端点）。
    :param rule_version: 计算规则版本（如 ``candidate-0.1.0``）。
    :param model_version: 模型版本（如 LLM 模型标识；无则空串）。
    :param risk_state: 报告生成时的风险状态（如 ``normal``/``high``）。
    :param strategy_type: 策略类型（默认 season）。
    :param is_recalculation: 是否历史回顾性重算（§11.5）。
    :param recalc_of_generated_at: 若为重算，原报告的生成时间（用于溯源；不得伪装成原报告）。
    """
    store = StrategyVersionStore(session)
    active = store.get_active(strategy_type=strategy_type)
    start, end = data_range
    return {
        "report_generated_at": report_generated_at.isoformat(),
        "data_range": {"start": start.isoformat(), "end": end.isoformat()},
        "rule_version": rule_version,
        "model_version": model_version,
        "strategy_version_id": active.strategy_version_id if active else None,
        "strategy_version_number": active.version_number if active else None,
        "strategy_status": active.status if active else None,
        "strategy_effective_at": active.effective_at.isoformat()
        if active and active.effective_at
        else None,
        "risk_state": risk_state,
        "is_recalculation": bool(is_recalculation),
        "recalc_of_generated_at": (
            recalc_of_generated_at.isoformat() if recalc_of_generated_at else None
        ),
    }


def linkage_complete(link: dict) -> bool:
    """关联块是否完整（§11.1：足以支持追溯）。

    策略版本可能合法地不存在（首启前），但若存在则版本标识必须齐全。
    """
    required = (
        "report_generated_at",
        "data_range",
        "rule_version",
        "model_version",
        "is_recalculation",
    )
    if not all(k in link for k in required):
        return False
    # 有 strategy_status 就必须有版本标识
    if link.get("strategy_status"):
        return bool(link.get("strategy_version_id") and link.get("strategy_version_number"))
    return True


def strategy_changes_in_window(
    session: Session,
    *,
    start: dt.date,
    end: dt.date,
    strategy_type: str = StrategyType.SEASON.value,
) -> list[dict]:
    """窗口内发生的策略版本变化（供周报/月报注明，§11.3/§11.4）。

    只读策略事件；**不**触发任何重算或策略变更。
    """
    store = StrategyVersionStore(session)
    events = store.list_events()
    out: list[dict] = []
    for e in events:
        when = e.triggered_at.date() if isinstance(e.triggered_at, dt.datetime) else None
        if when is None or not (start <= when <= end):
            continue
        target = store.get_version(e.target_version_id) if e.target_version_id else None
        out.append(
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "changed_at": e.triggered_at.isoformat(),
                "version_number": target.version_number if target else None,
                "change_type": target.change_type if target else None,
                "reason": e.trigger_reason or (target.change_reason if target else ""),
                "effective_at": (
                    target.effective_at.isoformat()
                    if target and target.effective_at
                    else None
                ),
            }
        )
    return out


def active_strategy_summary(
    session: Session, *, strategy_type: str = StrategyType.SEASON.value
) -> dict:
    """当前有效策略摘要（供报告主要分析依据，§11.4）。"""
    active = StrategyVersionStore(session).get_active(strategy_type=strategy_type)
    if active is None:
        return {"strategy_version_number": None, "season": None, "status": None}
    return {
        "strategy_version_number": active.version_number,
        "season": (active.payload or {}).get("season"),
        "status": active.status,
        "effective_at": active.effective_at.isoformat() if active.effective_at else None,
    }


__all__ = [
    "StrategyStatus",
    "active_strategy_summary",
    "build_strategy_link",
    "linkage_complete",
    "strategy_changes_in_window",
]
