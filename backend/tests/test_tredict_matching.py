"""P6 匹配引擎测试（纯函数层）。

覆盖三处最容易错的地方：
1. **类型映射**：``misc`` 是兜底桶，其下混着 meditation / breathwork / hiit，
   不能用等号判定，必须走白名单。
2. **时长口径**：游泳取 ``durationTotal``，其余取 ``duration``——用反了
   游泳全差 2000+ 秒、力量训练全差 1000+ 秒。
3. **定级规则**：时长超差只降 ``medium``，**绝不判 none**（Q11）；
   重复占用要暴露成 ``ambiguous``，不能按遍历顺序随意分配。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.matching import (
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_NONE,
    GENERIC_GARMIN,
    SPORT_MAP,
    GarminRef,
    TredictRef,
    garmin_type_for,
    match_activities,
    pick_duration,
    summarize,
    types_compatible,
    unmatched_garmin,
)

BASE = dt.datetime(2026, 10, 12, 9, 59, 32)


def g(
    aid: str = "G1",
    offset_sec: float = 0,
    activity_type: str = "running",
    duration: float | None = 2600,
) -> GarminRef:
    return GarminRef(
        activity_id=aid,
        start_utc=BASE + dt.timedelta(seconds=offset_sec),
        activity_type=activity_type,
        duration_sec=duration,
    )


def t(
    tid: str = "T1",
    offset_sec: float = 0,
    sport: str = "running",
    sub: str = "generic",
    duration: float | None = 2600,
    duration_total: float | None = 2600,
) -> TredictRef:
    return TredictRef(
        tredict_id=tid,
        start_utc=BASE + dt.timedelta(seconds=offset_sec),
        sport_type=sport,
        sub_sport_type=sub,
        duration_sec=duration,
        duration_total_sec=duration_total,
    )


# ============================================================
# 类型映射
# ============================================================
@pytest.mark.parametrize(
    ("sport", "sub", "want"),
    [
        ("running", "generic", "running"),
        ("swimming", "lap_swimming", "lap_swimming"),
        ("misc", "walking", "walking"),
        ("misc", "strength_training", "strength_training"),
        ("misc", "hiking", "hiking"),
        ("misc", "cardio_training", "indoor_cardio"),
    ],
)
def test_sport_map_covers_observed_pairs(sport: str, sub: str, want: str) -> None:
    assert garmin_type_for(sport, sub) == want


def test_unmapped_falls_back_to_misc() -> None:
    assert garmin_type_for("cycling", "road") == "misc"
    assert garmin_type_for("", "") == "misc"


def test_every_map_value_is_a_garmin_type_key() -> None:
    """映射值必须能在 Garmin 的 typeKey 集合里找到，防止手滑写错。"""
    observed = {
        "running",
        "walking",
        "hiking",
        "strength_training",
        "lap_swimming",
        "indoor_cardio",
        "meditation",
        "breathwork",
        "hiit",
    }
    assert set(SPORT_MAP.values()) <= observed


@pytest.mark.parametrize("garmin_type", sorted(GENERIC_GARMIN))
def test_generic_bucket_accepts_known_types(garmin_type: str) -> None:
    assert types_compatible(garmin_type, "misc", "generic") is True


def test_generic_bucket_rejects_unrelated_type() -> None:
    assert types_compatible("running", "misc", "generic") is False


def test_same_sport_different_sub_is_mismatch() -> None:
    # Tredict 说是步行，Garmin 说是跑步 → 类型不符
    assert types_compatible("running", "misc", "walking") is False


# ============================================================
# 时长口径
# ============================================================
def test_swim_uses_duration_total() -> None:
    ref = t(sport="swimming", sub="lap_swimming", duration=2036, duration_total=4800)
    assert pick_duration(ref) == (4800, "durationTotal")


def test_non_swim_uses_duration() -> None:
    ref = t(sport="misc", sub="strength_training", duration=3600, duration_total=5795)
    assert pick_duration(ref) == (3600, "duration")


def test_swim_duration_choice_decides_match() -> None:
    """2025-07-26 实测：duration 2036 / durationTotal 4800，Garmin 4800。

    用错字段会从 high 掉到 medium，这条测试锁死口径。
    """
    garmin = [g("G-swim", activity_type="lap_swimming", duration=4800)]
    tredict = [
        t("T-swim", sport="swimming", sub="lap_swimming", duration=2036, duration_total=4800)
    ]
    out = match_activities(garmin, tredict)[0]
    assert out.confidence == CONFIDENCE_HIGH
    assert out.detail["duration_field"] == "durationTotal"
    assert out.detail["duration_delta"] == 0


# ============================================================
# 定级
# ============================================================
def test_exact_match_is_high() -> None:
    out = match_activities([g()], [t()])[0]
    assert out.garmin_id == "G1"
    assert out.confidence == CONFIDENCE_HIGH
    assert out.detail["time_delta_sec"] == 0


def test_within_two_seconds_is_high() -> None:
    out = match_activities([g()], [t(offset_sec=2)])[0]
    assert out.confidence == CONFIDENCE_HIGH


def test_beyond_two_seconds_is_none() -> None:
    out = match_activities([g()], [t(offset_sec=3)])[0]
    assert out.confidence == CONFIDENCE_NONE
    assert out.garmin_id is None
    assert out.detail["reason"] == "no_candidate"


def test_no_garmin_at_all_is_none() -> None:
    out = match_activities([], [t()])[0]
    assert out.confidence == CONFIDENCE_NONE


def test_duration_delta_degrades_to_medium_not_none() -> None:
    """Q11：时长不符绝不判 none——否则一次时长口径变化就会丢配对。"""
    out = match_activities([g(duration=2600)], [t(duration=2607)])[0]
    assert out.confidence == CONFIDENCE_MEDIUM
    assert out.garmin_id == "G1"
    assert out.detail["duration_delta"] == 7


def test_duration_exactly_at_tolerance_is_high() -> None:
    out = match_activities([g(duration=2600)], [t(duration=2605)])[0]
    assert out.confidence == CONFIDENCE_HIGH


def test_type_mismatch_degrades_to_medium() -> None:
    garmin = [g(activity_type="running", duration=2600)]
    tredict = [t(sport="misc", sub="walking", duration=2600)]
    out = match_activities(garmin, tredict)[0]
    assert out.confidence == CONFIDENCE_MEDIUM
    assert out.detail["type_mismatch"]["garmin"] == "running"
    assert out.detail["type_mismatch"]["mapped"] == "walking"


def test_missing_duration_degrades_to_medium() -> None:
    out = match_activities([g(duration=None)], [t()])[0]
    assert out.confidence == CONFIDENCE_MEDIUM
    assert out.detail["duration_missing"] is True


def test_multiple_candidates_is_ambiguous() -> None:
    garmin = [g("G1", offset_sec=0), g("G2", offset_sec=1)]
    out = match_activities(garmin, [t()])[0]
    assert out.confidence == CONFIDENCE_AMBIGUOUS
    assert out.garmin_id is None
    assert out.detail["candidates"] == ["G1", "G2"]


def test_duplicate_claim_is_ambiguous_for_all() -> None:
    """两条 Tredict 抢同一 Garmin → 双方都 ambiguous，不按顺序分配。"""
    out = match_activities([g("G1")], [t("T1"), t("T2", offset_sec=1)])
    assert [o.confidence for o in out] == [CONFIDENCE_AMBIGUOUS, CONFIDENCE_AMBIGUOUS]
    assert all(o.garmin_id is None for o in out)
    assert out[0].detail["reason"] == "duplicate_claim"
    assert set(out[0].detail["claimants"]) == {"T1", "T2"}


def test_ordering_does_not_change_result() -> None:
    """候选按时间排序查找，输入顺序不应影响结论。"""
    garmin = [g("G2", offset_sec=3600), g("G1", offset_sec=0)]
    tredict = [t("T2", offset_sec=3600), t("T1", offset_sec=0)]
    out = {o.tredict_id: o.garmin_id for o in match_activities(garmin, tredict)}
    assert out == {"T1": "G1", "T2": "G2"}


def test_near_miss_pairs_are_not_confused() -> None:
    """2026-10-04 三条步行只差 68s 时长，但开始时间分离 → 各自唯一。"""
    garmin = [
        g("G-a", offset_sec=0, activity_type="walking", duration=2099),
        g("G-b", offset_sec=600, activity_type="walking", duration=2167),
        g("G-c", offset_sec=1200, activity_type="walking", duration=2067),
    ]
    tredict = [
        t("T-a", offset_sec=0, sport="misc", sub="walking", duration=2099),
        t("T-b", offset_sec=600, sport="misc", sub="walking", duration=2167),
        t("T-c", offset_sec=1200, sport="misc", sub="walking", duration=2067),
    ]
    out = {o.tredict_id: (o.garmin_id, o.confidence) for o in match_activities(garmin, tredict)}
    assert out == {
        "T-a": ("G-a", CONFIDENCE_HIGH),
        "T-b": ("G-b", CONFIDENCE_HIGH),
        "T-c": ("G-c", CONFIDENCE_HIGH),
    }


# ============================================================
# 汇总
# ============================================================
def test_summarize_counts_by_level() -> None:
    garmin = [g("G1"), g("G2", offset_sec=100)]
    tredict = [t("T1"), t("T2", offset_sec=100, duration=9999), t("T3", offset_sec=999)]
    stats = summarize(match_activities(garmin, tredict))
    assert stats == {
        CONFIDENCE_HIGH: 1,
        CONFIDENCE_MEDIUM: 1,
        CONFIDENCE_AMBIGUOUS: 0,
        CONFIDENCE_NONE: 1,
    }


def test_unmatched_garmin_lists_only_unclaimed() -> None:
    garmin = [g("G1"), g("G2", offset_sec=500)]
    tredict = [t("T1")]
    assert unmatched_garmin(garmin, match_activities(garmin, tredict)) == ["G2"]
