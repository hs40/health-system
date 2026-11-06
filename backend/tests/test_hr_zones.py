"""逐秒心率 → Karvonen 五区时长（三期 §8 跑步详情，Q8 口径）。

期望值来自 Karvonen 公式的**手工推导**，不是调用被测模块重算::

    HRR = 180 - 52 = 128
    Z1 116..129 / Z2 129..142 / Z3 142..154 / Z4 154..167 / Z5 167..180

边界沿用 ``domain/load/hr.py`` 的 ``zone_index()``：「hr <= 上界」归较低区。
"""

from __future__ import annotations

from app.domain.load.hr_zones import summarize


def test_summarize_buckets_each_second_into_karvonen_zones() -> None:
    # 每区一个代表心率（取区间中点，避开边界）；逐秒序列里 1 条 = 1 秒
    hrs = [120, 135, 148, 160, 175]
    stats = summarize(hrs)
    assert [z.name for z in stats.zones] == ["Z1", "Z2", "Z3", "Z4", "Z5"]
    assert [z.seconds for z in stats.zones] == [1, 1, 1, 1, 1]
    assert stats.counted_seconds == 5


def test_summarize_to_dict_exposes_zone_seconds_and_missing_coverage() -> None:
    """API 序列化契约：五区时长 + 覆盖情况（Q8 脚注披露）。

    缺失的心率秒不补 0、不塞进任何区；低于 Z1 下界的心率单独计数。
    """
    hrs = [120, 125, 135, 115, None, None]
    payload = summarize(hrs).to_dict()
    assert payload["zones"] == [
        {"name": "Z1", "low_bpm": 116, "high_bpm": 129, "seconds": 2},
        {"name": "Z2", "low_bpm": 129, "high_bpm": 142, "seconds": 1},
        {"name": "Z3", "low_bpm": 142, "high_bpm": 154, "seconds": 0},
        {"name": "Z4", "low_bpm": 154, "high_bpm": 167, "seconds": 0},
        {"name": "Z5", "low_bpm": 167, "high_bpm": 180, "seconds": 0},
    ]
    assert payload["counted_seconds"] == 3
    assert payload["below_zone_seconds"] == 1
    assert payload["missing_seconds"] == 2


def test_summarize_marks_karvonen_method_and_keeps_zone_boundaries() -> None:
    """Q8：五区口径来自项目 Karvonen 参数，且边界归属与 ``zone_index()`` 一致。

    边界值按「hr <= 上界归较低区」手工推导；高于 Z5 上界归 Z5。
    """
    hrs = [116, 129, 130, 142, 143, 154, 155, 167, 168, 180, 181]
    stats = summarize(hrs)
    assert [z.seconds for z in stats.zones] == [2, 2, 2, 2, 3]
    assert [(z.low_bpm, z.high_bpm) for z in stats.zones] == [
        (116, 129),
        (129, 142),
        (142, 154),
        (154, 167),
        (167, 180),
    ]
    payload = stats.to_dict()
    assert payload["method"] == "karvonen"
    assert payload["max_hr"] == 180
    assert payload["rest_hr"] == 52
