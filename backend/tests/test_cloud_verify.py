"""云端三路校验引擎 + raw 滚动窗口的纯函数测试。

锁住 6 条**踩过坑**的规则：

1. **时长口径按运动类型分流**：游泳的 Tredict ``duration`` 是移动时长
   （对应 Garmin ``movingDuration``），``durationTotal`` 才对应 Garmin ``duration``。
   不分流 → 51 条假红。
2. **时长容差 10 秒**：Tredict 用 FIT 采样点重算，实测最大偏差 8.2 秒。
   容差太小 → 16 条假红；太大会漏真错。
3. **健康 raw 只比参与计算的字段**：Garmin 的 ``latestRespirationValue`` /
   ``restStressPercentage`` 是滚动值，整份比会天天红。
4. **bodyvalues 不比 ``hrRestDynamic`` / ``hrMaxDynamic``**：上传能进、GET 不读回。
5. **raw 滚动窗口**：窗口内覆盖并留痕，窗口外冻结。
6. **退出码 3 = 可修复**，与 1（需人工）区分，避免自动修复吞掉真故障。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from app.adapters.garmin import RAW_REFRESH_WINDOW_DAYS
from app.domain.verify import (
    ACTIVITY_DURATION_TOL,
    BODYVALUES_COMPARABLE,
    STATE_OK,
    CheckResult,
    compare_cloud_pair,
    compare_health_fields,
    compare_health_raw,
    duration_fields,
    exit_code,
    health_field_value,
)


# ============================================================
# 1. 时长口径分流
# ============================================================
def test_swim_duration_maps_to_moving_duration() -> None:
    d, total = duration_fields("lap_swimming")
    assert d == ("movingDuration", "duration")
    assert total == ("duration", "durationTotal")


def test_non_swim_duration_maps_directly() -> None:
    d, total = duration_fields("generic")
    assert d == ("duration", "duration")
    assert total == ("elapsedDuration", "durationTotal")


def test_duration_fields_tolerates_none() -> None:
    assert duration_fields(None)[0] == ("duration", "duration")


def test_tolerance_covers_observed_noise() -> None:
    """实测 602 组最大偏差 8.2 秒，容差必须 > 8.2 且不该无限放宽。"""
    assert 8.2 < ACTIVITY_DURATION_TOL <= 15


# ============================================================
# 2. 活动云端配对
# ============================================================
def _garmin(dur: float, elapsed: float, moving: float, **kw) -> dict:
    return {
        "duration": dur,
        "elapsedDuration": elapsed,
        "movingDuration": moving,
        "calories": kw.get("calories", 200),
        "averageHR": kw.get("hr", 130),
        "maxHR": kw.get("hr_max", 150),
    }


def test_cloud_pair_ignores_rounding_noise() -> None:
    g = {"1": _garmin(2349.8, 2415.6, 1170.5)}
    t = {"T1": {"subSportType": "strength_training",
                "summary": {"duration": 2350, "durationTotal": 2415,
                            "calories": 200, "heartrate": 130, "heartrateMax": 150}}}
    out = compare_cloud_pair(g, t, [("1", "T1")])
    assert out["mismatch"] == 0
    assert out["compared"] == 1


def test_cloud_pair_swim_uses_moving_duration() -> None:
    """游泳：Garmin duration 2972.9 是总时长，Tredict duration 1035 是移动时长。

    不按类型分流会报 2972.9 vs 1035 的假红。
    """
    g = {"1": _garmin(2972.9, 2978.2, 1034.2)}
    t = {"T1": {"subSportType": "lap_swimming",
                "summary": {"duration": 1035, "durationTotal": 2973,
                            "calories": 200, "heartrate": 130, "heartrateMax": 150}}}
    out = compare_cloud_pair(g, t, [("1", "T1")])
    assert out["mismatch"] == 0


def test_cloud_pair_detects_real_duration_error() -> None:
    """差 5 分钟是数据错误，不是噪声。"""
    g = {"1": _garmin(3000.0, 3000.0, 3000.0)}
    t = {"T1": {"subSportType": "generic",
                "summary": {"duration": 2700, "durationTotal": 3000,
                            "calories": 200, "heartrate": 130, "heartrateMax": 150}}}
    out = compare_cloud_pair(g, t, [("1", "T1")])
    assert out["mismatch"] == 1
    assert out["field_mismatch"][0]["fields"][0]["field"] == "duration"


def test_cloud_pair_detects_heartrate_error() -> None:
    g = {"1": _garmin(1000.0, 1000.0, 1000.0, hr=130)}
    t = {"T1": {"subSportType": "generic",
                "summary": {"duration": 1000, "durationTotal": 1000,
                            "calories": 200, "heartrate": 99, "heartrateMax": 150}}}
    out = compare_cloud_pair(g, t, [("1", "T1")])
    assert any(f["field"] == "heartrate" for f in out["field_mismatch"][0]["fields"])


def test_cloud_pair_skips_missing_pairs() -> None:
    out = compare_cloud_pair({"1": _garmin(1, 1, 1)}, {}, [("1", "T1")])
    assert out["skipped"] == 1
    assert out["compared"] == 0


def test_online_garmin_ignores_user_roles_metadata() -> None:
    """``userRoles`` 是 Garmin 回显的**登录用户权限 scope 列表**，不是活动数据。

    2026-10-25 实测：Garmin 增删 scope 后 302/304 条历史活动全部报「值不符」，
    日任务天天 exit 3。排除它；真数据仍然要报。
    """
    from app.domain.verify import compare_online_garmin

    remote = [{"activityId": 1, "duration": 100.0, "userRoles": ["SCOPE_A"]}]
    local = {"1": {"activityId": 1, "duration": 100.0, "userRoles": ["SCOPE_B"]}}
    assert compare_online_garmin(remote, local)["field_mismatch"] == []

    remote[0]["duration"] = 200.0
    out = compare_online_garmin(remote, local)
    assert [f["fields"] for f in out["field_mismatch"]] == [["duration"]]


# ============================================================
# 3. 健康 raw 只比参与计算的字段
# ============================================================
def test_health_raw_ignores_rolling_fields() -> None:
    """``latestRespirationValue`` 这类滚动字段变了不该报。"""
    local = {"restingHeartRate": 56, "totalSteps": 8000, "latestRespirationValue": 14.1}
    cloud = {"restingHeartRate": 56, "totalSteps": 8000, "latestRespirationValue": 15.7}
    assert compare_health_raw(local, cloud, dataset="daily_summary") == []


def test_health_raw_detects_step_change() -> None:
    """当天没结束就拉 → 37 步 vs 7180 步，必须报。"""
    local = {"totalSteps": 37}
    cloud = {"totalSteps": 7180}
    diffs = compare_health_raw(local, cloud, dataset="daily_summary")
    assert len(diffs) == 1
    assert diffs[0]["field"] == "totalSteps"
    assert diffs[0]["local"] == 37
    assert diffs[0]["remote"] == 7180


def test_health_raw_compares_nested_sleep_dto() -> None:
    local = {"dailySleepDTO": {"sleepTimeSeconds": 27180}}
    cloud = {"dailySleepDTO": {"sleepTimeSeconds": 25000}}
    diffs = compare_health_raw(local, cloud, dataset="sleep")
    assert [d["field"] for d in diffs] == ["dailySleepDTO.sleepTimeSeconds"]


def test_health_raw_reports_one_sided_missing() -> None:
    diffs = compare_health_raw({}, {"restingHeartRate": 56}, dataset="daily_summary")
    assert diffs[0]["kind"] == "missing_side"


def test_health_raw_unknown_dataset_is_noop() -> None:
    assert compare_health_raw({"a": 1}, {"a": 2}, dataset="heart_rate") == []


def test_health_field_value_handles_shape_mismatch() -> None:
    assert health_field_value({"hrvSummary": None}, "hrvSummary", "lastNightAvg") is None
    assert health_field_value({"hrvSummary": {"lastNightAvg": 64}},
                              "hrvSummary", "lastNightAvg") == 64
    assert health_field_value("not-a-dict", "x", None) is None


# ============================================================
# 4. bodyvalues 可比字段
# ============================================================
def test_bodyvalues_excludes_unreadable_hr_fields() -> None:
    """``hrRestDynamic`` / ``hrMaxDynamic`` 上传能进、GET 不返回 → 必须排除。"""
    assert "hrRestDynamic" not in BODYVALUES_COMPARABLE
    assert "hrMaxDynamic" not in BODYVALUES_COMPARABLE
    assert "restingHeartrate" in BODYVALUES_COMPARABLE


def test_compare_health_fields_detects_rmssd_drift() -> None:
    local = {"hrv": {"20261017": {"rmssd": 64}}, "sleep": {}, "bodyvalues": {}}
    remote = {"hrv": {"20261017": [46, 58]}, "sleep": {}, "bodyvalues": []}
    out = compare_health_fields(local, remote)
    assert out["mismatch"] == 1
    assert out["kinds"]["hrv"]["mismatch"][0]["field"] == "rmssd"


def test_compare_health_fields_ignores_extra_cloud_fields() -> None:
    """云端多出来的字段（Tredict 自己算的）不算错。"""
    local = {"hrv": {"20261017": {"rmssd": 64}}, "sleep": {}, "bodyvalues": {}}
    remote = {"hrv": {"20261017": [64, 58]}, "sleep": {}, "bodyvalues": []}
    assert compare_health_fields(local, remote)["mismatch"] == 0


# ============================================================
# 5. raw 滚动窗口
# ============================================================
def test_window_default_is_five_days() -> None:
    assert RAW_REFRESH_WINDOW_DAYS == 5


def _adapter(tmp_path: Path):
    from app.adapters.garmin import GarminAdapter
    from app.config import Settings

    return GarminAdapter(Settings(raw_data_dir=tmp_path))


def test_refresh_raw_writes_first_time(tmp_path: Path) -> None:
    a = _adapter(tmp_path)
    day = dt.date(2026, 10, 19)
    state, old = a.refresh_raw("daily_summary", day, {"v": 1}, today=day)
    assert state == "written"
    assert old is None
    assert json.loads(a._raw_path("daily_summary", day).read_text("utf-8")) == {"v": 1}


def test_refresh_raw_overwrites_inside_window_and_keeps_copy(tmp_path: Path) -> None:
    a = _adapter(tmp_path)
    today = dt.date(2026, 10, 19)
    day = today - dt.timedelta(days=RAW_REFRESH_WINDOW_DAYS - 1)
    a.refresh_raw("daily_summary", day, {"v": 1}, today=today)
    state, old = a.refresh_raw("daily_summary", day, {"v": 2}, today=today)
    assert state == "superseded"
    assert old == {"v": 1}
    assert json.loads(a._raw_path("daily_summary", day).read_text("utf-8")) == {"v": 2}
    kept = list((a.raw_root / "_superseded" / "daily_summary").glob("*.json"))
    assert len(kept) == 1
    assert json.loads(kept[0].read_text("utf-8")) == {"v": 1}


def test_refresh_raw_freezes_outside_window(tmp_path: Path) -> None:
    a = _adapter(tmp_path)
    today = dt.date(2026, 10, 19)
    day = today - dt.timedelta(days=RAW_REFRESH_WINDOW_DAYS)
    a.refresh_raw("daily_summary", day, {"v": 1}, today=today)
    state, old = a.refresh_raw("daily_summary", day, {"v": 2}, today=today)
    assert state == "frozen"
    assert old == {"v": 1}
    assert json.loads(a._raw_path("daily_summary", day).read_text("utf-8")) == {"v": 1}


def test_refresh_raw_same_content_does_not_rewrite(tmp_path: Path) -> None:
    a = _adapter(tmp_path)
    today = dt.date(2026, 10, 19)
    a.refresh_raw("daily_summary", today, {"v": 1}, today=today)
    state, old = a.refresh_raw("daily_summary", today, {"v": 1}, today=today)
    assert state == "same"
    assert old == {"v": 1}


def test_write_raw_stays_append_only(tmp_path: Path) -> None:
    """``_write_raw`` 的旧语义必须保留：回填脚本仍依赖它。"""
    a = _adapter(tmp_path)
    day = dt.date(2026, 10, 19)
    assert a._write_raw("daily_summary", day, {"v": 1}) is True
    assert a._write_raw("daily_summary", day, {"v": 2}) is False
    assert json.loads(a._raw_path("daily_summary", day).read_text("utf-8")) == {"v": 1}


# ============================================================
# 6. 退出码
# ============================================================
def test_exit_code_three_when_repairable() -> None:
    assert exit_code([CheckResult("e", "1", STATE_OK)], online_diff=2,
                     needs_repair=True) == 3


def test_exit_code_one_when_not_repairable() -> None:
    assert exit_code([CheckResult("e", "1", STATE_OK)], online_diff=2) == 1


def test_exit_code_zero_when_clean() -> None:
    assert exit_code([CheckResult("e", "1", STATE_OK)], online_diff=0) == 0


# ------------------------------------------------------------
# 跨 seam 一致性（⑧ 安全子集，2026-10-26）
# 报告 ⑧：verify 与 adapters 的字段清单「必须一致，靠单测保证」，
# 但此前没有断言两者相等的用例。这里把契约真正锁住。
# ------------------------------------------------------------
def test_bodyvalues_comparable_matches_tredict_readable():
    """verify 的 bodyvalues 比对清单必须与 tredict 的「可读字段」完全一致。"""
    from app.adapters.tredict import BODYVALUES_READABLE

    assert BODYVALUES_COMPARABLE == BODYVALUES_READABLE, (
        "verify.BODYVALUES_COMPARABLE 与 tredict.BODYVALUES_READABLE 漂移："
        "逐字段校验会漏比或假红"
    )


def test_health_raw_comparable_keys_are_read_by_standardize():
    """HEALTH_RAW_COMPARABLE 的每个 (outer, inner) 都必须是 standardize() 实际读取的键。

    防止「standardize 改了 raw 键名、比对清单没跟」→ 校验静默比了个不存在的字段。
    """
    import inspect

    from app.adapters.garmin import GarminAdapter
    from app.domain.verify import HEALTH_RAW_ALIASES, HEALTH_RAW_COMPARABLE

    src = inspect.getsource(GarminAdapter.standardize)
    merged: dict[str, tuple[tuple[str, str | None], ...]] = {}
    for mapping in (HEALTH_RAW_COMPARABLE, HEALTH_RAW_ALIASES):
        for dataset, fields in mapping.items():
            merged[dataset] = merged.get(dataset, ()) + fields

    for dataset, fields in merged.items():
        assert fields, f"{dataset} 的比对清单为空"
        for outer, inner in fields:
            assert f'"{outer}"' in src, f"{dataset}.{outer} 未被 standardize 读取"
            if inner is not None:
                assert f'"{inner}"' in src, f"{dataset}.{outer}.{inner} 未被 standardize 读取"
