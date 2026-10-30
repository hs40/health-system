"""报告服务门面（① report_service 拆分后，2026-10-26）。

原 1339 行单体已拆为 ``app/services/reports/`` 下四个模块，各司其职：

- ``daily_report``  —— 日报 + 周计划卡片 + 负荷/ACWR 重算 + 冲突检测；
- ``period_report`` —— 周报 + 月报 + 区间聚合；
- ``goal_progress`` —— 四项长期目标进展（日/周/月共用口径）；
- ``report_store``  —— 日/周/月报告落库与审计。

本模块只做 re-export，保持既有调用方（api / orchestrator / services / scripts / tests）
的 ``from app.services.report_service import ...`` 与 ``report_service.xxx`` 不变。
"""

from __future__ import annotations

from app.services.reports.daily_report import (
    build_week_cards,
    detect_day_conflicts,
    generate_daily,
    recompute_day_load,
    refresh_acwr,
)
from app.services.reports.goal_progress import load_goal_progress
from app.services.reports.period_report import (
    analyze_monthly,
    generate_monthly,
    generate_weekly,
)

__all__ = [
    "analyze_monthly",
    "build_week_cards",
    "detect_day_conflicts",
    "generate_daily",
    "generate_monthly",
    "generate_weekly",
    "load_goal_progress",
    "recompute_day_load",
    "refresh_acwr",
]
