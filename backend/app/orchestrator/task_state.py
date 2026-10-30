"""任务状态机（TA.12 / REQ-TASK-001..005）。

状态语义：
- ``success``：全部子步成功；
- ``partial``：部分成功（外部已产生副作用，需人工/自动补齐）；
- ``failed``：未产生副作用，可安全重试；
- ``retry``：等待重试；
- ``unknown``：**外部执行结果未知**（如上传请求超时）→ 严禁盲目重发，必须先回读核对。

幂等（REQ-TASK-002）：以 ``idempotency_key`` 去重；已 ``success`` 的键直接复用，
``unknown`` 的键必须先经回读确认后才可再次执行。
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlmodel import Session, select

from app.db.models import TaskRunRow
from app.schemas.domain import TaskError
from app.schemas.enums import TaskStatus, TaskType

# 可安全重试的状态（外部未产生副作用或已确认可重放）
SAFE_RETRY_STATUSES = {TaskStatus.FAILED, TaskStatus.RETRY}
# 严禁盲目重发的状态（需先回读核对外部真实结果）
GUARDED_STATUSES = {TaskStatus.UNKNOWN}


def new_id() -> str:
    return uuid.uuid4().hex


def find_by_key(session: Session, idempotency_key: str) -> TaskRunRow | None:
    return session.exec(
        select(TaskRunRow)
        .where(TaskRunRow.idempotency_key == idempotency_key)
        .order_by(TaskRunRow.started_at.desc())  # type: ignore[attr-defined]
    ).first()


def begin(
    session: Session,
    task_type: TaskType,
    idempotency_key: str,
    *,
    checkpoint: str | None = None,
) -> TaskRunRow:
    """开始一次任务；若同键已存在则**复用并累加尝试次数**（幂等）。"""
    existing = find_by_key(session, idempotency_key)
    if existing is not None:
        existing.attempts += 1
        existing.status = TaskStatus.RETRY.value
        existing.started_at = dt.datetime.now()
        existing.finished_at = None
        if checkpoint:
            existing.checkpoint = checkpoint
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    row = TaskRunRow(
        id=new_id(),
        task_type=task_type.value,
        idempotency_key=idempotency_key,
        status=TaskStatus.RETRY.value,
        started_at=dt.datetime.now(),
        attempts=1,
        checkpoint=checkpoint,
        error={},
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def finish(
    session: Session,
    task_id: str,
    status: TaskStatus,
    *,
    error: TaskError | None = None,
    checkpoint: str | None = None,
) -> TaskRunRow:
    row = session.get(TaskRunRow, task_id)
    if row is None:
        raise KeyError(f"任务不存在: {task_id}")
    row.status = status.value
    row.finished_at = dt.datetime.now()
    if error is not None:
        row.error = error.model_dump(mode="json")
    if checkpoint:
        row.checkpoint = checkpoint
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def may_retry(session: Session, idempotency_key: str) -> tuple[bool, str]:
    """判断某幂等键是否可直接重试（``unknown`` 需先回读，返回 ``False``）。"""
    row = find_by_key(session, idempotency_key)
    if row is None:
        return True, "无历史任务"
    status = TaskStatus(row.status)
    if status == TaskStatus.SUCCESS:
        return False, "已完成，无需重试（幂等）"
    if status in GUARDED_STATUSES:
        return False, "上次执行结果未知，须先回读外部真实状态后再决定"
    if status in SAFE_RETRY_STATUSES or status == TaskStatus.PARTIAL:
        return True, "可安全重试"
    return False, f"状态 {status.value} 不可重试"
