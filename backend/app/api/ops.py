"""运维接口（REQ-UI-004 / REQ-OPS-001）。"""

from __future__ import annotations

import os
from datetime import date as dt_date
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app import __version__
from app.config import get_settings
from app.db.models import AuditEventRow, TaskRunRow
from app.db.session import get_session
from app.schemas.api import (
    AuditListResponse,
    DepStatus,
    FaultListResponse,
    FaultReportRequest,
    HealthResponse,
    PrecheckResponse,
    SyncRequest,
    TaskListResponse,
)
from app.schemas.domain import FaultRecoveryTask

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/health", response_model=HealthResponse)
def health(session: Session = Depends(get_session)) -> HealthResponse:
    """健康检查：数据库、目录、外部依赖配置状态。"""
    settings = get_settings()
    deps: list[DepStatus] = []

    # 数据库
    try:
        session.exec(select(TaskRunRow).limit(1)).all()
        deps.append(DepStatus(name="sqlite", ok=True, detail=str(settings.db_path)))
    except Exception as exc:  # noqa: BLE001
        deps.append(DepStatus(name="sqlite", ok=False, detail=str(exc)))

    # 目录
    for name, path in (("raw", settings.raw_data_dir), ("audit", settings.audit_dir)):
        deps.append(DepStatus(name=name, ok=path.exists(), detail=str(path)))

    # 外部依赖：仅检查是否已配置（不在健康检查中发起真实调用）
    tokenstore = settings.garmin_tokenstore / "tokens.json"
    plugin_session = Path(
        os.path.expanduser(
            "~/AppData/Local/dsh-plugin-garmin-connect/accounts/personal-cn.session.json"
        )
    )
    garmin_ready = tokenstore.exists() or plugin_session.exists() or bool(settings.garmin_email)
    if tokenstore.exists():
        garmin_detail = f"tokenstore={tokenstore}"
    elif plugin_session.exists():
        garmin_detail = "可用插件 session 引导（尚无本地 tokenstore）"
    elif settings.garmin_email:
        garmin_detail = "credentials configured"
    else:
        garmin_detail = "not configured"
    deps.append(DepStatus(name="garmin", ok=garmin_ready, detail=garmin_detail))
    tredict_cfg = Path(r"F:/WorkBuddy AI/Garmin_Tredict/_tools/tredict.config.json")
    if settings.tredict_ready:
        tredict_ready, tredict_detail = True, "token configured"
    elif tredict_cfg.exists():
        tredict_ready, tredict_detail = True, f"token from {tredict_cfg.name}"
    else:
        tredict_ready, tredict_detail = False, "not configured"
    deps.append(DepStatus(name="tredict", ok=tredict_ready, detail=tredict_detail))
    deps.append(
        DepStatus(
            name="llm",
            ok=settings.llm_ready,
            detail=f"{settings.llm_provider} / {settings.llm_model_daily}"
            if settings.llm_ready
            else "not configured",
        )
    )

    overall = "ok" if all(d.ok for d in deps[:3]) else "degraded"
    return HealthResponse(
        status=overall,
        version=__version__,
        risk_rule_version=settings.risk_rule_version,
        deps=deps,
    )


@router.get("/sync-status")
def sync_status(
    session: Session = Depends(get_session),
) -> dict:
    """数据同步状态（2026-10-21 决策 Q8：运维台展示本地覆盖 + 最近校验）。

    返回：各 Garmin raw 数据集的本地覆盖（文件数 / 最新日期）、Tredict 镜像计数、
    最近一次云端校验报告的摘要（从 ``data/audit/cloud-verify-*.json`` 读）。
    纯本地读取，不联网。
    """
    from pathlib import Path as _Path

    from sqlmodel import select as _select

    from app.db.models import SyncLedgerRow, TredictActivityRow
    from app.services.sync_status import build_sync_status

    settings = get_settings()
    raw_root = _Path(settings.raw_data_dir) / "garmin"
    status = build_sync_status(raw_root)

    with_mirror = len(session.exec(_select(TredictActivityRow)).all())
    ledger = session.exec(_select(SyncLedgerRow)).all()
    _FAIL = ("mismatch", "missing_local", "missing_remote")
    status["tredict_activity_mirror"] = with_mirror
    status["sync_ledger"] = {
        "rows": len(ledger),
        "failed": sum(1 for r in ledger if r.state in _FAIL),
    }

    # 最近一次云端校验报告
    audit_dir = _Path(settings.audit_dir)
    reports = sorted(audit_dir.glob("cloud-verify-*.json")) if audit_dir.exists() else []
    if reports:
        import json as _json

        latest = reports[-1]
        try:
            data = _json.loads(latest.read_text(encoding="utf-8"))
            status["last_verification"] = {
                "report": latest.name,
                "generated_at": data.get("generated_at"),
                "diff": data.get("diff"),
            }
        except Exception:  # noqa: BLE001 — 报告损坏不应让运维台挂掉
            status["last_verification"] = {"report": latest.name, "error": "unreadable"}
    else:
        status["last_verification"] = None
    return status


