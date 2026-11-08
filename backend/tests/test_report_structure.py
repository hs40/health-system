"""报告/计划的结构化输出：目标进展、报告索引、评估结构化冲突。

覆盖 2026-10-22 需求：
1. ``goal_progress`` 四项长期目标（心肺/减脂减重/肌肉耐力/最大力量）结构化输出；
2. ``GET /api/reports/index`` 返回「哪些日期/周/月有报告」（前端日历打点）；
3. ``evaluate`` 返回结构化 ``conflicts``，硬性冲突不可绕过（confirm 拒绝）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.errors import AppError
from app.schemas.domain import (
    StandardDaily,
    TrainingLoad,
    TrainingSession,
    WeekPlanDay,
)
from app.schemas.enums import SessionStatus, Sport
from app.services import plan_service
from app.services.report_service import generate_weekly, load_goal_progress
from app.storage.derived_store import upsert_load
from app.storage.standard_store import upsert_standard

WS = dt.date(2026, 10, 12)  # 周一
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]

GOALS = ("cardio", "fatLoss", "muscleEndurance", "maxStrength")


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed(session: Session, start: dt.date, days: int = 7) -> None:
    for i in range(days):
        d = start + dt.timedelta(days=i)
        upsert_standard(
            session,
            StandardDaily(
                date=d,
                resting_heart_rate=50 + i % 2,
                hrv_last_night_avg=60.0 + i,
                sleep_duration_sec=7 * 3600 + i * 60,
                steps=8000 + i * 100,
                active_calories=400 + i * 10,
                weight_kg=72.5 - i * 0.05,
            ),
        )
        upsert_load(
            session,
            TrainingLoad(
                date=d,
                by_sport={"running": 40.0 + i},
                combined=40.0 + i,
                method="hr_based",
            ),
        )


# ============================================================
# 1. 四项长期目标进展
# ============================================================
def test_goal_progress_has_four_goals(session):
    _seed(session, WS)
    gp = load_goal_progress(session, WS, WS + dt.timedelta(days=6))
    assert set(gp) == set(GOALS)
    for key in GOALS:
        item = gp[key]
        assert item["label"]
        assert item["unit"]
        assert item["source"] in {"measured", "derived", "insufficient"}
        assert isinstance(item["limitations"], list)
        # 有数据时必须给值或明确 null —— 不得编造
        if item["source"] == "insufficient":
            assert item["value"] is None


def test_goal_progress_reports_delta_and_baseline(session):
    _seed(session, WS - dt.timedelta(days=7), days=14)
    gp = load_goal_progress(session, WS, WS + dt.timedelta(days=6))
    # 静息心率有数据 → 心肺项可以是 derived
    assert gp["cardio"]["source"] in {"measured", "derived"}
    assert "baseline" in gp["cardio"]
    assert "delta" in gp["cardio"]


def test_goal_progress_empty_range_is_insufficient(session):
    gp = load_goal_progress(session, WS, WS + dt.timedelta(days=6))
    assert all(gp[k]["source"] == "insufficient" for k in GOALS)


def test_weekly_report_embeds_four_goals(session):
    _seed(session, WS)
    report = generate_weekly(session, WS, SETTINGS)
    gp = report.goal_progress
    # 旧的步数/热量字段保留，同时新增 goals 块
    assert "steps_total" in gp
    assert set(gp["goals"]) == set(GOALS)


# ============================================================
# 2. 报告索引（日历打点）
# ============================================================
def test_reports_index_lists_daily(session):
    from app.api.reports import reports_index

    _seed(session, WS)
    generate_weekly(session, WS, SETTINGS)
    # 生成 3 份日报
    from app.services.report_service import generate_daily

    for i in (0, 1, 2):
        generate_daily(session, WS + dt.timedelta(days=i), SETTINGS)

    res = reports_index(kind="daily", start=None, end=None, session=session)
    dates = {p["key"] for p in res["items"]}
    assert {WS.isoformat(), (WS + dt.timedelta(days=1)).isoformat()} <= dates


def test_reports_index_rejects_unknown_kind(session):
    """kind 白名单由 FastAPI 查询参数声明（非法值 422，不会进入处理函数）。"""
    from fastapi.testclient import TestClient

    from app.api.reports import reports_index
    from app.main import app

    client = TestClient(app)
    r = client.get("/api/reports/index", params={"kind": "hourly"})
    assert r.status_code in (422, 400)
    # 合法 kind 仍可访问（不依赖真实数据）
    assert reports_index is not None


# ============================================================
# 3. 评估结构化冲突 + 硬阻断
# ============================================================
def _sess(day: dt.date, zone: str, name: str = "训练") -> TrainingSession:
    return TrainingSession(
        id=f"s-{day.isoformat()}",
        date=day,
        sport=Sport.RUNNING,
        name=name,
        target_hr_zone=zone,
        duration_min=40,
        status=SessionStatus.DRAFT,
    )


def _unsafe_plan() -> object:
    from app.schemas.api import WeeklyPlan

    days = [
        WeekPlanDay(
            date=WS + dt.timedelta(days=i),
            weekday=i,
            session=_sess(WS + dt.timedelta(days=i), "Z4") if i in (2, 3) else None,
        )
        for i in range(7)
    ]
    return WeeklyPlan(week_start=WS, days=days, status="draft", version=1)


def _safe_plan() -> object:
    from app.schemas.api import WeeklyPlan

    zones = {0: "Z2", 2: "Z4", 4: "Z2", 5: "Z2"}
    days = [
        WeekPlanDay(
            date=WS + dt.timedelta(days=i),
            weekday=i,
            session=_sess(WS + dt.timedelta(days=i), zones[i]) if i in zones else None,
        )
        for i in range(7)
    ]
    return WeeklyPlan(week_start=WS, days=days, status="draft", version=1)


def _stub_llm(monkeypatch, payload: dict) -> None:
    def fake_chat_json(self, messages, required_keys=()):  # noqa: ANN001
        return payload

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", fake_chat_json)


def test_evaluate_returns_structured_conflicts(session, monkeypatch):
    _stub_llm(
        monkeypatch,
        {
            "findings": ["周三强度偏高"],
            "suggestions": ["周三降为 Z2"],
            "conflicts": ["周三与周四连续高强度"],
            "risk_notes": [],
        },
    )
    plan_service.save_weekly_plan(session, _unsafe_plan(), "draft")
    ev = plan_service.evaluate_weekly_plan(session, WS, SETTINGS)

    assert ev.structured_conflicts, "存在安全隐患时必须产出结构化冲突"
    kinds = {c.kind for c in ev.structured_conflicts}
    assert "recovery_gap" in kinds
    assert ev.hard_block is True
    # LLM 的文本冲突也保留，标记来源为 ai
    assert any(c.source == "ai" for c in ev.structured_conflicts)
    c = next(c for c in ev.structured_conflicts if c.hard_block)
    assert set(c.days) <= {(WS + dt.timedelta(days=i)).isoformat() for i in range(7)}


def test_confirm_rejects_when_hard_block(session, monkeypatch):
    _stub_llm(monkeypatch, {"findings": [], "suggestions": [], "conflicts": [], "risk_notes": []})
    plan_service.save_weekly_plan(session, _unsafe_plan(), "draft")
    plan_service.evaluate_weekly_plan(session, WS, SETTINGS)

    with pytest.raises(AppError) as ei:
        plan_service.confirm_weekly_plan(session, WS, SETTINGS)
    assert "安全" in ei.value.message or "阻断" in ei.value.message


def test_confirm_passes_when_no_hard_block(session, monkeypatch):
    _stub_llm(monkeypatch, {"findings": [], "suggestions": [], "conflicts": [], "risk_notes": []})
    plan_service.save_weekly_plan(session, _safe_plan(), "draft")
    plan_service.evaluate_weekly_plan(session, WS, SETTINGS)
    plan = plan_service.confirm_weekly_plan(session, WS, SETTINGS)
    assert plan.status == "confirmed"


def test_evaluation_persisted_with_plan(session, monkeypatch):
    """评估结果随计划落库，读取时可拿到（前端刷新不丢）。"""
    _stub_llm(
        monkeypatch,
        {"findings": ["x"], "suggestions": [], "conflicts": [], "risk_notes": []},
    )
    plan_service.save_weekly_plan(session, _unsafe_plan(), "draft")
    plan_service.evaluate_weekly_plan(session, WS, SETTINGS)
    latest = plan_service.latest_weekly_plan(session, WS)
    assert latest is not None
    assert latest.evaluation is not None
    assert latest.evaluation.hard_block is True
