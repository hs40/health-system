"""报告与策略版本关联测试（阶段④ T4.1；验收 §11.1 / §11.5）。

覆盖：
- 日报/周报/月报/周计划/12 周计划均记录生成时实际使用的有效策略版本；
- 关联块字段齐全（生成时间/数据范围/规则版本/模型版本/策略版本/风险状态/是否重算）；
- 策略版本变化**不自动触发**历史报告重算（§11.5）；
- 重算必须标注 ``is_recalculation=True``，不得伪装成原始报告。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import Settings
from app.db import models as _models  # noqa: F401  — 注册全部模型
from app.db.models import (  # noqa: F401
    BaselineRow,
    DailyReportRow,
    StandardDailyRow,
)
from app.domain import rule_registry as rr
from app.domain.report_link import (
    active_strategy_summary,
    build_strategy_link,
    linkage_complete,
    strategy_changes_in_window,
)
from app.schemas.enums import StrategyChangeType
from app.services import strategy_service
from app.storage.strategy_store import StrategyVersionStore


@pytest.fixture
def session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _settings(**kw) -> Settings:
    base = {"_env_file": None, "validate_min_replay_match": 0.80}
    base.update(kw)
    return Settings(**base)


def _seed_history(session: Session, *, n: int = 30) -> None:
    for metric, mean, std, p10, p90 in (
        ("hrv_last_night_avg", 60, 5, 52, 68),
        ("resting_heart_rate", 52, 3, 48, 56),
        ("sleep_duration_sec", 25200, 3600, 18000, 32400),
    ):
        session.add(
            BaselineRow(
                metric=metric, version=1, window_days=28, mean=mean, std=std,
                p10=p10, p90=p90, mature=True,
            )
        )
    base = dt.date(2026, 1, 1)
    for i in range(n):
        day = base + dt.timedelta(days=i)
        session.add(
            StandardDailyRow(
                date=day, resting_heart_rate=52, sleep_duration_sec=25200,
                hrv_last_night_avg=60,
            )
        )
        session.add(
            _models.DailyReportRow(
                date=day, payload={"risk": {"level": "normal", "triggered_rules": []}}
            )
        )
    session.commit()


# ---------------------------- 关联块 ----------------------------
def test_strategy_link_fields(session):
    """关联块包含 §11.1 要求的全部字段。"""
    link = build_strategy_link(
        session,
        report_generated_at=dt.datetime(2026, 10, 16, 8, 0, 0),
        data_range=(dt.date(2026, 9, 18), dt.date(2026, 10, 16)),
        rule_version=rr.RULE_VERSION,
        model_version="deepseek-chat",
        risk_state="normal",
    )
    for k in (
        "report_generated_at",
        "data_range",
        "rule_version",
        "model_version",
        "strategy_version_id",
        "strategy_version_number",
        "risk_state",
        "is_recalculation",
    ):
        assert k in link
    assert link["data_range"] == {"start": "2026-09-18", "end": "2026-10-16"}
    assert link["is_recalculation"] is False
    assert linkage_complete(link)


def test_strategy_link_records_active_version(session):
    """有关联有效策略时，版本标识必须齐全。"""
    s = _settings()
    strategy_service.ensure_initial_version(session, s)
    active = StrategyVersionStore(session).get_active()

    link = build_strategy_link(
        session,
        report_generated_at=dt.datetime.now(),
        data_range=(dt.date(2026, 10, 10), dt.date(2026, 10, 16)),
        rule_version=rr.RULE_VERSION,
        model_version="deepseek-chat",
    )
    assert link["strategy_version_id"] == active.strategy_version_id
    assert link["strategy_version_number"] == active.version_number
    assert link["strategy_status"] == "active"
    assert link["strategy_effective_at"] is not None
    assert linkage_complete(link)


def test_strategy_link_no_strategy_still_complete(session):
    """首启前无策略 → 关联块仍完整（版本标识为空但结构齐全）。"""
    link = build_strategy_link(
        session,
        report_generated_at=dt.datetime.now(),
        data_range=(dt.date(2026, 10, 16), dt.date(2026, 10, 16)),
        rule_version=rr.RULE_VERSION,
        model_version="",
    )
    assert link["strategy_version_id"] is None
    assert link["strategy_version_number"] is None
    assert linkage_complete(link)


def test_recalculation_flag(session):
    """重算必须标注 is_recalculation 且保留原报告生成时间（§11.5）。"""
    link = build_strategy_link(
        session,
        report_generated_at=dt.datetime(2026, 10, 20, 9, 0, 0),
        data_range=(dt.date(2026, 10, 16), dt.date(2026, 10, 16)),
        rule_version=rr.RULE_VERSION,
        model_version="",
        is_recalculation=True,
        recalc_of_generated_at=dt.datetime(2026, 10, 16, 8, 0, 0),
    )
    assert link["is_recalculation"] is True
    assert link["recalc_of_generated_at"] == "2026-10-16T08:00:00"


# ---------------------------- 日报接线 ----------------------------
def test_daily_report_records_strategy(session):
    """日报生成后 payload 含 strategy 关联块（§11.1）。"""
    from app.services import report_service

    s = _settings()
    _seed_history(session)
    strategy_service.ensure_initial_version(session, s)
    day = dt.date(2026, 1, 20)
    report_service.generate_daily(session, day, s)

    row = session.exec(
        select(DailyReportRow).where(DailyReportRow.date == day)
    ).first()
    assert row is not None
    link = row.payload.get("strategy")
    assert link is not None
    assert link["rule_version"] == rr.RULE_VERSION
    assert link["strategy_version_number"] is not None
    assert link["risk_state"]


# ---------------------------- 策略变化不触发重算（§11.5）--------------------------
def test_strategy_change_does_not_recompute_history(session):
    """策略版本变化不自动重算历史报告：旧报告 payload 保持不变。"""
    from app.services import report_service

    s = _settings()
    _seed_history(session)
    strategy_service.ensure_initial_version(session, s)
    day = dt.date(2026, 1, 20)
    report_service.generate_daily(session, day, s)
    before = session.exec(
        select(DailyReportRow).where(DailyReportRow.date == day)
    ).first().payload

    # 产生一次策略变化（新版本 + 激活）
    store = StrategyVersionStore(session)
    v2 = store.create_version(
        payload={"season": "winter", "focus": "冬训"},
        change_type=StrategyChangeType.AI_OPTIMIZE,
        change_reason="新策略",
        created_by="ai",
    )
    store.set_validation(v2.strategy_version_id, status="passed", result={"ok": True})
    store.activate(v2.strategy_version_id)

    after = session.exec(
        select(DailyReportRow).where(DailyReportRow.date == day)
    ).first().payload
    # 历史报告未被改写
    assert before == after
    assert after["strategy"]["strategy_version_number"] != v2.version_number


# ---------------------------- 周报/月报策略变化注明 ----------------------------
def test_strategy_changes_in_window(session):
    """窗口内策略变化可列出（供周报/月报注明，§11.3/§11.4）。"""
    s = _settings()
    strategy_service.ensure_initial_version(session, s)
    store = StrategyVersionStore(session)
    v2 = store.create_version(
        payload={"season": "winter", "focus": "冬训"},
        change_type=StrategyChangeType.AI_OPTIMIZE,
        change_reason="换季优化",
        created_by="ai",
    )
    store.append_event(event_type="optimize", target_version_id=v2.strategy_version_id)

    changes = strategy_changes_in_window(
        session,
        start=dt.date(2020, 1, 1),
        end=dt.date(2030, 1, 1),
    )
    assert any(c["version_number"] == v2.version_number for c in changes)
    assert all("changed_at" in c and "event_type" in c for c in changes)


def test_active_strategy_summary(session):
    s = _settings()
    strategy_service.ensure_initial_version(session, s, on_date=dt.date(2026, 10, 16))
    summary = active_strategy_summary(session)
    assert summary["strategy_version_number"] == "season-1.0.0"
    assert summary["season"] == "autumn"
    assert summary["status"] == "active"
