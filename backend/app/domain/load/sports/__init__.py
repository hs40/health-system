"""训练负荷域 —— 运动类型公式注册表。

新增运动类型只需在此登记，**不在调用方写 if/else**。
未登记的类型走 ``generic``（Banister TRIMP），并标记为 ``unregistered``（待验证）——
宁可复用已验证的公式，也不为未验证的类型新造系数（需求 4.2）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.domain.load.sports import generic, running, strength
from app.domain.load.types import VS_INSUFFICIENT, VS_UNREGISTERED, VS_VERIFIED, RawLoad

Formula = Callable[..., RawLoad]

# 类型 → 公式。同一公式可服务多个类型（有氧类共用 Banister TRIMP）
_REGISTRY: dict[str, Formula] = {
    "running": running.compute,
    "lap_swimming": running.compute,
    "hiit": running.compute,
    "indoor_cardio": running.compute,
    "hiking": running.compute,
    "strength_training": strength.compute,
}

DEFAULT: Formula = generic.compute

# 各运动类型的**跨类型可比性验证状态**（需求 6.2 / 6.3，逐类型独立管理）。
# 判定依据：2026-10-20 真实 Tredict 验证结果（`scripts/verify_load_validation.py`）。
# 取报告自身分档口径（`validation._relation_text`：R²≥0.5 可识别 / ≥0.16 较弱 /
# 更低未见关系；样本 <5 不出结论）：
#
# | 类型            | n   | R²     | 报告结论             | 状态          |
# |-----------------|-----|--------|----------------------|---------------|
# | running         | 99  | 0.51   | 存在可识别线性关系   | verified      |
# | strength_training| 66  | 0.26   | 存在较弱线性关系     | insufficient  |
# | hiit            | 9   | —      | 样本不足，仅供参考   | insufficient  |
# | lap_swimming    | 4   | —      | 证据不足             | insufficient  |
# | indoor_cardio   | 1   | —      | 证据不足             | insufficient  |
# | hiking          | 1   | —      | 证据不足             | insufficient  |
#
# 「结果不理想」与「样本不足」在状态上同为 insufficient（都须披露），区别写进
# ``note``；需求 6.2/6.3 只要求两条：继续算、继续展示、披露状态。
#
# **未验证不等于不计算**：分值照常计入 combined，只在日报/图表披露状态。
# 该表由用户决定变更（需求 6.1：参数正式采用后固定，不自动更新）。
VERIFICATION: dict[str, str] = {
    "running": VS_VERIFIED,
    "strength_training": VS_INSUFFICIENT,
    "hiit": VS_INSUFFICIENT,
    "hiking": VS_INSUFFICIENT,
    "indoor_cardio": VS_INSUFFICIENT,
    "lap_swimming": VS_INSUFFICIENT,
}

_STATUS_NOTE: dict[str, str] = {
    VS_INSUFFICIENT: "该类型验证样本不足或结果不理想，跨运动类型可比性尚未确认",
    VS_UNREGISTERED: "该类型无专属公式（按 Banister TRIMP 兜底），可比性待验证",
}


def verification_for(sport: str) -> tuple[str, str]:
    """``(状态, 说明)``。一个类型的未验证不影响其他类型（需求 6.3）。"""
    if sport not in _REGISTRY:
        return VS_UNREGISTERED, _STATUS_NOTE[VS_UNREGISTERED]
    status = VERIFICATION.get(sport, VS_INSUFFICIENT)
    return status, _STATUS_NOTE.get(status, "")


def formula_for(sport: str) -> Formula:
    return _REGISTRY.get(sport, DEFAULT)


def formula_name(sport: str) -> str:
    return getattr(formula_for(sport), "__module__", "").rsplit(".", 1)[-1]


def compute(
    activity: dict[str, Any],
    *,
    max_hr: int | None = None,
    rest_hr: int | None = None,
    body_weight_kg: float | None = None,
    laps: list[dict[str, Any]] | None = None,
) -> RawLoad:
    """统一入口：按 ``typeKey`` 选公式并计算原始负荷。

    ``laps``：FIT 圈（2026-10-25 U-8）。力量走组口径，不消费圈；有氧类透传给
    ``running.compute``，由它决定分段还是回退整次。
    """
    sport = str((activity.get("activityType") or {}).get("typeKey") or "other")
    fn = formula_for(sport)
    if fn is strength.compute:
        return fn(activity, body_weight_kg=body_weight_kg)
    kwargs: dict[str, Any] = {}
    if max_hr is not None:
        kwargs["max_hr"] = max_hr
    if rest_hr is not None:
        kwargs["rest_hr"] = rest_hr
    if laps is not None:
        kwargs["laps"] = laps
    return fn(activity, **kwargs)


__all__ = ["Formula", "VERIFICATION", "compute", "formula_for", "formula_name", "verification_for"]
