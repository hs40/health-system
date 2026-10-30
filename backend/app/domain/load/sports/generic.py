"""训练负荷域 —— 兜底公式（未登记的运动类型）。

复用 Banister TRIMP（与跑步同源），并在 ``inputs`` 中标记
``unregistered_sport=True``——上层据此在报告中提示「该类型公式待验证」，
但**不因此拒绝计算**（需求 6.3：验证未通过仍应展示可计算分值并披露状态）。
"""

from __future__ import annotations

from typing import Any

from app.domain.load.sports import running
from app.domain.load.types import RawLoad

FORMULA = running.FORMULA
UNIT = running.UNIT


def compute(
    activity: dict[str, Any],
    *,
    max_hr: int = 180,
    rest_hr: int = 52,
    laps: list[dict[str, Any]] | None = None,
) -> RawLoad:
    raw = running.compute(activity, max_hr=max_hr, rest_hr=rest_hr, laps=laps)
    inputs = dict(raw.inputs)
    inputs["unregistered_sport"] = True
    inputs["note"] = "该运动类型未登记专属公式，按 Banister TRIMP 兜底，口径待验证"
    return RawLoad(raw.activity_id, raw.sport, raw.value, raw.unit, raw.formula, inputs)
