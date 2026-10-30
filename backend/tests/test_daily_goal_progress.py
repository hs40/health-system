"""日报四项长期目标进展测试（需求 §11.2：日报中部可展示关键图表）。

断言来自需求与已确认口径，不依赖实现细节：

| 测试组 | 对应要求 |
|---|---|
| 日报带齐四项长期目标（固定优先级顺序） | 需求 §4.1 四目标固定 |
| 窗口 = 近 7 天，基线 = 前一个 7 天 | 日报「基线取紧邻的前一个等长区间」 |
| 无数据 → insufficient，不编造 0 | 需求 §13.8 / §5.2「不编造缺失数据」 |
| 落库 payload 含该字段 | API `/reports/daily` 读 payload |
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import Settings
from app.schemas.domain import StandardDaily
from app.services.report_service import generate_daily
from app.storage.standard_store import upsert_standard

D = dt.date(2026, 10, 20)
GOAL_KEYS = ["cardio", "fatLoss", "muscleEndurance", "maxStrength"]


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def settings(tmp_path):
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        raw_data_dir=tmp_path / "raw",
        db_path=tmp_path / "t.db",
        audit_dir=tmp_path / "audit",
    )


def _seed(session, *, days: int = 14, current_weight: float = 80.0, prev_weight: float = 82.0):
    """近 14 天标准化数据：前 7 天体重 82，近 7 天体重 80（用于验证基线窗口）。"""
    for i in range(days):
        day = D - dt.timedelta(days=i)
        weight = current_weight if i < 7 else prev_weight
        upsert_standard(
            session,
            StandardDaily(
                date=day,
                resting_heart_rate=50,
                max_heart_rate=180,
                hrv_last_night_avg=60.0,
                sleep_duration_sec=8 * 3600,
                steps=8000,
                body_battery_wake=70,
                weight_kg=weight,
            ),
        )
    session.commit()


def test_daily_report_carries_four_goals(session, settings):
    """四项长期目标必须全部出现，且保持需求 §4.1 的固定顺序。"""
    _seed(session)
    report = generate_daily(session, D, settings)
    assert report.goal_progress is not None
    goals = report.goal_progress["goals"]
    assert list(goals) == GOAL_KEYS
    for key in GOAL_KEYS:
        g = goals[key]
        # 每项都必须能自洽展示：名称/单位/方向/取值来源
        assert {"label", "unit", "direction", "value", "baseline", "delta", "source"} <= set(g)


def test_window_is_trailing_seven_days(session, settings):
    """窗口 = [D-6, D]，基线 = [D-13, D-7]（与周报同口径，避免两处漂移）。"""
    _seed(session)
    report = generate_daily(session, D, settings)
    rng = report.goal_progress["range"]
    assert rng["start"] == (D - dt.timedelta(days=6)).isoformat()
    assert rng["end"] == D.isoformat()
    fat = report.goal_progress["goals"]["fatLoss"]
    assert fat["baseline_start"] == (D - dt.timedelta(days=13)).isoformat()
    assert fat["baseline_end"] == (D - dt.timedelta(days=7)).isoformat()


def test_baseline_is_previous_seven_days(session, settings):
    """近 7 天均值 80，前 7 天均值 82 → 变化量 -2.0。"""
    _seed(session)
    report = generate_daily(session, D, settings)
    fat = report.goal_progress["goals"]["fatLoss"]
    assert fat["value"] == 80.0
    assert fat["baseline"] == 82.0
    assert fat["delta"] == -2.0
    assert fat["source"] == "measured"


def test_missing_data_is_insufficient_not_zero(session, settings):
    """无训练记录时，训练侧目标必须标 insufficient，不得伪造 0。"""
    _seed(session)
    report = generate_daily(session, D, settings)
    goals = report.goal_progress["goals"]
    for key in ("muscleEndurance", "maxStrength"):
        assert goals[key]["value"] is None
        assert goals[key]["source"] == "insufficient"


def test_persisted_payload_contains_goal_progress(session, settings):
    """目标进展必须随日报落库，历史日报读回来同样可用。"""
    _seed(session)
    generate_daily(session, D, settings)
    from app.db.models import DailyReportRow

    row = session.exec(select(DailyReportRow).where(DailyReportRow.date == D)).first()
    assert row is not None
    gp = row.payload.get("goal_progress")
    assert gp is not None
    assert list(gp["goals"]) == GOAL_KEYS
