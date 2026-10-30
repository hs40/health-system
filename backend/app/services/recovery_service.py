"""故障恢复服务（阶段③ T3.9；REQ §8.2/§8.3）。

**流程**（§8.2）：修复 → 恢复前校验 → 确认已解决 → 确认策略与依赖有效 →
确认硬性安全有效 → 生成报告 → 等待用户确认 → 用户同意后恢复优化 → 记录。

**硬性约束**：用户未确认前，**不得**恢复季节策略优化（§8.2）；
一般技术故障修复并通过校验后可自动恢复（§8.1）。

**已读 ≠ 同意**（§19.6.10）：确认必须由显式 ``confirm()`` 调用完成，
读取消息/标记已读不触发恢复。
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlmodel import Session, select

from app.config import Settings
from app.db.models import FaultRecoveryTaskRow
from app.domain.fault_classifier import RECOVERY_STEPS, classify_fault, needs_user_confirm
from app.domain.strategy_replay import load_metrics_available, replay_history
from app.domain.strategy_validator import (
    BOUNDARY_CASES,
    DEPENDENCIES,
    validate_strategy,
)
from app.schemas.domain import FaultRecoveryTask
from app.storage.strategy_store import StrategyVersionStore


def _to_domain(row: FaultRecoveryTaskRow) -> FaultRecoveryTask:
    return FaultRecoveryTask(
        fault_id=row.fault_id,
        severity=row.severity,
        impact_scope=row.impact_scope,
        started_at=row.started_at,
        fix_measures=row.fix_measures,
        fixed_at=row.fixed_at,
        precheck_status=row.precheck_status,
        precheck_report=row.precheck_report or {},
        residual_risk=list(row.residual_risk or []),
        needs_user_confirm=row.needs_user_confirm,
        user_confirmed=row.user_confirmed,
        user_confirmed_at=row.user_confirmed_at,
        recovery_executed_at=row.recovery_executed_at,
        final_result=row.final_result,
    )


def report_fault(
    session: Session,
    *,
    kind: str,
    impact_scope: str = "",
    fix_measures: str = "",
    fault_id: str | None = None,
) -> FaultRecoveryTask:
    """登记一个故障（分类 → 落库）。"""
    severity = classify_fault(kind)
    row = FaultRecoveryTaskRow(
        fault_id=fault_id or uuid.uuid4().hex,
        severity=severity,
        impact_scope=impact_scope,
        started_at=dt.datetime.now(),
        fix_measures=fix_measures,
        needs_user_confirm=needs_user_confirm(severity),
        precheck_status="pending",
        final_result="reported",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_domain(row)


def _precheck(session: Session, settings: Settings) -> dict:
    """恢复前校验（§8.2.2）：故障已解决 + 策略依赖 + 硬性安全规则。

    复用完整运行校验器（依赖/结构/回放一致性 + 边界语义用例）。
    """
    store = StrategyVersionStore(session)
    active = store.get_active(strategy_type=settings.strategy_type)
    replay = replay_history(session)
    metrics = load_metrics_available(session)
    payload = (active.payload if active else {}) or {}
    report = validate_strategy(
        payload,
        cases=list(BOUNDARY_CASES),  # 语义边界用例（雨天/寒冷/高风险拦截等）
        available_metrics=metrics if active else None,
        replay_total=replay.total,
        replay_matched=replay.matched,
        min_replay_match=settings.validate_min_replay_match,
    )
    # 附加：策略存在性、依赖有效
    strategy_ok = active is not None
    deps_ok = metrics and all(m in metrics for m in DEPENDENCIES)
    constraints = (active.safety_constraints or {}) if active else {}
    safety_ok = bool(constraints.get("hard_env_constraints", True)) if active else False
    passed = bool(strategy_ok and deps_ok and safety_ok and report.passed)
    return {
        "steps": list(RECOVERY_STEPS),
        "strategy_ok": strategy_ok,
        "deps_ok": deps_ok,
        "safety_ok": safety_ok,
        "runtime_validate_passed": report.passed,
        "detail": report.to_dict(),
        "passed": passed,
    }


def run_precheck(
    session: Session, settings: Settings, fault_id: str
) -> FaultRecoveryTask:
    """执行恢复前校验并落库（§8.2.2~8.2.6）。"""
    row = _require(session, fault_id)
    report = _precheck(session, settings)
    row.precheck_status = "passed" if report["passed"] else "failed"
    row.precheck_report = report
    if report["passed"]:
        row.final_result = "precheckPassed"
    else:
        row.final_result = "precheckFailed"
        row.residual_risk = ["恢复前校验未通过，保持暂停优化"]
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_domain(row)


def confirm_recovery(
    session: Session,
    settings: Settings,
    fault_id: str,
    *,
    confirmed_by: str = "user",
) -> FaultRecoveryTask:
    """用户确认恢复（§8.2.8~8.2.10）。

    约束：
    - 重大故障必须经过**显式确认**（本调用即确认动作）；
    - 未通过恢复前校验 → 不得恢复（``E_RECOVERY_NEED_CONFIRM`` 前置）；
    - 记录确认人/时间/结果。
    """
    from app.errors import AppError
    from app.schemas.enums import ErrorCode

    row = _require(session, fault_id)

    if row.precheck_status != "passed":
        raise AppError(
            ErrorCode.VALIDATION,
            f"恢复前校验未通过（{row.precheck_status}），不得恢复优化",
        )

    now = dt.datetime.now()
    row.user_confirmed = True
    row.user_confirmed_at = now
    row.recovery_executed_at = now
    row.final_result = f"recovered by {confirmed_by} at {now.isoformat()}"
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_domain(row)


def auto_recover_general(
    session: Session, settings: Settings, fault_id: str
) -> FaultRecoveryTask:
    """一般技术故障：通过校验后可**自动恢复**（§8.1/§19.6.2）。"""
    from app.errors import AppError
    from app.schemas.enums import ErrorCode

    row = _require(session, fault_id)
    if row.severity != "general":
        raise AppError(ErrorCode.RECOVERY_NEED_CONFIRM, "重大故障不可自动恢复，需用户确认")

    report = _precheck(session, settings)
    if not report["passed"]:
        row.precheck_status = "failed"
        row.precheck_report = report
        row.final_result = "autoRecoverFailed"
        session.add(row)
        session.commit()
        session.refresh(row)
        return _to_domain(row)

    now = dt.datetime.now()
    row.precheck_status = "passed"
    row.precheck_report = report
    row.recovery_executed_at = now
    row.final_result = "autoRecovered"
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_domain(row)


def optimize_allowed(session: Session, settings: Settings) -> tuple[bool, str]:
    """当前是否允许恢复/继续季节策略优化（§19.6.3/19.6.6）。

    存在未确认的重大故障 → 禁止（返回 False）。
    一般故障或已确认恢复 → 允许。
    """
    rows = session.exec(select(FaultRecoveryTaskRow)).all()
    for row in rows:
        if row.severity == "major" and not row.user_confirmed:
            return False, f"重大故障 {row.fault_id} 待用户确认，暂停优化"
    return True, "无阻断项"


def list_faults(session: Session, *, severity: str | None = None) -> list[FaultRecoveryTask]:
    stmt = select(FaultRecoveryTaskRow)
    if severity:
        stmt = stmt.where(FaultRecoveryTaskRow.severity == severity)
    stmt = stmt.order_by(FaultRecoveryTaskRow.started_at.desc())  # type: ignore[attr-defined]
    return [_to_domain(r) for r in session.exec(stmt).all()]


def get_fault(session: Session, fault_id: str) -> FaultRecoveryTask | None:
    row = session.get(FaultRecoveryTaskRow, fault_id)
    return _to_domain(row) if row else None


def _require(session: Session, fault_id: str) -> FaultRecoveryTaskRow:
    row = session.get(FaultRecoveryTaskRow, fault_id)
    if row is None:
        raise KeyError(f"故障恢复任务不存在: {fault_id}")
    return row
