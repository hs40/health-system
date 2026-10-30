"""训练负荷域 —— 汇总（活动 → 类型日累计 → 每日总负荷）。

汇总顺序（需求 4.4，用户明确选择）：

1. 每项活动**分别**计算原始负荷；
2. 每项活动**分别**标准化 → 单次分值（0–100）；
3. 同一类型当日所有活动的**未取整**分值求和 → 类型日累计；
4. 各类型日累计相加 → 每日统一总负荷。

**不得先合并原始负荷再标准化**——量纲不同的原始负荷相加无意义。

类型日累计与每日总负荷**均可超过 100**（需求 4.1 明确：日累计不封顶）。
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from app.domain.load.types import DailyLoad, StandardScore


def aggregate(
    scores: list[StandardScore],
    date: dt.date,
) -> DailyLoad:
    """把某日所有活动的分值汇总成 ``DailyLoad``。"""
    by_sport: dict[str, float] = defaultdict(float)
    contributions: list[StandardScore] = []
    not_computable: list[StandardScore] = []

    for s in scores:
        if not s.computed:
            not_computable.append(s)
            continue
        by_sport[s.sport] += s.score
        contributions.append(s)

    return DailyLoad(
        date=date,
        by_sport={k: round(v, 4) for k, v in sorted(by_sport.items())},
        total=round(sum(by_sport.values()), 4),
        contributions=tuple(contributions),
        not_computable=tuple(not_computable),
    )


def aggregate_many(
    scores_by_date: dict[dt.date, list[StandardScore]],
) -> dict[dt.date, DailyLoad]:
    return {d: aggregate(items, d) for d, items in sorted(scores_by_date.items())}
