"""运动训练页面服务层（三期 §7/§8）。

raw 是唯一事实源；测试用 tmp_path 构造最小活动列表与 FIT jsonl。
期望值来自需求文档 §7 的排序/筛选/导航规则，不调用被测模块重算。
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from app.services.training_service import activity_detail, calendar_month

ROOT = "garmin"


def _write_day(tmp_path: Path, filename: str, payloads: list[dict]) -> None:
    d = tmp_path / ROOT / "activities"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{filename}.json").write_text(json.dumps(payloads, ensure_ascii=False), encoding="utf-8")


def _write_fit(tmp_path: Path, aid: str, rows: list[dict]) -> None:
    d = tmp_path / ROOT / "fit_jsonl"
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / f"{aid}.jsonl.gz", "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _act(aid: str, type_key: str, start_local: str, **extra: object) -> dict:
    return {
        "activityId": aid,
        "activityName": f"训练{aid}",
        "activityType": {"typeKey": type_key},
        "startTimeLocal": start_local,
        "startTimeGMT": start_local,
        "durationSeconds": 1800,
        **extra,
    }


def test_calendar_month_lists_full_month_and_sorts_same_day_by_start(tmp_path: Path) -> None:
    _write_day(
        tmp_path,
        "2026-11-05",
        [
            _act("a3", "running", "2026-11-05 18:00:00"),
            _act("a1", "strength_training", "2026-11-05 07:00:00"),
            _act("a2", "walking", "2026-11-05 12:00:00"),
        ],
    )
    data = calendar_month(tmp_path / ROOT, "2026-11")
    # 完整自然月：2026 年 11 月 30 天，含无训练日期
    assert len(data["days"]) == 30
    assert data["days"][0]["date"] == "2026-11-01"
    assert data["days"][-1]["date"] == "2026-11-30"
    assert data["days"][0]["items"] == []

    # §7.3/§7.5 当天第一条按开始时间从早到晚
    day = data["days"][4]
    assert day["date"] == "2026-11-05"
    assert [x["activity_id"] for x in day["items"]] == ["a1", "a2", "a3"]
    assert [x["seq"] for x in day["items"]] == [1, 2, 3]


def test_calendar_filter_keeps_full_month_stable_seq_and_navigation(tmp_path: Path) -> None:
    _write_day(
        tmp_path,
        "2026-11-05",
        [
            _act("a1", "strength_training", "2026-11-05 07:00:00"),
            _act("a2", "running", "2026-11-05 12:00:00"),
            _act("a3", "walking", "2026-11-05 18:00:00"),
        ],
    )
    _write_day(tmp_path, "2026-10-31", [_act("b1", "running", "2026-10-31 08:00:00")])

    data = calendar_month(tmp_path / ROOT, "2026-11", "running")
    # §7.2 筛选后仍保留完整月历；无符合条件的日期仍显示但无标识
    assert len(data["days"]) == 30
    assert data["days"][0]["items"] == []
    assert data["filter"] == "running"

    # seq 基于当天全部训练排序，不因筛选重新编号（§7.6 详情不沿用筛选）
    items = data["days"][4]["items"]
    assert len(items) == 1
    assert items[0]["type"] == "running"
    assert items[0]["seq"] == 2

    assert data["prev_ym"] == "2026-10"
    assert data["next_ym"] == "2026-12"
    assert {t["key"] for t in data["types"]} == {"strength_training", "running", "walking"}


def test_activity_detail_empty_day_sequence_and_natural_day_navigation(tmp_path: Path) -> None:
    _write_day(
        tmp_path,
        "2026-11-05",
        [
            _act("a1", "running", "2026-11-05 07:00:00"),
            _act("a2", "walking", "2026-11-05 18:00:00"),
        ],
    )

    # §7.6 无训练日：空白提示 + 仍保留自然日前后的日期，不跳过
    empty = activity_detail(tmp_path / ROOT, "2026-11-04")
    assert empty["has_training"] is False
    assert empty["total"] == 0
    assert empty["activity"] is None
    assert empty["prev_date"] == "2026-11-03"
    assert empty["next_date"] == "2026-11-05"

    # §7.5 「第 N 次训练 / 共 M 次」，按开始时间从早到晚
    first = activity_detail(tmp_path / ROOT, "2026-11-05", 1)
    assert first["has_training"] is True
    assert first["total"] == 2
    assert first["seq"] == 1
    assert first["activity"]["activity_id"] == "a1"
    second = activity_detail(tmp_path / ROOT, "2026-11-05", 2)
    assert second["activity"]["activity_id"] == "a2"

    # 越界 seq 返回空 activity，不抛异常
    assert activity_detail(tmp_path / ROOT, "2026-11-05", 9)["activity"] is None


def test_running_detail_has_metrics_karvonen_zones_series_and_segments(tmp_path: Path) -> None:
    _write_day(
        tmp_path,
        "2026-11-05",
        [
            _act(
                "a1",
                "running",
                "2026-11-05 07:00:00",
                distance=2375.56,
                duration=1550.74,
                averagePaceMinPerKm=10.88,
                maxPaceMinPerKm=6.84,
                averageHR=130,
                maxHR=155,
                calories=159,
                trainingEffectLabel="AEROBIC_BASE",
                aerobicTrainingEffectMessage="MAINTAINING_AEROBIC_BASE_7",
                anaerobicTrainingEffectMessage="NO_ANAEROBIC_BENEFIT_0",
            )
        ],
    )
    _write_fit(
        tmp_path,
        "a1",
        [
            {"m": "record", "d": {"timestamp": "2026-11-05T07:00:00+00:00", "heart_rate": 120}},
            {"m": "record", "d": {"timestamp": "2026-11-05T07:00:01+00:00", "heart_rate": 135}},
            {"m": "record", "d": {"timestamp": "2026-11-05T07:00:02+00:00", "heart_rate": 160}},
            {
                "m": "lap",
                "d": {
                    "intensity": "interval",
                    "total_timer_time": 60.0,
                    "total_distance": 250.0,
                    "avg_heart_rate": 150,
                },
            },
        ],
    )

    a = activity_detail(tmp_path / ROOT, "2026-11-05")["activity"]
    assert a["template"] == "running"
    metrics = {m["label"]: m["value"] for m in a["metrics"]}
    assert metrics["训练类型"] == "跑步"
    assert metrics["距离"] == "2.38 km"
    assert metrics["平均配速"] == "10'53\"/km"
    assert metrics["最佳配速"] == "6'50\"/km"
    assert metrics["平均心率 / 最高心率（bpm）"] == "130 / 155"

    # Q8：逐秒 record + 项目 Karvonen 五区重算，不展示 Garmin hrTimeInZone_*
    assert [z["seconds"] for z in a["hr_zones"]["zones"]] == [1, 1, 0, 1, 0]
    assert a["hr_zones"]["method"] == "karvonen"
    assert len(a["hr_series"]) == 3
    assert a["segments"][0]["label"] == "间歇"
    assert a["training_effect"]["label"] == "有氧基础"


def test_strength_detail_has_timeline_muscle_table_and_heatmap(tmp_path: Path) -> None:
    _write_day(
        tmp_path,
        "2026-11-05",
        [
            _act(
                "s1",
                "strength_training",
                "2026-11-05 07:00:00",
                duration=1431.97,
                totalSets=18,
                totalReps=120,
                averageHR=130,
                maxHR=148,
            )
        ],
    )
    _write_fit(
        tmp_path,
        "s1",
        [
            {
                "m": "set",
                "d": {
                    "set_type": "active",
                    "category": ["bench_press", "bench_press"],
                    "category_subtype": [None, None],
                    "duration": 40.0,
                    "weight": 4.0,
                    "repetitions": 12,
                    "start_time": "2026-11-05T07:00:00+00:00",
                },
            },
            {
                "m": "set",
                "d": {
                    "set_type": "rest",
                    "duration": 45.0,
                    "start_time": "2026-11-05T07:00:40+00:00",
                },
            },
        ],
    )

    a = activity_detail(tmp_path / ROOT, "2026-11-05")["activity"]
    assert a["template"] == "strength"
    # §8.4 时间轴：动作 + 休息（休息统一秒），总运动 / 总休息分开
    assert a["timeline"]["active_seconds"] == 40.0
    assert a["timeline"]["rest_seconds"] == 45.0
    assert a["timeline"]["segments"][1]["kind"] == "rest"
    assert a["timeline"]["segments"][0]["weight_kg"] == 4.0

    # Q11 表格按「肌群 → 本次涉及动作」聚合
    assert len(a["muscle_table"]) > 0
    assert all("primary_moves" in r and "secondary_moves" in r for r in a["muscle_table"])
    # Q10 肌肉图：17 个官方分区，primary 主色 / secondary 浅一档 / 未涉及灰显
    assert len(a["muscles"]) == 17
    levels = {m["code"]: m["level"] for m in a["muscles"]}
    assert levels["CHEST"] == "primary"
    assert levels["CALVES"] == "none"


def test_swimming_other_hiit_templates_and_no_fabricated_metrics(tmp_path: Path) -> None:
    # §8.5 游泳：真实专属指标（泳池长度按 unitOfPoolLength.factor 换算）
    _write_day(
        tmp_path,
        "2026-11-06",
        [
            _act(
                "w1",
                "lap_swimming",
                "2026-11-06 07:00:00",
                distance=1000,
                duration=2972.9,
                poolLength=2500,
                unitOfPoolLength={"unitId": 1, "unitKey": "meter", "factor": 100},
                strokes=720,
                activeLengths=40,
                averageSwolf=44,
                averageHR=127,
                maxHR=169,
            )
        ],
    )
    _write_fit(
        tmp_path,
        "w1",
        [
            {
                "m": "length",
                "d": {
                    "swim_stroke": "breaststroke",
                    "total_strokes": 11,
                    "avg_speed": 1.159,
                    "total_elapsed_time": 21.562,
                    "length_type": "active",
                },
            }
        ],
    )
    swim = activity_detail(tmp_path / ROOT, "2026-11-06")["activity"]
    assert swim["template"] == "swimming"
    assert swim["swimming"]["pool_length_m"] == 25.0
    assert swim["swimming"]["strokes"] == 720
    assert swim["swimming"]["stroke_types"] == ["蛙泳"]

    # §8.6 其他类型：只展示实际存在的基本信息，不虚构距离 / 配速
    _write_day(
        tmp_path,
        "2026-11-08",
        [
            _act(
                "m1",
                "meditation",
                "2026-11-08 09:00:00",
                duration=600.07,
                averageHR=70,
                maxHR=78,
            )
        ],
    )
    med = activity_detail(tmp_path / ROOT, "2026-11-08")["activity"]
    assert med["template"] == "other"
    med_metrics = {x["label"]: x["value"] for x in med["metrics"]}
    assert "距离" not in med_metrics
    assert "平均配速" not in med_metrics

    # §8.3 HIIT：不支持的距离/配速标注，分段注明动作中文名（Q7 动作库）
    _write_day(
        tmp_path,
        "2026-11-09",
        [
            _act(
                "h1",
                "hiit",
                "2026-11-09 07:00:00",
                duration=1074.2,
                averageHR=135,
                maxHR=157,
            )
        ],
    )
    _write_fit(
        tmp_path,
        "h1",
        [
            {
                "m": "set",
                "d": {
                    "set_type": "active",
                    "category": ["run", "run"],
                    "category_subtype": [None, None],
                    "duration": 30.0,
                    "weight": 0.0,
                    "start_time": "2026-11-09T07:00:00+00:00",
                },
            }
        ],
    )
    hiit = activity_detail(tmp_path / ROOT, "2026-11-09")["activity"]
    assert hiit["template"] == "hiit"
    hiit_metrics = {x["label"]: x["value"] for x in hiit["metrics"]}
    assert hiit_metrics["距离"] == "该运动不支持"
    assert hiit["segments"][0]["name_zh"] == "跑步"
