"""Garmin 官方动作肌群查询（三期 §8.4，Q9/Q10/Q11 口径）。

期望值来自 Garmin 官方 ``garmin_exercises.json`` 原文（已逐条核验），
不是调用被测模块重算：键格式 ``CATEGORY/SUBCATEGORY`` 与项目动作库一致。
"""

from __future__ import annotations

from app.domain.strength.muscles import lookup


def test_lookup_exact_exercise_returns_primary_and_secondary() -> None:
    # 原文：BENCH_PRESS/DUMBBELL_BENCH_PRESS
    #   primaryMuscles   = ["CHEST"]
    #   secondaryMuscles = ["SHOULDERS", "TRICEPS"]
    hit = lookup("BENCH_PRESS/DUMBBELL_BENCH_PRESS")
    assert [m.code for m in hit.primary] == ["CHEST"]
    assert [m.code for m in hit.secondary] == ["SHOULDERS", "TRICEPS"]
    assert hit.source == "exercise"


def test_lookup_falls_back_to_category_and_drops_empty_muscles() -> None:
    # 类别兜底：原文 SQUAT 类别级 primary=[QUADS, GLUTES]、secondary=[HAMSTRINGS]
    hit = lookup("SQUAT/")
    assert [m.code for m in hit.primary] == ["QUADS", "GLUTES"]
    assert [m.code for m in hit.secondary] == ["HAMSTRINGS"]
    assert hit.source == "category"

    # 无数据：WARM_UP 类别既无动作级也无类别级肌群定义
    none_hit = lookup("WARM_UP/")
    assert none_hit.primary == ()
    assert none_hit.secondary == ()
    assert none_hit.source == "none"

    # 脏数据：官方 secondary 共 7 项含 1 个空串，不得进入结果
    dirty = lookup("PLANK/WEIGHTED_SIDE_PLANK_LIFT")
    assert [m.code for m in dirty.primary] == ["OBLIQUES"]
    assert [m.code for m in dirty.secondary] == [
        "ABS",
        "GLUTES",
        "SHOULDERS",
        "LOWER_BACK",
        "ADDUCTORS",
        "ABDUCTORS",
    ]


def test_lookup_exposes_chinese_label_with_english_sublabel() -> None:
    from app.domain.strength.muscles import official_muscles

    # 原文 CORE/RUSSIAN_TWIST：primary=[OBLIQUES]、secondary=[ABS, HIPS]
    hit = lookup("CORE/RUSSIAN_TWIST")
    assert [(m.code, m.zh, m.en) for m in hit.primary] == [
        ("OBLIQUES", "腹斜肌", "obliques")
    ]
    assert [(m.zh, m.en) for m in hit.secondary] == [
        ("腹直肌", "abs"),
        ("髋屈肌", "hip flexors"),
    ]

    # 官方 17 个有效肌群枚举全部有中文标签，英文小字一律小写（Q10 肌肉图灰显用）
    muscles = official_muscles()
    assert len(muscles) == 17
    assert all(m.zh and m.en == m.en.lower() for m in muscles)


def test_aggregate_groups_moves_by_muscle_with_primary_secondary_split() -> None:
    from app.domain.strength.muscles import aggregate

    # 原文：卧推 primary=CHEST / secondary=SHOULDERS+TRICEPS
    #       俄罗斯转体 primary=OBLIQUES / secondary=ABS+HIPS
    #       WARM_UP/ 无肌群数据，不应产生表格行
    rows = aggregate(
        [
            ("BENCH_PRESS/DUMBBELL_BENCH_PRESS", "哑铃卧推"),
            ("CORE/RUSSIAN_TWIST", "俄罗斯转体"),
            ("WARM_UP/", "热身"),
        ]
    )
    assert [(r.muscle.code, r.primary_moves, r.secondary_moves) for r in rows] == [
        ("CHEST", ("哑铃卧推",), ()),
        ("SHOULDERS", (), ("哑铃卧推",)),
        ("TRICEPS", (), ("哑铃卧推",)),
        ("OBLIQUES", ("俄罗斯转体",), ()),
        ("ABS", (), ("俄罗斯转体",)),
        ("HIPS", (), ("俄罗斯转体",)),
    ]
