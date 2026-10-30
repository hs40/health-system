"""训练负荷域 —— 类型定义（先定类型，再定实现）。

设计原则（需求 4.1 / 4.4 / 4.5）：

1. **原始负荷** 与 **标准化分值** 严格分层。原始负荷量纲随运动类型不同
   （TRIMP 是 HRR·分钟，力量是 kg·reps），**绝不跨类型相加**；
2. 只有**标准化后的 0–100 分值**可以按类型求和；
3. 不可计算的活动保留 ``computed=False`` 与原因，**不填 0、不估算**。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# 档位（沿用已确认的运动分类表）
TIER_COUNTED = "counted"          # 计入负荷
TIER_RECORD_ONLY = "record_only"  # 落库展示，不计入
TIER_EXCLUDED = "excluded"        # 完全不参与

# 跨运动类型可比性的验证状态（需求 6.2 / 6.3，各运动类型**独立**管理）
VS_VERIFIED = "verified"                # 公式与参数已按 Tredict 指标验证
VS_INSUFFICIENT = "insufficient"        # 有公式但验证证据不足 / 结果不理想
VS_UNREGISTERED = "unregistered"        # 无专属公式，走通用兜底


@dataclass(frozen=True, slots=True)
class RawLoad:
    """单次活动的原始负荷（量纲随类型不同，不进 combined）。"""

    activity_id: str
    sport: str
    value: float
    unit: str            # "hrr_min" | "kg_reps"
    formula: str         # "banister_trimp" | "strength_volume_v1"
    inputs: dict = field(default_factory=dict)   # 计算依赖的字段快照

    def to_dict(self) -> dict:
        return {
            "activity_id": self.activity_id,
            "sport": self.sport,
            "value": round(self.value, 3),
            "unit": self.unit,
            "formula": self.formula,
            "inputs": self.inputs,
        }


@dataclass(frozen=True, slots=True)
class StandardScore:
    """单次活动的标准化分值（0–100，内部保留精度）。"""

    activity_id: str
    sport: str
    raw: float
    score: float                 # 未取整，0–100
    reference_load: float        # 该类型参考负荷 R
    computed: bool = True
    reason: str = ""
    verification_status: str = VS_VERIFIED   # 该运动类型的可比性验证状态
    verification_note: str = ""

    @property
    def verified(self) -> bool:
        """该类型是否已验证通过 —— 未通过不影响展示，只影响披露（需求 6.3）。"""
        return self.verification_status == VS_VERIFIED

    @property
    def display(self) -> int:
        """展示用整数：已完成且可计算的活动最低 1 分（需求 4.5）。"""
        if not self.computed:
            return 0
        return max(1, int(self.score + 0.5))

    def to_dict(self) -> dict:
        return {
            "activity_id": self.activity_id,
            "sport": self.sport,
            "raw": round(self.raw, 3),
            "score": round(self.score, 2),
            "display": self.display if self.computed else None,
            "reference_load": self.reference_load,
            "computed": self.computed,
            "reason": self.reason,
            "verification_status": self.verification_status,
            "verification_note": self.verification_note,
        }


@dataclass(frozen=True, slots=True)
class DailyLoad:
    """某日的负荷：类型日累计 + 每日统一总负荷。"""

    date: dt.date
    by_sport: dict[str, float]                  # 类型日累计（未取整分值之和，可超 100）
    total: float                                # 各类型日累计之和，可超 100
    contributions: tuple[StandardScore, ...] = ()
    not_computable: tuple[StandardScore, ...] = ()

    @property
    def unverified_sports(self) -> dict[str, str]:
        """当日计入负荷但**跨类型可比性未验证**的运动类型 → 状态（需求 6.3）。"""
        out: dict[str, str] = {}
        for c in self.contributions:
            if not c.verified:
                out.setdefault(c.sport, c.verification_status)
        return out

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "total": round(self.total, 2),
            "by_sport": {k: round(v, 2) for k, v in sorted(self.by_sport.items())},
            "activities": [c.to_dict() for c in self.contributions],
            "not_computable": [c.to_dict() for c in self.not_computable],
            "unverified_sports": self.unverified_sports,
        }


@dataclass(frozen=True, slots=True)
class AcwrState:
    """EWMA ACWR 状态（需求 8.1–8.5）。"""

    date: dt.date
    acute_ewma: float
    chronic_ewma: float
    ratio: float | None          # 慢性为 0 → None，不伪造数值
    sufficient: bool = True
    note: str = ""

    @property
    def zone(self) -> str:
        """参考区间（需求 9.3）。边界连续不重叠，不可计算返回 ``not_available``。"""
        if self.ratio is None:
            return "not_available"
        r = self.ratio
        if r < 0.8:
            return "undertraining"
        if r <= 1.3:
            return "sweet_spot"
        if r <= 1.5:
            return "elevated"
        return "danger"

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "acute_ewma": round(self.acute_ewma, 2),
            "chronic_ewma": round(self.chronic_ewma, 2),
            "ratio": None if self.ratio is None else round(self.ratio, 3),
            "zone": self.zone,
            "sufficient": self.sufficient,
            "note": self.note,
        }
