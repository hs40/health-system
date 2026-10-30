"""业务规则测试（TD.1）：源自 REQ-RISK-002/003、REQ-DATA-001/006。

这些断言是需求基线的"可执行规格"——任何一条失败都意味着已确认需求被破坏。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.adapters.garmin import GarminAdapter
from app.config import Settings
from app.db.models import DailyReportRow
from app.domain import rule_registry as rr
from app.domain.quality import grade, may_generate_plan
from app.domain.risk import RiskInput, evaluate
from app.schemas.domain import (
    DailyReport,
    DataConflict,
    DataQualityReport,
    RiskAssessment,
)
from app.schemas.enums import (
    ConflictResolution,
    ConflictSeverity,
    DataState,
    PriorityLevel,
    QualityGrade,
    QualityLevel,
)
from app.services.coach_service import build_today_plan

D = dt.date(2026, 10, 14)
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]


# ============================================================
# REQ-RISK-002：阈值边界（相对近 28 天基线，严格不等号）
# ============================================================
def _inp(**kw) -> RiskInput:
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
    base.update(kw)
    return RiskInput(**base)


@pytest.mark.parametrize(
    ("kw", "rule"),
    [
        # P1 负荷路径：恰为 1.5 不触发（严格大于）
        ({"acwr_ratio": 1.5, "hrv_status": "LOW"}, "P1-LOAD-HRV"),
        # P1 睡眠/电量路径：恰为阈值不触发（严格小于）
        ({"sleep_score": 50, "body_battery_morning": 40}, "P1-SLEEP-BATTERY"),
        # P3：恰为睡眠良好阈值 → 满足条件（≥），不应因此被排除
        ({"sleep_score": 75}, "P2-SLEEP"),
    ],
)
def test_threshold_is_strict_equality_does_not_trigger(kw, rule):
    """恰等于判定阈值不触发（阈值比较为严格不等号）。"""
    a = evaluate(_inp(**kw))
    assert rule not in a.triggered_rules


def test_p1_requires_both_conditions():
    """P1 负荷路径需「负荷过高 ∧ HRV 不平衡」同时成立，缺一不可。"""
    # 负荷高但 HRV 平衡 → 不判 P1，降为 P2 减量
    a = evaluate(_inp(acwr_ratio=1.6, hrv_status="BALANCED"))
    assert "P1-LOAD-HRV" not in a.triggered_rules
    assert a.priority is PriorityLevel.REDUCE

    # HRV 不平衡但负荷不高 → 不判 P1
    b = evaluate(_inp(acwr_ratio=1.0, hrv_status="LOW"))
    assert "P1-LOAD-HRV" not in b.triggered_rules
    assert b.priority is PriorityLevel.PROGRESS


def test_risk_evaluation_carries_rule_version():
    """REQ-RISK-007：规则版本进入评估结果，保证可追溯。"""
    a = evaluate(_inp())
    assert a.rule_version


# ============================================================
# REQ-DATA-006：质量分级与"关键数据不足不得下结论"
# ============================================================
def _conflict(severity: ConflictSeverity) -> DataConflict:
    return DataConflict(
        metric="resting_heart_rate",
        values=[{"source": "garmin", "value": 50}],
        severity=severity,
        resolution=ConflictResolution.FLAGGED,
    )


def test_quality_all_present_is_ok():
    all_metrics = {"resting_heart_rate", "hrv_last_night_avg", "sleep_duration_sec"}
    r = grade(D, QualityLevel.ADJUSTMENT, all_metrics)
    assert r.grade == QualityGrade.OK
    assert may_generate_plan(r) is True


def test_quality_one_missing_is_partial_and_allows_plan():
    r = grade(D, QualityLevel.ADJUSTMENT, {"resting_heart_rate", "hrv_last_night_avg"})
    assert r.grade == QualityGrade.PARTIAL
    assert r.missing == ["sleep_duration_sec"]
    assert may_generate_plan(r) is True


def test_quality_all_missing_is_insufficient_and_blocks_plan():
    r = grade(D, QualityLevel.ADJUSTMENT, set())
    assert r.grade == QualityGrade.INSUFFICIENT
    assert may_generate_plan(r) is False
def test_irrelevant_conflict_does_not_downgrade():
    r = grade(
        D,
        QualityLevel.ADJUSTMENT,
        {"resting_heart_rate", "hrv_last_night_avg", "sleep_duration_sec"},
        conflicts=[_conflict(ConflictSeverity.IRRELEVANT)],
    )
    assert r.grade == QualityGrade.OK


# ============================================================
# REQ-DATA-001：原始数据只读保护
# ============================================================
def test_raw_data_is_append_only(tmp_path):
    """首次写入即权威快照，后续写入不覆盖（REQ-DATA-001）。"""
    adapter = GarminAdapter(Settings(_env_file=None, raw_data_dir=tmp_path))  # type: ignore[call-arg]
    day = dt.date(2026, 10, 14)

    assert adapter._write_raw("daily_summary", day, {"v": 1}) is True
    assert adapter._write_raw("daily_summary", day, {"v": 2}) is False  # 不覆盖
    assert adapter.read_raw("daily_summary", day) == {"v": 1}


# ============================================================
# REQ-RISK-003：高风险 / 关键数据不足 → 不生成当天计划
# ============================================================
def _persist_report(session: Session, report: DailyReport) -> None:
    session.add(
        DailyReportRow(date=report.date, payload=report.model_dump(mode="json"), version=1)
    )
    session.commit()


def _report(
    *,
    priority: PriorityLevel,
    rules: list[str],
    grade_value: QualityGrade,
    missing: list[str],
) -> DailyReport:
    quality = DataQualityReport(
        date=D,
        level=QualityLevel.ADJUSTMENT,
        missing=missing,
        grade=grade_value,
    )
    risk = RiskAssessment(
        date=D,
        priority=priority,
        data_state=DataState.FULL,
        triggered_rules=rules,
        evidence={},
        missing_inputs=missing,
        rule_version=rr.RULE_VERSION,
        decided_at=dt.datetime.now(),
    )
    return DailyReport(date=D, quality=quality, risk=risk, version=1)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _forbid_llm(monkeypatch):
    def _boom(*_a, **_kw):  # noqa: ANN002, ANN003
        raise AssertionError("被拦截时不应调用 LLM")

    monkeypatch.setattr("app.adapters.llm.LlmAdapter.chat_json", _boom)


def test_p1_blocks_regular_plan_without_calling_llm(session, monkeypatch):
    """REQ-RISK-003 / Q6：P1 → 不出常规计划、不调 LLM，只出 15–20min Z1 恢复日模板。"""
    _forbid_llm(monkeypatch)
    _persist_report(
        session,
        _report(
            priority=PriorityLevel.REST,
            rules=["P1-LOAD-HRV"],
            grade_value=QualityGrade.OK,
            missing=[],
        ),
    )
    plan = build_today_plan(session, D, SETTINGS)
    assert plan is not None
    assert plan.upload_allowed is False
    assert 15 <= plan.duration_min <= 20


def test_p2_allows_generate_but_flags_review(session, monkeypatch):
    """Q26：P2 减量允许生成计划，但 **必须标记为不可上传**（只拦上传）。"""
    _persist_report(
        session,
        _report(
            priority=PriorityLevel.REDUCE,
            rules=["P2-LOAD"],
            grade_value=QualityGrade.OK,
            missing=[],
        ),
    )
    # 不禁止 LLM：P2 需要正常出计划
    monkeypatch.setattr(
        "app.adapters.llm.LlmAdapter.chat_json",
        lambda *_a, **_kw: {
            "sport": "running",
            "name": "减量课",
            "duration_min": 30,
            "target_hr_zone": "Z2",
            "rationale": "P2 减量：容量减半",
        },
    )
    plan = build_today_plan(session, D, SETTINGS)
    assert plan is not None
    # Q26：P2 只拦上传 —— 计划必须落地为「不可上传」，否则 upload_session 守卫形同虚设
    assert plan.upload_allowed is False


def test_p4_plan_is_uploadable(session, monkeypatch):
    """P4 维持：不拦生成、不拦上传（对照组）。"""
    _persist_report(
        session,
        _report(
            priority=PriorityLevel.MAINTAIN,
            rules=[],
            grade_value=QualityGrade.OK,
            missing=[],
        ),
    )
    monkeypatch.setattr(
        "app.adapters.llm.LlmAdapter.chat_json",
        lambda *_a, **_kw: {
            "sport": "running",
            "name": "常规课",
            "duration_min": 40,
            "target_hr_zone": "Z2",
            "rationale": "P4 维持原计划",
        },
    )
    plan = build_today_plan(session, D, SETTINGS)
    assert plan is not None
    assert plan.upload_allowed is True


def test_insufficient_quality_blocks_plan_without_calling_llm(session, monkeypatch):
    """REQ-DATA-006：关键安全数据不足 → 不生成计划（即便优先级非 P1）。"""
    _forbid_llm(monkeypatch)
    _persist_report(
        session,
        _report(
            priority=PriorityLevel.MAINTAIN,
            rules=[],
            grade_value=QualityGrade.INSUFFICIENT,
            missing=["resting_heart_rate", "hrv_last_night_avg", "sleep_duration_sec"],
        ),
    )
    assert build_today_plan(session, D, SETTINGS) is None


def test_generate_daily_missing_data_falls_to_safety_fallback(session):
    """端到端：无任何标准化数据 → ACWR 不可用 → C 态兜底，不拦计划。

    旧 R-007「数据缺失即硬拦截」已退役（Q1/Q8）：数据缺失改由三态关卡表达，
    无负荷数据时按原计划执行，而不是拒绝生成（Q2）。
    """
    from app.services.report_service import generate_daily

    report = generate_daily(session, D, SETTINGS)
    assert report.risk.data_state is DataState.FALLBACK
    assert "acwr_ratio" in report.risk.missing_inputs
    assert report.risk.priority is PriorityLevel.MAINTAIN
    assert report.quality.grade == QualityGrade.INSUFFICIENT


def test_generate_daily_ignores_rule_version_gate(session):
    """E26：规则集不再受 rule_version 表约束，日报不再出现闸门提示。

    历史背景：T1.3 曾要求「未验证版本 → 保守分支」，但该表全链路无写入代码，
    闸门恒为 NOT_VERIFIED，且结果只写进 limitations 文案、不回流判定（空壳）。
    用户 2026-10-17 决定去除该限制：风险判定只取决于数据本身。

    2026-10-25：``rule_version`` 表本身已删除（死表，与 G-17 同型）——
    「无表即无闸门」由 schema 保证；本用例锁定「不出现闸门文案 + 版本号仍记录」。
    """
    from app.services.report_service import generate_daily

    report = generate_daily(session, D, SETTINGS)
    assert "规则版本闸门" not in "；".join(report.limitations)
    # 版本号仍随风险结论记录（仅追溯用途，不参与判定）
    assert report.risk.rule_version == SETTINGS.risk_rule_version


def test_p2_plan_upload_is_blocked_at_upload_gate(session, monkeypatch):
    """Q26 端到端：P2 计划进入上传门禁 → 返回 blocked，绝不调 Garmin。"""
    from app.services.plan_service import upload_session

    _persist_report(
        session,
        _report(
            priority=PriorityLevel.REDUCE,
            rules=["P2-LOAD"],
            grade_value=QualityGrade.OK,
            missing=[],
        ),
    )
    monkeypatch.setattr(
        "app.adapters.llm.LlmAdapter.chat_json",
        lambda *_a, **_kw: {
            "sport": "running",
            "name": "减量课",
            "duration_min": 30,
            "target_hr_zone": "Z2",
            "rationale": "P2 减量",
        },
    )
    plan = build_today_plan(session, D, SETTINGS)
    assert plan is not None

    def _no_garmin(*_a, **_kw):  # noqa: ANN002, ANN003
        raise AssertionError("P2 不应到达 Garmin 上传")

    monkeypatch.setattr(GarminAdapter, "upload_workout", _no_garmin)
    result = upload_session(session, plan.id, SETTINGS, schedule=False)
    assert result.status == "blocked"
    assert result.garmin_workout_id is None


def test_upload_gate_blocks_before_idempotency_check(session, monkeypatch):
    """Q26：`upload_allowed=False` 的会话即使残留 garmin_workout_id，也必须先被拦下。

    回归护栏：守卫必须排在幂等分支之前，否则残留 id 会让「已上传」直接短路返回。
    """
    from app.services.plan_service import upload_session

    _persist_report(
        session,
        _report(
            priority=PriorityLevel.REDUCE,
            rules=["P2-LOAD"],
            grade_value=QualityGrade.OK,
            missing=[],
        ),
    )
    monkeypatch.setattr(
        "app.adapters.llm.LlmAdapter.chat_json",
        lambda *_a, **_kw: {
            "sport": "running",
            "name": "减量课",
            "duration_min": 30,
            "rationale": "P2 减量",
        },
    )
    plan = build_today_plan(session, D, SETTINGS)
    assert plan is not None

    from app.db.models import TrainingSessionRow

    row = session.get(TrainingSessionRow, plan.id)
    row.garmin_workout_id = 999999  # 模拟残留 id
    session.add(row)
    session.commit()

    monkeypatch.setattr(
        GarminAdapter,
        "upload_workout",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("不应调用 Garmin")),
    )
    result = upload_session(session, plan.id, SETTINGS, schedule=False)
    assert result.status == "blocked"


def test_daily_report_recovery_exposes_true_morning_battery(session):
    """日报 `recovery` 必须给出**醒时电量**，与「同步时刻残值」分列（2026-10-22 修正）。

    傍晚生成日报时 MostRecent 已是残值（实测中位数 5），前端若拿它当「晨起电量」，
    卡片会长期显示个位数 —— 这就是本次缺陷的表现。
    """
    from app.services.coach_service import _report_payload

    report = DailyReport(
        date=D,
        version=1,
        quality=DataQualityReport(date=D, level=QualityLevel.ADJUSTMENT, grade=QualityGrade.OK),
        recovery={
            "resting_heart_rate": 51,
            "hrv_last_night_avg": 69.0,
            "body_battery_wake": 38,
            "body_battery_morning": 76,
            "sleep_score": 77,
        },
    )

    payload = _report_payload(report)

    assert payload["body_battery_morning"] == 76
    assert payload["sleep_score"] == 77
    # 残值**不得**发给模型：否则模型会把傍晚残值当成「晨起电量」
    assert "body_battery_wake" not in payload
