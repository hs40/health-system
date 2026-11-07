"""计划服务 · Garmin 课程上传/排期/回读（TA.11 / REQ-RISK-003 / REQ-TASK-004）。

从 ``plan_service.py`` 拆出（ARCHITECTURE_REVIEW A.1.2）。

安全约束：高风险或存在硬性拦截时**不上传计划**。
未知处理：上传请求超时 → 状态记 ``unknown``，**不盲目重发**，
须先用 ``reconcile_unknown`` 回读 Garmin 端真实状态再决定。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlmodel import Session

from app.adapters.garmin import GarminAdapter
from app.config import Settings
from app.db.models import TrainingSessionRow
from app.domain.prescription import to_garmin_workout
from app.errors import AppError
from app.schemas.api import UploadResult
from app.schemas.domain import TaskError, TrainingSession
from app.schemas.enums import AuditCategory, ErrorCode, SessionStatus, TaskStatus, TaskType
from app.storage import task_state
from app.storage.audit_store import record as audit

logger = logging.getLogger(__name__)


def _idem(session_id: str) -> str:
    return f"garmin:plan:{session_id}"


def upload_session(
    session: Session,
    session_id: str,
    settings: Settings,
    *,
    schedule: bool = False,
) -> UploadResult:
    """上传（或更新）课程；默认**只进课程库、不排期**（需要时可显式传 ``schedule=True``）。"""
    row = session.get(TrainingSessionRow, session_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"训练处方不存在: {session_id}")
    ts = TrainingSession(**row.payload)

    # 风险门禁：高风险不得上传
    if ts.status == SessionStatus.SKIPPED:
        return UploadResult(session_id=session_id, status="skipped", message="该处方已被跳过")

    # E25：恢复日计划（R-003 命中）**仅本地可见，禁止上传** ——
    # 身体状态差时不应在 Garmin 日历/课程库里多出一个待练课程。
    if not ts.upload_allowed:
        return UploadResult(
            session_id=session_id,
            status="blocked",
            message="恢复日计划仅本地可见，不上传 Garmin",
        )

    may, reason = task_state.may_retry(session, _idem(session_id))
    if not may:
        return UploadResult(
            session_id=session_id,
            status="uploaded" if row.garmin_workout_id else "unknown",
            garmin_workout_id=row.garmin_workout_id,
            garmin_scheduled_id=row.garmin_scheduled_id,
            message=reason,
        )

    run = task_state.begin(session, TaskType.PLAN_UPLOAD, _idem(session_id))
    adapter = GarminAdapter(settings)
    payload = to_garmin_workout(ts)

    try:
        if row.garmin_workout_id:
            workout_id = adapter.update_workout(row.garmin_workout_id, payload)
        else:
            workout_id = adapter.upload_workout(payload)
    except AppError as e:
        # 超时/未知 → 记 unknown，禁止盲目重发
        guarded = e.code in (ErrorCode.EXTERNAL_TIMEOUT, ErrorCode.EXTERNAL_UNKNOWN)
        status = TaskStatus.UNKNOWN if guarded else TaskStatus.FAILED
        task_state.finish(
            session, run.id, status, error=TaskError(code=e.code.value, message=e.message)
        )
        audit(
            session,
            AuditCategory.UPLOAD,
            ref=session_id,
            detail={"system": "garmin", "op": "upload_workout", "result": status.value},
            audit_dir=settings.audit_dir,
        )
        return UploadResult(
            session_id=session_id,
            status=status.value,
            message=f"{e.message}（{'需回读核对' if guarded else '可重试'}）",
        )

    row.garmin_workout_id = workout_id
    row.status = SessionStatus.UPLOADED.value

    scheduled_id = row.garmin_scheduled_id
    if schedule:
        try:
            scheduled_id = adapter.schedule_workout(workout_id, ts.date)
            row.garmin_scheduled_id = scheduled_id
        except AppError as e:
            session.add(row)
            session.commit()
            task_state.finish(
                session,
                run.id,
                TaskStatus.PARTIAL,
                error=TaskError(code=e.code.value, message=e.message),
            )
            return UploadResult(
                session_id=session_id,
                status="unknown" if e.code == ErrorCode.EXTERNAL_TIMEOUT else "failed",
                garmin_workout_id=workout_id,
                message=f"课程已上传但排期失败：{e.message}",
            )

    session.add(row)
    session.commit()
    task_state.finish(session, run.id, TaskStatus.SUCCESS)
    audit(
        session,
        AuditCategory.PLAN_VERSION,
        ref=session_id,
        detail={
            "system": "garmin",
            "workout_id": workout_id,
            "scheduled_id": scheduled_id,
            "date": ts.date.isoformat(),
        },
        audit_dir=settings.audit_dir,
    )
    return UploadResult(
        session_id=session_id,
        status="scheduled" if schedule else "uploaded",
        garmin_workout_id=workout_id,
        garmin_scheduled_id=scheduled_id,
    )


def _sched_date(it: dict) -> str:
    """排期条目上的日期：日历端点用 ``date``，``upcoming`` 端点用 ``scheduleDate``。"""
    return str(it.get("date") or it.get("scheduleDate") or it.get("calendarDate") or "")


def _sched_id(it: dict) -> int | None:
    """排期 id：日历端点用 ``id``，``upcoming`` 端点用 ``scheduledWorkoutId``。"""
    raw = it.get("id") or it.get("scheduledWorkoutId")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def scheduled_on(settings: Settings, day: dt.date) -> list[dict]:
    """回读 Garmin 端某日已排期课程（用于未知处理与幂等核对）。

    **两个来源都要查**：日历端点（``/calendar-service``）覆盖正常排期；``upcoming``
    端点覆盖日历看不到的部分（含课程模板已删的孤儿排期，其 ``itemType`` 缺省）。
    """
    adapter = GarminAdapter(settings)
    target = day.isoformat()
    candidates = [
        *adapter.get_scheduled_workouts(day.year, day.month),
        *adapter.upcoming_schedules(),
    ]
    seen: set[int] = set()
    out: list[dict] = []
    for it in candidates:
        item_type = it.get("itemType")
        if item_type is not None and str(item_type) != "workout":
            continue
        if not _sched_date(it).startswith(target):
            continue
        sid = _sched_id(it)
        if sid is not None:
            if sid in seen:
                continue
            seen.add(sid)
        out.append(it)
    return out


def reconcile_unknown(session: Session, session_id: str, settings: Settings) -> UploadResult:
    """对 ``unknown`` 任务回读 Garmin 真实状态并落库（REQ-TASK-004）。"""
    row = session.get(TrainingSessionRow, session_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"训练处方不存在: {session_id}")
    ts = TrainingSession(**row.payload)
    items = scheduled_on(settings, ts.date)
    if items:
        first = items[0]
        row.garmin_workout_id = first.get("workoutId") or row.garmin_workout_id
        row.garmin_scheduled_id = first.get("id") or first.get("scheduledWorkoutId")
        row.status = SessionStatus.UPLOADED.value
        session.add(row)
        session.commit()
        audit(
            session,
            AuditCategory.UPLOAD,
            ref=session_id,
            detail={"op": "reconcile", "result": "found", "item": first},
            audit_dir=settings.audit_dir,
        )
        return UploadResult(
            session_id=session_id,
            status="scheduled",
            garmin_workout_id=row.garmin_workout_id,
            garmin_scheduled_id=row.garmin_scheduled_id,
            message="回读确认：Garmin 端已存在该日课程",
        )
    audit(
        session,
        AuditCategory.UPLOAD,
        ref=session_id,
        detail={"op": "reconcile", "result": "not_found"},
        audit_dir=settings.audit_dir,
    )
    return UploadResult(
        session_id=session_id, status="failed", message="回读确认：Garmin 端无该日课程，可安全重传"
    )


# ============================================================
# 周计划（TB.1/TB.2/TB.3 / REQ-PLAN-006 / D17 / D18）
# ============================================================
WEEK_SYSTEM = (
    "你是一名训练计划助手。请为给定的一周（周一至周日）安排训练，"
    "遵守：夜班日只安排恢复/轻活动或不安排；高风险日不安排强度课；"
    "每周至少 1 个完整休息日；同一天只安排一次主要训练。"
    "不要给出医学诊断。输出必须是 JSON 对象。"
)


