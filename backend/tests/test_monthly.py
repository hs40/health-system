"""月报与滚动 12 周计划测试（TC.1 / TC.2 / TC.3）。"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.db.models import TrainingSessionRow, WeeklyPlanRow
from app.errors import AppError
from app.schemas.domain import StandardDaily
from app.schemas.enums import ErrorCode
from app.services import plan_service
from app.services.report_service import analyze_monthly, generate_monthly
from app.storage.standard_store import upsert_standard

MONTH = "2026-10"
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _standard(d: dt.date, rhr: int, hrv: float, sleep: int) -> StandardDaily:
    return StandardDaily(
        date=d,
        resting_heart_rate=rhr,
        hrv_last_night_avg=hrv,
        sleep_duration_sec=sleep,
        steps=8000,
        active_calories=400,
        weight_kg=70.0,
    )


def _seed_month(session: Session, month: str, base_hr: int, base_hrv: float) -> None:
    year, mon = (int(x) for x in month.split("-"))
    start = dt.date(year, mon, 1)
    nxt = dt.date(year + 1, 1, 1) if mon == 12 else dt.date(year, mon + 1, 1)
    d = start
    i = 0
    while d < nxt:
        upsert_standard(session, _standard(d, base_hr + i % 3, base_hrv + i, 7 * 3600 + i * 60))
        d += dt.timedelta(days=1)
        i += 1


# ---------------------------- 月报 ----------------------------
def test_generate_monthly_aggregates(session):
    _seed_month(session, MONTH, 50, 60.0)
    _seed_month(session, "2026-09", 52, 55.0)

    report = generate_monthly(session, MONTH, SETTINGS)
    assert report.month == MONTH
    assert report.review["days_with_data"] == 31
    assert report.goal_progress["steps_total"] == 8000 * 31
    assert report.goal_progress["weight_avg_kg"] == 70.0
    # 与上月对比：静息心率下降 → 负 delta
    assert report.goal_progress["delta_vs_prev_month"]["resting_heart_rate_avg"] < 0
    assert report.long_term_adjustment is None

    # 幂等：再次生成不报错
    again = generate_monthly(session, MONTH, SETTINGS)
    assert again.month == MONTH


def test_generate_monthly_adherence(session):
    _seed_month(session, MONTH, 50, 60.0)
    ws = dt.date(2026, 10, 12)  # 周一
    days = [
        {"date": (ws + dt.timedelta(days=i)).isoformat(), "weekday": i,
         "is_night_shift": False, "session": {"id": f"s{i}"}, "note": None}
        for i in range(4)
    ]
    session.add(
        WeeklyPlanRow(week_start=ws, payload={"days": days, "status": "draft", "version": 1},
                      status="draft", version=1)
    )
    for i in range(2):
        d = ws + dt.timedelta(days=i)
        session.add(
            TrainingSessionRow(id=f"s{i}", date=d, sport="running", name="跑", payload={},
                               status="confirmed")
        )
    session.commit()

    report = generate_monthly(session, MONTH, SETTINGS)
    ad = report.goal_progress["adherence"]
    assert ad["planned_sessions"] == 4
    assert ad["completed_sessions"] == 2
    assert ad["adherence_rate"] == 0.5


def test_generate_monthly_rejects_bad_month(session):
    with pytest.raises(AppError) as ei:
        generate_monthly(session, "2026-13", SETTINGS)
    assert ei.value.code == ErrorCode.VALIDATION


def test_analyze_monthly_requires_existing(session):
    with pytest.raises(AppError) as ei:
        analyze_monthly(session, MONTH, SETTINGS)
    assert ei.value.code == ErrorCode.NOT_FOUND


def test_analyze_monthly_with_llm(session, monkeypatch):
    _seed_month(session, MONTH, 50, 60.0)
    generate_monthly(session, MONTH, SETTINGS)

    def fake_chat_json(self, messages, model=None, required_keys=()):  # noqa: ANN001
        return {
            "deviation": "依从性偏低",
            "corrections": ["降低周目标"],
            "adjustment": "转入基础期",
        }

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", fake_chat_json)
    report = analyze_monthly(session, MONTH, SETTINGS)
    assert report.long_term_adjustment == "转入基础期"


# ---------------------------- 12 周计划 ----------------------------
def test_generate_plan12w_structure(session):
    plan = plan_service.generate_plan12w(session, SETTINGS, start_date=dt.date(2026, 10, 14))
    assert plan.start_date == dt.date(2026, 10, 12)  # 对齐到周一
    assert len(plan.weeks) == 12
    phases = [w["phase"] for w in plan.weeks]
    assert phases[0] == "base"
    assert phases[3] == "deload"
    assert phases[4] == "build"
    assert phases[8] == "peak"
    assert phases[11] == "deload"
    # 每 4 周评估一次
    assert [w["week_index"] for w in plan.weeks if w["evaluation_due"]] == [4, 8, 12]
    assert plan.goal_weights["endurance"] == 0.4


def test_latest_plan12w_returns_last(session):
    plan_service.generate_plan12w(session, SETTINGS, start_date=dt.date(2026, 10, 12))
    plan_service.generate_plan12w(session, SETTINGS, start_date=dt.date(2026, 10, 19))
    latest = plan_service.latest_plan12w(session)
    assert latest is not None
    assert latest.start_date == dt.date(2026, 10, 19)


def test_generate_plan12w_custom_weights(session):
    plan = plan_service.generate_plan12w(
        session, SETTINGS, start_date=dt.date(2026, 10, 12), goal_weights={"endurance": 1.0}
    )
    assert plan.goal_weights == {"endurance": 1.0}


def test_plan12w_re_evaluates_season_per_week(session):
    """T2.7：12 周跨越换季 → 每周按其起始日重新评估季节。"""
    # 从 11 月中开始 → 跨入冬季（12 月）
    plan = plan_service.generate_plan12w(session, SETTINGS, start_date=dt.date(2026, 11, 16))
    seasons = [w["season"] for w in plan.weeks]
    assert "autumn" in seasons
    assert "winter" in seasons  # 12 月起进入冬季
    assert all("season_focus" in w for w in plan.weeks)
    assert all(w["long_term_priority"] for w in plan.weeks)


def test_plan12w_winter_season_priority(session):
    """冬季长期优先级以肌耐力/力量为先（季节策略内容）。"""
    plan = plan_service.generate_plan12w(session, SETTINGS, start_date=dt.date(2026, 12, 7))
    first = plan.weeks[0]
    assert first["season"] == "winter"
    assert first["long_term_priority"][0] in ("muscleEndurance", "maxStrength")
