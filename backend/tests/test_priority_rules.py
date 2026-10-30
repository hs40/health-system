"""四级优先级判定（S2，2026-10-22 风险引擎重设计）。

接缝：``app.domain.priority.judge_priority``。

规则来源（Q2/Q18/Q19/Q20/Q23/Q26/Q27/Q28）：
- P1 两条路径：ACWR>1.5 ∧ HRV 不平衡 / 睡眠<50 ∧ 电量<40；
- P2 按触发条件分流：负荷类 → 容量减半；恢复类 → 强度减量；
- P2 两类同时命中 → 叠加执行，但下限不低于 P1 恢复日模板；
- P3 五项全满足才加量，B 态一律关闭；
- P4 兜底；
- B 态按缺失项分情况保留 P1 能力（Q23）；
- 沿用值（acwr_stale）可触发 P1（Q28-i）但必须打标记（Q27）。
"""

from __future__ import annotations

import datetime as dt

from app.domain.priority import DataStateInput, judge_data_state, judge_priority
from app.schemas.enums import DataState, PriorityLevel

D = dt.date(2026, 10, 22)


def _inp(**over: object) -> DataStateInput:
    base = {
        "on_date": D,
        "acwr_ratio": 1.05,
        "acwr_stale": False,
        "hrv_status": "BALANCED",
        "hrv_last_night_avg": 70.0,
        "hrv_baseline": 70.0,
        "rhr_today": 51,
        "rhr_baseline": 51.0,
        "sleep_score": 78,
        "body_battery_morning": 82,
        "hrv_balanced_streak": 3,
    }
    base.update(over)
    return DataStateInput(**base)  # type: ignore[arg-type]


def _judge(**over: object):  # noqa: ANN201
    inp = _inp(**over)
    state, missing = judge_data_state(inp)
    return judge_priority(inp, state), state, missing


# ============================================================
# P1 — 路径一：负荷过高 ∧ HRV 不平衡
# ============================================================
def test_high_acwr_with_unbalanced_hrv_forces_rest():
    result, state, _ = _judge(acwr_ratio=1.52, hrv_status="UNBALANCED")
    assert state is DataState.FULL
    assert result.priority is PriorityLevel.REST
    assert "P1-LOAD-HRV" in result.triggered_rules
    assert result.blocks_generate is True
    assert result.blocks_upload is True


def test_high_acwr_with_balanced_hrv_does_not_force_rest():
    """ACWR 高但 HRV 平衡 → P1 路径一不成立，落到 P2 容量减半。"""
    result, _, _ = _judge(acwr_ratio=1.52, hrv_status="BALANCED")
    assert result.priority is PriorityLevel.REDUCE
    assert result.volume_halved is True


def test_low_hrv_status_also_counts_as_unbalanced():
    result, _, _ = _judge(acwr_ratio=1.6, hrv_status="LOW")
    assert result.priority is PriorityLevel.REST


# ============================================================
# P1 — 路径二：睡眠极差 ∧ 电量极低
# ============================================================
def test_critical_sleep_and_battery_forces_rest():
    result, _, _ = _judge(sleep_score=45, body_battery_morning=35)
    assert result.priority is PriorityLevel.REST
    assert "P1-SLEEP-BATTERY" in result.triggered_rules


def test_critical_sleep_alone_is_not_p1():
    result, _, _ = _judge(sleep_score=45, body_battery_morning=60)
    assert result.priority is PriorityLevel.REDUCE


def test_battery_zero_participates_as_real_value():
    """Q24：睡眠得分 0 是真实值（域内），必须参与判定而不是被过滤成缺失。"""
    result, state, _ = _judge(sleep_score=0, body_battery_morning=0)
    assert state is DataState.FULL
    assert result.priority is PriorityLevel.REST


# ============================================================
# P2 — 分流（Q19）
# ============================================================
def test_elevated_acwr_halves_volume_only():
    result, _, _ = _judge(acwr_ratio=1.38)
    assert result.priority is PriorityLevel.REDUCE
    assert result.volume_halved is True
    assert result.intensity_reduced is False


