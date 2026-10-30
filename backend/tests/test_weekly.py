"""周报与周计划服务测试（TB.1 / TB.3）。

覆盖：周报聚合与幂等落库、周计划版本自增、确认状态机、LLM 生成（打桩）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.domain import rule_registry as rr
from app.errors import AppError
from app.schemas.api import WeeklyPlan
from app.schemas.domain import StandardDaily, WeekPlanDay
from app.schemas.enums import ErrorCode
from app.services import plan_service
from app.services.report_service import generate_weekly
from app.storage.standard_store import upsert_standard

WS = dt.date(2026, 10, 12)  # 周一
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_week(session: Session, start: dt.date, base_hr: int, base_hrv: float) -> None:
    for i in range(7):
        d = start + dt.timedelta(days=i)
        upsert_standard(
            session,
            StandardDaily(
                date=d,
                resting_heart_rate=base_hr + i % 2,
                hrv_last_night_avg=base_hrv + i,
                sleep_duration_sec=7 * 3600 + i * 60,
                steps=8000 + i * 100,
                active_calories=400 + i * 10,
            ),
        )


# ---------------------------- 周报 ----------------------------
def test_generate_weekly_aggregates_and_persists(session):
    _seed_week(session, WS, 50, 60.0)
    _seed_week(session, WS - dt.timedelta(days=7), 52, 55.0)

    report = generate_weekly(session, WS, SETTINGS)
    assert report.week_start == WS
    assert report.recovery["resting_heart_rate_avg"] is not None
    assert report.recovery["hrv_avg"] is not None
    assert report.recovery["sleep_avg_hours"] == pytest.approx(7.0, abs=0.2)
    # 与上周对比：静息心率下降 → 负 delta
    assert report.recovery["delta_vs_prev_week"]["resting_heart_rate_avg"] < 0
    assert report.goal_progress["steps_total"] is not None

    # 幂等：再次生成同一周不报错且覆盖落库
    again = generate_weekly(session, WS, SETTINGS)
    assert again.week_start == WS


def test_generate_weekly_rejects_non_monday(session):
    with pytest.raises(AppError) as ei:
        generate_weekly(session, WS + dt.timedelta(days=1), SETTINGS)
    assert ei.value.code == ErrorCode.VALIDATION


def test_generate_weekly_counts_risk_rows(session):
    """周内存在评估记录时按四级优先级统计（每天必写一条，含 P4，Q15）。"""
    from app.db.models import RiskAssessmentRow

    _seed_week(session, WS, 50, 60.0)
    rows = [
        (1, "P1", "A"),
        (4, "P1", "B"),
        (2, "P2", "A"),
        (3, "P4", "A"),
    ]
    for i, priority, state in rows:
        session.add(
            RiskAssessmentRow(
                date=WS + dt.timedelta(days=i),
                priority=priority,
                data_state=state,
                triggered_rules=["P1-LOAD-HRV"] if priority == "P1" else [],
                evidence={},
                missing_inputs=[],
                rule_version=rr.RULE_VERSION,
            )
        )
    session.commit()

    report = generate_weekly(session, WS, SETTINGS)
    assert report.review["rest_days"] == 2
    assert report.review["reduce_days"] == 1
    assert report.review["progress_days"] == 0
    assert report.review["maintain_days"] == 1
    assert report.review["assessed_days"] == 4
    assert sorted(report.review["priorities"]) == ["P1", "P1", "P2", "P4"]


# ---------------------------- 周计划版本 ----------------------------
def _plan(note: str = "x") -> WeeklyPlan:
    days = [
        WeekPlanDay(date=WS + dt.timedelta(days=i), weekday=i, note=note) for i in range(7)
    ]
    return WeeklyPlan(week_start=WS, days=days)


def test_save_weekly_plan_versions_increment(session):
    p1 = plan_service.save_weekly_plan(session, _plan("v1"), "draft")
    assert p1.version == 1 and p1.status == "draft"
    p2 = plan_service.save_weekly_plan(session, _plan("v2"), "draft")
    assert p2.version == 2
    latest = plan_service.latest_weekly_plan(session, WS)
    assert latest is not None and latest.version == 2
    assert latest.days[0].note == "v2"


def test_confirm_weekly_plan_state_machine(session):
    plan_service.save_weekly_plan(session, _plan(), "draft")
    confirmed = plan_service.confirm_weekly_plan(session, WS, SETTINGS)
    assert confirmed.status == "confirmed"
    assert confirmed.version == 2  # draft(1) → confirmed(2)


def test_confirm_requires_existing_plan(session):
    with pytest.raises(AppError) as ei:
        plan_service.confirm_weekly_plan(session, WS, SETTINGS)
    assert ei.value.code == ErrorCode.NOT_FOUND


# ---------------------------- LLM 生成（打桩） ----------------------------
def test_generate_weekly_plan_marks_night_shift(session, monkeypatch):
    _seed_week(session, WS, 50, 60.0)
    night = WS + dt.timedelta(days=3)

    def fake_chat_json(self, messages, required_keys=()):  # noqa: ANN001
        return {
            "days": [
                {"date": (WS + dt.timedelta(days=i)).isoformat(),
                 "sport": "running", "name": "轻松跑", "duration_min": 30}
                for i in range(7)
            ],
            "rationale": "打桩",
        }

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", fake_chat_json)
    plan = plan_service.generate_weekly_plan(
        session, WS, SETTINGS, night_shift_days=[night]
    )
    assert len(plan.days) == 7
    assert plan.days[3].is_night_shift is True
    assert plan.days[0].is_night_shift is False
    assert all(d.session is not None for d in plan.days)


def test_evaluate_weekly_plan_requires_existing(session):
    with pytest.raises(AppError) as ei:
        plan_service.evaluate_weekly_plan(session, WS, SETTINGS)
    assert ei.value.code == ErrorCode.NOT_FOUND


def test_generate_weekly_plan_injects_season_and_hard_constraint(session, monkeypatch):
    """T2.4/T2.7：季节策略 + 环境硬约束进入生成上下文（雨雪 → 室内，不可被季节覆盖）。"""
    from app.schemas.domain import EnvironmentDaily
    from app.storage.environment_store import upsert_environment

    _seed_week(session, WS, 50, 60.0)
    # 该周首日：降雪（硬性 → 室内）
    upsert_environment(
        session,
        EnvironmentDaily(
            date=WS,
            location_id="101220601",
            location_name="宜秀区",
            temp_c=2.0,
            is_snow=True,
            weather_text="小雪",
            available=True,
        ),
    )

    captured: dict = {}

    def fake_chat_json(self, messages, required_keys=()):  # noqa: ANN001
        captured["prompt"] = messages[-1]["content"]
        return {"days": [], "rationale": "打桩"}

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", fake_chat_json)
    plan_service.generate_weekly_plan(session, WS, SETTINGS)

    prompt = captured["prompt"]
    assert "季节" in prompt
    assert "硬性约束" in prompt
    assert "室内" in prompt  # 雨雪 → 室内


def test_generate_weekly_plan_marks_weather_unavailable_without_fabrication(
    session, monkeypatch
):
    """§13.8：环境数据缺失 → 明确标注，不编造、不误判为硬约束。"""
    _seed_week(session, WS, 50, 60.0)  # 无环境数据落库

    captured: dict = {}

    def fake_chat_json(self, messages, required_keys=()):  # noqa: ANN001
        captured["prompt"] = messages[-1]["content"]
        return {"days": [], "rationale": "打桩"}

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", fake_chat_json)
    plan_service.generate_weekly_plan(session, WS, SETTINGS)
    # 无数据 → 天气不可用提示，但不强制室内
    assert "不可用" in captured["prompt"]


def test_evaluate_weekly_plan_with_llm(session, monkeypatch):
    plan_service.save_weekly_plan(session, _plan(), "draft")

    def fake_chat_json(self, messages, required_keys=()):  # noqa: ANN001
        return {
            "findings": ["周三强度偏高"],
            "suggestions": ["周三改为轻松跑"],
            "conflicts": [],
            "risk_notes": ["关注睡眠不足"],
        }

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", fake_chat_json)
    ev = plan_service.evaluate_weekly_plan(session, WS, SETTINGS)
    assert ev.findings == ["周三强度偏高"]
    assert ev.risk_notes == ["关注睡眠不足"]
    # 评估后状态变为 evaluated，版本自增
    latest = plan_service.latest_weekly_plan(session, WS)
    assert latest is not None and latest.status == "evaluated" and latest.version == 2


# ---------------------------- 周计划强度目标（2026-10-20 决策 Q8）----------------------------
def test_generate_weekly_plan_carries_target_hr_zone(session, monkeypatch):
    """Q8-A：周计划携带强度数据——LLM 输出的 target_hr_zone 落到 session 上。"""
    _seed_week(session, WS, 50, 60.0)

    def fake_chat_json(self, messages, required_keys=()):  # noqa: ANN001
        return {
            "days": [
                {"date": (WS + dt.timedelta(days=i)).isoformat(),
                 "sport": "running", "name": "轻松跑 Z2", "duration_min": 30,
                 "target_hr_zone": "Z2"}
                for i in range(7)
            ],
            "rationale": "打桩",
        }

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", fake_chat_json)
    plan = plan_service.generate_weekly_plan(session, WS, SETTINGS)
    assert all(d.session.target_hr_zone == "Z2" for d in plan.days if d.session)


def test_generate_weekly_plan_rejects_invalid_target_zone(session, monkeypatch):
    """越界区间值（Z9）→ None（不污染判定基准；宁可降级为仅时长判定）。"""
    _seed_week(session, WS, 50, 60.0)

    def fake_chat_json(self, messages, required_keys=()):  # noqa: ANN001
        return {
            "days": [
                {"date": (WS + dt.timedelta(days=i)).isoformat(),
                 "sport": "running", "name": "轻松跑", "duration_min": 30,
                 "target_hr_zone": "Z9"}
                for i in range(7)
            ],
            "rationale": "打桩",
        }

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", fake_chat_json)
    plan = plan_service.generate_weekly_plan(session, WS, SETTINGS)
    assert all(d.session.target_hr_zone is None for d in plan.days if d.session)


def test_generate_weekly_plan_prompt_asks_for_target_zone(session, monkeypatch):
    """生成 prompt 必须要求区间输出（否则 LLM 永远不吐，功能空转）。"""
    _seed_week(session, WS, 50, 60.0)
    captured: dict = {}

    def fake_chat_json(self, messages, required_keys=()):  # noqa: ANN001
        captured["prompt"] = messages[-1]["content"]
        return {"days": [], "rationale": "打桩"}

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", fake_chat_json)
    plan_service.generate_weekly_plan(session, WS, SETTINGS)
    assert "target_hr_zone" in captured["prompt"]
    assert "Karvonen" in captured["prompt"]
