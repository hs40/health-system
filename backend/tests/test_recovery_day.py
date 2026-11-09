"""P1 强制休息 → 15–20min Z1 恢复日模板、仅本地、禁止上传（Q6 / Q26）。"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.db.models import DailyReportRow
from app.domain import rule_registry as rr
from app.schemas.domain import (
    DailyReport,
    DataQualityReport,
    RiskAssessment,
    TrainingSession,
    WeekPlanDay,
)
from app.schemas.enums import (
    DataState,
    PriorityLevel,
    QualityGrade,
    QualityLevel,
    SessionStatus,
    Sport,
)

D = dt.date(2026, 10, 6)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _settings() -> Settings:
    return Settings(_env_file=None)


def _report(*, rest: bool) -> DailyReport:
    risk = RiskAssessment(
        date=D,
        priority=PriorityLevel.REST if rest else PriorityLevel.MAINTAIN,
        data_state=DataState.FULL,
        triggered_rules=["P1-LOAD-HRV"] if rest else [],
        evidence={},
        missing_inputs=[],
        rule_version=rr.RULE_VERSION,
        decided_at=dt.datetime.now(),
    )
    quality = DataQualityReport(
        date=D,
        level=QualityLevel.ADJUSTMENT,
        missing=[],
        conflicts=[],
        grade=QualityGrade.OK,
        notes=[],
    )
    return DailyReport(date=D, quality=quality, risk=risk, version=1)


def _seed_report(session: Session, *, rest: bool) -> None:
    rep = _report(rest=rest)
    session.add(
        DailyReportRow(
            date=D,
            payload=rep.model_dump(mode="json"),
            version=rep.version,
        )
    )
    session.commit()


def _forbid_llm(monkeypatch):
    def _boom(*_a, **_kw):  # noqa: ANN002, ANN003
        raise AssertionError("恢复日不应调用 LLM（固定模板）")

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", _boom)


def test_recovery_day_builds_fixed_template_without_llm(session, monkeypatch):
    from app.services.coach_service import build_today_plan

    _seed_report(session, rest=True)
    _forbid_llm(monkeypatch)

    ts = build_today_plan(session, D, _settings())

    assert ts is not None
    assert ts.sport == Sport.INDOOR_CARDIO
    assert ts.upload_allowed is False
    # Q6：15–20min Z1（不再是旧的 28min）
    assert rr.RECOVERY_TEMPLATE_MIN_SEC // 60 <= ts.duration_min <= (
        rr.RECOVERY_TEMPLATE_MAX_SEC // 60
    )
    assert "恢复" in ts.name
    assert ts.rationale == rr.RECOVERY_TEMPLATE_NOTE
    # 低强度：无力量动作、主阶段只有 Z1。
    # hr_zone 契约是数字 1-5（target_hr_zone 才是 "Z1"）——v3.4.7 修正。
    from app.domain.plan_rules import zone_int

    assert ts.strength is None
    assert all(
        x.get("hr_zone") in (None, zone_int(rr.RECOVERY_TEMPLATE_ZONE))
        for x in ts.main
    )


def test_normal_day_still_goes_through_llm_path(session, monkeypatch):
    """对照：非 P1 日不受影响（仍走 LLM 路径，未被恢复日分支劫持）。"""
    from app.services import coach_service

    _seed_report(session, rest=False)
    called = {"n": 0}

    def _fake(*_a, **_kw):  # noqa: ANN002, ANN003
        called["n"] += 1
        return {"sport": None, "name": "x", "duration_min": 0, "rationale": "模型判定休息"}

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", _fake)
    coach_service.build_today_plan(session, D, _settings())
    assert called["n"] == 1


def test_upload_blocked_for_recovery_session(session):
    from app.services.plan_service import upload_session

    ts = TrainingSession(
        id=f"session-{D.isoformat()}",
        date=D,
        sport=Sport.INDOOR_CARDIO,
        name="恢复日：15–20min 主动恢复",
        duration_min=rr.RECOVERY_TEMPLATE_MAX_SEC // 60,
        status=SessionStatus.DRAFT,
        upload_allowed=False,
    )
    from app.db.models import TrainingSessionRow

    session.add(
        TrainingSessionRow(
            id=ts.id,
            date=ts.date,
            sport=ts.sport.value,
            name=ts.name,
            status=ts.status.value,
            payload=ts.model_dump(mode="json"),
        )
    )
    session.commit()

    res = upload_session(session, ts.id, _settings())
    assert res.status == "blocked"
    assert "不上传" in (res.message or "")


def test_p1_allows_recovery_day():
    """P1 → 允许恢复日模板（Q6）；P1 之外的优先级不产生恢复日。"""
    from app.domain.risk import RiskInput, allows_recovery_day, evaluate

    base = dict(
        on_date=D,
        acwr_ratio=1.0,
        hrv_status="BALANCED",
        hrv_last_night_avg=60.0,
        hrv_baseline=60.0,
        hrv_balanced_streak=3,
        resting_heart_rate=52,
        rhr_baseline=52.0,
        sleep_score=80,
        body_battery_morning=80,
    )
    p1 = evaluate(RiskInput(**{**base, "acwr_ratio": 1.6, "hrv_status": "LOW"}))
    assert p1.priority is PriorityLevel.REST
    assert allows_recovery_day(p1) is True

    p2 = evaluate(RiskInput(**{**base, "acwr_ratio": 1.35}))
    assert p2.priority is PriorityLevel.REDUCE
    assert allows_recovery_day(p2) is False


# ---------------------------- 当日计划继承周计划强度目标（Q8-A 链路）----------------------------
def _report_with_week_plan(zone: str | None) -> DailyReport:
    report = _report(rest=False)
    # 用模型而非裸 dict：字段声明是 list[WeekPlanDay]，塞 dict 能过（pydantic
    # 赋值不校验），但后续 model_dump 会报 PydanticSerializationUnexpectedValue。
    report.week_plan = [
        WeekPlanDay(
            date=D,
            weekday=D.weekday(),
            session=TrainingSession(
                id=f"wp-{D.isoformat()}",
                date=D,
                sport=Sport.RUNNING,
                name="1006-有氧轻松跑 Z2 30min",
                target_hr_zone=zone,
                duration_min=30,
                status="confirmed",
            ),
            is_night_shift=False,
            note=None,
        )
    ]
    return report


def _stub_llm(monkeypatch, zone: object = "__缺失__"):
    def fake_chat_json(self, messages, model=None, required_keys=()):  # noqa: ANN001
        data = {
            "sport": "running",
            "name": "1006-有氧轻松跑 25min",
            "duration_min": 25,
            "rationale": "HRV 偏低，减量",
        }
        if zone != "__缺失__":
            data["target_hr_zone"] = zone
        return data

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", fake_chat_json)


def test_today_plan_inherits_zone_from_week_plan(session, monkeypatch):
    """LLM 未给区间 → 从周计划当天安排继承（否则调整日判定没有强度基准）。"""
    _seed_report(session, rest=False)
    row = session.exec(
        __import__("sqlmodel").select(DailyReportRow).where(DailyReportRow.date == D)
    ).first()
    row.payload = _report_with_week_plan("Z2").model_dump(mode="json")
    session.add(row)
    session.commit()
    _stub_llm(monkeypatch)

    from app.services.coach_service import build_today_plan

    ts = build_today_plan(session, D, _settings())
    assert ts is not None
    assert ts.target_hr_zone == "Z2"


def test_today_plan_prefers_llm_zone_over_inherited(session, monkeypatch):
    """LLM 给了区间 → 以模型输出为准（继承只是兜底）。"""
    _seed_report(session, rest=False)
    row = session.exec(
        __import__("sqlmodel").select(DailyReportRow).where(DailyReportRow.date == D)
    ).first()
    row.payload = _report_with_week_plan("Z2").model_dump(mode="json")
    session.add(row)
    session.commit()
    _stub_llm(monkeypatch, zone="Z3")

    from app.services.coach_service import build_today_plan

    ts = build_today_plan(session, D, _settings())
    assert ts is not None
    assert ts.target_hr_zone == "Z3"


def test_today_plan_zone_none_when_neither_source_has_it(session, monkeypatch):
    """两边都没有 → None（历史/异常情况下降级为仅时长判定，并披露）。"""
    _seed_report(session, rest=False)
    _stub_llm(monkeypatch)

    from app.services.coach_service import build_today_plan

    ts = build_today_plan(session, D, _settings())
    assert ts is not None
    assert ts.target_hr_zone is None
