"""风险引擎入口单元测试（安全关键，REQ-RISK-*）。

这些断言是「程序逻辑不得被模型覆盖」的回归护栏（REQ-RISK-002）：
任何一条失败都意味着高风险可能被放行，属最高优先级缺陷。

本文件只测 **``risk.evaluate`` 的组装与拦截语义**；
判定细节（三态、四级、阈值边界）分别在 ``test_priority_state.py`` 与
``test_priority_rules.py`` 覆盖，避免重复。
"""

from __future__ import annotations

from datetime import date

from app.domain import rule_registry as rr
from app.domain.risk import (
    RiskInput,
    allows_recovery_day,
    blocks_generate,
    blocks_plan,
    blocks_upload,
    evaluate,
)
from app.schemas.enums import DataState, PriorityLevel

D = date(2026, 10, 14)


def _base(**kw) -> RiskInput:
    """构造「四维齐全、负荷适宜、恢复良好」的输入（默认落到 P3 加量通道）。"""
    defaults = dict(
        on_date=D,
        acwr_ratio=1.0,
        hrv_status="BALANCED",
        hrv_last_night_avg=60.0,
        hrv_baseline=60.0,
        hrv_balanced_streak=3,
        resting_heart_rate=50,
        rhr_baseline=50.0,
        sleep_score=80,
        body_battery_morning=80,
    )
    defaults.update(kw)
    return RiskInput(**defaults)


# ------------------------------------------------------------------
# 组装与版本
# ------------------------------------------------------------------
def test_all_clear_yields_progress():
    a = evaluate(_base())
    assert a.priority is PriorityLevel.PROGRESS
    assert a.data_state is DataState.FULL
    assert a.missing_inputs == []
    assert a.rule_version == rr.RULE_VERSION


def test_evaluation_is_reproducible():
    """相同输入 → 相同优先级、状态、规则命中（REQ-RISK-002 可复现）。"""
    inp = _base(acwr_ratio=1.6, hrv_status="LOW")
    a1, a2 = evaluate(inp), evaluate(inp)
    assert a1.priority is a2.priority
    assert a1.data_state is a2.data_state
    assert a1.triggered_rules == a2.triggered_rules
# ------------------------------------------------------------------
# P1：强制休息 → 拦「计划生成」+「上传」
# ------------------------------------------------------------------
def test_p1_blocks_generate_and_upload():
    a = evaluate(_base(acwr_ratio=1.6, hrv_status="LOW"))
    assert a.priority is PriorityLevel.REST
    assert blocks_generate(a) is True
    assert blocks_plan(a) is True  # 兼容别名，语义等同 blocks_generate
    assert blocks_upload(a) is True
    assert allows_recovery_day(a) is True


def test_p1_evidence_records_rule_and_reason():
    a = evaluate(_base(acwr_ratio=1.6, hrv_status="LOW"))
    assert a.triggered_rules == ["P1-LOAD-HRV"]
    assert any("负荷过高" in r for r in a.evidence["reasons"])


# ------------------------------------------------------------------
# P2：减量 → 只拦「上传」，不拦「计划生成」
# ------------------------------------------------------------------
def test_p2_blocks_upload_only():
    a = evaluate(_base(acwr_ratio=1.35))
    assert a.priority is PriorityLevel.REDUCE
    assert blocks_generate(a) is False
    assert blocks_plan(a) is False
    assert blocks_upload(a) is True
    assert allows_recovery_day(a) is False
    assert a.volume_halved is True
    assert a.intensity_reduced is False


def test_p2_recovery_only_reduces_intensity():
    a = evaluate(_base(sleep_score=55))
    assert a.priority is PriorityLevel.REDUCE
    assert a.volume_halved is False
    assert a.intensity_reduced is True


def test_p2_stacked_flags_floor_at_recovery_template():
    """负荷类 + 恢复类同时命中 → 叠加执行且触及恢复日模板下限（Q20-b'）。"""
    a = evaluate(_base(acwr_ratio=1.35, sleep_score=55))
    assert a.priority is PriorityLevel.REDUCE
    assert a.volume_halved is True
    assert a.intensity_reduced is True
    assert a.floored_at_recovery_template is True


