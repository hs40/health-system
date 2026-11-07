"""日流水线下游步骤失败 → 结构化登记到 Fault 表（ARCHITECTURE_REVIEW A.4.2）。

背景：``_run_downstream`` 对 analysis / plan / dingtalk / upload 一律用
``except Exception`` 只写进 ``out["steps"]`` 字符串。字符串留在内存返回值和日志里，
运维台查不到、复盘要翻日志、告警也不结构化。系统已有 ``fault_recovery_task``
表与 ``recovery_service.report_fault()``，只是没接线。

契约：
1. 任一步失败仍**不阻断**后续步骤（既有语义不变）；
2. 失败同时写入 Fault 表，可经 ``/api/ops/faults`` 查询；
3. 步骤失败默认按「一般技术故障」登记（``generalTech``）——它不该每天
   制造一堆需要人工确认的重大故障条目；
4. 登记的 ``impact_scope`` 能定位到「哪一天的哪一步」。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import FaultRecoveryTaskRow
from app.orchestrator.scheduler import _run_downstream
from app.schemas.domain import RiskAssessment, StandardDaily
from app.schemas.enums import DataState, PriorityLevel


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class _FakeSync:
    errors = {}


class _FakeSettings:
    pass


def _fake_report(*_args, **_kwargs):
    """最小日报：有 risk，无其它依赖。"""
    risk = RiskAssessment(
        date=dt.date(2026, 11, 7),
        priority=PriorityLevel.PROGRESS,
        data_state=DataState.LOAD_ONLY,
        triggered_rules=[],
        reasons=[],
        rule_version="test",
        decided_at=dt.datetime(2026, 11, 7, 7, 30),
    )
    daily = StandardDaily(date=dt.date(2026, 11, 7))
    return type("_R", (), {"risk": risk, "daily": daily})()


def _patch_downstream(monkeypatch, *, analysis=None, plan=None, dingtalk=None, upload=None):
    """替换 _run_downstream 内部的四个下游服务入口。

    scheduler 已在模块顶层导入这些符号（ARCHITECTURE_REVIEW A.2.2），
    因此必须替换 ``scheduler`` 命名空间上的绑定，而不是源模块上的同名属性——
    替换源模块对已绑定的函数引用无效。
    """
    import app.orchestrator.scheduler as sched

    monkeypatch.setattr(sched, "generate_report", _fake_report, raising=False)
    monkeypatch.setattr(
        sched, "analyze_daily", analysis or (lambda *a, **k: {"summary": "ok"})
    )
    monkeypatch.setattr(sched, "build_today_plan", plan or (lambda *a, **k: None))
    monkeypatch.setattr(
        sched, "push_daily_report", dingtalk or (lambda *a, **k: "skipped")
    )
    monkeypatch.setattr(sched, "upload_session", upload or (lambda *a, **k: None))
    return sched


def _faults(session: Session) -> list[FaultRecoveryTaskRow]:
    return list(session.exec(select(FaultRecoveryTaskRow)).all())


DAY = dt.date(2026, 11, 7)


def test_analysis_failure_is_recorded_as_fault(session, monkeypatch):
    """LLM 分析失败：仍继续后续步骤，同时登记一条 Fault。"""

    def _boom(*_a, **_k):
        raise RuntimeError("llm 502")

    _patch_downstream(monkeypatch, analysis=_boom)
    out: dict = {"date": DAY.isoformat(), "steps": {}}

    _run_downstream(session, DAY, _FakeSettings(), out, _FakeSync())

    # 不阻断语义保留
    assert out["steps"]["analysis"].startswith("failed:")
    assert "dingtalk" in out["steps"]

    # 结构化登记
    faults = _faults(session)
    assert len(faults) == 1
    fault = faults[0]
    assert fault.severity == "general"
    assert fault.needs_user_confirm is False
    assert DAY.isoformat() in fault.impact_scope
    assert "analysis" in fault.impact_scope
    assert "RuntimeError" in fault.fix_measures
    assert "llm 502" in fault.fix_measures


def test_analysis_failure_does_not_block_plan(session, monkeypatch):
    """分析失败时计划仍照常生成（失败隔离）。"""
    called: list[str] = []

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    def _plan(*_a, **_k):
        called.append("plan")
        return None

    _patch_downstream(monkeypatch, analysis=_boom, plan=_plan)
    out: dict = {"date": DAY.isoformat(), "steps": {}}
    _run_downstream(session, DAY, _FakeSettings(), out, _FakeSync())

    assert called == ["plan"]


def test_dingtalk_failure_is_recorded(session, monkeypatch):
    """钉钉推送失败同样登记。"""

    def _boom(*_a, **_k):
        raise ConnectionError("webhook timeout")

    _patch_downstream(monkeypatch, dingtalk=_boom)
    out: dict = {"date": DAY.isoformat(), "steps": {}}
    _run_downstream(session, DAY, _FakeSettings(), out, _FakeSync())

    faults = _faults(session)
    assert len(faults) == 1
    assert "dingtalk" in faults[0].impact_scope
    assert "ConnectionError" in faults[0].fix_measures


def test_upload_failure_is_recorded(session, monkeypatch):
    """上传课程库失败登记（此时计划存在）。"""

    def _plan(*_a, **_k):
        return type("_TS", (), {"id": "s-1"})()

    def _boom(*_a, **_k):
        raise RuntimeError("tredict 500")

    _patch_downstream(monkeypatch, plan=_plan, upload=_boom)
    out: dict = {"date": DAY.isoformat(), "steps": {}}
    _run_downstream(session, DAY, _FakeSettings(), out, _FakeSync())

    faults = _faults(session)
    assert len(faults) == 1
    assert "upload" in faults[0].impact_scope
    assert faults[0].severity == "general"


def test_multiple_failures_each_get_own_fault(session, monkeypatch):
    """多个步骤同时失败 → 各自一条记录，不合并掩盖。"""

    def _boom(*_a, **_k):
        raise RuntimeError("x")

    _patch_downstream(monkeypatch, analysis=_boom, dingtalk=_boom)
    out: dict = {"date": DAY.isoformat(), "steps": {}}
    _run_downstream(session, DAY, _FakeSettings(), out, _FakeSync())

    scopes = sorted(f.impact_scope for f in _faults(session))
    assert len(scopes) == 2
    assert any("analysis" in s for s in scopes)
    assert any("dingtalk" in s for s in scopes)


def test_fault_recording_failure_does_not_break_pipeline(session, monkeypatch):
    """Fault 表本身写失败时不得让日任务崩掉（可观测性不能成为新的故障源）。"""
    import app.orchestrator.scheduler as sched

    def _db_down(*_a, **_k):
        raise RuntimeError("fault table unavailable")

    def _boom(*_a, **_k):
        raise RuntimeError("llm 502")

    _patch_downstream(monkeypatch, analysis=_boom)
    monkeypatch.setattr(sched, "report_fault", _db_down)

    out: dict = {"date": DAY.isoformat(), "steps": {}}
    _run_downstream(session, DAY, _FakeSettings(), out, _FakeSync())

    assert out["steps"]["analysis"].startswith("failed:")
    assert "dingtalk" in out["steps"]


def test_no_fault_recorded_on_full_success(session, monkeypatch):
    """全部成功 → 一条 Fault 都不产生（避免噪声污染运维台）。"""
    _patch_downstream(monkeypatch)
    out: dict = {"date": DAY.isoformat(), "steps": {}}
    _run_downstream(session, DAY, _FakeSettings(), out, _FakeSync())

    assert _faults(session) == []


def test_recorded_fault_is_queryable_via_ops_api(session, monkeypatch):
    """登记的故障能经 ``GET /api/ops/faults`` 的同一查询路径取到。

    这是接线的真正价值：日任务失败后，运维台不用翻日志就能看到条目。
    """

    def _boom(*_a, **_k):
        raise RuntimeError("llm 502")

    _patch_downstream(monkeypatch, analysis=_boom)
    out: dict = {"date": DAY.isoformat(), "steps": {}}
    _run_downstream(session, DAY, _FakeSettings(), out, _FakeSync())

    from app.services import recovery_service

    faults = recovery_service.list_faults(session)
    assert len(faults) == 1
    assert faults[0].severity == "general"
    assert faults[0].impact_scope == f"analysis@{DAY.isoformat()}"
    assert faults[0].final_result == "reported"
