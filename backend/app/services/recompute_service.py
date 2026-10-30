"""训练负荷与 ACWR 重算编排（需求第十章，开发计划 4.9）。

业务规则（用户 2026-10-20 确认，逐条对应需求）：

- **10.1 自动触发**：数据补充/修正校验通过后自动重算，**不新增手动重算入口**
  （``POST /data/change`` 只登记变更并触发，不接受「重算某日」的直接请求）。
- **10.2 范围**：从**最早受影响日期**重算**至当前日期**，逐日更新日负荷汇总，
  再从最早受影响日回放 EWMA/ACWR（不能只补当天，否则递推断链）。
- **10.3 合并**：重叠任务合并，取最早受影响日为起算点。
- **10.4 执行中的任务不中途合并**：执行中到达的变更只落 ``pending``，
  当前任务跑完后**再触发新任务**（不在同一次执行里追加重算范围）。
- **10.5 沿用现有机制**：状态机 ``task_run``、幂等键、并发控制、审计，
  不新增专用日志表/审计表。
- **10.6 历史日报不自动更新**；当天日报在重算结束后重新生成并覆盖（Q4）。

Q4：重算执行期间**不生成当天日报**（``report_service.generate_daily`` 会拒绝，
返回 ``E_TASK_IN_PROGRESS``）；重算结束后由本模块在任务末尾生成当天日报。
"""

from __future__ import annotations

import datetime as dt
import json

from sqlmodel import Session, select

from app.db.models import TaskRunRow
from app.orchestrator import task_state
from app.schemas.domain import TaskError
from app.schemas.enums import AuditCategory, ErrorCode, TaskStatus, TaskType
from app.services import report_service
from app.storage.audit_store import record as audit

# 任务类型与状态口径
RECOMPUTE_TYPE = TaskType.RECOMPUTE.value
# 「执行中」= 未结束：状态机中执行中的行仍是 retry（begin 写入，finish 才落终态）
RUNNING_STATUSES = {TaskStatus.RETRY.value}
# 僵尸判定阈值：单步执行受 daily_sync.STEP_TIMEOUT_SEC=3600s 封顶，
# 超过 6h 仍「执行中」只可能是进程已死（崩溃 / 被 kill / 断电）——必须收割，
# 否则它会按 Q4 永久挡住当天日报，也让后续重算全部合并进一个死任务。
STALE_TASK_AFTER = dt.timedelta(hours=6)


def _settings():  # noqa: ANN202 - 延迟导入避免启动期循环依赖
    from app.config import get_settings

    return get_settings()


# ============================================================
# 执行中判定（供日报生成入口复用，见 report_service.generate_daily）
# ============================================================
def running_task(session: Session) -> TaskRunRow | None:
    """当前是否有重算任务在执行中（纯读，不回收僵尸）。"""
    return session.exec(
        select(TaskRunRow)
        .where(
            TaskRunRow.task_type == RECOMPUTE_TYPE,
            TaskRunRow.status.in_(sorted(RUNNING_STATUSES)),  # type: ignore[attr-defined]
        )
        .order_by(TaskRunRow.started_at.desc())  # type: ignore[attr-defined]
    ).first()


def blocking_task(session: Session) -> TaskRunRow | None:
    """日报闸门与变更登记共用的「执行中」判定：**先收割僵尸再返回**。

    崩溃残留的 ``retry`` 行不会自己变终态。若放任不管：
    - ``generate_daily`` 会按 Q4 永久返回 409，日报再也生成不出来；
    - 后续每次变更都合并进这个死任务，重算名存实亡。

    收割动作：``rollback()``（会话可能停在失败事务里）→ 落 ``failed``
    → 记审计。阈值见 ``STALE_TASK_AFTER``；未超时视为正常执行中。
    """
    running = running_task(session)
    if running is None:
        return None
    if dt.datetime.now() - running.started_at < STALE_TASK_AFTER:
        return running

    session.rollback()  # 上一次 flush 失败会让会话卡在 PendingRollbackError
    age_h = (dt.datetime.now() - running.started_at).total_seconds() / 3600.0
    message = (
        f"重算任务启动 {age_h:.1f} 小时后仍无终态，判定进程已死，"
        f"按僵尸任务终止（checkpoint={running.checkpoint}）"
    )
    task_state.finish(
        session,
        running.id,
        TaskStatus.FAILED,
        error=TaskError(code=ErrorCode.EXTERNAL_TIMEOUT.value, message=message),
    )
    audit(
        session,
        AuditCategory.TASK,
        ref=running.id,
        detail={"reason": "stale_recompute", "age_hours": round(age_h, 2),
                "checkpoint": running.checkpoint},
        audit_dir=_settings().audit_dir,
    )
    return None


