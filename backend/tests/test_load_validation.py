"""Tredict 指标 ↔ 项目负荷 验证测试（需求第七章 / 开发计划 4.10）。

断言全部来自需求第七章业务规则，不是实现细节：

| 测试组 | 对应需求 |
|---|---|
| 白名单与禁用指标 | 7.1 |
| 运动类型适用性（heartrate/power/pace 仅跑步） | 7.1 |
| 指标不混组（Sample.metric 强制隔离） | 7.2 |
| 允许非零截距 | 7.2 |
| 不设统一通过阈值、报告无排名 | 7.3 |
| 样本不足 → 证据不足 | 7.3 |
| Tredict 指标仅用于比较，不并入负荷 | 7.4 |
| 拟合边界（n<2 / X 无方差 / Y 无方差） | 实现稳健性 |
"""

from __future__ import annotations

import pytest

from app.domain.load import validation as val


# ------------------------------------------------------------
# 辅助
# ------------------------------------------------------------
def _sample(metric: str, x: float, y: float, *, sport: str = "running",
            activity_id: str = "a") -> val.Sample:
    return val.Sample(metric=metric, activity_id=activity_id, sport=sport, x=x, y=y)


def _series(metric: str, n: int = 12, *, sport: str = "running",
            slope: float = 1.0, intercept: float = 0.0) -> list[val.Sample]:
    """Y = slope·X + intercept 的干净样本，用于精确还原拟合参数。"""
    return [
        _sample(metric, float(i), slope * i + intercept, sport=sport,
                activity_id=f"{metric}-{sport}-{i}")
        for i in range(n)
    ]


# ------------------------------------------------------------
# 需求 7.1：白名单与禁用指标
# ------------------------------------------------------------
def test_forbidden_metrics_are_not_validated():
    """需求 7.1：三个指标明确禁止作为验证指标。"""
    for metric in val.FORBIDDEN_METRICS:
        assert not val.metric_allowed(metric), metric
        result = val.evaluate_metric(metric, None, [_sample(metric, 1.0, 2.0)])
        assert result.status == "forbidden"
        assert result.regression is None


def test_unknown_metric_is_not_validated():
    """白名单外的指标不得进入验证。"""
    assert not val.metric_allowed("garmin.trainingLoad")
    result = val.evaluate_metric(
        "garmin.trainingLoad", None, [_sample("garmin.trainingLoad", 1.0, 2.0)]
    )
    assert result.status == "forbidden"


def test_four_allowed_metrics_appear_in_report():
    """需求 7.1：允许的指标四个，报告里每个指标一条，不增不减。"""
    samples: list[val.Sample] = []
    for metric in val.ALLOWED_METRICS:
        samples += _series(metric)
    report = val.build_report(samples)
    assert [m.metric for m in report.metrics] == list(val.ALLOWED_METRICS)


# ------------------------------------------------------------
# 需求 7.1：指标 × 运动类型适用性
# ------------------------------------------------------------
def test_heartrate_power_pace_only_use_running_samples():
    """需求 7.1：heartrate / power / pace 只有跑步活动可参与。"""
    samples = (
        _series(val.METRIC_HEARTRATE, sport="running")
        + [_sample(val.METRIC_HEARTRATE, 5.0, 900.0, sport="strength_training")]
        + [_sample(val.METRIC_HEARTRATE, 6.0, 910.0, sport="walking")]
    )
    result = val.evaluate_metric(val.METRIC_HEARTRATE, None, samples)
    assert result.status == "ok"
    # 非跑步样本被排除，不进 n（也绝不进回归）
    assert result.n == 12
    assert result.regression is not None and result.regression.n == 12


def test_training_efforts_not_sport_restricted():
    """trainingEfforts 对所有运动类型开放（需求 7.1）。"""
    samples = _series(val.METRIC_EFFORT, sport="running") + _series(
        val.METRIC_EFFORT, sport="hiking", slope=2.0
    )
    result = val.evaluate_metric(val.METRIC_EFFORT, None, samples)
    assert result.n == 24


# ------------------------------------------------------------
# 需求 7.2：不同指标不混在同一组数据
# ------------------------------------------------------------
def test_metrics_are_never_mixed_in_one_regression():
    """需求 7.2：混入其他指标的样本，既不进 n 也不影响斜率。"""
    clean = _series(val.METRIC_HEARTRATE, slope=1.0, intercept=10.0)
    polluted = clean + [
        _sample(val.METRIC_POWER, 1.0, 999.0),
        _sample(val.METRIC_PACE, 2.0, -999.0),
        _sample(val.METRIC_EFFORT, 3.0, 0.0),
    ]
    base = val.evaluate_metric(val.METRIC_HEARTRATE, None, clean)
    mixed = val.evaluate_metric(val.METRIC_HEARTRATE, None, polluted)
    assert mixed.n == base.n == 12
    assert mixed.regression.slope == pytest.approx(base.regression.slope)
    assert mixed.regression.intercept == pytest.approx(base.regression.intercept)


def test_sample_metric_field_is_required():
    """需求 7.2：metric 是样本必填归属，构造时缺失即报错。"""
    with pytest.raises(TypeError):
        val.Sample(activity_id="a", sport="running", x=1.0, y=2.0)  # type: ignore[call-arg]


