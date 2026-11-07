"""日报子系统 · 训练负荷与 ACWR（需求 4.4 / 8.1 / 8.3 / 10.1–10.2）。

从 ``daily_report.py`` 拆出（ARCHITECTURE_REVIEW A.1.2）。职责单一：
把「已入库活动 + 体重 → 当日负荷 → EWMA ACWR」这条计算链独立出来，
与日报装配/渲染解耦，便于重算编排单独复用与单测。

口径（用户 2026-10-20 确认，不可随意改动）：
- 当日负荷：逐活动先标准化到 0–100，再按类型求和、跨类型相加；无活动 → ``None``；
- ACWR：**EWMA**（τ=7/28），不是简单日均比；慢性为 0 → 不产出；
- 不可计算的活动随 ``not_computable`` 一并落库，供日报披露。
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session

from app.adapters.garmin import GarminAdapter
from app.config import Settings
from app.domain.load import CHRONIC_DAYS, METHOD_MIXED, acwr_for
from app.schemas.domain import AcwrSnapshot, StandardDaily, TrainingLoad
from app.storage.derived_store import load_range, upsert_acwr, upsert_load
from app.storage.standard_store import get_standard, load_standard_range


def latest_weight(
    session: Session, day: dt.date, lookback_days: int = 180
) -> float | None:
    """最近有效体重（前向沿用）。

    体重是慢变量，单日缺失不该让自重动作整片变成「不可计算」——
    实测 66 条力量活动有 11 条当日无体重。这里取 ``day`` 之前最近一次实测值，
    **不用估算、不用均值**；超过 ``lookback_days`` 仍无记录则返回 ``None``
    （宁可不可计算，也不拿半年前的体重充数）。
    """
    rows = load_standard_range(session, day - dt.timedelta(days=lookback_days), day)
    for r in sorted(rows, key=lambda x: x.date, reverse=True):
        if r.weight_kg:
            return float(r.weight_kg)
    return None


def refresh_acwr(session: Session, day: dt.date) -> AcwrSnapshot | None:
    """按当前负荷序列重算某日 ACWR 并落库（需求 8.x，EWMA τ=7/28）。

    只读已落库的 ``training_load``；缺失日按 0 参与 EWMA（需求 8.3）。
    """
    lookback = CHRONIC_DAYS * 2  # 让 EWMA 有足够预热
    start = day - dt.timedelta(days=lookback)
    history = load_range(session, start, day)
    if not history:
        return None
    totals = {x.date: x.combined for x in history}
    state = acwr_for(totals, day, lookback_start=start)
    snap = AcwrSnapshot(
        date=day,
        acute=state.acute_ewma,
        chronic=state.chronic_ewma,
        ratio=state.ratio,
        sufficient=state.sufficient,
        note=state.note,
    )
    upsert_acwr(session, snap)
    return snap


def compute_day_load(
    session: Session, day: dt.date, sd: StandardDaily, settings: Settings
) -> TrainingLoad | None:
    """标准化当日活动 → 负荷并落库；无活动则返回 ``None``。"""
    adapter = GarminAdapter(settings)
    daily = adapter.standardize_activities(
        day, sd, body_weight_kg=latest_weight(session, day) or sd.weight_kg
    )
    if not (daily.contributions or daily.not_computable):
        return None
    load = TrainingLoad(
        date=day,
        by_sport=daily.by_sport,
        combined=daily.total,
        method=METHOD_MIXED,
        not_computable=[c.to_dict() for c in daily.not_computable],
        unverified_sports=daily.unverified_sports,
    )
    upsert_load(session, load)
    return load


def recompute_day_load(
    session: Session, day: dt.date, settings: Settings
) -> TrainingLoad | None:
    """按**已入库**的原始活动重算某日负荷并落库（需求 10.1–10.2）。

    与 ``generate_daily`` 内的当日负荷计算同一口径；区别是不触碰基线/风险/日报，
    供重算编排从最早受影响日逐日回放。
    """
    sd = get_standard(session, day) or StandardDaily(date=day)
    return compute_day_load(session, day, sd, settings)


def loads_and_acwr(
    session: Session, day: dt.date, sd: StandardDaily, settings: Settings
) -> tuple[TrainingLoad | None, AcwrSnapshot | None]:
    """计算并落库当日训练负荷与 ACWR（``generate_daily`` 使用）。"""
    today_load = compute_day_load(session, day, sd, settings)
    return today_load, refresh_acwr(session, day)
