"""训练负荷域 —— 有氧类原始负荷（Banister TRIMP）。

适用范围（用户 2026-10-20 确认）：**跑步、游泳、HIIT、室内有氧、徒步** 全部用同一公式，
不乘任何运动专属系数。依据：

- 需求 4.2 对 HIIT 明确「不额外乘以未经验证的 HIIT 系数」；
- 游泳仅 4 条历史样本，不足以验证 sTSS 等复杂公式；
- 与跑步同源，跨类型可比性天然成立。

公式（Banister 1991）：

    TRIMP = t(min) × HRR_ratio × 0.64 × e^(1.92 × HRR_ratio)

``HRR_ratio = (HR_avg - HR_rest) / (HR_max - HR_rest)``，由 ``hr.py`` 统一计算。

**为什么不用心率区间时长**（需求 5.4 / 7.1）：
Garmin 各心率区间的训练时长**不得作为跨运动类型验证的共同指标**。
若用它计算负荷、再拿它做验证，属于循环论证。本公式只用 ``duration`` +
``averageHR`` 两个原始字段，验证时可与 Tredict ``trainingEfforts`` 独立对照。
"""

from __future__ import annotations

import math
from typing import Any

from app.domain.load import hr as hr_mod
from app.domain.load.types import RawLoad

FORMULA = "banister_trimp"
UNIT = "hrr_min"
SPORTS: frozenset[str] = frozenset(
    {"running", "lap_swimming", "hiit", "indoor_cardio", "hiking"}
)

# Banister 常数
_COEF = 0.64
_EXP = 1.92


def trimp(minutes: float, ratio: float) -> float:
    """Banister TRIMP。``ratio`` 已截断到 [0,1]，结果天然在合理区间内。"""
    return float(minutes) * ratio * _COEF * math.exp(_EXP * ratio)


def compute(
    activity: dict[str, Any],
    *,
    max_hr: int = hr_mod.MAX_HR,
    rest_hr: int = hr_mod.REST_HR_BASELINE,
    laps: list[dict[str, Any]] | None = None,
) -> RawLoad:
    """由一条活动 payload 计算原始负荷。

    ``laps``（FIT 圈，2026-10-25 U-8）：≥2 圈且每圈都有计时长与均心时按圈求和
    ——TRIMP 对心率比是指数凸函数，分段求和 ≥ 整次（Jensen），修掉间歇课被
    整次均值稀释的系统性低估。任一圈不可算 → 回退整次口径。

    必需字段（整次口径）：``duration``（秒）与 ``averageHR``。任一缺失或为 0 →
    返回 ``value=0.0`` 且 ``inputs["not_computable"]`` 给出原因，
    调用方据此标记不可计算（**不得**当作 0 分计入）。
    """
    activity_id = str(activity.get("activityId") or "")
    type_key = str((activity.get("activityType") or {}).get("typeKey") or "other")

    dur_sec = activity.get("durationSeconds") or activity.get("duration") or 0
    dur_sec = float(dur_sec or 0)
    avg_hr = activity.get("averageHR")

    inputs: dict[str, Any] = {
        "duration_sec": round(dur_sec, 1),
        "average_hr": avg_hr,
        "max_hr": max_hr,
        "rest_hr": rest_hr,
    }

    segmented = _lap_trimp(laps, max_hr=max_hr, rest_hr=rest_hr)
    if segmented is not None:
        value, ratios, minutes = segmented
        inputs.update(
            {
                "duration_sec": round(minutes * 60.0, 1),
                "segments": len(ratios),
                "segment_hrr": ratios,
                "segmented": True,
            }
        )
        return RawLoad(activity_id, type_key, value, UNIT, FORMULA, inputs)

    if dur_sec <= 0:
        inputs["not_computable"] = "缺少有效时长（duration）"
        return RawLoad(activity_id, type_key, 0.0, UNIT, FORMULA, inputs)

    ratio = hr_mod.hrr_ratio(avg_hr, max_hr, rest_hr)
    if ratio is None:
        inputs["not_computable"] = "缺少平均心率（averageHR）"
        return RawLoad(activity_id, type_key, 0.0, UNIT, FORMULA, inputs)

    value = trimp(dur_sec / 60.0, ratio)
    inputs["hrr_ratio"] = round(ratio, 4)
    return RawLoad(activity_id, type_key, value, UNIT, FORMULA, inputs)


def _lap_trimp(
    laps: list[dict[str, Any]] | None,
    *,
    max_hr: int,
    rest_hr: int,
) -> tuple[float, list[float], float] | None:
    """FIT 圈 → ``(TRIMP 合计, 各段 HRR, 总分钟)``；不可用返回 ``None``。

    要求 ≥2 圈，且**每一圈**都有正的计时长与可算的平均心率——任一段不可算就
    整体回退整次口径：少算时间比少算精度更糟（漏掉的段等于没训练）。

    圈计时长之和与活动 ``duration`` 实测比值 1.0000（102 个计数类活动，
    0.9997~1.0009），故不做缩放。
    """
    if not laps or len(laps) < 2:
        return None
    total = 0.0
    minutes = 0.0
    ratios: list[float] = []
    for lap in laps:
        raw_sec = lap.get("total_timer_time") or lap.get("total_elapsed_time") or 0
        try:
            sec = float(raw_sec)
        except (TypeError, ValueError):
            return None
        ratio = hr_mod.hrr_ratio(lap.get("avg_heart_rate"), max_hr, rest_hr)
        if sec <= 0 or ratio is None:
            return None
        total += trimp(sec / 60.0, ratio)
        minutes += sec / 60.0
        ratios.append(round(ratio, 4))
    return total, ratios, minutes
