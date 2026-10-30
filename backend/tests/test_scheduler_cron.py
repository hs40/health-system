"""调度器 cron 星期语义修正（P2）。

背景（真实故障 2026-10-27）：
``WEEKLY_JOB_CRON=0 9 * * 1`` 本意是「每周一 09:00」，但审计日志显示 weekly 任务在
**周二** 09:00 执行（``audit/2026-10-27.jsonl`` 中 ``weekly:2026-10-19`` 的时间戳为
``2026-10-27T09:00:00``，而 10-27 是周二）。

根因：``CronTrigger.from_crontab()`` 把 day_of_week 的数字按 **APScheduler 语义**解释
（0=周一 … 6=周日），而标准 crontab 是 **0/7=周日、1=周一 … 6=周六**。
两者相差一天，且 ``from_crontab`` 不做换算。

实测（APScheduler 3.x）：
    CronTrigger.from_crontab("0 9 * * 1") 的 next_fire_time = 2026-11-03（周二）

契约：``_crontab_trigger()`` 必须按 **crontab 标准**解释 day_of_week。
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("Asia/Shanghai")
NOW = dt.datetime(2026, 10, 27, 14, 0, tzinfo=TZ)  # 周二


def _next_weekday(expr: str) -> str:
    from app.orchestrator.scheduler import _crontab_trigger

    trigger = _crontab_trigger(expr, "Asia/Shanghai")
    nxt = trigger.get_next_fire_time(None, NOW)
    assert nxt is not None
    return nxt.astimezone(TZ).strftime("%A")


def test_weekday_1_means_monday() -> None:
    """crontab 的 1 = 周一（不是 APScheduler 的周二）。"""
    assert _next_weekday("0 9 * * 1") == "Monday"


def test_weekday_0_means_sunday() -> None:
    """crontab 的 0 = 周日。"""
    assert _next_weekday("0 9 * * 0") == "Sunday"


def test_weekday_7_also_means_sunday() -> None:
    """crontab 允许 7 作为周日的等价写法。"""
    assert _next_weekday("0 9 * * 7") == "Sunday"


def test_weekday_6_means_saturday() -> None:
    assert _next_weekday("0 9 * * 6") == "Saturday"


def test_wildcard_weekday_is_unaffected() -> None:
    """``*`` 不受换算影响：daily 任务次日 07:30 触发。"""
    from app.orchestrator.scheduler import _crontab_trigger

    trigger = _crontab_trigger("30 7 * * *", "Asia/Shanghai")
    nxt = trigger.get_next_fire_time(None, NOW)
    assert nxt is not None
    local = nxt.astimezone(TZ)
    assert (local.hour, local.minute) == (7, 30)
    assert local.date() == dt.date(2026, 10, 28)


def test_comma_separated_weekdays() -> None:
    """``1,3`` = 周一、周三；周二之后的下一次应是周三。"""
    assert _next_weekday("0 9 * * 1,3") == "Wednesday"


def test_invalid_field_count_raises() -> None:
    """段数不对必须显式报错，而不是静默降级。"""
    from app.orchestrator.scheduler import _crontab_trigger

    with pytest.raises(ValueError):
        _crontab_trigger("30 7 * * *  # 每日 07:30", "Asia/Shanghai")


def test_unsupported_range_syntax_raises() -> None:
    """区间/步长写法暂不支持，必须报错而非猜测。"""
    from app.orchestrator.scheduler import _crontab_trigger

    with pytest.raises(ValueError):
        _crontab_trigger("0 9 * * 1-5", "Asia/Shanghai")
