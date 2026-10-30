"""日报调整层级（阶段④ T4.2；REQ §12）。

**业务规则**：
- 周计划 = 用户确认的周训练安排；日报调整 = 按当天最新数据/风险/环境/恢复对**当天**训练的
  临时调整（§12.1）；
- **日报调整不得直接覆盖原周计划**（§12.1）——调整是叠加在周计划之上的「当天视图」；
- **命名规则**（§12.2）：仅日报调整后的训练名称前加「调整」，如
  ``1012-有氧轻松跑 Z2 30min`` → ``调整1012-有氧轻松跑 Z2 25min``；
  该规则**不得**扩展为所有周计划版本变化都加「调整」；
- **周计划卡片标识**（§12.3）：某日训练被日报调整 → 对应卡片显示「日报已调整」，
  可查看原周计划安排 / 日报调整后安排 / 调整原因 / 对应日报 / 实际训练情况；
- **周报对比**（§12.4）：区分 完整完成 / 部分完成 / 未完成 / 替代训练 / 日报调整后完成 /
  其他有记录状态；**计划训练量与实际训练负荷必须分开统计**，不得把原计划量当实际量。

本模块为纯逻辑，无 IO、无副作用。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.domain.load import hr as hr_mod

# 日报调整名称前缀（§12.2，仅此一种场景使用）
ADJUSTMENT_PREFIX = "调整"

# 周报执行状态分类（§12.4）
class ExecutionStatus:
    """执行状态分类（§12.4）。"""

    COMPLETED = "completed"  # 完整完成
    PARTIAL = "partial"  # 部分完成
    MISSED = "missed"  # 未完成
    SUBSTITUTED = "substituted"  # 替代训练
    ADJUSTED_COMPLETED = "adjustedCompleted"  # 日报调整后完成
    OTHER = "other"  # 其他有记录的执行状态


@dataclass(slots=True)
class DailyAdjustment:
    """一条日报调整（叠加在周计划当天安排之上）。"""

    date: dt.date
    original_name: str  # 周计划原训练名（不含前缀）
    adjusted_name: str  # 调整后训练名（含「调整」前缀）
    reason: str = ""
    report_ref: str | None = None  # 对应日报引用
    actual: str | None = None  # 实际训练情况

    @property
    def is_adjusted(self) -> bool:
        return self.adjusted_name.startswith(ADJUSTMENT_PREFIX)


def apply_adjustment_name(original_name: str, adjusted_name_body: str) -> str:
    """按 §12.2 生成调整后训练名：``调整`` + 原名（原名已在 body 内）。"""
    body = adjusted_name_body.strip()
    if body.startswith(ADJUSTMENT_PREFIX):
        return body  # 已带前缀，幂等
    return f"{ADJUSTMENT_PREFIX}{body}"
def card_view(
    original_name: str | None,
    adjustments: list[DailyAdjustment],
    *,
    date: dt.date,
) -> dict:
    """周计划卡片视图（§12.3）：原安排 / 调整后 / 原因 / 日报 / 实际。"""
    adj = next((a for a in adjustments if a.date == date), None)
    return {
        "date": date.isoformat(),
        "original": original_name,
        "adjusted": adj.adjusted_name if adj else None,
        "is_adjusted": bool(adj and adj.is_adjusted),
        "badge": "日报已调整" if (adj and adj.is_adjusted) else None,
        "reason": adj.reason if adj else None,
        "report_ref": adj.report_ref if adj else None,
        "actual": adj.actual if adj else None,
    }
def day_reason(j: DayJudgment) -> str | None:
    """单日差异原因（§12.4「记录日报调整及差异原因」）。无差异 → None。"""
    if j.status == ExecutionStatus.PARTIAL:
        parts: list[str] = []
        if j.duration_met is False:
            parts.append(f"时长不足（实际 {j.actual_min:g} / 计划 {j.planned_min:g} min）")
        if j.intensity_met is False:
            parts.append(f"强度不符（实际 {j.actual_zone or '无心率'} / 计划 {j.target_zone}）")
        return "；".join(parts) or "部分完成"
    if j.status == ExecutionStatus.MISSED:
        return "未执行"
    if j.status == ExecutionStatus.SUBSTITUTED:
        actual = "、".join(j.actual_sports) or "无记录"
        return f"替代训练（实际 {actual} / 计划 {j.planned_sport or '无'}）"
    if j.status == ExecutionStatus.OTHER:
        return "；".join(j.notes) or "其他有记录的执行状态"
    return None  # completed / adjustedCompleted：与计划一致，无差异


def build_week_execution(judgments: list[DayJudgment]) -> dict:
    """把一周的 :class:`DayJudgment` 聚成周报执行块（§12.4）。

    - ``status_counts`` 只统计**已判定**日；未到期 / 无计划无记录进
      ``unjudged_days`` 单独披露——**不进计数、不伪造**；
    - 计划量与实际量**分开统计**（实际量取当天全部 counted 分钟）；
    - 调整日、逐日明细与差异原因一并输出，供周报渲染。
    """
    judged = [j for j in judgments if j.status is not None]
    counts: dict[str, int] = {}
    for j in judged:
        counts[j.status] = counts.get(j.status, 0) + 1

    details: list[dict] = []
    for j in judged:
        d = j.to_dict()
        d["reason"] = day_reason(j)
        details.append(d)

    return {
        "status_counts": counts,
        "planned_volume_min": int(round(sum(j.planned_min or 0.0 for j in judged))),
        "actual_volume_min": int(round(sum(j.actual_volume_min for j in judged))),
        "adjusted_days": [j.date.isoformat() for j in judged if j.adjusted],
        "unjudged_days": [
            {"date": j.date.isoformat(), "reason": "；".join(j.notes) or "未判定"}
            for j in judgments
            if j.status is None
        ],
        "day_details": details,
        "note": "计划训练量与实际训练负荷分开统计，不得混同（§12.4）",
    }


# ------------------------------------------------------------
# 单日完成度判定（§12.4；规则来源见 judge_day docstring）
# ------------------------------------------------------------
# 时长记账容差（分钟，2026-10-20 决策 Q6）：只吸收设备记账噪声
# （Garmin 时长是墙钟、计划时长是 5 分钟粒度），**不是**「练多少算够」的
# 质量标准。该容差与强度同区判定一并登记为**候选判定口径**（L-11），
# 待用真实周报数据校准。
DURATION_TOLERANCE_MIN = 2.0

# 强度判定的已知局限（2026-10-20 数据核实）：Garmin splitSummaries 有
# splitType（WARMUP/ACTIVE/COOLDOWN）但**无分段心率**；Tredict 无阶段划分、
# 心率序列无时间戳 → 实际区间只能由**全程平均心率**映射，热身冷身会把它
# 往下拉。此局限必须随判定结果披露，不得伪装成「主阶段区间」。
_INTENSITY_LIMIT_NOTE = "强度按全程平均心率所在区间判定（含热身冷身；设备无分段心率）"
_NO_TARGET_NOTE = "无强度目标，未做强度判定（仅按时长判定）"
_NO_HR_NOTE = "实际活动无平均心率，无法判定强度（仅按时长判定）"


@dataclass(slots=True)
class DayJudgment:
    """单日完成度判定结果（§12.4 六态 + 时长/强度双达标 + 披露）。"""

    date: dt.date
    status: str | None = None  # 六态之一；None = 未判定（未到期 / 无计划无记录）
    planned_sport: str | None = None
    planned_min: float | None = None
    actual_min: float = 0.0  # 判定用分钟（仅计划项内实际分钟）
    actual_volume_min: float = 0.0  # 统计用：当天全部 counted 档分钟（§12.4 实际量）
    actual_sports: tuple[str, ...] = ()
    adjusted: bool = False
    target_zone: str | None = None
    actual_zone: str | None = None
    duration_met: bool | None = None  # None = 无从判定（无计划/未到期/计划缺时长）
    intensity_met: bool | None = None  # None = 无强度目标/无心率数据，未做强度判定
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """序列化（供日报 payload 落库、周报提取；S3/S5 的数据契约）。"""
        return {
            "date": self.date.isoformat(),
            "status": self.status,
            "planned_sport": self.planned_sport,
            "planned_min": self.planned_min,
            "actual_min": self.actual_min,
            "actual_volume_min": self.actual_volume_min,
            "actual_sports": list(self.actual_sports),
            "adjusted": self.adjusted,
            "target_zone": self.target_zone,
            "actual_zone": self.actual_zone,
            "duration_met": self.duration_met,
            "intensity_met": self.intensity_met,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> DayJudgment:
        """从日报 payload 还原（周报提取用；``reason`` 键由周报侧重算，忽略）。"""
        return cls(
            date=dt.date.fromisoformat(str(data["date"])),
            status=data.get("status"),
            planned_sport=data.get("planned_sport"),
            planned_min=data.get("planned_min"),
            actual_min=float(data.get("actual_min") or 0.0),
            actual_volume_min=float(data.get("actual_volume_min") or 0.0),
            actual_sports=tuple(data.get("actual_sports") or ()),
            adjusted=bool(data.get("adjusted")),
            target_zone=data.get("target_zone"),
            actual_zone=data.get("actual_zone"),
            duration_met=data.get("duration_met"),
            intensity_met=data.get("intensity_met"),
            notes=tuple(data.get("notes") or ()),
        )


def _hr_zone(hr: float | None) -> str | None:
    """平均心率 → Karvonen 五区名（``Z1``~``Z5``）；缺失/低于 Z1 下沿 → None。"""
    if hr is None:
        return None
    zones = hr_mod.build_zones()
    idx = hr_mod.zone_index(float(hr), zones)
    return None if idx is None else hr_mod.ZONE_NAMES[idx]


def judge_day(
    date: dt.date,
    *,
    planned_sport: str | None,
    planned_min: float | None = None,
    target_zone: str | None = None,
    adjusted: bool = False,
    activities: list[dict] | None = None,
    counted_sports: frozenset[str] | None = None,
    as_of: dt.date | None = None,
) -> DayJudgment:
    """单日完成度判定（§12.4；2026-10-20 决策 Q5/Q6/Q7/Q9）。

    规则（按优先级）：

    1. **未到期**（``date > as_of``）不判定——周报只评已过去的日子；
    2. **无计划**：有 counted 档活动 → 替代训练；无活动 → 不判定（无计划无记录）；
    3. **有计划无活动** → 未完成；
    4. **实际运动 ≠ 计划运动** → 替代训练；
    5. 同运动：**时长**（实际 ≥ 计划−2min 容差）**且强度**（全程均心所在区 ==
       目标区；无目标区则强度不判、只按时长）双达标 → 完整完成 / 日报调整后完成；
       任一不达标 → 部分完成；
    6. 计划缺时长 → 其他有记录的执行状态（有记录但无从判定）。

    ``activities`` 元素为服务层归一化后的 ``{"sport", "minutes", "average_hr"}``；
    ``counted_sports`` 传入集合则本函数按档位过滤（与负荷域同一口径），
    None 表示调用方已过滤。``planned_*``/``target_zone`` 应传**当天生效计划**
    （被日报调整的日子传调整后计划，``adjusted=True``）。
    """
    acts = list(activities or [])
    if counted_sports is not None:
        acts = [a for a in acts if str(a.get("sport")) in counted_sports]

    j = DayJudgment(
        date=date,
        planned_sport=planned_sport,
        planned_min=planned_min,
        adjusted=adjusted,
        target_zone=target_zone,
        actual_sports=tuple(sorted({str(a.get("sport")) for a in acts})),
    )

    if as_of is not None and date > as_of:
        j.status = None
        j.notes = ("未到期，不做判定",)
        return j

    actual_min = sum(float(a.get("minutes") or 0.0) for a in acts)
    j.actual_min = round(actual_min, 1)
    j.actual_volume_min = j.actual_min

    if planned_sport is None:
        if not acts:
            j.status = None
            j.notes = ("无计划且无记录，不做判定",)
            return j
        j.status = ExecutionStatus.SUBSTITUTED
        return j

    if not acts:
        j.status = ExecutionStatus.MISSED
        return j

    # 计划运动当天是否做过（做过则只比这一项；另做其他类型不计入替代）
    matching = [a for a in acts if str(a.get("sport")) == planned_sport]
    if not matching:
        j.status = ExecutionStatus.SUBSTITUTED
        return j

    j.actual_min = round(sum(float(a.get("minutes") or 0.0) for a in matching), 1)
    hrs = [a["average_hr"] for a in matching if a.get("average_hr") is not None]
    if hrs:
        j.actual_zone = _hr_zone(sum(hrs) / len(hrs))

    if planned_min is None or planned_min <= 0:
        j.status = ExecutionStatus.OTHER
        j.notes = ("计划时长缺失，无法判定时长",)
        return j

    j.duration_met = actual_min >= planned_min - DURATION_TOLERANCE_MIN
    notes: list[str] = []
    if target_zone is None:
        j.intensity_met = None
        notes.append(_NO_TARGET_NOTE)
    elif hrs:
        # 有心率才判强度；无心率时 intensity_met 保持 None（无从判定，不是不达标）
        j.intensity_met = j.actual_zone == target_zone
        notes.append(_INTENSITY_LIMIT_NOTE)
    else:
        notes.append(_NO_HR_NOTE)
    j.notes = tuple(notes)

    ok = j.duration_met and (j.intensity_met is not False)
    if ok:
        j.status = (
            ExecutionStatus.ADJUSTED_COMPLETED if adjusted else ExecutionStatus.COMPLETED
        )
    else:
        j.status = ExecutionStatus.PARTIAL
    return j


__all__ = [
    "ADJUSTMENT_PREFIX",
    "DURATION_TOLERANCE_MIN",
    "DailyAdjustment",
    "DayJudgment",
    "ExecutionStatus",
    "apply_adjustment_name",
    "build_week_execution",
    "card_view",
    "day_reason",
    "judge_day",
]
