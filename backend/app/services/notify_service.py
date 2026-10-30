"""消息通知服务（阶段③ T3.10；REQ §10）。

**核心约束**：
- 策略版本变化（优化/回退/手动恢复）→ **立即**发送系统消息（§10.1/§10.4）；
- 消息必须关联相应策略版本与事件（§10.1）；
- 发送失败 → 记录 ``send_status``，**不得**将「版本已创建」与「消息已送达」混淆（§10.1）；
- **已读 ≠ 已解决**（§10.3）：``is_read`` 与 ``is_resolved`` 分离；
- 标记已读/查看消息**不触发**任何策略变化（§10.3）；
- 清理消息**不删除**底层审计/故障记录（§10.5）。
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlmodel import Session, select

from app.db.models import MessageRow
from app.schemas.domain import Message
from app.schemas.enums import MessageCategory, MessageImportance, NotifyStatus


def _to_domain(row: MessageRow) -> Message:
    return Message(
        message_id=row.message_id,
        at=row.at,
        category=MessageCategory(row.category),
        importance=MessageImportance(row.importance),
        title=row.title,
        body=row.body,
        ref_type=row.ref_type,
        ref_id=row.ref_id,
        link=row.link,
        is_read=row.is_read,
        is_resolved=row.is_resolved,
        send_status=row.send_status,
    )


def send_message(
    session: Session,
    *,
    category: MessageCategory,
    title: str,
    body: str = "",
    ref_type: str = "",
    ref_id: str = "",
    link: str = "",
    importance: MessageImportance = MessageImportance.NORMAL,
    at: dt.datetime | None = None,
) -> Message:
    """发送（落库）一条系统消息。

    ``send_status`` 先置 ``pending``，落库成功即视为 ``sent``（本地单用户无外部通道）。
    发送失败由调用方捕获并置 ``failed``（复用任务机制重发）。
    """
    row = MessageRow(
        message_id=uuid.uuid4().hex,
        at=at or dt.datetime.now(),
        category=category.value,
        importance=importance.value,
        title=title,
        body=body,
        ref_type=ref_type,
        ref_id=ref_id,
        link=link,
        is_read=False,
        is_resolved=False,
        send_status=NotifyStatus.PENDING.value,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    # 本地落库即视为已送达
    row.send_status = NotifyStatus.SENT.value
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_domain(row)


def notify_strategy_change(
    session: Session,
    *,
    change_type: str,
    version_number: str,
    version_id: str,
    event_id: str | None = None,
    reason: str = "",
    key_changes: str = "",
    success: bool = True,
    has_failure: bool = False,
    risk_state: str = "",
    needs_user_confirm: bool = False,
) -> Message:
    """策略版本变化 → 立即发消息（§10.1）。

    消息包含：事件类型、时间、相关版本、变更原因、关键变化概况、执行结果、
    是否失败/风险、查看详情入口（§10.2）。
    """
    type_label = {
        "aiOptimize": "AI 优化产生新版本",
        "autoRollback": "系统自动回退产生新版本",
        "manualRestore": "用户手动恢复产生新版本",
    }.get(change_type, f"策略变化（{change_type}）")

    status_line = "成功" if success else "失败"
    flagged = has_failure or needs_user_confirm
    importance = MessageImportance.HIGH if flagged else MessageImportance.NORMAL

    lines = [
        f"事件类型：{type_label}",
        f"相关策略版本：{version_number}",
        f"变更原因：{reason or '（未提供）'}",
        f"关键变化概况：{key_changes or '（无显著变化）'}",
        f"执行结果：{status_line}",
    ]
    if has_failure:
        lines.append("存在失败或风险：是")
    if risk_state:
        lines.append(f"当前状态：{risk_state}")
    if needs_user_confirm:
        lines.append("必要限制：等待用户确认后方可继续季节性策略优化")

    return send_message(
        session,
        category=MessageCategory.STRATEGY_VERSION,
        importance=importance,
        title=f"{type_label}：{version_number}",
        body="\n".join(lines),
        ref_type="strategy_version",
        ref_id=version_id,
        link=f"/strategy/versions/{version_id}",
    )


# ---------------------------- 查询与交互 ----------------------------
def list_messages(
    session: Session,
    *,
    unread_only: bool = False,
    category: str | None = None,
    q: str | None = None,
    limit: int = 200,
) -> tuple[list[Message], int]:
    """列出消息 + 未读数（§10.5）。"""
    stmt = select(MessageRow)
    if unread_only:
        stmt = stmt.where(MessageRow.is_read == False)  # noqa: E712
    if category:
        stmt = stmt.where(MessageRow.category == category)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(MessageRow.title.like(like) | MessageRow.body.like(like))  # type: ignore[attr-defined]
    stmt = stmt.order_by(MessageRow.at.desc()).limit(limit)  # type: ignore[attr-defined]
    rows = session.exec(stmt).all()

    unread = session.exec(
        select(MessageRow).where(MessageRow.is_read == False)  # noqa: E712
    ).all()
    return [_to_domain(r) for r in rows], len(unread)


def mark_read(session: Session, message_id: str) -> Message:
    """标记已读（**不改变任何策略**，§10.3/§19.7.4）。"""
    row = _require(session, message_id)
    row.is_read = True
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_domain(row)


def mark_all_read(session: Session) -> int:
    rows = session.exec(
        select(MessageRow).where(MessageRow.is_read == False)  # noqa: E712
    ).all()
    for r in rows:
        r.is_read = True
        session.add(r)
    session.commit()
    return len(rows)


def mark_resolved(session: Session, message_id: str) -> Message:
    """标记已解决（与已读分离，§10.3）。"""
    row = _require(session, message_id)
    row.is_resolved = True
    session.add(row)
    session.commit()
    session.refresh(row)
    return _to_domain(row)


def delete_messages(session: Session, ids: list[str]) -> int:
    """清理消息（**仅删消息行**；底层审计/故障记录不删，§10.5/§19.7.5）。"""
    n = 0
    for mid in ids:
        row = session.get(MessageRow, mid)
        if row is not None:
            session.delete(row)
            n += 1
    session.commit()
    return n


def _require(session: Session, message_id: str) -> MessageRow:
    row = session.get(MessageRow, message_id)
    if row is None:
        raise KeyError(f"消息不存在: {message_id}")
    return row
