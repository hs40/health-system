"""同步校验引擎（P7）——Garmin 健康 raw 只比参与计算的字段。

公共入口见 ``app.domain.verify``（包门面）。
"""

from __future__ import annotations

from typing import Any

from app.domain.verify.fields import _close

# Garmin 健康原始载荷里**参与计算**的字段（``standardize()`` 实际读取的那些）。
#
# 为什么不能整份 JSON 比：Garmin 健康接口有一批**滚动/实时字段**
# （``latestRespirationValue``、``restStressPercentage``、``floorsAscendedInMeters``、
# ``latestSpO2``、``*TimestampGMT`` 等），它们随时间变化，
# 任何快照都不可能与之永久相等 —— 整份比会**每天永久红**，把真问题淹掉。
#
# 分层：``(外层键, 内层键)``，内层为 ``None`` 表示取外层的标量。
HEALTH_RAW_COMPARABLE: dict[str, tuple[tuple[str, str | None], ...]] = {
    "daily_summary": (
        ("restingHeartRate", None),
        ("minHeartRate", None),
        ("maxHeartRate", None),
        ("totalSteps", None),
        ("activeKilocalories", None),
        ("moderateIntensityMinutes", None),
        ("vigorousIntensityMinutes", None),
        ("bodyBatteryMostRecentValue", None),
        ("weight", None),
    ),
    "sleep": (
        ("dailySleepDTO", "sleepTimeSeconds"),
        ("dailySleepDTO", "sleepStartTimestampLocal"),
        ("dailySleepDTO", "sleepEndTimestampLocal"),
    ),
    "hrv": (
        ("hrvSummary", "lastNightAvg"),
        ("hrvSummary", "weeklyAvg"),
        ("hrvSummary", "status"),
        ("hrvSummary", "baseline"),
    ),
    "spo2": (("averageSpO2", None),),
    "respiration": (
        ("avgRespirationRate", None),
        ("avgWakingRespirationValue", None),
    ),
}

# ``standardize()`` 另有回退链：呼吸优先取 ``daily_summary.avgWakingRespirationValue``；
# 因此 ``respiration`` 的这两个键也要在 ``daily_summary`` 上比一次。
HEALTH_RAW_ALIASES: dict[str, tuple[tuple[str, str | None], ...]] = {
    "daily_summary": (("avgWakingRespirationValue", None),),
}


def health_field_value(payload: Any, outer: str, inner: str | None) -> Any:
    """按 ``(outer, inner)`` 取值；``inner=None`` 表示取标量。"""
    if not isinstance(payload, dict):
        return None
    value = payload.get(outer)
    if inner is None:
        return value
    return value.get(inner) if isinstance(value, dict) else None


def compare_health_raw(
    local: Any,
    remote: Any,
    *,
    dataset: str,
    abs_tol: float = 0.0,
) -> list[dict[str, Any]]:
    """本地 raw 与 Garmin 云端**只比参与计算的字段**。

    返回差异列表；空列表表示「云端改动的要么没有，要么落在不参与计算的滚动字段上」。
    """
    fields = HEALTH_RAW_COMPARABLE.get(dataset, ())
    fields += HEALTH_RAW_ALIASES.get(dataset, ())
    out: list[dict[str, Any]] = []
    for outer, inner in fields:
        mine = health_field_value(local, outer, inner)
        cloud = health_field_value(remote, outer, inner)
        if mine is None and cloud is None:
            continue
        if mine is None or cloud is None:
            out.append(
                {"dataset": dataset, "field": f"{outer}.{inner or ''}".rstrip("."),
                 "local": mine, "remote": cloud, "kind": "missing_side"}
            )
            continue
        if not _close(mine, cloud, abs_tol=abs_tol) and mine != cloud:
            out.append(
                {"dataset": dataset, "field": f"{outer}.{inner or ''}".rstrip("."),
                 "local": mine, "remote": cloud, "kind": "value"}
            )
    return out

