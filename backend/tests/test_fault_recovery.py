"""故障恢复测试（阶段③ T3.9；验收 §19.6，共 10 项）。

覆盖：
1. 故障自动分级（一般技术故障 / 重大故障）
2. 一般技术故障修复并通过校验后可自动恢复
3. 未确认的重大故障 → 禁止恢复优化
4. 恢复前校验未通过 → 不得恢复
5. 恢复前校验通过 → 允许用户确认
6. 用户确认后可恢复优化
7. 恢复流程记录完整（时间/措施/结果）
8. 恢复后重新执行完整运行校验（复用校验器）
9. 恢复失败保留暂停状态并记录残留风险
10. 已读 ≠ 同意：仅读取/标记已读不触发恢复
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
from app.schemas.enums import ErrorCode
from app.services import recovery_service, strategy_service


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


def _seed_active_strategy(session: Session, settings: Settings, *, n: int = 30) -> None:
    """造历史 + 初始 active 策略（恢复前校验需要有效 active + 依赖指标）。"""
    session.add(
        BaselineRow(
            metric="hrv_last_night_avg", version=1, window_days=28,
            mean=60, std=5, p10=52, p90=68, mature=True,
        )
    )
    session.add(
        BaselineRow(
            metric="resting_heart_rate", version=1, window_days=28,
            mean=52, std=3, p10=48, p90=56, mature=True,
        )
    )
    session.add(
        BaselineRow(
            metric="sleep_duration_sec", version=1, window_days=28,
            mean=25200, std=3600, p10=18000, p90=32400, mature=True,
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
            DailyReportRow(
                date=day, payload={"risk": {"level": "normal", "triggered_rules": []}}
            )
        )
    session.commit()
    strategy_service.ensure_initial_version(session, settings)


# ---------------------------- 1. 分级 ----------------------------
def test_fault_classified_general(session):
    """一般技术故障 → severity=general，不需用户确认。"""
    task = recovery_service.report_fault(
        session, kind="generalTech", impact_scope="日志采集"
    )
    assert task.severity == "general"
    assert task.needs_user_confirm is False


def test_fault_classified_major(session):
    """重大故障（安全/数据可信/决策/策略依赖）→ severity=major，需用户确认。"""
    for kind in ("safety", "dataTrust", "decision", "strategyDependency"):
        task = recovery_service.report_fault(session, kind=kind)
        assert task.severity == "major", kind
        assert task.needs_user_confirm is True


# ---------------------------- 2. 一般故障自动恢复 ----------------------------
def test_general_fault_auto_recover(session):
    """一般技术故障修复且校验通过 → 自动恢复（§19.6.2）。"""
    s = _settings()
    _seed_active_strategy(session, s)
    task = recovery_service.report_fault(session, kind="generalTech")
    out = recovery_service.auto_recover_general(session, s, task.fault_id)
    assert out.precheck_status == "passed"
    assert out.final_result == "autoRecovered"
    assert out.recovery_executed_at is not None


def test_major_fault_cannot_auto_recover(session):
    """重大故障不可自动恢复 → 抛 RECOVERY_NEED_CONFIRM（§19.6.3）。"""
    s = _settings()
    _seed_active_strategy(session, s)
    task = recovery_service.report_fault(session, kind="safety")
    with pytest.raises(AppError) as ei:
        recovery_service.auto_recover_general(session, s, task.fault_id)
    assert ei.value.code == ErrorCode.RECOVERY_NEED_CONFIRM


# ---------------------------- 3. 未确认重大故障阻断优化 ----------------------------
def test_unconfirmed_major_fault_blocks_optimize(session):
    """存在未确认重大故障 → 禁止恢复/继续优化（§19.6.3/19.6.6）。"""
    s = _settings()
    _seed_active_strategy(session, s)
    allowed, reason = recovery_service.optimize_allowed(session, s)
    assert allowed is True

    recovery_service.report_fault(session, kind="dataTrust")
    allowed, reason = recovery_service.optimize_allowed(session, s)
    assert allowed is False
    assert "待用户确认" in reason


def test_confirmed_major_fault_unblocks_optimize(session):
    """重大故障确认恢复后 → 允许优化（§19.6.6）。"""
    s = _settings()
    _seed_active_strategy(session, s)
    task = recovery_service.report_fault(session, kind="safety")
    recovery_service.run_precheck(session, s, task.fault_id)
    recovery_service.confirm_recovery(session, s, task.fault_id)
    allowed, _ = recovery_service.optimize_allowed(session, s)
    assert allowed is True


# ---------------------------- 4/5. 恢复前校验 ----------------------------
def test_precheck_failed_blocks_recovery(session):
    """恢复前校验未通过（无策略/依赖缺失）→ 不得恢复（§19.6.4）。"""
    s = _settings()
    # 无 active 策略、无历史 → 校验必失败
    task = recovery_service.report_fault(session, kind="safety")
    out = recovery_service.run_precheck(session, s, task.fault_id)
    assert out.precheck_status == "failed"
    assert out.final_result == "precheckFailed"
    assert out.residual_risk  # 记录残留风险

    with pytest.raises(AppError) as ei:
        recovery_service.confirm_recovery(session, s, task.fault_id)
    assert ei.value.code == ErrorCode.VALIDATION


def test_precheck_passed_allows_confirm(session):
    """恢复前校验通过 → 允许用户确认（§19.6.5）。"""
    s = _settings()
    _seed_active_strategy(session, s)
    task = recovery_service.report_fault(session, kind="safety")
    out = recovery_service.run_precheck(session, s, task.fault_id)
    assert out.precheck_status == "passed"
    assert out.precheck_report.get("passed") is True
    # 校验报告包含 10 步恢复流程定义
    assert len(out.precheck_report.get("steps", [])) == 10


# ---------------------------- 6/7. 确认恢复 + 记录完整 ----------------------------
def test_confirm_records_actor_time_result(session):
    """用户确认后恢复优化，并记录确认人/时间/结果（§19.6.6/19.6.7）。"""
    s = _settings()
    _seed_active_strategy(session, s)
    task = recovery_service.report_fault(session, kind="decision")
    recovery_service.run_precheck(session, s, task.fault_id)
    out = recovery_service.confirm_recovery(
        session, s, task.fault_id, confirmed_by="alice"
    )
    assert out.user_confirmed is True
    assert out.user_confirmed_at is not None
    assert out.recovery_executed_at is not None
    assert "alice" in out.final_result


# ---------------------------- 8. 恢复后重新执行完整运行校验 ----------------------------
def test_precheck_reuses_runtime_validator(session):
    """恢复前校验复用完整运行校验器（依赖/结构/回放一致性）（§19.6.8）。"""
    s = _settings()
    _seed_active_strategy(session, s)
    task = recovery_service.report_fault(session, kind="safety")
    out = recovery_service.run_precheck(session, s, task.fault_id)
    detail = out.precheck_report["detail"]
    # 校验器输出 §7.4 的 12 项校验（回放三项合并为 path/safety/constraint → 11 个 key）
    assert "checks" in detail
    checks = {c["key"]: c for c in detail["checks"]}
    assert len(checks) == 11
    assert {"load", "params", "range", "deps", "replay", "no_mix", "version_ref", "audit"} <= set(
        checks
    )
    # 包含历史回放一致性 + 边界语义用例（硬性安全 / 计划约束）
    assert checks["replay"]["passed"] is True
    assert checks["safety"]["passed"] is True
    assert checks["constraint"]["passed"] is True


# ---------------------------- 9. 自动恢复失败保留暂停 ----------------------------
def test_auto_recover_general_failure_keeps_residual(session):
    """一般故障但校验失败 → 自动恢复失败，记录 failed，不标记成功（§19.6.9）。"""
    s = _settings()
    # 无 active 策略 → 校验失败
    task = recovery_service.report_fault(session, kind="generalTech")
    out = recovery_service.auto_recover_general(session, s, task.fault_id)
    assert out.final_result == "autoRecoverFailed"
    assert out.precheck_status == "failed"
    assert out.recovery_executed_at is None


# ---------------------------- 10. 已读 ≠ 同意 ----------------------------
def test_run_precheck_does_not_confirm(session):
    """仅执行恢复前校验（读取性质的检查）不构成用户同意（§19.6.10）。"""
    s = _settings()
    _seed_active_strategy(session, s)
    task = recovery_service.report_fault(session, kind="safety")
    out = recovery_service.run_precheck(session, s, task.fault_id)
    assert out.precheck_status == "passed"
    # 校验通过但未确认：优化仍被阻断
    allowed, _ = recovery_service.optimize_allowed(session, s)
    assert allowed is False
    assert out.user_confirmed is False


# ---------------------------- 查询 ----------------------------
def test_list_and_get_faults(session):
    task = recovery_service.report_fault(session, kind="generalTech")
    recovery_service.report_fault(session, kind="safety")
    assert len(recovery_service.list_faults(session)) == 2
    assert len(recovery_service.list_faults(session, severity="major")) == 1
    got = recovery_service.get_fault(session, task.fault_id)
    assert got is not None and got.fault_id == task.fault_id
    assert recovery_service.get_fault(session, "nope") is None
