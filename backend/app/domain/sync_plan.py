"""回补分批计划（2026-10-21 决策 Q4）——纯计算，无 IO。

用户口径：新增数据类型「先补近 90 天，其余历史逐日全部补完」。
分批只为控制请求量与限流风险，不改变「最终补全」的目标。
"""

from __future__ import annotations

import datetime as dt


def plan_batches(
    start: dt.date,
    end: dt.date,
    *,
    first_days: int = 90,
    batch_days: int = 30,
) -> list[tuple[dt.date, dt.date]]:
    """把 ``[start, end]`` 切成「近端优先」的批次（首尾相接、无缝无叠）。

    - 第一批 = 最近 ``first_days`` 天（用户当下最关心的数据先到）；
    - 之后从近往远，每批 ``batch_days`` 天；
    - 任何时刻中断，都保留「最近的数据最全」。

    :raises ValueError: 起止倒置 / 参数非正
    """
    if start > end:
        raise ValueError(f"起始日期 {start} 晚于结束日期 {end}")
    if first_days <= 0 or batch_days <= 0:
        raise ValueError("first_days / batch_days 必须为正")

    span = (end - start).days + 1
    if span <= first_days:
        return [(start, end)]

    batches: list[tuple[dt.date, dt.date]] = []
    first_from = end - dt.timedelta(days=first_days - 1)
    batches.append((first_from, end))

    cursor = first_from - dt.timedelta(days=1)
    while cursor >= start:
        batch_from = max(start, cursor - dt.timedelta(days=batch_days - 1))
        batches.append((batch_from, cursor))
        cursor = batch_from - dt.timedelta(days=1)
    return batches


__all__ = ["plan_batches"]
