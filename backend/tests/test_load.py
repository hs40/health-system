"""训练负荷与 ACWR 单元测试。

断言全部来自业务规则（需求 4.1 / 4.4 / 4.5 / 8.x / 9.3），不是实现细节：

| 测试组 | 对应需求 |
|---|---|
| 心率参数与五区 | 5.1 / 5.2 / 5.3 |
| 跑步 Banister TRIMP | 4.2 |
| 力量 volume load | 4.2（外部重量、自重不为零、静力不虚构次数） |
| 标准化 0–100 | 4.1 / 4.5 |
| 汇总顺序 | 4.4（先标准化再求和，不先合并原始负荷） |
| 不可计算 | 4.5（不填 0、不估算） |
| EWMA ACWR | 8.1 / 8.3 / 8.4 |
| 参考区间 | 9.3（边界连续不重叠） |
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.load import (
    SPORT_TIERS,
    acwr_for,
    acwr_range,
    compute_day,
    hr_mod,
    sport_tier,
)
from app.domain.load import sports as sports_mod
from app.domain.load.acwr import ACUTE_DAYS, CHRONIC_DAYS, alpha, ewma_series
from app.domain.load.sports import running as run_mod
from app.domain.load.sports import strength as str_mod
from app.domain.load.standardize import standardize
from app.domain.load.types import (
    VS_INSUFFICIENT,
    VS_UNREGISTERED,
    VS_VERIFIED,
    AcwrState,
    RawLoad,
)

D = dt.date(2026, 10, 14)


def _run(activity_id: str, minutes: float, hr: int) -> dict:
    return {
        "activityId": activity_id,
        "activityType": {"typeKey": "running"},
        "duration": minutes * 60,
        "averageHR": hr,
    }


# ============================================================
# 心率参数与五区（需求 5.1 / 5.2）
# ============================================================
def test_hr_defaults_are_180_and_52() -> None:
    """需求 5.1：最大心率 180、静息心率 52。"""
    assert hr_mod.MAX_HR == 180
    assert hr_mod.REST_HR_BASELINE == 52


def test_zones_are_contiguous_integers() -> None:
    """需求 5.2：边界取整为整数 bpm，且连续不重叠。"""
    zones = hr_mod.build_zones(180, 52)
    assert [z.name for z in zones] == ["Z1", "Z2", "Z3", "Z4", "Z5"]
    for a, b in zip(zones, zones[1:], strict=False):
        assert a.high_bpm == b.low_bpm, "相邻区间必须共用边界"
    for z in zones:
        assert isinstance(z.low_bpm, int)
        assert z.low_bpm <= z.high_bpm


def test_zone_bounds_match_karvonen() -> None:
    """Karvonen：rest + p × (max - rest)，四舍五入。"""
    zones = hr_mod.build_zones(180, 52)
    assert zones[0].low_bpm == 116
    assert zones[-1].high_bpm == 180


def test_hrr_ratio_clamps_and_handles_missing() -> None:
    assert hr_mod.hrr_ratio(None) is None, "缺失心率必须返回 None，不得当 0"
    assert hr_mod.hrr_ratio(52) == 0.0
    assert hr_mod.hrr_ratio(180) == 1.0
    assert hr_mod.hrr_ratio(250) == 1.0, "超上限截断到 1"


# ============================================================
# 跑步 Banister TRIMP（需求 4.2）
# ============================================================
def test_banister_monotonic_in_intensity() -> None:
    """同样 60 分钟，强度越高负荷越大（且非线性）。"""
    low = run_mod.trimp(60, 0.5)
    mid = run_mod.trimp(60, 0.7)
    high = run_mod.trimp(60, 0.9)
    assert low < mid < high
    assert high / low > 3, "指数项应让高强度显著高于低强度"


def test_running_missing_hr_is_not_computable() -> None:
    """需求 4.5：必需字段缺失 → 不可计算，不得当 0。"""
    raw = run_mod.compute(
        {"activityId": "1", "activityType": {"typeKey": "running"}, "duration": 1800}
    )
    assert "not_computable" in raw.inputs
    score = standardize(raw)
    assert score.computed is False


def test_running_missing_duration_is_not_computable() -> None:
    raw = run_mod.compute(
        {"activityId": "2", "activityType": {"typeKey": "running"}, "averageHR": 150}
    )
    assert "not_computable" in raw.inputs


# ============================================================
# 力量 volume load（需求 4.2）
# ============================================================
def test_strength_machine_uses_garmin_volume() -> None:
    """器械：volume(克×次) ÷ 1000 = kg·reps。"""
    act = {
        "activityId": "s1",
        "activityType": {"typeKey": "strength_training"},
        "summarizedExerciseSets": [
            {
                "category": "CURL",
                "subCategory": "DUMBBELL_BICEPS_CURL",
                "sets": 3,
                "reps": 10,
                "volume": 120000,
                "duration": 100000,
            },
        ],
    }
    raw = str_mod.compute(act, body_weight_kg=70.0)
    assert raw.value == pytest.approx(120.0)


def test_strength_bodyweight_is_not_zero() -> None:
    """需求 4.2：自重不因外部负重为零就认定负荷为零。"""
    act = {
        "activityId": "s2",
        "activityType": {"typeKey": "strength_training"},
        "summarizedExerciseSets": [
            {
                "category": "SQUAT",
                "subCategory": "",
                "sets": 3,
                "reps": 10,
                "volume": 0,
                "duration": 60000,
            },
        ],
    }
    raw = str_mod.compute(act, body_weight_kg=70.0)
    # 3 × 10 × 70 × 0.70 = 1470
    assert raw.value == pytest.approx(1470.0, rel=0.01)


def test_strength_isometric_uses_time_not_reps() -> None:
    """需求 4.2：静力用保持时间，不虚构成动态重复次数。"""
    act = {
        "activityId": "s3",
        "activityType": {"typeKey": "strength_training"},
        "summarizedExerciseSets": [
            {
                "category": "PLANK",
                "subCategory": "PLANK",
                "sets": 3,
                "reps": 0,
                "volume": 0,
                "duration": 120000,
            },
        ],
    }
    raw = str_mod.compute(act, body_weight_kg=70.0)
    # duration 已是累计 120 秒：120 × 70 × 0.50 ÷ 3 = 1400（不再乘组数）
    assert raw.value == pytest.approx(1400.0, rel=0.01)
    assert raw.inputs["isometric_sec_per_rep"] == 3.0


def test_strength_unknown_exercise_is_reported() -> None:
    act = {
        "activityId": "s4",
        "activityType": {"typeKey": "strength_training"},
        "summarizedExerciseSets": [
            {
                "category": "NEW_MOVE",
                "subCategory": "SOMETHING",
                "sets": 3,
                "reps": 10,
                "volume": 0,
                "duration": 60000,
            },
        ],
    }
    raw = str_mod.compute(act, body_weight_kg=70.0)
    keys = [u["key"] for u in raw.inputs.get("unknown_exercises", [])]
    assert "NEW_MOVE/SOMETHING" in keys
    assert raw.value == 0.0, "未登记动作默认不计入"


def test_strength_missing_body_weight_blocks_bodyweight_only() -> None:
    """缺体重时：自重不可算，器械仍可算。"""
    act = {
        "activityId": "s5",
        "activityType": {"typeKey": "strength_training"},
        "summarizedExerciseSets": [
            {
                "category": "SQUAT",
                "subCategory": "",
                "sets": 3,
                "reps": 10,
                "volume": 0,
                "duration": 60000,
            },
            {
                "category": "CURL",
                "subCategory": "DUMBBELL_BICEPS_CURL",
                "sets": 3,
                "reps": 10,
                "volume": 120000,
                "duration": 100000,
            },
        ],
    }
    raw = str_mod.compute(act, body_weight_kg=None)
    assert raw.value == pytest.approx(120.0), "器械部分照常计算"
    assert raw.inputs.get("not_computable"), "自重部分必须标记不可计算"


def test_strength_excludes_warmup() -> None:
    act = {
        "activityId": "s6",
        "activityType": {"typeKey": "strength_training"},
        "summarizedExerciseSets": [
            {
                "category": "WARM_UP",
                "subCategory": "STRETCH_QUAD",
                "sets": 3,
                "reps": 0,
                "volume": 0,
                "duration": 60000,
            },
        ],
    }
    raw = str_mod.compute(act, body_weight_kg=70.0)
    assert raw.value == 0.0
    assert raw.inputs["excluded_sets"] == 3


# ============================================================
# 标准化（需求 4.1 / 4.5）
# ============================================================
def test_standardize_uses_reference_load() -> None:
    raw = RawLoad("a", "running", 65.0, "hrr_min", "banister_trimp")
    score = standardize(raw, reference=130.0)
    assert score.computed is True
    assert score.score == pytest.approx(50.0)


def test_standardize_does_not_truncate_over_100() -> None:
    """需求 4.5：超界不截断，而是提示检查参数。"""
    raw = RawLoad("a", "running", 260.0, "hrr_min", "banister_trimp")
    score = standardize(raw, reference=130.0)
    assert score.score > 100
    assert "参考负荷" in score.reason


def test_display_has_minimum_one_for_computed() -> None:
    """需求 4.5：可计算活动展示最低 1 分。"""
    raw = RawLoad("a", "running", 0.05, "hrr_min", "banister_trimp")
    score = standardize(raw, reference=130.0)
    assert score.computed is True
    assert score.display == 1


def test_not_computable_never_gets_display_score() -> None:
    """需求 4.5：不可计算活动不能靠「最低 1 分」变成有效负荷。"""
    raw = RawLoad(
        "a", "running", 0.0, "hrr_min", "banister_trimp", {"not_computable": "缺少平均心率"}
    )
    score = standardize(raw)
    assert score.computed is False
    assert score.display == 0


# ============================================================
# 汇总（需求 4.4）
# ============================================================
def test_two_runs_same_day_are_standardized_then_summed() -> None:
    """同类型两项活动：分别标准化后求和（不是先合并原始负荷）。"""
    acts = [_run("1", 30, 150), _run("2", 30, 150)]
    daily = compute_day(acts, D, body_weight_kg=70.0)
    assert len(daily.contributions) == 2
    assert daily.total == pytest.approx(sum(c.score for c in daily.contributions), rel=1e-6)
    assert daily.by_sport["running"] == pytest.approx(daily.total, rel=1e-6)


def test_multi_sport_day_sums_by_sport_then_total() -> None:
    acts = [
        _run("1", 40, 150),
        {
            "activityId": "2",
            "activityType": {"typeKey": "strength_training"},
            "summarizedExerciseSets": [
                {
                    "category": "SQUAT",
                    "subCategory": "",
                    "sets": 3,
                    "reps": 10,
                    "volume": 0,
                    "duration": 60000,
                },
            ],
        },
    ]
    daily = compute_day(acts, D, body_weight_kg=70.0)
    assert set(daily.by_sport) == {"running", "strength_training"}
    assert daily.total == pytest.approx(sum(daily.by_sport.values()), rel=1e-6)


def test_record_only_and_excluded_do_not_enter_load() -> None:
    """walking 仅记录、meditation 不纳入，都不进负荷。"""
    acts = [
        {
            "activityId": "w",
            "activityType": {"typeKey": "walking"},
            "duration": 3600,
            "averageHR": 100,
        },
        {
            "activityId": "m",
            "activityType": {"typeKey": "meditation"},
            "duration": 1800,
            "averageHR": 70,
        },
    ]
    daily = compute_day(acts, D, body_weight_kg=70.0)
    assert daily.total == 0.0
    assert len(daily.not_computable) == 2


def test_sport_tiers_cover_expected_keys() -> None:
    assert sport_tier("running") == "counted"
    assert sport_tier("walking") == "record_only"
    assert sport_tier("meditation") == "excluded"
    assert sport_tier("breathwork") == "excluded"
    assert sport_tier("skiroing") == "record_only", "未登记类型默认仅记录"


# ============================================================
# 各运动类型独立验证状态（需求 6.2 / 6.3）
# ============================================================
def test_verified_and_insufficient_sports_are_independent() -> None:
    """需求 6.3：一个类型未验证**不改变**其他类型的验证状态。"""
    # running：n=99 / R²=0.51 → 报告结论「存在可识别线性关系」
    assert sports_mod.verification_for("running")[0] == VS_VERIFIED
    # strength_training：n=66 / R²=0.26 → 「存在较弱线性关系」，结果不理想
    assert sports_mod.verification_for("strength_training")[0] == VS_INSUFFICIENT
    assert sports_mod.verification_for("hiking")[0] == VS_INSUFFICIENT
    # 未登记类型 → unregistered，且与「样本不足」区分开
    assert sports_mod.verification_for("skiing")[0] == VS_UNREGISTERED


def test_insufficient_sport_still_gets_score_with_status() -> None:
    """需求 6.3：验证未通过仍展示可计算分值，并披露验证状态（不隐藏）。"""
    acts = [
        _run("r1", 60, 150),
        {
            "activityId": "h1",
            "activityType": {"typeKey": "hiking"},
            "duration": 3600,
            "averageHR": 110,
        },
    ]
    daily = compute_day(acts, D)
    assert daily.total > 0, "未验证类型的负荷照常计入"
    hiking = [c for c in daily.contributions if c.sport == "hiking"]
    assert len(hiking) == 1
    assert hiking[0].computed is True
    assert hiking[0].verification_status == VS_INSUFFICIENT
    assert hiking[0].verification_note


def test_daily_load_exposes_only_unverified_counted_sports() -> None:
    """unverified_sports 只列**计入负荷**且未验证的类型。"""
    acts = [
        _run("r1", 60, 150),
        {
            "activityId": "h1",
            "activityType": {"typeKey": "hiking"},
            "duration": 3600,
            "averageHR": 110,
        },
        {
            "activityId": "w1",
            "activityType": {"typeKey": "walking"},
            "duration": 3600,
            "averageHR": 100,
        },
    ]
    daily = compute_day(acts, D)
    # hiking 计入但未验证 → 出现；running 已验证 → 不出现；walking 不计入 → 不出现
    assert daily.unverified_sports == {"hiking": VS_INSUFFICIENT}
    assert "running" not in daily.unverified_sports
    assert "walking" not in daily.unverified_sports


def test_to_dict_carries_verification_status() -> None:
    """持久化/接口输出必须带验证状态，否则前端无法披露（需求 6.3）。"""
    acts = [
        _run("r1", 60, 150),
        {
            "activityId": "h1",
            "activityType": {"typeKey": "hiking"},
            "duration": 3600,
            "averageHR": 110,
        },
    ]
    dumped = compute_day(acts, D).to_dict()
    assert dumped["unverified_sports"] == {"hiking": VS_INSUFFICIENT}
    hiking = [c for c in dumped["activities"] if c["sport"] == "hiking"]
    assert hiking[0]["verification_status"] == VS_INSUFFICIENT


# ============================================================
# EWMA ACWR（需求 8.1 / 8.3 / 8.4）
# ============================================================
def test_alpha_matches_time_constant() -> None:
    assert alpha(7) == pytest.approx(0.25)
    assert alpha(28) == pytest.approx(2 / 29)


def test_missing_day_counts_as_zero() -> None:
    """需求 8.3：缺失日按 0 参与 EWMA（会被拉低），不是跳过。"""
    loads = {D: 50.0, D - dt.timedelta(days=2): 50.0}
    series = ewma_series(loads, D - dt.timedelta(days=2), D, ACUTE_DAYS)
    d0, d1 = D - dt.timedelta(days=2), D - dt.timedelta(days=1)
    assert series[d1] == pytest.approx(50.0 * (1 - alpha(ACUTE_DAYS))), "缺失日按 0 拉低"
    assert series[d1] < series[d0]


def test_acwr_none_when_chronic_is_zero() -> None:
    """需求 8.4：慢性为 0（完全无训练）→ 不可计算，不伪造数值。"""
    state = acwr_for({}, D)
    assert state.ratio is None
    assert state.sufficient is False
    assert "不可计算" in state.note


def test_acwr_ratio_is_ewma_based() -> None:
    loads = {D - dt.timedelta(days=i): 40.0 for i in range(CHRONIC_DAYS)}
    state = acwr_for(loads, D)
    assert state.ratio == pytest.approx(1.0, abs=0.05), "稳定负荷下 ACWR 应接近 1"


def test_acwr_insufficient_when_few_active_days() -> None:
    loads = {D - dt.timedelta(days=i * 7): 100.0 for i in range(4)}
    state = acwr_for(loads, D)
    assert state.sufficient is False
    assert state.ratio is not None, "仍给出 ratio，但标注仅供参考"


def test_acwr_zone_boundaries_are_continuous() -> None:
    """需求 9.3：0.8 / 1.3 / 1.5 边界连续不重叠。"""

    def zone(r: float) -> str:
        return AcwrState(D, 1.0, 1.0, r).zone

    assert zone(0.79) == "undertraining"
    assert zone(0.80) == "sweet_spot"
    assert zone(1.30) == "sweet_spot"
    assert zone(1.31) == "elevated"
    assert zone(1.50) == "elevated"
    assert zone(1.51) == "danger"
    assert AcwrState(D, 0.0, 0.0, None).zone == "not_available"


def test_acwr_range_covers_every_day() -> None:
    loads = {D - dt.timedelta(days=i): 30.0 for i in range(30)}
    start = D - dt.timedelta(days=6)
    out = acwr_range(loads, start, D)
    assert len(out) == 7
    assert all(d in out for d in (start, D))


def test_sp_tiers_single_source() -> None:
    """分类表只有一处定义，避免漂移。"""
    assert SPORT_TIERS["running"] == "counted"