def test_low_hrv_reduces_intensity_only():
    """HRV 低于基线 5ms → 强度减量（不减容量）。"""
    result, _, _ = _judge(hrv_last_night_avg=63.0, hrv_baseline=70.0)
    assert result.priority is PriorityLevel.REDUCE
    assert result.intensity_reduced is True
    assert result.volume_halved is False


def test_low_sleep_reduces_intensity():
    result, _, _ = _judge(sleep_score=55)
    assert result.priority is PriorityLevel.REDUCE
    assert result.intensity_reduced is True


def test_elevated_rhr_reduces_intensity():
    result, _, _ = _judge(rhr_today=54, rhr_baseline=51.0)
    assert result.priority is PriorityLevel.REDUCE
    assert result.intensity_reduced is True


def test_p2_stacking_floors_at_recovery_template():
    """Q20：负荷类 + 恢复类同时命中 → 叠加执行，但下限为 P1 恢复日模板。"""
    result, _, _ = _judge(acwr_ratio=1.38, hrv_last_night_avg=60.0, hrv_baseline=70.0)
    assert result.priority is PriorityLevel.REDUCE
    assert result.volume_halved is True
    assert result.intensity_reduced is True
    assert result.floored_at_recovery_template is True


# ============================================================
# P3 — 五项全满足（Q18）
# ============================================================
def test_all_five_conditions_satisfied_progresses():
    result, _, _ = _judge(
        acwr_ratio=0.95,
        hrv_status="BALANCED",
        hrv_balanced_streak=3,
        sleep_score=80,
        body_battery_morning=85,
        rhr_today=50,
        rhr_baseline=51.0,
    )
    assert result.priority is PriorityLevel.PROGRESS
    assert result.progress_duration_pct == (5, 10)


def test_progress_requires_three_day_balanced_streak():
    result, _, _ = _judge(acwr_ratio=0.95, hrv_balanced_streak=2)
    assert result.priority is not PriorityLevel.PROGRESS


def test_progress_requires_low_acwr():
    result, _, _ = _judge(acwr_ratio=1.25)
    assert result.priority is not PriorityLevel.PROGRESS


def test_progress_requires_good_sleep():
    result, _, _ = _judge(acwr_ratio=0.95, sleep_score=70)
    assert result.priority is not PriorityLevel.PROGRESS


def test_progress_requires_good_battery():
    result, _, _ = _judge(acwr_ratio=0.95, body_battery_morning=70)
    assert result.priority is not PriorityLevel.PROGRESS


def test_progress_allows_rhr_flat_within_tolerance():
    """Q25：「持平」= ≤ 基线 + 1bpm。"""
    result, _, _ = _judge(acwr_ratio=0.95, rhr_today=52, rhr_baseline=51.0)
    assert result.priority is PriorityLevel.PROGRESS


def test_progress_blocked_when_rhr_exceeds_tolerance():
    result, _, _ = _judge(acwr_ratio=0.95, rhr_today=53, rhr_baseline=51.0)
    assert result.priority is not PriorityLevel.PROGRESS


# ============================================================
# P4 — 兜底
# ============================================================
def test_no_rule_hit_maintains_plan():
    """兜底：负荷适中、恢复良好但不足以加量（HRV 未连续 3 天平衡）→ P4。"""
    result, state, _ = _judge(acwr_ratio=1.15, hrv_balanced_streak=1)
    assert state is DataState.FULL
    assert result.priority is PriorityLevel.MAINTAIN
    assert result.triggered_rules == []


# ============================================================
# B 态分情况保留 P1 能力（Q23）
# ============================================================
def test_b_state_missing_hrv_keeps_sleep_battery_p1_path():
    """缺 HRV → 「睡眠 ∧ 电量」路径仍可判 P1。"""
    result, state, _ = _judge(hrv_status=None, sleep_score=40, body_battery_morning=30)
    assert state is DataState.LOAD_ONLY
    assert result.priority is PriorityLevel.REST


