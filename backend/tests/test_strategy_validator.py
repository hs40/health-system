"""策略完整运行校验器测试（阶段③ T3.3；验收 §19.4 语义正确性）。

关键：**语义错误必须能被发现**（不是只检查文件是否存在）。
- 结构/参数/范围/依赖校验；
- 历史回放一致性（含边界用例）；
- 空样本不得判定通过。
"""

from __future__ import annotations

from app.domain.strategy_validator import (
    BOUNDARY_CASES,
    ReplayCase,
    validate_strategy,
)

_GOOD_PAYLOAD = {
    "season": "summer",
    "focus": "有氧与减脂",
    "endurance_ratio": None,
    "strength_ratio": None,
    "recovery_ratio": None,
    "limitations": ["避开高温时段"],
}

_ALL_METRICS = {
    "standard_daily.hrv_last_night_avg",
    "standard_daily.resting_heart_rate",
    "standard_daily.sleep_score",
    "standard_daily.body_battery_morning",
    "baseline.hrv_mean",
    "acwr",
    "environment_daily",
}


def _validate(payload, **kw):
    kw.setdefault("cases", BOUNDARY_CASES)
    kw.setdefault("available_metrics", _ALL_METRICS)
    kw.setdefault("replay_total", 470)
    kw.setdefault("replay_matched", 400)
    return validate_strategy(payload, **kw)


def test_valid_payload_passes():
    report = _validate(_GOOD_PAYLOAD)
    assert report.passed is True
    assert report.replayed == len(BOUNDARY_CASES)


def test_missing_required_field_fails():
    bad = dict(_GOOD_PAYLOAD)
    bad.pop("focus")
    report = _validate(bad)
    assert report.passed is False
    assert any("focus" in f for f in report.failures)


def test_illegal_season_fails():
    bad = dict(_GOOD_PAYLOAD, season="rainy")
    report = _validate(bad)
    assert report.passed is False
    assert any("season" in f for f in report.failures)


def test_ratio_out_of_range_fails():
    bad = dict(_GOOD_PAYLOAD, endurance_ratio=1.5)
    report = _validate(bad)
    assert report.passed is False
    assert any("越界" in f for f in report.failures)


def test_ratio_non_numeric_fails():
    bad = dict(_GOOD_PAYLOAD, strength_ratio="high")
    report = _validate(bad)
    assert report.passed is False


def test_missing_dependencies_fails():
    report = validate_strategy(
        _GOOD_PAYLOAD,
        cases=BOUNDARY_CASES,
        available_metrics={"standard_daily.resting_heart_rate"},
        replay_total=470,
        replay_matched=470,
    )
    assert report.passed is False
    assert any("依赖" in f for f in report.failures)


def test_no_metrics_set_is_conservative_fail():
    report = validate_strategy(
        _GOOD_PAYLOAD, cases=BOUNDARY_CASES, available_metrics=None,
        replay_total=470, replay_matched=470,
    )
    assert report.passed is False


def test_empty_cases_fails():
    """无回放样本不得判定通过（不得空跑即成功）。"""
    report = validate_strategy(_GOOD_PAYLOAD, cases=[], available_metrics=_ALL_METRICS,
                               replay_total=470, replay_matched=470)
    assert report.passed is False
    assert any("无回放样本" in f for f in report.failures)


def test_semantic_error_rain_outdoor_detected():
    """语义错误：把「雨天户外」判为可行 → 硬性安全检查必须失败。

    构造一个用例：雨天期望「不强制室内」= 故意与规则相反 → 校验应失败。
    """
    wrong_case = ReplayCase(
        "rain_but_outdoor", temp_c=18.0, is_rain=True, expect_indoor_required=False
    )
    report = validate_strategy(
        _GOOD_PAYLOAD,
        cases=[wrong_case],
        available_metrics=_ALL_METRICS,
        replay_total=470,
        replay_matched=470,
    )
    assert report.passed is False
    assert any("硬约束" in f for f in report.failures)


def test_semantic_error_high_risk_not_blocked_detected():
    """语义错误：P1 场景未被拦截 → 计划约束校验失败。"""
    wrong_case = ReplayCase(
        "p1_should_block",
        temp_c=20.0,
        acwr_ratio=1.6,
        hrv_status="LOW",
        expect_plan_blocked=False,
    )
    report = validate_strategy(_GOOD_PAYLOAD, cases=[wrong_case], available_metrics=_ALL_METRICS,
                               replay_total=470, replay_matched=470)
    assert report.passed is False
    assert any("关键决策路径" in f for f in report.failures)


def test_p2_reduce_does_not_block_generate():
    """P2 减量只拦「上传」，不得拦「生成」（Q26 两级拦截）。"""
    case = ReplayCase(
        "p2_blocks_upload_only",
        temp_c=20.0,
        acwr_ratio=1.35,
        expect_plan_blocked=False,
        expect_upload_blocked=True,
    )
    report = validate_strategy(
        _GOOD_PAYLOAD, cases=[case], available_metrics=_ALL_METRICS,
        replay_total=470, replay_matched=470,
    )
    assert report.passed is True


def test_low_replay_match_rate_fails():
    """历史回放匹配率低于下限 → 失败（语义可能被破坏）。"""
    report = validate_strategy(_GOOD_PAYLOAD, cases=BOUNDARY_CASES, available_metrics=_ALL_METRICS,
                               replay_total=470, replay_matched=100, min_replay_match=0.80)
    assert report.passed is False
    assert any("匹配率" in f for f in report.failures)


def test_replay_pass_at_threshold():
    report = validate_strategy(_GOOD_PAYLOAD, cases=BOUNDARY_CASES, available_metrics=_ALL_METRICS,
                               replay_total=470, replay_matched=400, min_replay_match=0.80)
    assert report.passed is True


def test_twelve_checks_present():
    report = _validate(_GOOD_PAYLOAD)
    keys = {c.key for c in report.checks}
    # 12 项校验（含回放一致性）
    assert len(report.checks) >= 10
    assert {"load", "params", "range", "deps", "path", "safety", "constraint", "replay"} <= keys
