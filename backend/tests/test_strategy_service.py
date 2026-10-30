"""策略服务测试（阶段③ T3.2；验收 §19.4）。

覆盖：
- 首启幂等初始化；
- AI 优化生成候选 → 校验通过置 verified / 未通过置 invalid；
- 未验证版本不得激活（§19.4.7）；
- 激活产生新版本、旧版本 superseded；
- 每次变化都产生新版本（§19.4.1）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.db.models import (  # noqa: F401
    BaselineRow,
    DailyReportRow,
    StandardDailyRow,
)
from app.errors import AppError
from app.schemas.enums import StrategyStatus
from app.services import strategy_service
from app.storage.strategy_store import StrategyVersionStore


@pytest.fixture
def session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _settings(**kw) -> Settings:
    base = {
        "_env_file": None,
        "validate_min_replay_match": 0.80,
    }
    base.update(kw)
    return Settings(**base)


def _seed_history(session: Session, *, n: int = 30) -> None:
    """写入合成历史：标准数据 + 基线 + 日报（使回放有一致样本）。"""
    session.add(
        BaselineRow(
            metric="hrv_last_night_avg", version=1, window_days=28, mean=60, std=5,
            p10=52, p90=68, mature=True,
        )
    )
    session.add(
        BaselineRow(
            metric="resting_heart_rate", version=1, window_days=28, mean=52, std=3,
            p10=48, p90=56, mature=True,
        )
    )
    session.add(
        BaselineRow(
            metric="sleep_duration_sec", version=1, window_days=28, mean=25200, std=3600,
            p10=18000, p90=32400, mature=True,
        )
    )
    base = dt.date(2026, 1, 1)
    for i in range(n):
        day = base + dt.timedelta(days=i)
        session.add(
            StandardDailyRow(
                date=day, resting_heart_rate=52, sleep_duration_sec=25200, hrv_last_night_avg=60
            )
        )
        session.add(
            DailyReportRow(
                date=day,
                payload={"risk": {"level": "normal", "triggered_rules": []}},
            )
        )
    session.commit()


def test_ensure_initial_version_idempotent(session):
    s = _settings()
    v1 = strategy_service.ensure_initial_version(session, s, on_date=dt.date(2026, 7, 15))
    v2 = strategy_service.ensure_initial_version(session, s, on_date=dt.date(2026, 7, 15))
    assert v1 == v2  # 幂等
    store = StrategyVersionStore(session)
    assert len(store.list_versions()) == 1
    assert store.get_active() is not None


def test_optimize_passes_with_history(session):
    s = _settings()
    _seed_history(session)
    strategy_service.ensure_initial_version(session, s)
    res = strategy_service.optimize(session, s, change_reason="优化A")
    assert res.validation_passed is True
    assert res.status == StrategyStatus.VERIFIED.value
    # 新版本产生（§19.4.1）
    store = StrategyVersionStore(session)
    assert len(store.list_versions()) == 2


def test_optimize_fails_without_history(session):
    """无历史 → 回放校验失败 → invalid，不得参与决策。"""
    s = _settings()
    strategy_service.ensure_initial_version(session, s)
    res = strategy_service.optimize(session, s, change_reason="优化B")
    assert res.validation_passed is False
    assert res.status == StrategyStatus.INVALID.value
    assert any("无历史回放样本" in f for f in res.failures)


def test_activate_unverified_raises(session):
    """未通过校验的版本不得激活（§19.4.7）。"""
    s = _settings()
    strategy_service.ensure_initial_version(session, s)
    res = strategy_service.optimize(session, s, change_reason="优化C")  # 无历史 → invalid
    with pytest.raises(AppError):
        strategy_service.activate(session, s, res.version_id)


def test_activate_verified_supersedes_old(session):
    s = _settings()
    _seed_history(session)
    strategy_service.ensure_initial_version(session, s)
    store = StrategyVersionStore(session)
    old_active = store.get_active()

    res = strategy_service.optimize(session, s, change_reason="优化D")
    strategy_service.activate(session, s, res.version_id)

    new_active = store.get_active()
    assert new_active.version_number == res.version_number
    old = store.get_version(old_active.strategy_version_id)
    assert old.status == StrategyStatus.SUPERSEDED.value
    # 至多一个 active
    assert len(store.list_versions(status=StrategyStatus.ACTIVE.value)) == 1


def test_every_change_creates_new_version(session):
    """每次变化产生新版本（§19.4.1）。"""
    s = _settings()
    _seed_history(session)
    strategy_service.ensure_initial_version(session, s)
    for i in range(3):
        strategy_service.optimize(session, s, change_reason=f"优化{i}")
    store = StrategyVersionStore(session)
    nums = [v.version_number for v in store.list_versions()]
    assert nums == ["season-1.0.0", "season-1.0.1", "season-1.0.2", "season-1.0.3"]


def test_optimize_records_events(session):
    s = _settings()
    _seed_history(session)
    strategy_service.ensure_initial_version(session, s)
    strategy_service.optimize(session, s, change_reason="优化E")
    store = StrategyVersionStore(session)
    types = [e.event_type for e in store.list_events()]
    assert "optimize" in types
    assert "validate" in types
    assert "activate" in types  # 首启初始化


def test_current_initializes_when_none(session):
    s = _settings()
    cur = strategy_service.current(session, s)
    assert cur is not None
    assert cur.status == StrategyStatus.ACTIVE.value


def test_season_for_date():
    assert strategy_service.season_for(dt.date(2026, 7, 15)) == "summer"
    assert strategy_service.season_for(dt.date(2026, 1, 15)) == "winter"
    assert strategy_service.season_for(dt.date(2026, 4, 15)) == "spring"
    assert strategy_service.season_for(dt.date(2026, 10, 15)) == "autumn"
