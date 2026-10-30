"""训练负荷域 —— 标准化（原始负荷 → 0–100）。

方法：**参考负荷比值法**

    分值 = 原始负荷 ÷ 参考负荷 R × 100

为什么不用历史分位数（需求 4.1 硬约束）：

> 「同一运动类型的所有活动、所有日期，使用同一套正式采用的标准化公式及参数」
> 「个人历史数据用于公式开发、参数估计及结果验证，**不作为每次活动计算时
> 自动变化的动态输入**」

分位数映射（P95=100）会随新数据漂移，同一次历史活动明天重算可能变分 → 违反上述两条。

参考负荷 R（用户 2026-10-20 确认）
----------------------------------
| 类型 | R | 可解释为 | 依据 |
|---|---:|---|---|
| 有氧类（跑步/游泳/HIIT/有氧/徒步） | 130 | 约 64min @ Z3 中段 | 两类均值 48.7 vs 48.4 |
| 力量（volume load） | 60000 | 约 120 组深蹲等效 | 同上 |

R 一经采用即固定，**不自动更新**（需求 6.1）。
"""

from __future__ import annotations

from app.domain.load.sports import verification_for
from app.domain.load.types import RawLoad, StandardScore

# 参考负荷 R（kg·reps 或 HRR·分钟）
REFERENCE_LOAD: dict[str, float] = {
    "strength_training": 60_000.0,
}
DEFAULT_REFERENCE = 130.0

# 有氧类（同一 R）
AEROBIC_SPORTS = frozenset(
    {"running", "lap_swimming", "hiit", "indoor_cardio", "hiking"}
)

# 超出 0–100 视为参数问题，需检查（需求 4.5：不得靠截断掩盖）
MAX_SCORE = 100.0


def reference_for(sport: str) -> float:
    return REFERENCE_LOAD.get(sport, DEFAULT_REFERENCE)


def standardize(raw: RawLoad, *, reference: float | None = None) -> StandardScore:
    """把原始负荷映射为 0–100 分值。

    不可计算（``inputs["not_computable"]`` 存在）→ ``computed=False``，
    分值不参与任何汇总（需求 4.5：不能通过最低 1 分规则转为有效负荷）。

    无论是否可计算，都带上该运动类型的**验证状态**（需求 6.3：未验证也要
    展示可计算分值并披露状态，不得隐藏）。
    """
    ref = reference if reference is not None else reference_for(raw.sport)
    reason = str(raw.inputs.get("not_computable") or "")
    status, status_note = verification_for(raw.sport)

    if reason:
        return StandardScore(
            activity_id=raw.activity_id,
            sport=raw.sport,
            raw=raw.value,
            score=0.0,
            reference_load=ref,
            computed=False,
            reason=reason,
            verification_status=status,
            verification_note=status_note,
        )

    if raw.value <= 0 or ref <= 0:
        return StandardScore(
            activity_id=raw.activity_id,
            sport=raw.sport,
            raw=raw.value,
            score=0.0,
            reference_load=ref,
            computed=False,
            reason="原始负荷为 0，无法标准化",
            verification_status=status,
            verification_note=status_note,
        )

    score = raw.value / ref * MAX_SCORE
    if score > MAX_SCORE:
        # 不截断：保留真实值，由上层报告「参数需检查」
        return StandardScore(
            activity_id=raw.activity_id,
            sport=raw.sport,
            raw=raw.value,
            score=score,
            reference_load=ref,
            computed=True,
            reason=f"超出 100（{score:.1f}），参考负荷 R={ref:g} 可能偏小",
            verification_status=status,
            verification_note=status_note,
        )

    return StandardScore(
        activity_id=raw.activity_id,
        sport=raw.sport,
        raw=raw.value,
        score=score,
        reference_load=ref,
        verification_status=status,
        verification_note=status_note,
    )
