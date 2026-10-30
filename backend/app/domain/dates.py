"""日期工具（⑥ 去重，2026-10-26）。

此前 `_week_start_of` / `_monday_of` 在 `api/reports.py`、`daily_report.py`、
`plan_service.py` 各写一份（语义同为「周一为一周之始」，D17）。收敛到这里。
"""

from __future__ import annotations

import datetime as dt


def week_start(day: dt.date) -> dt.date:
    """周一为一周之始（D17）。"""
    return day - dt.timedelta(days=day.weekday())