@router.get("/tasks", response_model=TaskListResponse)
def list_tasks(
    status: str | None = Query(default=None),
    type: str | None = Query(default=None),  # noqa: A002
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> TaskListResponse:
    stmt = select(TaskRunRow).order_by(TaskRunRow.started_at.desc()).limit(limit)  # type: ignore[attr-defined]
    if status:
        stmt = stmt.where(TaskRunRow.status == status)
    if type:
        stmt = stmt.where(TaskRunRow.task_type == type)
    rows = session.exec(stmt).all()
    from app.schemas.domain import TaskError, TaskRun
    from app.schemas.enums import TaskStatus

    tasks = [
        TaskRun(
            id=r.id,
            task_type=r.task_type,
            idempotency_key=r.idempotency_key,
            status=TaskStatus(r.status),
            started_at=r.started_at,
            finished_at=r.finished_at,
            attempts=r.attempts,
            checkpoint=r.checkpoint,
            error=TaskError(**r.error) if r.error else None,
        )
        for r in rows
    ]
    return TaskListResponse(tasks=tasks)


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, session: Session = Depends(get_session)) -> dict:
    """重试失败任务（阶段 A 由编排器实现；当前仅校验存在性）。"""
    row = session.get(TaskRunRow, task_id)
    if row is None:
        from app.errors import AppError
        from app.schemas.enums import ErrorCode

        raise AppError(ErrorCode.NOT_FOUND, f"任务不存在: {task_id}")
    # TODO(阶段A): 交由 orchestrator.retry 处理
    return {"task_id": task_id, "accepted": False, "reason": "orchestrator not implemented yet"}


@router.post("/sync")
def trigger_sync(req: SyncRequest, session: Session = Depends(get_session)) -> dict:
    """手动触发同步：Garmin 同步 → 标准化 → 日报（阶段 A 端到端闭环）。"""
    import datetime as dt

    from app.services.daily_pipeline import run_daily

    settings = get_settings()
    target = dt.date.fromisoformat(req.date) if req.date else dt.date.today()
    sync, report = run_daily(session, target, settings)
    return {
        "accepted": True,
        "mode": req.mode,
        "date": target.isoformat(),
        "sync": {
            "status": sync.status,
            "written": sync.written,
            "skipped": sync.skipped,
            "missing": sync.missing,
            "errors": sync.errors,
        },
        "report": {
            "priority": report.risk.priority.value if report.risk else None,
            "data_state": report.risk.data_state.value if report.risk else None,
            "triggered_rules": report.risk.triggered_rules if report.risk else [],
            "quality": report.quality.grade.value,
        },
    }


@router.get("/audit", response_model=AuditListResponse)
def list_audit(
    category: str | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> AuditListResponse:
    stmt = select(AuditEventRow).order_by(AuditEventRow.at.desc()).limit(limit)  # type: ignore[attr-defined]
    if category:
        stmt = stmt.where(AuditEventRow.category == category)
    if from_:
        stmt = stmt.where(AuditEventRow.at >= from_)
    if to:
        stmt = stmt.where(AuditEventRow.at <= to)
    rows = session.exec(stmt).all()
    from app.schemas.domain import AuditEvent
    from app.schemas.enums import AuditCategory

    events = [
        AuditEvent(id=r.id, at=r.at, category=AuditCategory(r.category), ref=r.ref, detail=r.detail)
        for r in rows
    ]
    return AuditListResponse(events=events)


# ---------------------------- 故障恢复（阶段③ T3.9）---------------------------
@router.get("/faults", response_model=FaultListResponse)
def list_faults(
    severity: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> FaultListResponse:
    from app.services import recovery_service

    return FaultListResponse(faults=recovery_service.list_faults(session, severity=severity))


@router.post("/faults", response_model=FaultRecoveryTask)
def report_fault(
    payload: FaultReportRequest, session: Session = Depends(get_session)
) -> FaultRecoveryTask:
    """登记故障（自动分级 general/major）。"""
    from app.services import recovery_service

    return recovery_service.report_fault(
        session,
        kind=payload.kind,
        impact_scope=payload.impact_scope,
        fix_measures=payload.fix_measures,
    )


@router.get("/faults/{fault_id}/precheck", response_model=PrecheckResponse)
def fault_precheck(
    fault_id: str,
    session: Session = Depends(get_session),
) -> PrecheckResponse:
    """执行并返回恢复前校验报告（§8.2.2~8.2.6）。"""
    from app.errors import AppError
    from app.schemas.enums import ErrorCode
    from app.services import recovery_service

    try:
        fault = recovery_service.run_precheck(session, get_settings(), fault_id)
    except KeyError as exc:
        raise AppError(ErrorCode.NOT_FOUND, str(exc)) from exc
    return PrecheckResponse(fault=fault)


@router.post("/faults/{fault_id}/confirm", response_model=FaultRecoveryTask)
def fault_confirm(fault_id: str, session: Session = Depends(get_session)) -> FaultRecoveryTask:
    """用户确认恢复优化（§8.2.8~8.2.10；未确认不得恢复）。"""
    from app.errors import AppError
    from app.schemas.enums import ErrorCode
    from app.services import recovery_service

    try:
        return recovery_service.confirm_recovery(session, get_settings(), fault_id)
    except KeyError as exc:
        raise AppError(ErrorCode.NOT_FOUND, str(exc)) from exc
    except AppError:
        raise


@router.post("/validation/load")
def validate_load(
    start: dt_date | None = Query(default=None, alias="from", description="起始日期（含）"),
    end: dt_date | None = Query(default=None, alias="to", description="结束日期（含）"),
    session: Session = Depends(get_session),
) -> dict:
    """Tredict 指标 ↔ 项目负荷 验证（需求 6.4 / 开发计划 4.10）。

    手动触发、只读：不联网、不写库、不保存运行历史（需求 6.4）。
    结果按指标逐项报告，不设统一通过阈值、不做排名（需求 7.3）。
    """
    from app.services import validation_service

    return validation_service.run_validation(
        session,
        get_settings(),
        start=start,
        end=end,
    )
