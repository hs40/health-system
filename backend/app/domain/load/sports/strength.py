"""训练负荷域 —— 力量训练原始负荷（volume load 统一形式）。

口径（用户 2026-10-20 确认，详见 ``app/domain/strength/exercises.py``）：

- 器械（``volume>0``）：``volume / 1000``（Garmin 已算好的 kg·reps）
- 自重（``reps>0``）：``sets × reps × 体重 × bw_factor``（逐小类系数）
- 静力（``reps=0``）：``sets × 秒 × 体重 × iso_factor ÷ 3``

**为什么选 volume 而不是心率 TRIMP 或组数加权**（五项实测）：

| 维度 | volume | TRIMP | 组数加权 |
|---|---|---|---|
| 负重敏感性（0→13kg） | 0 → 390 | ρ=0.057（看不见） | 仅 +18.6% |
| 器械/自重平衡 | 18 倍差 | 好 | 好 |
| 与时长相关性 | 0.825 | 0.914 | 0.882 |

只有 volume 满足需求 4.2 的「外部重量」要求。器械/自重 18 倍差是真实物理差异
（70kg 自重深蹲 vs 4kg 哑铃弯举），不是公式缺陷。
"""

from __future__ import annotations

from typing import Any

from app.domain.load.types import RawLoad
from app.domain.strength import exercises

FORMULA = exercises.FORMULA
UNIT = exercises.UNIT
SPORTS: frozenset[str] = frozenset({"strength_training"})


def compute(
    activity: dict[str, Any],
    *,
    body_weight_kg: float | None = None,
    **_unused: Any,
) -> RawLoad:
    """由一条力量活动 payload 计算原始负荷（kg·reps）。

    必需字段：``summarizedExerciseSets``。缺失或全被剔除 →
    ``value=0.0`` 且 ``inputs["not_computable"]`` 给出原因。
    """
    activity_id = str(activity.get("activityId") or "")
    type_key = str((activity.get("activityType") or {}).get("typeKey") or "strength_training")
    sets = activity.get("summarizedExerciseSets") or []

    inputs: dict[str, Any] = {
        "sets_count": len(sets) if isinstance(sets, list) else 0,
        "body_weight_kg": body_weight_kg,
        "isometric_sec_per_rep": exercises.ISOMETRIC_SEC_PER_REP,
    }

    if not isinstance(sets, list) or not sets:
        inputs["not_computable"] = "缺少动作明细（summarizedExerciseSets）"
        return RawLoad(activity_id, type_key, 0.0, UNIT, FORMULA, inputs)

    value, detail = exercises.estimate_load(sets, body_weight_kg)
    inputs.update(detail)

    unknown = exercises.unknown_keys(sets)
    if unknown:
        inputs["unknown_exercises"] = unknown

    if value <= 0:
        reason = "所有动作均被剔除或缺少体重"
        if detail.get("not_computable"):
            reason = "；".join(detail["not_computable"])
        inputs["not_computable"] = reason

    return RawLoad(activity_id, type_key, value, UNIT, FORMULA, inputs)
