"""运动训练详情纯计算（三期 §7/§8，Q4/Q12 口径）。

期望值来自需求文档 §8 的六类模板划分与 Garmin 真实字段，不调用被测模块重算。
"""

from __future__ import annotations

from app.domain.training import style_of, template_of


def test_style_and_template_cover_six_templates_and_extend_to_unknown_types() -> None:
    # 需求 §8：跑步 / 室内有氧 / HIIT / 力量 / 游泳 / 其他 六类详情模板
    assert template_of("running") == "running"
    assert template_of("indoor_cardio") == "indoor_cardio"
    assert template_of("hiit") == "hiit"
    assert template_of("strength_training") == "strength"
    assert template_of("lap_swimming") == "swimming"
    assert template_of("walking") == "other"

    style = style_of("running")
    assert style["label"] == "跑步"
    assert style["emoji"] == "🏃"
    assert style["color"].startswith("#")

    # §7.2「未来 Garmin 增加新运动类型也应可扩展」：不虚构中文名，走「其他」模板
    unknown = style_of("future_garmin_sport")
    assert unknown["label"] == "future_garmin_sport"
    assert unknown["template"] == "other"
    assert unknown["color"].startswith("#")


def test_format_pace_and_training_effect_labels() -> None:
    from app.domain.training import format_pace, training_effect

    # Q4：统一「分'秒"/km」；Garmin 字段是小数分钟（10.88 → 10'53"，6.84 → 6'50"）
    assert format_pace(10.88) == "10'53\"/km"
    assert format_pace(6.84) == "6'50\"/km"
    assert format_pace(0) is None
    assert format_pace(None) is None

    # §8.1 训练效果中文标签（真实枚举 AEROBIC_BASE / MAINTAINING_AEROBIC_BASE_*）
    eff = training_effect(
        {
            "trainingEffectLabel": "AEROBIC_BASE",
            "aerobicTrainingEffectMessage": "MAINTAINING_AEROBIC_BASE_7",
            "anaerobicTrainingEffectMessage": "NO_ANAEROBIC_BENEFIT_0",
        }
    )
    assert eff["label"] == "有氧基础"
    assert eff["aerobic"] == "维持有氧基础"
    assert eff["anaerobic"] == "无无氧收益"

    # 缺字段不虚构
    assert training_effect({}) == {
        "label": "未知",
        "label_raw": None,
        "aerobic": None,
        "anaerobic": None,
    }


def test_build_hr_series_segments_and_strength_timeline() -> None:
    from app.domain.training import (
        build_hr_series,
        build_segments,
        build_strength_timeline,
    )

    # §8.1 时间—心率折线图：缺失心率保留 None，不补 0（Q8）
    series = build_hr_series(
        [
            {"timestamp": "2026-11-05T10:00:00+00:00", "heart_rate": 120},
            {"timestamp": "2026-11-05T10:00:01+00:00", "heart_rate": None},
            {"timestamp": "2026-11-05T10:00:02+00:00", "heart_rate": 135},
        ]
    )
    assert [p["hr"] for p in series] == [120, None, 135]
    assert [p["t"] for p in series][0] == "2026-11-05T10:00:00+00:00"

    # §8.1 分段：以真实 FIT lap 的 intensity 标注训练内容，不虚构轻松跑/间歇跑
    segs = build_segments(
        [
            {
                "intensity": "warmup",
                "total_timer_time": 300.0,
                "total_distance": 500.0,
                "avg_heart_rate": 110,
            },
            {
                "intensity": "interval",
                "total_timer_time": 60.0,
                "total_distance": 250.0,
                "avg_heart_rate": 165,
            },
        ]
    )
    assert [s["label"] for s in segs] == ["热身", "间歇"]
    assert segs[1]["duration_sec"] == 60.0
    assert segs[1]["distance_m"] == 250.0

    # §8.4 / Q12 力量时间轴：动作 + 休息，休息统一用秒，重量为 kg
    tl = build_strength_timeline(
        [
            {
                "set_type": "active",
                "category": ["warm_up", "warm_up"],
                "category_subtype": [17, 17],
                "duration": 30.0,
                "weight": 0.0,
                "start_time": "2026-11-05T10:00:00+00:00",
            },
            {
                "set_type": "rest",
                "duration": 45.0,
                "start_time": "2026-11-05T10:00:30+00:00",
            },
            {
                "set_type": "active",
                "category": ["squat", "squat"],
                "category_subtype": [0, 0],
                "duration": 40.0,
                "weight": 4.0,
                "repetitions": 12,
                "start_time": "2026-11-05T10:01:15+00:00",
            },
        ]
    )
    assert [s["kind"] for s in tl["segments"]] == ["exercise", "rest", "exercise"]
    assert tl["segments"][1] == {"kind": "rest", "duration_sec": 45}
    assert tl["segments"][2]["weight_kg"] == 4.0
    assert tl["segments"][2]["reps"] == 12
    assert tl["active_seconds"] == 70.0
    assert tl["rest_seconds"] == 45.0
