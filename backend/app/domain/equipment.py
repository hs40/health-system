"""器材配置（REQ-PLAN-003）：用户实有器材与档位，供力量处方使用。

核实记录（V8，2026-10-15 用户确认）：
- 用户实有**成对**可调哑铃（每档两只），单只档位为
  ``2.5 / 3 / 4 / 5.5 / 6.5 / 7 / 9.5 kg``，共 7 档；
- 另有**徒手**（自重）。
- Plan 原记录「2.5/4/5/7.5/9 kg、单只/成对待核实」**有误**，已按实测更正。

约定：``StrengthSet.load_kg`` 记**单只**重量；双臂动作（如哑铃卧推、推举）
双手各持同重一只，双侧合计为单只 ×2（见 ``pair_total_kg``）。

取值来源：``Settings.dumbbell_weights_kg`` / ``Settings.dumbbell_is_pair``，
本模块只提供纯函数（描述 / 吸附 / 合计），不重复存放配置值。
"""

from __future__ import annotations

from collections.abc import Sequence


def _fmt(kg: float) -> str:
    """去掉无意义的小数点（2.5 → "2.5"，3.0 → "3"）。"""
    return f"{kg:g}"


def describe(weights_kg: Sequence[float], *, is_pair: bool) -> str:
    """生成供 LLM 提示使用的器材说明（单行）。"""
    tiers = "/".join(_fmt(w) for w in weights_kg)
    if is_pair:
        return (
            f"徒手（自重）+ 成对可调哑铃（单只档位 {tiers} kg；"
            "双臂动作双手各持同重一只，双侧合计＝单只×2）"
        )
    return f"徒手（自重）+ 单只可调哑铃（档位 {tiers} kg）"


def snap_load_kg(load_kg: float, weights_kg: Sequence[float]) -> float:
    """把任意负重吸附到最接近的可用**单只**档位（校验/纠正模型输出用）。"""
    if not weights_kg:
        return float(load_kg)
    return float(min(weights_kg, key=lambda w: abs(float(w) - float(load_kg))))


def pair_total_kg(per_side_kg: float, *, is_pair: bool) -> float:
    """双臂动作的双侧总负重：成对时＝单只 ×2，单只时＝单只 ×1。"""
    return float(per_side_kg) * (2 if is_pair else 1)
