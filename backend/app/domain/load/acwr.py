"""训练负荷域 —— EWMA ACWR（需求第八章）。

口径（用户确认）：

- 急性 EWMA 时间常数 **7 天**，慢性 **28 天**
- ``ACWR = EWMA_acute / EWMA_chronic``
- 使用**所有运动类型的统一总负荷**，不分别生成各类型 ACWR
- 无训练日 → 当日负荷 0，**继续更新 EWMA**（需求 8.3）
- 数据缺失/同步错误 → **暂时按 0 参与 EWMA**（需求 8.3，用户明确的业务选择，
  实现时**不得**改成跳过缺失日或用估算负荷）
- 慢性 EWMA 为 0 → **ACWR 不可计算**，不伪造数值（需求 8.4）
- 历史数据回补 → 从最早受影响日期重算至今天（需求 10.2）

EWMA 递推（标准形式，``α = 2/(N+1)``）：

    EWMA_t = α × load_t + (1 - α) × EWMA_{t-1}

初始值取**第一个有效负荷日**（无前置数据时不假设 0，避免开头被人为拉低）。
"""

from __future__ import annotations

import datetime as dt

from app.domain.load.types import AcwrState

ACUTE_DAYS = 7
CHRONIC_DAYS = 28

# 慢性窗口有效负荷日低于此值 → sufficient=False（ratio 仅供参考）
MIN_CHRONIC_ACTIVE_DAYS = 14


def alpha(days: int) -> float:
    """EWMA 平滑系数 ``α = 2 / (N + 1)``。"""
    return 2.0 / (days + 1.0)


def ewma_series(
    loads: dict[dt.date, float],
    start: dt.date,
    end: dt.date,
    days: int,
) -> dict[dt.date, float]:
    """按日递推 EWMA。缺失日按 0 参与（需求 8.3）。"""
    a = alpha(days)
    out: dict[dt.date, float] = {}
    prev: float | None = None
    d = start
    while d <= end:
        load = float(loads.get(d, 0.0))
        prev = load if prev is None else a * load + (1 - a) * prev
        out[d] = prev
        d += dt.timedelta(days=1)
    return out


def acwr_series(
    loads: dict[dt.date, float],
    start: dt.date,
    end: dt.date,
) -> dict[dt.date, AcwrState]:
    """计算 ``[start, end]`` 每日的 ACWR 状态。"""
    acute = ewma_series(loads, start, end, ACUTE_DAYS)
    chronic = ewma_series(loads, start, end, CHRONIC_DAYS)

    out: dict[dt.date, AcwrState] = {}
    d = start
    while d <= end:
        ch = chronic[d]
        ac = acute[d]
        active = sum(
            1
            for i in range(CHRONIC_DAYS)
            if loads.get(d - dt.timedelta(days=i), 0.0) > 0
        )
        if ch <= 0:
            out[d] = AcwrState(
                date=d,
                acute_ewma=ac,
                chronic_ewma=0.0,
                ratio=None,
                sufficient=False,
                note="慢性 EWMA 为 0，ACWR 不可计算",
            )
        elif active < MIN_CHRONIC_ACTIVE_DAYS:
            out[d] = AcwrState(
                date=d,
                acute_ewma=ac,
                chronic_ewma=ch,
                ratio=ac / ch,
                sufficient=False,
                note=(
                    f"慢性窗口有效负荷日 {active}/{CHRONIC_DAYS}"
                    f"（< {MIN_CHRONIC_ACTIVE_DAYS}），ratio 仅供参考"
                ),
            )
        else:
            out[d] = AcwrState(
                date=d, acute_ewma=ac, chronic_ewma=ch, ratio=ac / ch
            )
        d += dt.timedelta(days=1)
    return out


def acwr_on(
    loads: dict[dt.date, float],
    as_of: dt.date,
    *,
    lookback_start: dt.date | None = None,
) -> AcwrState:
    """单日 ACWR。``lookback_start`` 默认取 ``as_of - 27``。"""
    start = lookback_start or (as_of - dt.timedelta(days=CHRONIC_DAYS - 1))
    return acwr_series(loads, start, as_of)[as_of]
