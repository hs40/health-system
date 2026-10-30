"""训练负荷域 —— 对外统一入口。

分层（需求 4.1，不得混淆）：

    Garmin 活动 payload
        ↓  sports.compute()        原始负荷（量纲随类型不同，不跨类型相加）
    RawLoad
        ↓  standardize.standardize()  标准化 0–100（参考负荷比值法）
    StandardScore
        ↓  aggregate.aggregate()      类型日累计 → 每日总负荷
    DailyLoad
        ↓  acwr.acwr_series()          EWMA τ=7/28
    AcwrState

调用方**只需要** ``compute_day()`` 与 ``acwr_for()``，不应自行拼装中间步骤。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

from app.domain.load import acwr as acwr_mod
from app.domain.load import aggregate as agg_mod
from app.domain.load import hr as hr_mod
from app.domain.load import sports
from app.domain.load import standardize as std_mod
from app.domain.load.acwr import ACUTE_DAYS, CHRONIC_DAYS
from app.domain.load.types import (
    TIER_COUNTED,
    TIER_EXCLUDED,
    TIER_RECORD_ONLY,
    VS_VERIFIED,
    AcwrState,
    DailyLoad,
    RawLoad,
    StandardScore,
)

# 运动分类表（沿用 2026-10-19 用户决策）
SPORT_TIERS: dict[str, str] = {
    "running": TIER_COUNTED,
    "strength_training": TIER_COUNTED,
    "indoor_cardio": TIER_COUNTED,
    "lap_swimming": TIER_COUNTED,
    "hiit": TIER_COUNTED,
    "hiking": TIER_COUNTED,
    "walking": TIER_RECORD_ONLY,
    "meditation": TIER_EXCLUDED,
    "breathwork": TIER_EXCLUDED,
}
DEFAULT_TIER = TIER_RECORD_ONLY

# 每日负荷的方法标记：各类型公式不同，故不是单一算法名，而是「混合」。
# v2（2026-10-25 U-8）：有氧类改为按 FIT 圈分段算 TRIMP（v1 用整次均心）。
METHOD_MIXED = "mixed_banister_strength_v2"


def sport_tier(type_key: str | None) -> str:
    return SPORT_TIERS.get(str(type_key or ""), DEFAULT_TIER)


def compute_day(
    activities: Iterable[dict[str, Any]],
    date: dt.date,
    *,
    max_hr: int = hr_mod.MAX_HR,
    rest_hr: int = hr_mod.REST_HR_BASELINE,
    body_weight_kg: float | None = None,
    laps_by_activity: dict[str, list[dict[str, Any]]] | None = None,
) -> DailyLoad:
    """计算某日负荷：逐活动算原始负荷 → 标准化 → 按类型汇总。

    ``laps_by_activity``（2026-10-25 U-8）：``{activityId: FIT 圈}``。有氧类按圈
    分段算 TRIMP（修间歇课低估），缺圈/圈不完整时由公式层回退整次。
    """
    scores: list[StandardScore] = []
    for act in activities:
        type_key = str((act.get("activityType") or {}).get("typeKey") or "other")
        tier = sport_tier(type_key)
        if tier != TIER_COUNTED:
            # 仅记录 / 不纳入：不算负荷，但仍登记为「不计入」以便报告
            scores.append(
                StandardScore(
                    activity_id=str(act.get("activityId") or ""),
                    sport=type_key,
                    raw=0.0,
                    score=0.0,
                    reference_load=0.0,
                    computed=False,
                    reason=f"运动类型档位为 {tier}，不计入负荷",
                    verification_status=VS_VERIFIED,
                    verification_note="未计入负荷，不涉及可比性验证",
                )
            )
            continue
        raw = sports.compute(
            act,
            max_hr=max_hr,
            rest_hr=rest_hr,
            body_weight_kg=body_weight_kg,
            laps=(laps_by_activity or {}).get(str(act.get("activityId") or "")),
        )
        scores.append(std_mod.standardize(raw))
    return agg_mod.aggregate(scores, date)


def acwr_for(
    daily_totals: dict[dt.date, float],
    as_of: dt.date,
    *,
    lookback_start: dt.date | None = None,
) -> AcwrState:
    return acwr_mod.acwr_on(daily_totals, as_of, lookback_start=lookback_start)


def acwr_range(
    daily_totals: dict[dt.date, float],
    start: dt.date,
    end: dt.date,
) -> dict[dt.date, AcwrState]:
    return acwr_mod.acwr_series(daily_totals, start, end)


__all__ = [
    "ACUTE_DAYS",
    "CHRONIC_DAYS",
    "AcwrState",
    "DailyLoad",
    "RawLoad",
    "SPORT_TIERS",
    "StandardScore",
    "acwr_for",
    "acwr_range",
    "compute_day",
    "hr_mod",
    "sport_tier",
    "std_mod",
]
