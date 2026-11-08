"""P8 力量 notes 生成器测试。

移植的难点不在「算法」，而在**逐字对齐 JS**——差一个字符，``--parity`` 就报不一致，
65 条历史 notes 就再也无法证明「Python 版与 Node 版等价」。所以这里的用例集中在
三类最容易漂移的地方：

1. **数字渲染**：``String(4.0) === "4"``，而 Python ``str(4.0) === "4.0"``。
2. **取整规则**：``Math.round`` 平局向上，Python ``round`` 是银行家舍入；
   ``toFixed(2)`` 还要按**二进制真值**取整（``2.675`` 得 ``2.67`` 而非 ``2.68``）。
3. **剔除与覆盖**：热身、``excludeSubs``、逐次 ``moves`` 覆盖、单侧表达。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.strength.notes import (
    NOTES_HEADER,
    build_strength_notes,
    data_fingerprints,
    decode_sub,
    fit_groups,
    fmt_per_set,
    js_fixed_number,
    js_number,
    js_round,
    load_corrections,
    load_moves,
    prettify,
    truncate_notes,
)

MOVES = {
    "LUNGE/SIDE_LUNGE": "侧弓步",
    "LUNGE/": "弓箭步",
    "CURL/DUMBBELL_BICEPS_CURL": "哑铃弯举",
    "PLANK/": "平板支撑",
}
CORR: dict = {
    "excludeSubs": ["HIP_STABILITY/HIP_CIRCLES"],
    "singleSide": ["LUNGE/"],
    "activities": {},
}


def _set(cat: str, sub: str, reps: float, sets: int = 1, weight: float = 0.0) -> dict:
    """Garmin ``summarizedExerciseSets`` 的一条。"""
    return {
        "category": cat,
        "subCategory": sub,
        "reps": reps,
        "sets": sets,
        "duration": 0.0,
        "maxWeight": weight,
    }


def _fit(cat: str, sub: str, reps: list[float], start: str | None = None) -> list[dict]:
    """构造 FIT ``set`` 消息（``category`` / ``category_subtype`` 都是二元数组）。"""
    rows = []
    for r in reps:
        rows.append(
            {
                "set_type": "active",
                "category": [cat.lower(), cat.lower()],
                "category_subtype": [0, 0],
                "repetitions": r,
                "duration": 0.0,
                "weight": 0.0,
                "start_time": start,
            }
        )
    return rows


# ============================================================
# 数字渲染
# ============================================================
@pytest.mark.parametrize(
    ("value", "want"),
    [
        (4.0, "4"),
        (4.5, "4.5"),
        (0.0, "0"),
        (10, "10"),
        (2.25, "2.25"),
    ],
)
def test_js_number(value: float, want: str) -> None:
    assert js_number(value) == want


@pytest.mark.parametrize(
    ("value", "want"),
    [(30.5, 31), (30.4, 30), (0.5, 1), (1.5, 2), (2.5, 3)],
)
def test_js_round_rounds_half_up(value: float, want: int) -> None:
    """JS ``Math.round(2.5) === 3``；Python ``round(2.5) === 2``（银行家舍入）。"""
    assert js_round(value) == want


@pytest.mark.parametrize(
    ("value", "want"),
    [
        (8.0, "8"),  # +(8).toFixed(2) === 8 → "8"
        (7.5, "7.5"),
        (1.125, "1.13"),  # 平局向上
        (2.675, "2.67"),  # 二进制真值 2.674999… 向下
        (0.005, "0.01"),
    ],
)
def test_js_fixed_number_matches_tofixed(value: float, want: str) -> None:
    assert js_fixed_number(value, 2) == want


def test_python_round_would_have_been_wrong() -> None:
    """反证：直接用 ``round`` 会与 JS 不一致，所以必须走 ``js_fixed_number``。"""
    assert round(1.125, 2) == 1.12
    assert js_fixed_number(1.125, 2) == "1.13"


# ============================================================
# 枚举解码 / 分组
# ============================================================
def test_decode_sub_uses_official_profile_table() -> None:
    assert decode_sub("warm_up", 13) == "NECK_ROTATIONS"
    assert decode_sub("lunge", 0) == "OVERHEAD_LUNGE"


def test_decode_sub_returns_empty_for_unknown() -> None:
    assert decode_sub("no_such_category", 1) == ""
    assert decode_sub(None, 1) == ""
    assert decode_sub("warm_up", None) == ""


def test_prettify_falls_back_to_readable_lowercase() -> None:
    assert prettify("SIDE_LYING_LEG_RAISE") == "side lying leg raise"
    assert prettify(None) == ""


def test_fit_groups_keeps_only_active_and_takes_earliest_start() -> None:
    rows = _fit("LUNGE", "SIDE_LUNGE", [10, 8], start="2026-10-15T09:20:00+00:00")
    rows.append(
        {
            "set_type": "rest",
            "category": ["lunge", "lunge"],
            "category_subtype": [0, 0],
            "repetitions": 0,
            "duration": 60.0,
            "weight": 0.0,
            "start_time": "2026-10-15T09:19:00+00:00",
        }
    )
    groups = fit_groups(rows)

    assert list(groups) == ["LUNGE/OVERHEAD_LUNGE"]
    group = groups["LUNGE/OVERHEAD_LUNGE"]
    assert [x["reps"] for x in group["sets"]] == [10.0, 8.0]
    # rest 组被剔除，故 start 仍取 active 组的时间
    expected = dt.datetime(2026, 10, 15, 9, 20, tzinfo=dt.UTC).timestamp()
    assert group["start"] == pytest.approx(expected)


def test_fit_groups_without_start_time_sorts_last() -> None:
    groups = fit_groups(_fit("LUNGE", "SIDE_LUNGE", [10], start=None))
    assert groups["LUNGE/OVERHEAD_LUNGE"]["start"] == float("inf")


# ============================================================
# 逐组文本
# ============================================================
def test_fmt_per_set_double_side_all_equal() -> None:
    assert fmt_per_set([10, 10, 10, 10], "次", False) == "10次 × 4组"


def test_fmt_per_set_single_value() -> None:
    assert fmt_per_set([10], "次", False) == "10次"


def test_fmt_per_set_double_side_unequal() -> None:
    assert fmt_per_set([9, 9, 9, 5], "次", False) == "9 / 9 / 9 / 5 次"


def test_fmt_per_set_single_side_halves_the_set_count() -> None:
    """单侧动作每组记两条（左/右），故组数 = round(n/2)。"""
    assert fmt_per_set([8, 8, 8, 8], "次", True) == "8次/侧 × 2组"


def test_fmt_per_set_single_side_unequal_lists_every_rep() -> None:
    assert fmt_per_set([6, 8], "次", True) == "6次/侧 8次/侧"


def test_fmt_per_set_duration_unit() -> None:
    assert fmt_per_set([30, 30, 30], "s", False) == "30s × 3组"


# ============================================================
# 主函数
# ============================================================
def test_returns_empty_when_nothing_to_write() -> None:
    assert build_strength_notes("1", [], [], moves=MOVES, corrections=CORR) == ""
    assert build_strength_notes("1", None, None, moves=MOVES, corrections=CORR) == ""


def test_warm_up_is_always_dropped() -> None:
    sets = [_set("WARM_UP", "STRETCH_CALF", 0), _set("LUNGE", "SIDE_LUNGE", 10, 3)]
    notes = build_strength_notes("1", sets, [], moves=MOVES, corrections=CORR)
    assert notes == NOTES_HEADER + "侧弓步 共10次（3组，缺逐组明细）"


def test_exclude_subs_is_dropped() -> None:
    sets = [_set("HIP_STABILITY", "HIP_CIRCLES", 10, 2)]
    assert build_strength_notes("1", sets, [], moves=MOVES, corrections=CORR) == ""


def test_per_activity_exclude_is_dropped() -> None:
    corr = {**CORR, "activities": {"9": {"exclude": ["LUNGE/SIDE_LUNGE"]}}}
    sets = [_set("LUNGE", "SIDE_LUNGE", 10, 2)]
    assert build_strength_notes("9", sets, [], moves=MOVES, corrections=corr) == ""


def test_per_activity_override_wins_verbatim() -> None:
    corr = {**CORR, "activities": {"9": {"moves": {"LUNGE/SIDE_LUNGE": "自定义：侧向弓步 3 组"}}}}
    sets = [_set("LUNGE", "SIDE_LUNGE", 10, 3)]
    notes = build_strength_notes("9", sets, [], moves=MOVES, corrections=corr)
    assert notes == NOTES_HEADER + "自定义：侧向弓步 3 组"


def test_weight_comes_from_max_weight_in_grams() -> None:
    sets = [_set("LUNGE", "SIDE_LUNGE", 10, 3, weight=8000)]
    notes = build_strength_notes("1", sets, [], moves=MOVES, corrections=CORR)
    assert notes.endswith("@8kg")


def test_weight_with_half_kilo() -> None:
    sets = [_set("LUNGE", "SIDE_LUNGE", 10, 3, weight=7500)]
    notes = build_strength_notes("1", sets, [], moves=MOVES, corrections=CORR)
    assert notes.endswith("@7.5kg")


def test_fit_reps_override_summary_distribution() -> None:
    """汇总只说「10 次 3 组」，真实逐组分布要由 FIT 决定。"""
    sets = [_set("LUNGE", "SIDE_LUNGE", 10, 3)]
    fit = _fit("LUNGE", "SIDE_LUNGE", [10, 10, 8])
    notes = build_strength_notes("1", sets, fit, moves=MOVES, corrections=CORR)
    assert notes == NOTES_HEADER + "侧弓步 10 / 10 / 8 次"


def test_fit_weight_used_when_summary_has_none() -> None:
    sets = [_set("LUNGE", "SIDE_LUNGE", 10, 2)]
    fit = _fit("LUNGE", "SIDE_LUNGE", [10, 10])
    for row in fit:
        row["weight"] = 12.0
    notes = build_strength_notes("1", sets, fit, moves=MOVES, corrections=CORR)
    assert notes == NOTES_HEADER + "侧弓步 10次 × 2组 @12kg"


def test_falls_back_to_summary_when_fit_missing() -> None:
    sets = [_set("LUNGE", "SIDE_LUNGE", 10, 3)]
    assert build_strength_notes("1", sets, [], moves=MOVES, corrections=CORR).endswith(
        "侧弓步 共10次（3组，缺逐组明细）"
    )


def test_falls_back_to_duration_when_no_reps() -> None:
    sets = [
        {
            "category": "PLANK",
            "subCategory": "",
            "reps": 0,
            "sets": 4,
            "duration": 30500.0,
            "maxWeight": 0,
        }
    ]
    notes = build_strength_notes("1", sets, [], moves=MOVES, corrections=CORR)
    assert notes == NOTES_HEADER + "平板支撑 共31s（4组，缺逐组明细）"


def test_entries_sorted_by_fit_start_and_unmatched_last() -> None:
    """有 FIT 的按 FIT 起始时间排；没有 FIT 的（start=inf）排最后。"""
    sets = [_set("LUNGE", "SIDE_LUNGE", 10, 1), _set("CURL", "DUMBBELL_BICEPS_CURL", 8, 1)]
    fit = _fit("LUNGE", "SIDE_LUNGE", [10], start="2026-10-15T10:00:00+00:00")
    notes = build_strength_notes("1", sets, fit, moves=MOVES, corrections=CORR)
    assert notes == NOTES_HEADER + "侧弓步 10次；哑铃弯举 共8次（1组，缺逐组明细）"


def test_entries_reordered_by_fit_start() -> None:
    """汇总顺序与训练顺序不一致时，以 FIT 时间为准。"""
    sets = [_set("CURL", "DUMBBELL_BICEPS_CURL", 8, 1), _set("LUNGE", "SIDE_LUNGE", 10, 1)]
    fit = _fit("LUNGE", "SIDE_LUNGE", [10], start="2026-10-15T10:00:00+00:00")
    fit += _fit("CURL", "DUMBBELL_BICEPS_CURL", [8], start="2026-10-15T11:00:00+00:00")
    notes = build_strength_notes("1", sets, fit, moves=MOVES, corrections=CORR)
    assert notes == NOTES_HEADER + "侧弓步 10次；哑铃弯举 8次"


def test_falls_back_to_category_name_when_sub_missing() -> None:
    sets = [_set("LUNGE", "", 10, 2)]
    notes = build_strength_notes("1", sets, [], moves=MOVES, corrections=CORR)
    assert notes == NOTES_HEADER + "弓箭步 共10次（2组，缺逐组明细）"


def test_truncate_notes_cuts_to_limit() -> None:
    assert truncate_notes("力量明细：abc", 5) == "力量明细："
    assert truncate_notes("abc", 10) == "abc"


# ============================================================
# 无 FIT 兜底：不得把「总次数」冒充「每组次数」（2026-10-26 回归）
# ============================================================
def test_fallback_states_total_not_per_set_when_no_fit() -> None:
    """无 FIT 逐组明细时，汇总 reps 是「N 组总次数」，不是「每组次数」。

    用户实测 10-27「宽距平板鸟狗式」：汇总 reps=60、sets=3，正确语义是
    3 组共 60 次；旧兜底输出「60次 × 3组」把 60 当成每组次数（错）。
    C 层改为不误导的总量表述，且绝不输出伪造的每组值。
    """
    sets = [
        {
            "category": "PLANK",
            "subCategory": "WIDE_STANCE_PLANK_WITH_OPPOSITE_ARM_AND_LEG_LIFT",
            "reps": 60,
            "sets": 3,
            "duration": 0,
            "maxWeight": 0,
        }
    ]
    notes = build_strength_notes("1", sets, [], moves=MOVES, corrections=CORR)
    assert "共60次（3组，缺逐组明细）" in notes
    assert "60次 × 3组" not in notes


def test_fallback_never_invents_per_set_for_non_divisible_total() -> None:
    """23 次 / 3 组、70 次 / 6 组都不整除：绝不输出 7.67 这类伪造每组值。"""
    sets = [
        {"category": "CURL", "subCategory": "DUMBBELL_FLY", "reps": 23,
         "sets": 3, "duration": 0, "maxWeight": 8000},
        {"category": "CURL", "subCategory": "DUMBBELL_BICEPS_CURL", "reps": 70,
         "sets": 6, "duration": 0, "maxWeight": 4000},
    ]
    notes = build_strength_notes("1", sets, [], moves=MOVES, corrections=CORR)
    assert "共23次（3组，缺逐组明细） @8kg" in notes
    assert "共70次（6组，缺逐组明细） @4kg" in notes
    assert "× 3组" not in notes and "× 6组" not in notes
    assert "7.67" not in notes and "11.67" not in notes


def test_fallback_single_side_uses_total_without_fake_per_side() -> None:
    """单侧动作无逐组数据时，不编造「N次/侧 × M组」（会伪装成实测每组值）。"""
    sets = [_set("LUNGE", "SIDE_LUNGE", 60, 3)]  # LUNGE/ 在 singleSide
    notes = build_strength_notes("1", sets, [], moves=MOVES, corrections=CORR)
    assert "共60次（3组，缺逐组明细）" in notes
    assert "/侧" not in notes


def test_fallback_source_never_formats_total_as_per_set() -> None:
    """静态防线：兜底分支不得把总量塞回「N次 × N组」的每组位置（防回退）。"""
    import inspect

    import app.domain.strength.notes as mod

    src = inspect.getsource(mod)
    assert "缺逐组明细" in src
    assert 'f"{js_number(reps)}次 × {js_number(n_sets)}组"' not in src
    assert 'f"{js_number(seconds)}s × {js_number(n_sets)}组"' not in src


# ============================================================
# 随包数据
# ============================================================
def test_packaged_data_files_are_loadable() -> None:
    moves = load_moves()
    corr = load_corrections()
    assert len(moves) > 50
    assert "LUNGE/SIDE_LUNGE" in moves
    assert "excludeSubs" in corr and "singleSide" in corr


def test_data_fingerprints_are_short_sha256() -> None:
    fp = data_fingerprints()
    assert set(fp) == {"moves_zh.json", "corrections.json"}
    assert all(len(v) == 16 for v in fp.values())
