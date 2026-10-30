"""LLM 适配器与教练服务单元测试（TA.10，不依赖网络）。"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, SQLModel, create_engine

from app.adapters.llm import _parse_json
from app.config import Settings
from app.db.models import DailyReportRow
from app.domain.quality import grade
from app.domain.risk import RiskInput, evaluate
from app.schemas.domain import DailyReport
from app.schemas.enums import PriorityLevel, QualityLevel
from app.services.coach_service import build_today_plan

D = dt.date(2026, 10, 14)


# ---------------------------- JSON 解析 ----------------------------
def test_parse_json_plain():
    assert _parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_fenced():
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_embedded():
    assert _parse_json('前言 {"a": 1} 后记') == {"a": 1}


def test_parse_json_invalid_returns_none():
    assert _parse_json("not json at all") is None
    assert _parse_json("[1,2,3]") is None  # 非对象


# ---------------------------- 风险拦截 ----------------------------
def _p1_report() -> DailyReport:
    quality = grade(D, QualityLevel.ADJUSTMENT, {"resting_heart_rate"})
    # P1（负荷过高 ∧ HRV 不平衡）→ 强制休息，出 15–20min Z1 模板、不调 LLM
    risk = evaluate(RiskInput(on_date=D, acwr_ratio=1.6, hrv_status="LOW"))
    return DailyReport(date=D, quality=quality, risk=risk, version=1)


def test_build_plan_p1_uses_template_without_llm():
    """P1 → 不调 LLM，直接出恢复日模板（REQ-RISK-002/003 + Q6）。"""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    rep = _p1_report()
    assert rep.risk.priority is PriorityLevel.REST

    with Session(engine) as s:
        s.add(DailyReportRow(date=D, payload=rep.model_dump(mode="json"), version=1))
        s.commit()
        # 未配置 API Key：若走到 LLM 会抛错；能返回模板即证明未调 LLM
        ts = build_today_plan(s, D, Settings(llm_api_key=None))
        assert ts is not None
        assert ts.upload_allowed is False
        assert 15 <= ts.duration_min <= 20
