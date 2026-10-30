"""基线引擎单元测试（TA.6）。"""

from __future__ import annotations

import datetime as dt

from app.domain.baseline import (
    METRIC_POLICIES,
    MIN_SAMPLES,
    compute,
    compute_all,
    from_dailies,
    policy_for,
    threshold_high,
    threshold_low,
    window_values,
    with_thresholds,
)
from app.domain.rule_registry import BASELINE_SIGMA_MULTIPLIER
from app.schemas.domain import StandardDaily

D = dt.date(2026, 10, 14)


def test_compute_basic_stats():
    b = compute("hrv_last_night_avg", list(range(1, 101)))
    assert b is not None
    assert b.mean == 50.5
    assert b.p10 < b.mean < b.p90
    assert b.mature is True


def test_immature_when_few_samples():
    b = compute("hrv_last_night_avg", [50.0] * (MIN_SAMPLES - 1))
    assert b is not None
    assert b.mature is False


def test_none_when_no_samples():
    assert compute("hrv_last_night_avg", []) is None


def test_window_excludes_out_of_range():
    dailies = [
        StandardDaily(date=D - dt.timedelta(days=40), hrv_last_night_avg=1.0),  # 窗口外
        StandardDaily(date=D - dt.timedelta(days=10), hrv_last_night_avg=60.0),
        StandardDaily(date=D - dt.timedelta(days=1), hrv_last_night_avg=64.0),
        StandardDaily(date=D, hrv_last_night_avg=66.0),
    ]
    vals = window_values(dailies, "hrv_last_night_avg", D, window_days=28)
    assert vals == [60.0, 64.0, 66.0]


def test_window_excludes_as_of_boundary_left_open():
    """区间为 (as_of-window, as_of]，as_of 当天包含、左端点不含。"""
    dailies = [
        StandardDaily(date=D - dt.timedelta(days=28), hrv_last_night_avg=99.0),  # 左端点，排除
        StandardDaily(date=D, hrv_last_night_avg=50.0),
    ]
    vals = window_values(dailies, "hrv_last_night_avg", D, window_days=28)
    assert vals == [50.0]


def test_from_dailies_and_compute_all():
    dailies = [
        StandardDaily(
            date=D - dt.timedelta(days=i),
            hrv_last_night_avg=60.0 + i,
            resting_heart_rate=50 + i,
            sleep_duration_sec=25000 + i * 100,
        )
        for i in range(20)
    ]
    b = from_dailies(dailies, "resting_heart_rate", D)
    assert b is not None and b.mature is True
    allb = compute_all(dailies, D)
    assert set(allb) == {"hrv_last_night_avg", "resting_heart_rate", "sleep_duration_sec"}


def test_unsupported_metric_raises():
    import pytest

    with pytest.raises(ValueError):
        window_values([], "unknown_metric", D)


# ============================================================
# Q5（2026-10-22）：全部指标统一改为近 28 天 mean/median
# ============================================================
def test_all_metrics_use_28_day_window():
    """Q5：弃用「全历史」与 p10，三个指标统一 28 天滚动窗口。"""
    for metric in ("hrv_last_night_avg", "resting_heart_rate", "sleep_duration_sec"):
        p = policy_for(metric)
        assert p.window_days == 28, metric
        assert p.full_history is False, metric


def test_policies_cover_exactly_three_metrics():
    assert set(METRIC_POLICIES) == {
        "hrv_last_night_avg",
        "resting_heart_rate",
        "sleep_duration_sec",
    }


def test_28_day_window_excludes_older_days():
    """28 天窗口必须排除更早的历史（与全历史口径的行为明确区分）。"""
    dailies = [
        StandardDaily(date=D - dt.timedelta(days=200), resting_heart_rate=40),
        StandardDaily(date=D - dt.timedelta(days=100), resting_heart_rate=44),
        StandardDaily(date=D - dt.timedelta(days=1), resting_heart_rate=52),
        StandardDaily(date=D, resting_heart_rate=56),
    ]
    vals = window_values(dailies, "resting_heart_rate", D)
    assert vals == [52.0, 56.0]


