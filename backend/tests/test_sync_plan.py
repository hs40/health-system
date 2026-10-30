"""回补分批计划（2026-10-21 决策 Q4）。

用户口径：新增数据类型「先补近 90 天，其余历史逐日全部补完」。
分批的意义是控制请求量与限流风险；本模块只做**纯计算**，不做 IO。

不变量（可证伪）：
- 覆盖完整：所有批次首尾相接，无缝无叠，合起来正好覆盖 [start, end]；
- 近端优先：第一批永远是最近的 ``first_days`` 天（用户当下最关心的数据先到）；
- 倒序推进：之后从近往远分批，任何时刻中断都保留「最近的数据最全」。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.sync_plan import plan_batches

END = dt.date(2026, 10, 21)
START = dt.date(2025, 7, 3)


def test_first_batch_is_recent_window() -> None:
    """第一批 = 最近 90 天（含结束日）。"""
    batches = plan_batches(START, END, first_days=90, batch_days=30)
    assert batches[0] == (END - dt.timedelta(days=89), END)


def test_batches_cover_range_without_gap_or_overlap() -> None:
    """批次首尾相接、无重叠、无缺口，合起来正好覆盖全区间。"""
    batches = plan_batches(START, END, first_days=90, batch_days=30)
    # 按日期升序排列后逐段相接
    ordered = sorted(batches)
    assert ordered[0][0] == START
    assert ordered[-1][1] == END
    for prev, nxt in zip(ordered, ordered[1:], strict=False):
        assert nxt[0] == prev[1] + dt.timedelta(days=1), "批次之间必须无缝且不重叠"


def test_batches_are_descending_after_first() -> None:
    """近端优先：除第一批外，后续批次从近往远推进。"""
    batches = plan_batches(START, END, first_days=90, batch_days=30)
    assert batches[0][1] == END
    for prev, nxt in zip(batches[1:], batches[2:], strict=False):
        assert nxt[1] < prev[0], "后续批次应更早"


def test_short_range_yields_single_batch() -> None:
    """区间短于 first_days → 一批搞定，不造空批。"""
    start = END - dt.timedelta(days=10)
    assert plan_batches(start, END, first_days=90, batch_days=30) == [(start, END)]


def test_invalid_range_rejected() -> None:
    with pytest.raises(ValueError):
        plan_batches(END, START, first_days=90, batch_days=30)