def test_b_state_missing_sleep_keeps_load_hrv_p1_path():
    """缺睡眠得分 → 「ACWR ∧ HRV」路径仍可判 P1。"""
    result, state, _ = _judge(sleep_score=None, acwr_ratio=1.7, hrv_status="LOW")
    assert state is DataState.LOAD_ONLY
    assert result.priority is PriorityLevel.REST


def test_b_state_never_progresses():
    """Q7：B 态关闭加量通道，即使其他条件全部满足。"""
    result, state, _ = _judge(hrv_status=None, sleep_score=90, body_battery_morning=95)
    assert state is DataState.LOAD_ONLY
    assert result.priority is not PriorityLevel.PROGRESS


# ============================================================
# C 态：只关负荷类规则，恢复类照判（2026-10-25 用户决策 L-7）
# ============================================================
def test_fallback_state_still_judges_recovery_rules():
    """缺 ACWR 不等于「恢复极差也按原课表」——睡眠+电量双低仍应强制休息。"""
    result, state, _ = _judge(acwr_ratio=None, sleep_score=30, body_battery_morning=20)
    assert state is DataState.FALLBACK
    assert result.priority is PriorityLevel.REST
    assert "P1-SLEEP-BATTERY" in result.triggered_rules
    assert result.blocks_generate is True
    assert result.blocks_upload is True


def test_fallback_state_ignores_load_rules_without_acwr():
    """C 态没有 ACWR：负荷类规则不得凭空调用，恢复良好时仍按原计划。"""
    result, state, _ = _judge(
        acwr_ratio=None, sleep_score=85, body_battery_morning=80, hrv_status="BALANCED"
    )
    assert state is DataState.FALLBACK
    assert result.priority is PriorityLevel.MAINTAIN
    assert result.blocks_generate is False


def test_fallback_state_can_reduce_on_poor_sleep():
    """恢复一般偏差 → P2 减量（只减强度，不因缺负荷而减容量）。"""
    result, state, _ = _judge(
        acwr_ratio=None, sleep_score=45, body_battery_morning=60, hrv_status="BALANCED"
    )
    assert state is DataState.FALLBACK
    assert result.priority is PriorityLevel.REDUCE
    assert result.triggered_rules == ["P2-SLEEP"]
    assert result.blocks_upload is True
    assert result.volume_halved is False
    assert result.intensity_reduced is True


# ============================================================
# 沿用值（Q27 / Q28-i）
# ============================================================
def test_stale_acwr_can_still_trigger_p1():
    result, _, _ = _judge(
        acwr_ratio=1.7,
        acwr_stale=True,
        acwr_source_date=dt.date(2026, 10, 19),
        hrv_status="UNBALANCED",
    )
    assert result.priority is PriorityLevel.REST
    assert result.acwr_stale is True
    assert result.acwr_source_date == dt.date(2026, 10, 19)


# ============================================================
# 边界值特征测试（重构前后的行为契约，必须始终为绿）
# 这些用例把每条规则的「开/闭边界」钉死：任何重构若让它们在开关处翻转，
# 说明判定语义被悄悄改变了。
# ============================================================
def test_boundary_acwr_high_is_strict_greater():
    """ACWR 恰为 1.5 → 不触发 P1 负荷路径（严格大于）。"""
    result, _, _ = _judge(acwr_ratio=1.5, hrv_status="UNBALANCED")
    assert result.priority is not PriorityLevel.REST
    assert result.priority is PriorityLevel.REDUCE  # 1.5 ≥ 1.3 → P2-LOAD


def test_boundary_acwr_elevated_is_inclusive():
    """ACWR 恰为 1.3 → 触发 P2-LOAD（含等号）。"""
    result, _, _ = _judge(acwr_ratio=1.3)
    assert result.priority is PriorityLevel.REDUCE
    assert "P2-LOAD" in result.triggered_rules
    assert result.volume_halved is True


