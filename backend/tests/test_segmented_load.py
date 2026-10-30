"""U-8：单次活动按 FIT 圈分段计算负荷（2026-10-25 用户决策：全套重算）。

为什么要分段：TRIMP 对心率比是**指数凸函数**，用整次平均心率会系统性低估
间歇课（热身/恢复段把均值拉低，高心率段的权重被平均掉）。按圈求和符合
Jensen 不等式，**同总时长下分段值 ≥ 整次值**（本文件把这条当不变量测）。

为什么用 FIT 圈、不用 ``splitSummaries``：后者同时含 ``RWD_*``（走跑识别）
与 ``INTERVAL_*`` 两套互相重叠的分组，直接求和会双倍计时（实测 3101.5s
vs 活动时长 1551s）。FIT ``lap`` 是顺序、不重叠、带 ``avg_heart_rate`` 的。

回退规则：**少于 2 圈、或任一圈缺计时长/均心 → 整次口径**。宁可少一点精度，
也不能少算时间（漏算比不算更糟）。
"""

from __future__ import annotations

import gzip
import json
import math

from app.domain.load.sports import running

# 与 hr.py 默认值一致（max=180 / rest=52），便于手算
MAX_HR, REST_HR = 180, 52


def _ratio(hr: int) -> float:
    return (hr - REST_HR) / (MAX_HR - REST_HR)


def _trimp(minutes: float, ratio: float) -> float:
    return minutes * ratio * 0.64 * math.exp(1.92 * ratio)


def _lap(seconds: float, hr: int) -> dict:
    return {"total_timer_time": seconds, "avg_heart_rate": hr}


def _activity(duration_sec: float, avg_hr: int) -> dict:
    return {
        "activityId": 1,
        "activityType": {"typeKey": "running"},
        "duration": duration_sec,
        "averageHR": avg_hr,
    }


# ============================================================
# 纯函数：分段 TRIMP
# ============================================================
def test_segmented_trimp_sums_per_lap() -> None:
    laps = [_lap(1800, 140), _lap(1800, 170)]
    got = running._lap_trimp(laps, max_hr=MAX_HR, rest_hr=REST_HR)
    assert got is not None
    value, ratios, minutes = got
    expected = _trimp(30, _ratio(140)) + _trimp(30, _ratio(170))
    assert value == pytest_approx(expected)
    assert ratios == [round(_ratio(140), 4), round(_ratio(170), 4)]
    assert minutes == 60


def test_segmented_trimp_is_never_below_whole_activity() -> None:
    """同总时长下，指数凸函数的分段求和 ≥ 整次（Jensen）。"""
    laps = [_lap(1800, 140), _lap(1800, 170)]
    value, _, minutes = running._lap_trimp(laps, max_hr=MAX_HR, rest_hr=REST_HR)
    whole = _trimp(minutes, _ratio(155))  # 加权平均心率 155
    assert value >= whole


def test_segmented_trimp_requires_two_laps() -> None:
    assert running._lap_trimp([_lap(1800, 150)], max_hr=MAX_HR, rest_hr=REST_HR) is None
    assert running._lap_trimp([], max_hr=MAX_HR, rest_hr=REST_HR) is None
    assert running._lap_trimp(None, max_hr=MAX_HR, rest_hr=REST_HR) is None


def test_segmented_trimp_falls_back_when_any_lap_lacks_hr() -> None:
    laps = [_lap(1800, 140), {"total_timer_time": 1800}]
    assert running._lap_trimp(laps, max_hr=MAX_HR, rest_hr=REST_HR) is None


def test_segmented_trimp_falls_back_when_any_lap_lacks_time() -> None:
    laps = [_lap(1800, 140), {"avg_heart_rate": 170}]
    assert running._lap_trimp(laps, max_hr=MAX_HR, rest_hr=REST_HR) is None


# ============================================================
# 公式入口：有圈走分段、无圈回退整次
# ============================================================
def test_compute_uses_laps_when_available() -> None:
    act = _activity(3600, 155)
    laps = [_lap(1800, 140), _lap(1800, 170)]
    whole = running.compute(act, max_hr=MAX_HR, rest_hr=REST_HR)
    seg = running.compute(act, max_hr=MAX_HR, rest_hr=REST_HR, laps=laps)
    assert seg.value >= whole.value
    assert seg.inputs["segments"] == 2
    assert seg.inputs["segmented"] is True
    assert "not_computable" not in seg.inputs


def test_compute_without_laps_keeps_whole_activity_formula() -> None:
    act = _activity(3600, 155)
    out = running.compute(act, max_hr=MAX_HR, rest_hr=REST_HR)
    assert out.value == pytest_approx(_trimp(60, _ratio(155)))
    assert "segmented" not in out.inputs


def test_compute_with_single_lap_falls_back() -> None:
    act = _activity(3600, 155)
    out = running.compute(act, max_hr=MAX_HR, rest_hr=REST_HR, laps=[_lap(3600, 155)])
    assert "segmented" not in out.inputs


# ============================================================
# 数据源：FIT jsonl 的 lap 消息
# ============================================================
def test_load_laps_reads_lap_messages(tmp_path) -> None:
    from app.storage.activity_source import load_laps

    path = tmp_path / "fit_jsonl" / "42.jsonl.gz"
    path.parent.mkdir(parents=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"m": "record", "d": {"heart_rate": 120}}) + "\n")
        f.write(json.dumps({"m": "lap", "d": _lap(600, 150)}) + "\n")
        f.write("not-json\n")
        f.write(json.dumps({"m": "lap", "d": _lap(600, 160)}) + "\n")

    laps = load_laps(tmp_path, "42")
    assert [lap["avg_heart_rate"] for lap in laps] == [150, 160]


def test_load_laps_returns_empty_when_missing(tmp_path) -> None:
    from app.storage.activity_source import load_laps

    assert load_laps(tmp_path, "nope") == []


# ============================================================
# 日负荷：按活动把圈传下去
# ============================================================
def test_compute_day_passes_laps_per_activity() -> None:
    from datetime import date

    from app.domain.load import compute_day

    act = _activity(3600, 155)
    day_load = compute_day(
        [act],
        date(2026, 10, 20),
        max_hr=MAX_HR,
        rest_hr=REST_HR,
        laps_by_activity={"1": [_lap(1800, 140), _lap(1800, 170)]},
    )
    expected = _trimp(30, _ratio(140)) + _trimp(30, _ratio(170))
    assert day_load.contributions[0].raw == pytest_approx(expected)


def pytest_approx(value: float):
    import pytest

    return pytest.approx(value, rel=1e-9)
