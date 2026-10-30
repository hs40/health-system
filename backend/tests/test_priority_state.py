"""数据质量三态关卡（S1，2026-10-22 风险引擎重设计）。

接缝：``app.domain.priority.judge_data_state``。

规则来源（Q7 / Q11b / Q23 / Q24）：
- 核心 2 项 = HRV 状态 + 睡眠得分，**任一缺失即降 B**；
- 缺失 = 形态 1（无记录）或形态 2（字段为空）；形态 3（异常值）按真实值判定；
- ``hrv_status == "NONE"`` 算缺失（Garmin 表示无法判定）；
- ``body_battery_morning`` 缺失 → 当天降 B；
- B 态内部分两种：缺 HRV 保留「睡眠 ∧ 电量」路径，缺睡眠保留「ACWR ∧ HRV」路径；
- 连负荷（ACWR）都不可用 → C。
"""

from __future__ import annotations

import datetime as dt

from app.domain.priority import DataStateInput, judge_data_state
from app.schemas.enums import DataState

D = dt.date(2026, 10, 22)


def _full(**over: object) -> DataStateInput:
    """四维齐全的基线输入，用例只覆盖需要变化的字段。"""
    base = {
        "on_date": D,
        "acwr_ratio": 1.05,
        "acwr_stale": False,
        "hrv_status": "BALANCED",
        "hrv_last_night_avg": 70.0,
        "rhr_today": 51,
        "rhr_baseline": 51.0,
        "sleep_score": 78,
        "body_battery_morning": 82,
    }
    base.update(over)
    return DataStateInput(**base)  # type: ignore[arg-type]


# ============================================================
# A 态：四维齐全
# ============================================================
def test_all_dimensions_present_is_full_state():
    state, missing = judge_data_state(_full())
    assert state is DataState.FULL
    assert missing == []


def test_anomalous_but_in_range_values_still_count_as_present():
    """Q24：形态 3（异常值）按真实值判定，不因「看起来不正常」而降级。"""
    state, missing = judge_data_state(_full(sleep_score=0, body_battery_morning=0))
    assert state is DataState.FULL
    assert missing == []


# ============================================================
# B 态：核心恢复维度缺失
# ============================================================
def test_missing_hrv_status_drops_to_load_only():
    state, missing = judge_data_state(_full(hrv_status=None))
    assert state is DataState.LOAD_ONLY
    assert "hrv_status" in missing


def test_hrv_status_none_counts_as_missing():
    """Garmin 的 NONE 表示「无法判定」，是缺失而非「不平衡」（Q17 补充）。"""
    state, missing = judge_data_state(_full(hrv_status="NONE"))
    assert state is DataState.LOAD_ONLY
    assert "hrv_status" in missing


def test_missing_sleep_score_drops_to_load_only():
    state, missing = judge_data_state(_full(sleep_score=None))
    assert state is DataState.LOAD_ONLY
    assert "sleep_score" in missing


def test_missing_morning_battery_drops_to_load_only():
    """Q11b：body_battery_morning 缺失当天直接降 B。"""
    state, missing = judge_data_state(_full(body_battery_morning=None))
    assert state is DataState.LOAD_ONLY
    assert "body_battery_morning" in missing


def test_missing_both_core_items_lists_both():
    state, missing = judge_data_state(_full(hrv_status=None, sleep_score=None))
    assert state is DataState.LOAD_ONLY
    assert set(missing) == {"hrv_status", "sleep_score"}


# ============================================================
# C 态：连负荷也不可用
# ============================================================
def test_missing_acwr_falls_back_to_plan():
    state, missing = judge_data_state(_full(acwr_ratio=None))
    assert state is DataState.FALLBACK
    assert "acwr_ratio" in missing


def test_missing_acwr_wins_over_missing_hrv():
    """负荷缺失比恢复缺失更严重：没有负荷就无法做任何负荷判定。"""
    state, _ = judge_data_state(_full(acwr_ratio=None, hrv_status=None))
    assert state is DataState.FALLBACK


# ============================================================
# 沿用值（Q27）：有值即视为可用，另由 acwr_stale 标记
# ============================================================
def test_stale_acwr_still_counts_as_available():
    state, missing = judge_data_state(_full(acwr_stale=True))
    assert state is DataState.FULL
    assert missing == []


# ============================================================
# RHR 缺失：不是核心项，不降 B
# ============================================================
def test_missing_rhr_alone_keeps_full_state():
    """RHR 不在「核心 2 项」内，单独缺失不降级（Q7），只在缺失清单中记录。"""
    state, missing = judge_data_state(_full(rhr_today=None))
    assert state is DataState.FULL
    assert "rhr" in missing
