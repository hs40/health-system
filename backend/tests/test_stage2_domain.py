"""阶段② 纯域模块测试（Plan §5 T2.8）：季节策略 / 环境硬约束 / 跨源冲突检测。

这些模块**无 IO**，断言可直接对应业务规则（§3.5/§4.5/§4.6/§13.7）。
"""

from __future__ import annotations

from datetime import date

from app.domain.environment_rules import (
    ConstraintKind,
    evaluate_environment,
)
from app.domain.season_policy import Season, build_season_policy, infer_season


# ============================================================
# T2.3 季节策略
# ============================================================
def test_infer_season_by_month():
    assert infer_season(date(2026, 4, 1)) == Season.SPRING
    assert infer_season(date(2026, 7, 1)) == Season.SUMMER
    assert infer_season(date(2026, 10, 1)) == Season.AUTUMN
    assert infer_season(date(2026, 1, 1)) == Season.WINTER
    assert infer_season(date(2026, 12, 31)) == Season.WINTER


def test_infer_season_override():
    assert infer_season(date(2026, 7, 1), override=Season.WINTER) == Season.WINTER


def test_all_seasons_have_policy():
    for m in range(1, 13):
        p = build_season_policy(date(2026, m, 15))
        assert p.season
        assert p.focus
        assert p.rationale
        assert p.version


def test_no_fixed_ratio_preset_for_winter():
    """§4.5：不预设固定训练比例 —— 冬季资源分配为动态（None）。"""
    p = build_season_policy(date(2026, 1, 15))
    assert p.season == Season.WINTER.value
    assert p.endurance_ratio is None
    assert p.strength_ratio is None
    assert p.recovery_ratio is None


def test_season_policy_version_override():
    p = build_season_policy(date(2026, 4, 1), version="season-9.9.9")
    assert p.version == "season-9.9.9"
    assert p.season == Season.SPRING.value  # 内容不变


def test_priority_is_permutation_of_known_goals():
    valid = {"cardio", "fatLoss", "muscleEndurance", "maxStrength"}
    for m in range(1, 13):
        p = build_season_policy(date(2026, m, 15))
        assert set(p.long_term_priority) == valid


# ============================================================
# T2.4 硬性环境约束
# ============================================================
def test_rain_forces_indoor_hard():
    d = evaluate_environment(temp_c=20.0, is_rain=True)
    assert d.indoor_required is True
    assert any(c.kind == ConstraintKind.RAIN_SNOW_INDOOR and c.hard for c in d.constraints)


def test_snow_forces_indoor_hard():
    d = evaluate_environment(temp_c=20.0, is_snow=True)
    assert d.indoor_required is True


def test_temp_below_5_forces_indoor_hard():
    d = evaluate_environment(temp_c=4.0)
    assert d.indoor_required is True
    assert any(c.kind == ConstraintKind.TEMP_LOW_INDOOR and c.hard for c in d.constraints)


def test_temp_exactly_5_is_not_hard():
    """恰等于 5°C 不触发（严格小于）。"""
    d = evaluate_environment(temp_c=5.0)
    assert d.indoor_required is False


def test_normal_weather_no_constraint():
    d = evaluate_environment(temp_c=18.0)
    assert d.constraints == ()
    assert d.indoor_required is False
    assert d.weather_available is True


def test_high_temp_is_advisory_only():
    """高温为**建议性**，非硬性（不得强制室内）。"""
    d = evaluate_environment(temp_c=35.0)
    assert d.indoor_required is False
    assert any(c.kind == ConstraintKind.TEMP_HIGH_ADVISORY and not c.hard for c in d.constraints)


def test_weather_unavailable_does_not_fabricate():
    """天气不可用 → 标注缺失、不编造、不强制室内、不放宽安全。"""
    d = evaluate_environment(temp_c=None, weather_available=False)
    assert d.weather_available is False
    assert d.indoor_required is False
    assert any(c.kind == ConstraintKind.WEATHER_UNAVAILABLE for c in d.constraints)


# ============================================================
# T2.5 跨源冲突检测
# ============================================================