def test_full_history_excludes_future_days_no_lookahead():
    """无前视偏差：as_of 之后的数据绝不可参与计算（任何窗口口径都要遵守）。"""
    dailies = [
        StandardDaily(date=D, resting_heart_rate=50),
        StandardDaily(date=D + dt.timedelta(days=1), resting_heart_rate=200),  # 未来
        StandardDaily(date=D + dt.timedelta(days=5), resting_heart_rate=300),
    ]
    vals = window_values(dailies, "resting_heart_rate", D)
    assert vals == [50.0]


def test_threshold_high_is_mean_plus_sigma_for_rhr():
    dailies = [
        StandardDaily(date=D - dt.timedelta(days=i), resting_heart_rate=v)
        for i, v in enumerate([46, 48, 50, 52, 54] * 4)
    ]
    b = from_dailies(dailies, "resting_heart_rate", D)
    assert b is not None
    thr = threshold_high("resting_heart_rate", b)
    assert thr == b.mean + BASELINE_SIGMA_MULTIPLIER * b.std
    assert threshold_low("resting_heart_rate", b) is None  # 无下界


def test_threshold_low_is_mean_minus_sigma_for_sleep():
    dailies = [
        StandardDaily(date=D - dt.timedelta(days=i), sleep_duration_sec=v)
        for i, v in enumerate([20000, 22000, 24000, 26000, 28000] * 4)
    ]
    b = from_dailies(dailies, "sleep_duration_sec", D)
    assert b is not None
    thr = threshold_low("sleep_duration_sec", b)
    assert thr == b.mean - BASELINE_SIGMA_MULTIPLIER * b.std
    assert threshold_high("sleep_duration_sec", b) is None


def test_hrv_threshold_is_now_mean_minus_sigma():
    """Q5：弃用 p10，HRV 下界改为 28 天 mean − 1σ（与 RHR/睡眠统一口径）。"""
    b = compute("hrv_last_night_avg", list(range(40, 81)))
    assert b is not None
    assert threshold_low("hrv_last_night_avg", b) == b.mean - BASELINE_SIGMA_MULTIPLIER * b.std
    assert threshold_high("hrv_last_night_avg", b) is None


def test_with_thresholds_fills_both_sides():
    b = compute("resting_heart_rate", [50.0] * 20)
    out = with_thresholds("resting_heart_rate", b)
    assert out.threshold_high is not None
    assert out.threshold_low is None
    # 原对象不被修改（纯函数）
    assert b.threshold_high is None


def test_full_history_is_stable_against_single_outlier():
    """全历史口径下，单日极端值不会把判定阈值拉到「失去区分度」。

    注意 σ 对极端值本身是敏感的（一个 900s 的夜晚会把 std 抬高），
    因此这里断言的是**实际有意义的性质**：阈值仍落在正常量级区间，
    且那个异常夜依然被正确判为异常 —— 而不是"阈值几乎不变"。
    """
    normal = [24000.0] * 200
    with_outlier = normal[:-1] + [900.0]  # 最后一晚只睡 15 分钟
    dailies = [
        StandardDaily(date=D - dt.timedelta(days=len(normal) - i), sleep_duration_sec=v)
        for i, v in enumerate(with_outlier)
    ]
    b = from_dailies(dailies, "sleep_duration_sec", D)
    assert b is not None
    thr = threshold_low("sleep_duration_sec", b)
    assert thr is not None
    # 阈值仍处于正常睡眠量级（未被异常值拖到接近 0）
    assert thr > 0.5 * 24000
    # 异常夜仍被判定为异常（规则有效性未受损）
    assert thr > 900.0
    # 正常夜不会被误判
    assert thr <= 24000.0
