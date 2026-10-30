"""睡眠完整性判定（2026-10-30 需求）。

07:30 同步时，用户可能还在睡：Garmin 已有入睡时间、但起床时间尚未生成。
此时不应生成日报/计划/推送（睡眠数据不完整），而是每小时重试，直到有起床时间
或到 12:00 再一次性生成。

**关键边界（用户特别强调）**：没有入睡时间 = 没有睡眠，必须**直接跳过延迟逻辑**
正常生成，不能误判成"睡眠未结束"。

Garmin 缺失/占位时间戳会被 ``_epoch_ms_to_local(0)`` 转成 1970-01-01，因此不能
只判 ``is None``——用年份阈值识别「无有效时间」。
"""

from __future__ import annotations

import datetime as dt

from app.schemas.domain import StandardDaily

# 早于该年份的时间戳视为占位/无效（Garmin 缺失值 0 → 1970）
_SENTINEL_YEAR = 2000

# 睡眠状态
SLEEP_NONE = "none"  # 没有入睡时间 → 没有睡眠
SLEEP_ONGOING = "ongoing"  # 有入睡、无起床 → 睡眠未结束
SLEEP_COMPLETE = "complete"  # 入睡与起床都有

DEFAULT_DEADLINE_HOUR = 12  # 超过该小时不再延迟，强制生成


def _valid(ts: dt.datetime | None) -> bool:
    return ts is not None and ts.year >= _SENTINEL_YEAR


def sleep_state(sd: StandardDaily) -> str:
    """返回 ``none`` / ``ongoing`` / ``complete``。"""
    if not _valid(sd.sleep_start_ts):
        return SLEEP_NONE
    if not _valid(sd.sleep_end_ts):
        return SLEEP_ONGOING
    return SLEEP_COMPLETE


def should_defer(
    state: str, now: dt.datetime, *, deadline_hour: int = DEFAULT_DEADLINE_HOUR
) -> bool:
    """是否应延迟生成：仅「睡眠未结束」且未到截止小时。"""
    return state == SLEEP_ONGOING and now.hour < deadline_hour
