"""策略版本存储与版本号纯逻辑测试（阶段③ T3.1；验收 §19.4）。

覆盖：
- 版本号稳定递增、不复用；
- 新建版本初始为 candidate；
- 状态流转（candidate→verified→active→superseded）与原子激活（至多一个 active）；
- 正式版本内容不可改（不可变性）；
- 事件记录。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import StrategyEventRow, StrategyVersionRow  # noqa: F401
from app.domain.strategy_versioning import (
    can_participate_in_decision,
    is_active,
    is_immutable_status,
    next_version_number,
    parse_version,
)
from app.schemas.enums import StrategyChangeType, StrategyStatus
from app.storage.strategy_store import StrategyVersionStore


@pytest.fixture
def session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


# ---------------------------- 版本号纯逻辑 ----------------------------
def test_parse_version_valid_and_invalid():
    assert parse_version("season-1.0.0") == ("season", 1, 0, 0)
    assert parse_version("season-2.3.11") == ("season", 2, 3, 11)
    assert parse_version("bad") is None
    assert parse_version("season-1.0") is None


def test_next_version_number_empty_and_increment():
    assert next_version_number("season", []) == "season-1.0.0"
    assert next_version_number("season", ["season-1.0.0"]) == "season-1.0.1"
    assert next_version_number("season", ["season-1.0.0", "season-1.0.5"]) == "season-1.0.6"
    # 忽略非法版本号
    assert next_version_number("season", ["season-1.0.0", "garbage"]) == "season-1.0.1"


def test_status_predicates():
    assert is_active(StrategyStatus.ACTIVE)
    assert not is_active(StrategyStatus.CANDIDATE)
    assert can_participate_in_decision(StrategyStatus.ACTIVE)
    assert not can_participate_in_decision(StrategyStatus.VERIFIED)
    assert not can_participate_in_decision(StrategyStatus.CANDIDATE)
    assert is_immutable_status(StrategyStatus.ACTIVE)
    assert not is_immutable_status(StrategyStatus.CANDIDATE)


# ---------------------------- 存储层 ----------------------------
def _create(store: StrategyVersionStore, **kw):
    base = {
        "payload": {"season": "summer", "focus": "有氧"},
        "change_type": StrategyChangeType.AI_OPTIMIZE,
        "change_reason": "初始候选",
        "created_by": "ai",
    }
    base.update(kw)
    return store.create_version(**base)


def test_create_version_initial_candidate(session):
    store = StrategyVersionStore(session)
    v = _create(store)
    assert v.status == StrategyStatus.CANDIDATE.value
    assert v.version_number == "season-1.0.0"
    assert v.immutable is True
    assert v.validation_status == "pending"


def test_version_numbers_increment(session):
    store = StrategyVersionStore(session)
    v1 = _create(store)
    v2 = _create(store)
    assert (v1.version_number, v2.version_number) == ("season-1.0.0", "season-1.0.1")


def test_validation_pass_sets_verified(session):
    store = StrategyVersionStore(session)
    v = _create(store)
    updated = store.set_validation(v.strategy_version_id, status="passed", result={"ok": True})
    assert updated.status == StrategyStatus.VERIFIED.value
    assert updated.validation_status == "passed"


def test_validation_fail_sets_invalid(session):
    store = StrategyVersionStore(session)
    v = _create(store)
    updated = store.set_validation(v.strategy_version_id, status="failed", result={"errors": ["x"]})
    assert updated.status == StrategyStatus.INVALID.value
    assert not can_participate_in_decision(StrategyStatus(updated.status))


def test_activate_is_atomic_single_active(session):
    """激活新版本 → 旧 active 自动降级为 superseded，至多一个 active（§7.8）。"""
    store = StrategyVersionStore(session)
    v1 = _create(store)
    store.set_validation(v1.strategy_version_id, status="passed", result={})
    store.activate(v1.strategy_version_id)
    assert store.get_active().strategy_version_id == v1.strategy_version_id

    v2 = _create(store)
    store.set_validation(v2.strategy_version_id, status="passed", result={})
    store.activate(v2.strategy_version_id)

    active = store.get_active()
    assert active is not None
    assert active.strategy_version_id == v2.strategy_version_id
    # 旧版本被降级
    old = store.get_version(v1.strategy_version_id)
    assert old is not None
    assert old.status == StrategyStatus.SUPERSEDED.value
    # 全表至多一个 active
    actives = store.list_versions(status=StrategyStatus.ACTIVE.value)
    assert len(actives) == 1


def test_activate_sets_effective_at(session):
    store = StrategyVersionStore(session)
    v = _create(store)
    store.set_validation(v.strategy_version_id, status="passed", result={})
    at = dt.datetime(2026, 10, 16, 12, 0, 0)
    row = store.activate(v.strategy_version_id, at=at)
    assert row.effective_at == at


def test_payload_not_mutated_by_status_change(session):
    """状态流转不得改动 payload（不可变性，§19.4.4）。"""
    store = StrategyVersionStore(session)
    payload = {"season": "winter", "focus": "力量"}
    v = _create(store, payload=payload)
    store.set_validation(v.strategy_version_id, status="passed", result={})
    store.activate(v.strategy_version_id)
    again = store.get_version(v.strategy_version_id)
    assert again.payload == payload
    assert again.change_reason == "初始候选"


def test_list_versions_ordered(session):
    store = StrategyVersionStore(session)
    _create(store)
    _create(store)
    _create(store)
    nums = [v.version_number for v in store.list_versions()]
    assert nums == ["season-1.0.0", "season-1.0.1", "season-1.0.2"]


# ---------------------------- 事件 ----------------------------
def test_append_and_list_events(session):
    store = StrategyVersionStore(session)
    v = _create(store)
    ev = store.append_event(
        event_type="optimize",
        target_version_id=v.strategy_version_id,
        trigger_reason="AI 优化",
        steps=[{"step": 1, "ok": True}],
    )
    assert ev.event_id
    assert ev.notify_status == "pending"
    events = store.list_events(event_type="optimize")
    assert len(events) == 1
    # 更新通知状态
    store.update_event_notify(ev.event_id, "sent")
    assert store.list_events(event_type="optimize")[0].notify_status == "sent"


def test_mark_status_invalid(session):
    store = StrategyVersionStore(session)
    v = _create(store)
    store.mark_status(v.strategy_version_id, StrategyStatus.INVALID)
    assert store.get_version(v.strategy_version_id).status == StrategyStatus.INVALID.value