# ------------------------------------------------------------------
# P3 / P4：均不拦截
# ------------------------------------------------------------------
def test_p3_not_blocked_and_exposes_adjustments():
    a = evaluate(_base())
    assert blocks_generate(a) is False
    assert blocks_upload(a) is False
    assert list(a.progress_duration_pct) == list(rr.PROGRESS_DURATION_PCT)
    assert list(a.progress_strength_pct) == list(rr.PROGRESS_STRENGTH_PCT)
    assert list(a.progress_strength_reps) == list(rr.PROGRESS_STRENGTH_REPS)


def test_p4_not_blocked():
    a = evaluate(_base(acwr_ratio=1.15, hrv_balanced_streak=1))
    assert a.priority is PriorityLevel.MAINTAIN
    assert blocks_generate(a) is False
    assert blocks_upload(a) is False


# ------------------------------------------------------------------
# 三态关卡对拦截语义的影响
# ------------------------------------------------------------------
def test_b_state_cannot_progress_but_still_reduces():
    a = evaluate(_base(hrv_status=None))  # 缺 HRV 状态 → B
    assert a.data_state is DataState.LOAD_ONLY
    assert "hrv_status" in a.missing_inputs
    assert a.priority is not PriorityLevel.PROGRESS


def test_b_state_keeps_sleep_and_battery_p1_path():
    """缺 HRV 但睡眠/电量极低 → 仍能触发 P1（Q23-c 分情况保留）。"""
    a = evaluate(_base(hrv_status=None, sleep_score=40, body_battery_morning=30))
    assert a.data_state is DataState.LOAD_ONLY
    assert a.priority is PriorityLevel.REST
    assert blocks_generate(a) is True


def test_c_state_still_blocks_on_severe_recovery():
    """C 态（缺 ACWR）只关负荷类规则；恢复极差仍应拦截（L-7，2026-10-25）。"""
    a = evaluate(_base(acwr_ratio=None, sleep_score=40, body_battery_morning=30))
    assert a.data_state is DataState.FALLBACK
    assert "acwr_ratio" in a.missing_inputs
    assert a.priority is PriorityLevel.REST
    assert blocks_generate(a) is True
    assert blocks_upload(a) is True


def test_risk_carries_human_readable_reasons():
    """P1/P2 必须带「哪条规则 + 实测值 vs 阈值」，供前端直接展示（L-1 附带需求）。

    阈值观察期需要看到「为什么是 P1」而不是只有一句「规则判定 P1」。
    """
    p1 = evaluate(_base(acwr_ratio=1.6, hrv_status="LOW"))
    assert p1.priority is PriorityLevel.REST
    assert any("ACWR 1.60" in r and "1.5" in r for r in p1.reasons)
    assert p1.evidence["reasons"] == p1.reasons

    p2 = evaluate(_base(sleep_score=45))
    assert p2.priority is PriorityLevel.REDUCE
    assert any("睡眠" in r and "45" in r for r in p2.reasons)


def test_stale_acwr_can_still_trigger_p1():
    """沿用最近 ≤3 天的 ACWR（Q27）仍参与判定，并能触发 P1。"""
    a = evaluate(
        _base(
            acwr_ratio=1.6,
            acwr_stale=True,
            acwr_source_date=date(2026, 10, 12),
            hrv_status="LOW",
        )
    )
    assert a.priority is PriorityLevel.REST
    assert a.evidence["acwr_stale"] is True
    assert a.evidence["acwr_source_date"] == "2026-10-12"


def test_abnormal_values_are_taken_at_face_value():
    """异常值按真实值参与判定（Q24：只认「无记录 / 空值」为缺失）。"""
    a = evaluate(_base(resting_heart_rate=200, rhr_baseline=50.0))
    assert a.data_state is DataState.FULL
    assert "rhr" not in a.missing_inputs
    assert "P2-RHR" in a.triggered_rules