def is_running(session: Session) -> bool:
    return running_task(session) is not None


# ============================================================
# 范围合并（10.3 / 10.4）
# ============================================================
def _latest_task(session: Session) -> TaskRunRow | None:
    return session.exec(
        select(TaskRunRow)
        .where(TaskRunRow.task_type == RECOMPUTE_TYPE)
        .order_by(TaskRunRow.started_at.desc())  # type: ignore[attr-defined]
    ).first()


def _parse_checkpoint(raw: str | None) -> dict[str, str]:
    """解析 checkpoint：新格式为 JSON dict；旧格式为空格分隔的 ``key=value``（向后兼容）。"""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    out: dict[str, str] = {}
    for token in text.split():
        if "=" in token:
            key, value = token.split("=", 1)
            out[key] = value
    return out


def _format_checkpoint(data: dict[str, str]) -> str:
    """checkpoint 统一写为 JSON（结构化，避免「改写法必须同时改写入方与解析方」）。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _pending_start(row: TaskRunRow | None) -> dt.date | None:
    """从 checkpoint 里读出「待处理起点」（执行期间到达、尚未处理的变更）。"""
    value = _parse_checkpoint(row.checkpoint if row is not None else None).get("pending")
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


# ============================================================
# 入口：登记变更并自动触发（10.1 / 10.3 / 10.4）
# ============================================================
def request(session: Session, earliest_affected: dt.date) -> dict:
    """登记一次数据变更并**自动触发**重算（需求 10.1）。

    - 无执行中任务 → 立即重算 ``earliest_affected`` → 今天（10.2）；
      若上次任务还留有 ``pending``，一并取更早者（10.3 合并）；
    - 已有执行中任务 → **不在本次执行里追加重算范围**（10.4），
      只把起点记为该任务的 ``pending``，由调用方在该任务结束后再次触发。
    """
    today = dt.date.today()
    running = blocking_task(session)
    if running is not None:
        queued_from = min(earliest_affected, _pending_start(running) or earliest_affected)
        _set_pending(session, running, queued_from)
        return {
            "accepted": True,
            "merged": True,
            "task_id": running.id,
            "from": None,
            "to": today.isoformat(),
            "status": running.status,
            "queued_from": queued_from.isoformat(),
        }

    pending = _pending_start(_latest_task(session))
    start = min(earliest_affected, pending) if pending else earliest_affected
    result = run(session, start, today)
    result["merged"] = False
    return result


def _set_pending(session: Session, row: TaskRunRow, day: dt.date) -> None:
    """把执行中到达的变更记为待处理起点（不改变本任务已定的范围）。"""
    data = _parse_checkpoint(row.checkpoint)
    pending = _pending_start(row)
    earliest = day if pending is None else min(pending, day)
    data["pending"] = earliest.isoformat()
    row.checkpoint = _format_checkpoint(data)
    session.add(row)
    session.commit()


# ============================================================
# 执行（10.2 / 10.5 / 10.6 / Q4）
# ============================================================
def week_starts_in_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """区间覆盖的所有周一（含 ``start`` 所在周）。"""
    first = start - dt.timedelta(days=start.weekday())
    out: list[dt.date] = []
    day = first
    while day <= end:
        out.append(day)
        day += dt.timedelta(days=7)
    return out


def months_in_range(start: dt.date, end: dt.date) -> list[str]:
    """区间覆盖的月份（``YYYY-MM``）。"""
    out: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def regenerate_aggregates(
    session: Session, start: dt.date, end: dt.date, settings
) -> dict[str, int]:
    """重生成区间覆盖的**周报与月报**（口径变更后与日报保持一致）。

    为什么需要：只重算派生层不改聚合层，会让「日报是新口径、周月报是旧快照」——
    2026-10-25 实测 4/16 月报、2/6 周报与 ``risk_assessment`` 不一致，
    用户看到的月度风险分布与日报告自相矛盾。

    单期失败不阻断其余（与逐日重算同策略）：某一周/月生成失败时 rollback 后继续。
    """
    weekly_ok = monthly_ok = 0
    for week_start in week_starts_in_range(start, end):
        try:
            report_service.generate_weekly(session, week_start, settings)
            weekly_ok += 1
        except Exception:  # noqa: BLE001 — 单期失败不阻断
            session.rollback()
    for month in months_in_range(start, end):
        try:
            report_service.generate_monthly(session, month, settings)
            monthly_ok += 1
        except Exception:  # noqa: BLE001 — 单期失败不阻断
            session.rollback()
    return {"weekly": weekly_ok, "monthly": monthly_ok}


def run(session: Session, start: dt.date, end: dt.date) -> dict:
    """执行重算：``start`` → ``end`` 逐日重算负荷，再从 ``start`` 回放 ACWR。

    结束后生成 ``end``（当天）日报并覆盖（Q4 / 10.6），并**重生成区间覆盖的
    周报/月报**（2026-10-25 缺口 1：否则「日报新口径、周月报旧快照」自相矛盾）。
    单日失败不阻断后续日期，最终状态按失败范围落 ``partial`` / ``failed``。
    """
    key = f"recompute:{start.isoformat()}~{end.isoformat()}"
    row = task_state.begin(
        session,
        TaskType.RECOMPUTE,
        key,
        checkpoint=_format_checkpoint({"from": start.isoformat(), "to": end.isoformat()}),
    )

    errors: list[str] = []
    try:
        days = 0
        d = start
        while d <= end:
            try:
                report_service.recompute_day_load(session, d, _settings())
            except Exception as exc:  # noqa: BLE001 - 单日失败不阻断后续日期
                # 失败的那次 flush 已让会话进入 PendingRollbackError，
                # 必须 rollback 才能继续后续日期（否则每天都抛同一个错）
                session.rollback()
                errors.append(f"load {d.isoformat()}: {exc}")
            days += 1
            d += dt.timedelta(days=1)

        # 10.2：ACWR 必须从最早受影响日回放，不能只补当天
        # （``refresh_acwr`` 内部按 CHRONIC_DAYS*2 回看预热，递推结果与全程重算一致）
        d = start
        while d <= end:
            try:
                report_service.refresh_acwr(session, d)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                errors.append(f"acwr {d.isoformat()}: {exc}")
            d += dt.timedelta(days=1)

        # 10.6 + Q4：历史日报不更新，只重新生成当天日报（重算期间禁止外部生成）
        daily_ok = True
        try:
            report_service.generate_daily(
                session, end, _settings(), during_recompute=True
            )
        except Exception as exc:  # noqa: BLE001
            daily_ok = False
            errors.append(f"daily_report {end.isoformat()}: {exc}")

        # 聚合层（周报/月报）必须跟着重生成，否则会出现「日报新口径、周月报旧快照」
        try:
            aggregates = regenerate_aggregates(session, start, end, _settings())
        except Exception as exc:  # noqa: BLE001
            aggregates = {"weekly": 0, "monthly": 0}
            errors.append(f"aggregates: {exc}")

        status = TaskStatus.SUCCESS
        if errors:
            status = TaskStatus.PARTIAL if daily_ok else TaskStatus.FAILED
    except Exception as exc:  # noqa: BLE001
        # 先 rollback 再落终态：否则 finish() 自己的 commit 也会被
        # PendingRollbackError 顶掉，任务永远停在 retry（僵尸）。
        session.rollback()
        task_state.finish(
            session,
            row.id,
            TaskStatus.FAILED,
            error=TaskError(code=ErrorCode.DATA_CORRUPT.value, message=str(exc)),
        )
        raise

    # 保留执行期间 _set_pending 追加的 pending（否则 finish 覆盖会丢，违反 10.3）
    final_ckpt = _parse_checkpoint(session.get(TaskRunRow, row.id).checkpoint)  # type: ignore[union-attr]
    final_ckpt.update(
        {"from": start.isoformat(), "to": end.isoformat(), "days": str(days)}
    )
    task_state.finish(
        session,
        row.id,
        status,
        error=TaskError(code=ErrorCode.DATA_CORRUPT.value, message="；".join(errors[:5]))
        if errors
        else None,
        checkpoint=_format_checkpoint(final_ckpt),
    )
    audit(
        session,
        AuditCategory.CALCULATION,
        ref=f"recompute:{start.isoformat()}~{end.isoformat()}",
        detail={
            "task_id": row.id,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "days": days,
        "aggregates": aggregates,
            "status": status.value,
            "errors": errors,
            "daily_report": daily_ok,
        },
        audit_dir=_settings().audit_dir,
    )
    return {
        "accepted": True,
        "task_id": row.id,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": days,
        "status": status.value,
        "errors": errors,
        "daily_report_generated": daily_ok,
    }