def test_non_zero_intercept_is_allowed():
    """需求 7.2：允许非零截距，Y = 2X + 5 必须被精确还原。"""
    samples = _series(val.METRIC_EFFORT, slope=2.0, intercept=5.0)
    g = val.fit(samples)
    assert g is not None
    assert g.slope == pytest.approx(2.0)
    assert g.intercept == pytest.approx(5.0)
    assert g.r == pytest.approx(1.0)


def test_negative_correlation_is_reported_as_negative():
    """负相关如实报 negative，不取绝对值粉饰。"""
    samples = [
        _sample(val.METRIC_EFFORT, float(i), 100.0 - 3.0 * i,
                activity_id=f"{i}") for i in range(10)
    ]
    g = val.fit(samples)
    assert g is not None and g.direction == "negative"
    assert g.slope < 0 and g.r < 0


# ------------------------------------------------------------
# 需求 7.3：不设统一通过阈值、数据不足如实报
# ------------------------------------------------------------
def test_report_has_no_verdict_or_ranking_field():
    """需求 7.3：报告不含总体通过/不通过、不含排名。"""
    samples: list[val.Sample] = []
    for metric in val.ALLOWED_METRICS:
        samples += _series(metric)
    d = val.build_report(samples).to_dict()

    banned = ("pass", "passed", "fail", "failed", "verdict", "score", "rank",
              "ranking", "overall", "conclusion", "grade")
    for key in d:
        assert key not in banned
    for group in ("metrics", "by_sport"):
        for entry in d[group]:
            for key in entry:
                assert key not in banned
            for key in entry.get("fit", {}):
                assert key not in banned
    assert "disclaimer" in d  # 必须带免责说明（7.4）


def test_insufficient_samples_report_no_conclusion():
    """需求 7.3：样本不足 → 证据不足，不给 a/b 结论。"""
    few = _series(val.METRIC_EFFORT, n=3)
    result = val.evaluate_metric(val.METRIC_EFFORT, None, few)
    assert result.status == "insufficient"
    assert result.relation == "证据不足"
    assert result.regression is None


def test_small_sample_gets_warning_note():
    """样本够出结果但偏少 → 明确标注不稳定，不当作确定性结论。"""
    mid = _series(val.METRIC_EFFORT, n=7)
    result = val.evaluate_metric(val.METRIC_EFFORT, None, mid)
    assert result.status == "ok"
    assert result.note and "样本偏少" in result.note


def test_relation_text_only_describes_no_verdict():
    """文字结论只描述关系强度，不含「通过/达标」措辞。"""
    samples = _series(val.METRIC_EFFORT, n=20)
    result = val.evaluate_metric(val.METRIC_EFFORT, None, samples)
    assert "通过" not in result.relation
    assert "达标" not in result.relation


def test_no_relation_still_reported_not_dropped():
    """R² 很低也照实报「未见可识别的线性关系」，不删除、不隐藏。"""
    ys = [1.0, 9.0, 2.0, 8.0, 3.0, 7.0, 4.0, 6.0, 5.0, 5.5, 6.5, 4.5]
    samples = [
        _sample(val.METRIC_PACE, float(i), y, activity_id=f"{i}")
        for i, y in enumerate(ys)
    ]
    result = val.evaluate_metric(val.METRIC_PACE, None, samples)
    assert result.regression is not None
    assert "未见可识别的线性关系" in result.relation


# ------------------------------------------------------------
# 需求 7.3：分组口径（按指标 × 运动类型）
# ------------------------------------------------------------
def test_by_sport_only_emits_sports_with_samples():
    """只为有样本的运动类型出结论，不凑数、不造空组。"""
    samples = _series(val.METRIC_EFFORT, sport="running") + _series(
        val.METRIC_EFFORT, sport="strength_training"
    )
    report = val.build_report(samples)
    sports_seen = {(r.metric, r.sport) for r in report.sports}
    assert ("trainingEfforts", "running") in sports_seen
    assert ("trainingEfforts", "strength_training") in sports_seen
    assert all(r.sport is not None for r in report.sports)


# ------------------------------------------------------------
# 拟合边界（实现稳健性，避免伪造结论）
# ------------------------------------------------------------
def test_fit_needs_at_least_two_samples():
    assert val.fit([]) is None
    assert val.fit([_sample(val.METRIC_EFFORT, 1.0, 2.0)]) is None


def test_fit_returns_none_when_x_has_no_variance():
    samples = [_sample(val.METRIC_EFFORT, 5.0, float(i), activity_id=f"{i}") for i in range(6)]
    assert val.fit(samples) is None


def test_fit_zero_variance_y_gives_r_zero():
    """Y 无方差时不伪造 r=±1，如实记 r=0。"""
    samples = [_sample(val.METRIC_EFFORT, float(i), 7.0, activity_id=f"{i}") for i in range(6)]
    g = val.fit(samples)
    assert g is not None
    assert g.r == 0.0 and g.r2 == 0.0
    assert g.slope == 0.0


def test_evaluate_metric_insufficient_when_x_constant():
    """X 无变化时无法定义斜率 → insufficient，不硬给公式。"""
    samples = [_sample(val.METRIC_EFFORT, 5.0, float(i), activity_id=f"{i}") for i in range(8)]
    result = val.evaluate_metric(val.METRIC_EFFORT, None, samples)
    assert result.status == "insufficient"
    assert result.regression is None
