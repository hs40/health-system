"""策略版本接口（阶段③ T3.1~T3.8；Plan §4.3.1）。"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.config import Settings, get_settings
from app.db.session import get_session
from app.errors import AppError
from app.schemas.api import (
    StrategyEventListResponse,
    StrategyListResponse,
    StrategyOptimizeRequest,
    StrategyResponse,
    StrategyRestoreRequest,
)
from app.schemas.domain import StrategyDiff, StrategyEvent, StrategyVersion
from app.schemas.enums import ErrorCode, StrategyChangeType, StrategyStatus
from app.services import strategy_service
from app.storage.strategy_store import StrategyVersionStore

router = APIRouter(prefix="/strategy", tags=["strategy"])


def _to_domain(row) -> StrategyVersion:  # noqa: ANN001
    return StrategyVersion(
        strategy_version_id=row.strategy_version_id,
        strategy_type=row.strategy_type,
        version_number=row.version_number,
        status=StrategyStatus(row.status),
        change_type=StrategyChangeType(row.change_type),
        parent_version_id=row.parent_version_id,
        created_at=row.created_at,
        effective_at=row.effective_at,
        created_by=row.created_by,
        change_reason=row.change_reason,
        evidence_summary=row.evidence_summary,
        expected_effect=row.expected_effect,
        actual_effect=row.actual_effect,
        payload=row.payload or {},
        applicability_conditions=row.applicability_conditions or {},
        safety_constraints=row.safety_constraints or {},
        validation_status=row.validation_status,
        validation_result=row.validation_result or {},
        limitations=list(row.limitations or []),
        immutable=row.immutable,
    )


@router.get("/current", response_model=StrategyResponse)
def current(
    type: str = Query(default="season"),  # noqa: A002
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StrategyResponse:
    """当前有效策略（无则初始化）。"""
    row = strategy_service.current(session, settings)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "无有效策略版本")
    return StrategyResponse(**_to_domain(row).model_dump())


@router.get("/versions", response_model=StrategyListResponse)
def list_versions(
    type: str = Query(default="season"),  # noqa: A002
    status: str | None = Query(default=None),
    change_type: str | None = Query(default=None),
    # 历史策略筛选（§9.7）：时间 / 变化类型 / 调整原因 / 实际效果
    start: dt.date | None = Query(default=None),
    end: dt.date | None = Query(default=None),
    reason_contains: str | None = Query(default=None),
    effect_contains: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> StrategyListResponse:
    store = StrategyVersionStore(session)
    rows = store.list_versions(strategy_type=type, status=status)

    if change_type:
        rows = [r for r in rows if r.change_type == change_type]
    if start is not None:
        rows = [r for r in rows if _created_day(r) >= start]
    if end is not None:
        rows = [r for r in rows if _created_day(r) <= end]
    if reason_contains:
        rows = [r for r in rows if reason_contains in (r.change_reason or "")]
    if effect_contains:
        rows = [
            r
            for r in rows
            if effect_contains in (r.actual_effect or "")
            or effect_contains in (r.expected_effect or "")
        ]
    return StrategyListResponse(versions=[_to_domain(r) for r in rows])


@router.get("/versions/{version_id}", response_model=StrategyResponse)
def get_version(version_id: str, session: Session = Depends(get_session)) -> StrategyResponse:
    store = StrategyVersionStore(session)
    row = store.get_version(version_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"策略版本不存在: {version_id}")
    return StrategyResponse(**_to_domain(row).model_dump())


@router.post("/optimize", response_model=StrategyResponse)
def optimize(
    payload: StrategyOptimizeRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StrategyResponse:
    """AI 优化生成候选（校验后置 verified / 未通过置 invalid）。"""
    allowed, why = _optimize_allowed(session, settings)
    if not allowed:
        raise AppError(ErrorCode.STRATEGY_OPTIMIZE_PAUSED, why)
    res = strategy_service.optimize(
        session,
        settings,
        payload=payload.payload,
        change_reason=payload.change_reason,
        evidence_summary=payload.evidence_summary,
        expected_effect=payload.expected_effect,
    )
    store = StrategyVersionStore(session)
    row = store.get_version(res.version_id)
    return StrategyResponse(**_to_domain(row).model_dump())


@router.post("/{version_id}/validate", response_model=StrategyResponse)
def validate(
    version_id: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StrategyResponse:
    """对指定候选执行完整运行校验（重新校验）。"""
    store = StrategyVersionStore(session)
    row = store.get_version(version_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"策略版本不存在: {version_id}")
    from app.domain.strategy_replay import load_metrics_available, replay_history
    from app.domain.strategy_validator import BOUNDARY_CASES, validate_strategy

    replay = replay_history(session)
    report = validate_strategy(
        row.payload or {},
        cases=BOUNDARY_CASES,
        available_metrics=load_metrics_available(session),
        replay_total=replay.total,
        replay_matched=replay.matched,
        min_replay_match=settings.validate_min_replay_match,
    )
    store.set_validation(version_id, status="passed" if report.passed else "failed",
                         result=report.to_dict())
    store.append_event(
        event_type="validate",
        target_version_id=version_id,
        validation_result=report.to_dict(),
    )
    return StrategyResponse(**_to_domain(store.get_version(version_id)).model_dump())


@router.post("/{version_id}/restore", response_model=StrategyResponse)
def restore(
    version_id: str,
    payload: StrategyRestoreRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """手动恢复历史策略（§9.1~9.5）：产生新版本，不删后续版本、不重算旧报告。"""
    store = StrategyVersionStore(session)
    row = store.get_version(version_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"策略版本不存在: {version_id}")

    # 恢复前检查（§9.2）：存在冲突 → 不得直接恢复
    from app.domain.strategy_replay import load_metrics_available, replay_history
    from app.domain.strategy_restore import check_restorable

    replay = replay_history(session)
    gate = check_restorable(
        target=dict(row.payload or {}),
        available_metrics=load_metrics_available(session),
        safety_boundary_ok=True,
        replay_total=replay.total,
        replay_matched=replay.matched,
        min_replay_match=settings.validate_min_replay_match,
    )
    if not gate["allowed"]:
        raise AppError(
            ErrorCode.STRATEGY_NOT_VERIFIED,
            "目标版本不满足恢复前检查，不得恢复",
            {"conflicts": gate["conflicts"], "checks": gate["checks"]},
        )

    # 恢复 = 以目标 payload 新建版本（不修改历史、不删后续版本，§9.5）
    new = store.create_version(
        payload=dict(row.payload or {}),
        change_type=StrategyChangeType.MANUAL_RESTORE,
        change_reason=payload.reason,
        created_by="user",
        parent_version_id=store.get_active(strategy_type=settings.strategy_type).strategy_version_id
        if store.get_active(strategy_type=settings.strategy_type)
        else None,
        evidence_summary=f"手动恢复自 {row.version_number}",
        safety_constraints=dict(row.safety_constraints or {}),
        limitations=list(row.limitations or []),
        strategy_type=settings.strategy_type,
    )
    store.set_validation(new.strategy_version_id, status="passed", result={"manualRestore": True})
    from app.orchestrator.concurrency_guard import ConcurrencyGuard

    guard = ConcurrencyGuard(session, strategy_type=settings.strategy_type)
    guard.activate_if_verified(
        new.strategy_version_id, expected_current_id=guard.active_version_id()
    )
    store.append_event(
        event_type="restore",
        target_version_id=new.strategy_version_id,
        trigger_reason=payload.reason,
    )
    from app.services.notify_service import notify_strategy_change

    notify_strategy_change(
        session,
        change_type=StrategyChangeType.MANUAL_RESTORE.value,
        version_number=new.version_number,
        version_id=new.strategy_version_id,
        reason=payload.reason,
        key_changes=f"恢复自 {row.version_number}",
    )
    return StrategyResponse(**_to_domain(new).model_dump())


@router.get("/diff", response_model=StrategyDiff)
def diff(
    from_id: str = Query(...),  # noqa: B008  — FastAPI 依赖参数
    to_id: str = Query(...),  # noqa: B008
    session: Session = Depends(get_session),
) -> StrategyDiff:
    """两个版本的三层差异比较（§9.6）：摘要 / 关键差异 / 完整差异。"""
    from app.domain.strategy_diff import diff_versions

    store = StrategyVersionStore(session)
    a = store.get_version(from_id)
    b = store.get_version(to_id)
    if a is None or b is None:
        missing = from_id if a is None else to_id
        raise AppError(ErrorCode.NOT_FOUND, f"策略版本不存在: {missing}")
    return StrategyDiff(
        **diff_versions(
            _to_domain(a).model_dump(mode="json"),
            _to_domain(b).model_dump(mode="json"),
        )
    )


@router.get("/events", response_model=StrategyEventListResponse)
def list_events(
    type: str | None = Query(default=None),  # noqa: A002
    session: Session = Depends(get_session),
) -> StrategyEventListResponse:
    store = StrategyVersionStore(session)
    rows = store.list_events(event_type=type)
    events = [
        StrategyEvent(
            event_id=r.event_id,
            event_type=r.event_type,
            triggered_at=r.triggered_at,
            current_version_id=r.current_version_id,
            target_version_id=r.target_version_id,
            task_id=r.task_id,
            trigger_reason=r.trigger_reason,
            steps=list(r.steps or []),
            validation_result=r.validation_result or {},
            failure_reason=r.failure_reason,
            risk_state=r.risk_state,
            notify_status=r.notify_status,
            related_report_ref=r.related_report_ref,
            follow_up_status=r.follow_up_status,
        )
        for r in rows
    ]
    return StrategyEventListResponse(events=events)


def _created_day(row) -> dt.date:  # noqa: ANN001
    """版本创建日（用于 §9.7 时间筛选）。"""
    return row.created_at.date() if isinstance(row.created_at, dt.datetime) else row.created_at


def _optimize_allowed(session: Session, settings: Settings) -> tuple[bool, str]:
    from app.services.recovery_service import optimize_allowed

    return optimize_allowed(session, settings)
