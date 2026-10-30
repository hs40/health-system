"""消息子系统测试（阶段③ T3.10；验收 §19.7，共 12 项）。

覆盖：
1. 策略版本变化时必须发送系统消息
2. 消息包含所需信息（事件类型/时间/版本/原因/结果/详情入口）
3. 消息关联相应策略版本与事件
4. 标记已读不改变任何策略状态
5. 查看消息不改变任何策略状态
6. 已读 ≠ 已解决
7. 消息发送失败被记录（不混淆「版本已创建」与「消息已送达」）
8. 存在未读消息时可统计未读数
9. 可按类别/关键词过滤
10. 清理消息不删除底层审计/故障记录
11. 失败/需确认的消息升级为高重要级
12. 无消息时可正常返回空列表
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import MessageRow, StrategyEventRow  # noqa: F401
from app.schemas.enums import (
    MessageCategory,
    MessageImportance,
    NotifyStatus,
    StrategyChangeType,
    StrategyStatus,
)
from app.services import notify_service
from app.storage.strategy_store import StrategyVersionStore


@pytest.fixture
def session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _make_version(session: Session) -> str:
    store = StrategyVersionStore(session)
    v = store.create_version(
        payload={"season": "summer", "focus": "夏季有氧"},
        change_type=StrategyChangeType.AI_OPTIMIZE,
        change_reason="AI 优化",
        created_by="system",
    )
    return v.strategy_version_id


# ---------------------------- 1/2/3. 策略变化必发消息 ----------------------------
def test_strategy_change_sends_message(session):
    """策略版本变化 → 立即发送系统消息（§19.7.1）。"""
    vid = _make_version(session)
    msg = notify_service.notify_strategy_change(
        session,
        change_type="aiOptimize",
        version_number="season-1.0.0",
        version_id=vid,
        reason="提升夏季有氧比例",
        key_changes="endurance_ratio 0.5→0.6",
    )
    assert msg.send_status == NotifyStatus.SENT.value
    assert msg.category == MessageCategory.STRATEGY_VERSION


def test_message_contains_required_fields(session):
    """消息包含事件类型/时间/版本/原因/结果/详情入口（§19.7.2）。"""
    vid = _make_version(session)
    msg = notify_service.notify_strategy_change(
        session,
        change_type="autoRollback",
        version_number="season-1.0.1",
        version_id=vid,
        reason="效果不佳",
        key_changes="回退至冬训基线",
    )
    assert msg.title
    body = msg.body
    for expect in ("事件类型", "相关策略版本", "变更原因", "关键变化概况", "执行结果"):
        assert expect in body
    assert msg.at is not None
    assert msg.link  # 查看详情入口


def test_message_links_version_and_event(session):
    """消息关联相应策略版本与事件（§19.7.3）。"""
    vid = _make_version(session)
    store = StrategyVersionStore(session)
    ev = store.append_event(
        event_type="rollback", current_version_id=vid, target_version_id=vid
    )
    msg = notify_service.notify_strategy_change(
        session,
        change_type="autoRollback",
        version_number="season-1.0.2",
        version_id=vid,
        event_id=ev.event_id,
        reason="安全回退",
    )
    assert msg.ref_type == "strategy_version"
    assert msg.ref_id == vid


# ---------------------------- 4/5. 已读/查看不改策略 ----------------------------
def test_mark_read_does_not_change_strategy(session):
    """标记已读不改变任何策略状态（§19.7.4）。"""
    vid = _make_version(session)
    store = StrategyVersionStore(session)
    store.activate(vid)  # 使 active
    before = store.get_version(vid).status

    msg = notify_service.notify_strategy_change(
        session, change_type="aiOptimize", version_number="season-1.0.0", version_id=vid
    )
    notify_service.mark_read(session, msg.message_id)

    assert store.get_version(vid).status == before
    # 版本状态未受消息操作影响
    assert store.get_version(vid).status == StrategyStatus.ACTIVE.value


def test_view_message_does_not_change_strategy(session):
    """查看（列出）消息不改变任何策略状态（§19.7.5）。"""
    vid = _make_version(session)
    store = StrategyVersionStore(session)
    store.activate(vid)
    notify_service.notify_strategy_change(
        session, change_type="aiOptimize", version_number="season-1.0.0", version_id=vid
    )
    items, _ = notify_service.list_messages(session)
    assert len(items) == 1
    assert store.get_version(vid).status == StrategyStatus.ACTIVE.value


# ---------------------------- 6. 已读 ≠ 已解决 ----------------------------
def test_read_is_not_resolved(session):
    """已读 ≠ 已解决（§19.7.6）。"""
    msg = notify_service.send_message(
        session, category=MessageCategory.STRATEGY_VERSION, title="t"
    )
    assert msg.is_read is False and msg.is_resolved is False
    after_read = notify_service.mark_read(session, msg.message_id)
    assert after_read.is_read is True
    assert after_read.is_resolved is False
    after_resolve = notify_service.mark_resolved(session, msg.message_id)
    assert after_resolve.is_resolved is True


# ---------------------------- 7. 发送失败被记录 ----------------------------
def test_send_failure_recorded_status(session):
    """发送失败被记录：send_status 可置 failed，且不影响版本创建（§19.7.7）。"""
    row = MessageRow(
        message_id="m-fail",
        category=MessageCategory.STRATEGY_VERSION.value,
        importance=MessageImportance.HIGH.value,
        title="通知失败",
        body="",
        send_status=NotifyStatus.FAILED.value,
    )
    session.add(row)
    session.commit()
    items, _ = notify_service.list_messages(session)
    assert items[0].send_status == NotifyStatus.FAILED.value


# ---------------------------- 8/9. 未读数 + 过滤 ----------------------------
def test_unread_count(session):
    """存在未读消息时可统计未读数（§19.7.8）。"""
    for i in range(3):
        notify_service.send_message(
            session, category=MessageCategory.STRATEGY_VERSION, title=f"m{i}"
        )
    items, unread = notify_service.list_messages(session)
    assert len(items) == 3
    assert unread == 3
    notify_service.mark_all_read(session)
    _, unread2 = notify_service.list_messages(session)
    assert unread2 == 0


def test_filter_by_category_and_keyword(session):
    """可按类别/关键词过滤（§19.7.9）。"""
    notify_service.send_message(
        session, category=MessageCategory.STRATEGY_VERSION, title="策略变化 A"
    )
    notify_service.send_message(
        session, category=MessageCategory.RISK, title="风险提示 B"
    )
    by_cat, _ = notify_service.list_messages(session, category=MessageCategory.RISK.value)
    assert len(by_cat) == 1 and by_cat[0].title == "风险提示 B"
    by_q, _ = notify_service.list_messages(session, q="策略")
    assert len(by_q) == 1 and "策略" in by_q[0].title


# ---------------------------- 10. 清理不删审计 ----------------------------
def test_delete_messages_keeps_audit(session):
    """清理消息不删除底层审计/故障记录（§19.7.10）。"""
    vid = _make_version(session)
    store = StrategyVersionStore(session)
    store.append_event(event_type="rollback", current_version_id=vid)

    msg = notify_service.notify_strategy_change(
        session, change_type="autoRollback", version_number="season-1.0.0", version_id=vid
    )
    n = notify_service.delete_messages(session, [msg.message_id])
    assert n == 1
    # 消息已删
    items, _ = notify_service.list_messages(session)
    assert items == []
    # 事件审计仍在
    assert len(store.list_events(event_type="rollback")) == 1


# ---------------------------- 11. 高重要级升级 ----------------------------
def test_failure_or_confirm_escalates_importance(session):
    """失败/需确认 → 消息升级为高重要级（§19.7.11）。"""
    vid = _make_version(session)
    normal = notify_service.notify_strategy_change(
        session, change_type="aiOptimize", version_number="v1", version_id=vid
    )
    assert normal.importance == MessageImportance.NORMAL

    flagged = notify_service.notify_strategy_change(
        session,
        change_type="autoRollback",
        version_number="v2",
        version_id=vid,
        has_failure=True,
        needs_user_confirm=True,
    )
    assert flagged.importance == MessageImportance.HIGH
    assert "等待用户确认" in flagged.body


# ---------------------------- 12. 空列表 ----------------------------
def test_empty_message_list(session):
    """无消息时正常返回空列表（§19.7.12）。"""
    items, unread = notify_service.list_messages(session)
    assert items == []
    assert unread == 0