def test_boundary_sleep_critical_is_strict_less():
    """睡眠恰为 50 → 不触发 P1（严格小于）；电量 39 也不够。"""
    result, _, _ = _judge(sleep_score=50, body_battery_morning=39)
    assert result.priority is not PriorityLevel.REST


def test_boundary_battery_critical_is_strict_less():
    """电量恰为 40 → 不触发 P1（严格小于）。"""
    result, _, _ = _judge(sleep_score=30, body_battery_morning=40)
    assert result.priority is not PriorityLevel.REST


def test_boundary_hrv_delta_is_strict_less():
    """HRV 恰低于基线 5ms → 不触发 P2-HRV（严格小于）。"""
    result, _, _ = _judge(hrv_last_night_avg=65.0, hrv_baseline=70.0)
    assert "P2-HRV" not in result.triggered_rules


def test_boundary_rhr_elevated_is_inclusive():
    """RHR 恰高于基线 3bpm → 触发 P2-RHR（含等号）。"""
    result, _, _ = _judge(rhr_today=54, rhr_baseline=51.0)
    assert "P2-RHR" in result.triggered_rules


def test_p2_stacking_sets_floor_flag():
    """负荷类 + 恢复类同时命中 → 叠加执行并触及恢复日下限。"""
    result, _, _ = _judge(acwr_ratio=1.4, sleep_score=50)
    assert result.priority is PriorityLevel.REDUCE
    assert result.volume_halved is True
    assert result.intensity_reduced is True
    assert result.floored_at_recovery_template is True


def test_p2_load_only_does_not_reduce_intensity():
    """只有负荷类命中 → 只减容量、不减强度。"""
    result, _, _ = _judge(acwr_ratio=1.4)
    assert result.volume_halved is True
    assert result.intensity_reduced is False
    assert result.floored_at_recovery_template is False


def test_c_state_with_severe_recovery_still_rests():
    """C 态（ACWR 缺失）+ 恢复极差 → 仍 P1（L-7，2026-10-25）。"""
    result, state, _ = _judge(
        acwr_ratio=None, sleep_score=30, body_battery_morning=20
    )
    assert state is DataState.FALLBACK
    assert result.priority is PriorityLevel.REST
    assert "P1-SLEEP-BATTERY" in result.triggered_rules
    assert "P1-LOAD-HRV" not in result.triggered_rules


def test_c_state_with_good_recovery_keeps_plan():
    """C 态 + 恢复良好 → P4，负荷类规则不得凭空触发。"""
    result, state, _ = _judge(acwr_ratio=None)
    assert state is DataState.FALLBACK
    assert result.priority is PriorityLevel.MAINTAIN
    assert result.triggered_rules == []


def test_stale_acwr_can_trigger_p1_but_is_marked():
    """沿用 ≤3 天的 ACWR 仍可触发 P1，且必须打 stale 标记（Q27/Q28-i）。"""
    result, _, _ = _judge(
        acwr_ratio=1.6,
        acwr_stale=True,
        acwr_source_date=D - dt.timedelta(days=2),
        hrv_status="UNBALANCED",
    )
    assert result.priority is PriorityLevel.REST
    assert result.acwr_stale is True
    assert result.acwr_source_date == D - dt.timedelta(days=2)


def test_p3_progress_percentages_come_from_registry():
    """P3 的加量幅度必须来自注册表（5~10% / 2.5~5% / 1~2 reps）。"""
    from app.domain import rule_registry as rr

    result, _, _ = _judge()
    assert result.priority is PriorityLevel.PROGRESS
    assert result.progress_duration_pct == rr.PROGRESS_DURATION_PCT
    assert result.progress_strength_pct == rr.PROGRESS_STRENGTH_PCT
    assert result.progress_strength_reps == rr.PROGRESS_STRENGTH_REPS
