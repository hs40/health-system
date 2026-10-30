"""周计划确定性安全规则（纯函数、无 IO、不依赖 LLM）。

**为什么需要这一层**：``evaluate_weekly_plan`` 原来只把 LLM 的自由文本塞进
``conflicts: list[str]``，后果是：
1. 前端无法把冲突挂到具体日期卡片上（只能整体展示一段文字）；
2. 无法分级（提示 / 警告 / 阻断）；
3. 更关键的是**没有真正的硬性安全边界**——用户拖动造成的不安全排列
   可以靠"确认"绕过。

本模块补齐确定性规则，输出结构化 ``PlanConflict``：结构化冲突命中硬规则时
``hard_block=True``，前端据此禁用「保存为最终计划」，**不可因用户确认而绕过**。

规则口径（保守、可复现）：
- ``RECOVERY_GAP``  连续高强度（Z3 及以上）无间隔 → 硬性阻断；
                    连续两天 Z4/Z5 → 硬性阻断。
- ``NIGHT_SHIFT``   夜班次日安排 Z4/Z5 → 硬性阻断；Z2/Z3 → 仅提示。
- ``LOAD_SPIKE``    单日时长 ≥ 当日中位数 × 2.5 且 ≥ 120 分钟 → 硬性阻断；
                    ≥ 1.8 倍且 ≥ 90 分钟 → 警告。
- ``SPORT_CONFLICT`` 连续两天同一运动（力量/跑步）→ 警告。
- ``GOAL_MISMATCH`` 一周内四项长期目标中某一项完全缺席 → 提示。

阈值是**工程保守值**，不宣称医学效力；调整阈值只改本文件常量。
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from enum import StrEnum

from app.domain.load.hr import ZONE_NAMES
from app.schemas.domain import TrainingSession, WeekPlanDay
from app.schemas.enums import Sport

# —— 强度判定 ——
# Z3 及以上视为「中高强度」；Z4/Z5 视为「高强度」
_HARD_ZONES = frozenset({"Z4", "Z5"})
_MODERATE_ZONES = frozenset({"Z3", "Z4", "Z5"})
# 无心率区间时用运动类型推断（力量默认中高强度）
_STRENGTH_IS_MODERATE = True

# —— 负荷尖峰阈值（分钟） ——
_SPIKE_HARD_RATIO = 2.5
_SPIKE_HARD_MIN = 120
_SPIKE_WARN_RATIO = 1.8
_SPIKE_WARN_MIN = 90

# —— 目标覆盖判定：一周内至少出现一次的运动类型 ——
_GOAL_SPORTS: dict[str, Sport] = {
    "cardio": Sport.RUNNING,
    "muscleEndurance": Sport.INDOOR_CARDIO,
    "maxStrength": Sport.STRENGTH,
}

GOAL_LABELS: dict[str, str] = {
    "cardio": "心肺功能",
    "fatLoss": "减脂减重",
    "muscleEndurance": "肌肉耐力",
    "maxStrength": "最大力量",
}


class ConflictType(StrEnum):
    RECOVERY_GAP = "recovery_gap"
    NIGHT_SHIFT = "night_shift"
    LOAD_SPIKE = "load_spike"
    SPORT_CONFLICT = "sport_conflict"
    GOAL_MISMATCH = "goal_mismatch"


class ConflictSeverity(StrEnum):
    INFO = "info"
    WARN = "warn"
    BLOCK = "block"


@dataclass(slots=True)
class PlanConflict:
    """结构化冲突：可直接序列化给前端，按 ``days`` 挂到对应卡片。"""

    days: list[str]
    type: ConflictType
    severity: ConflictSeverity
    hard_block: bool
    message: str
    # 命中规则的来源：rule = 确定性规则，ai = LLM 输出
    source: str = "rule"

    def to_dict(self) -> dict:
        return {
            "days": list(self.days),
            "kind": str(self.type),
            "severity": str(self.severity),
            "hard_block": self.hard_block,
            "message": self.message,
            "source": self.source,
        }


def _zone_of(s: TrainingSession) -> str | None:
    z = (s.target_hr_zone or "").strip().upper()
    return z if z in {"Z1", "Z2", "Z3", "Z4", "Z5"} else None


def _is_moderate(s: TrainingSession) -> bool:
    z = _zone_of(s)
    if z is None:
        return s.sport is Sport.STRENGTH and _STRENGTH_IS_MODERATE
    return z in _MODERATE_ZONES


def _is_hard(s: TrainingSession) -> bool:
    z = _zone_of(s)
    if z is None:
        return False
    return z in _HARD_ZONES


def _duration(s: TrainingSession) -> int:
    return int(s.duration_min or 0)


def _fmt(day: dt.date) -> str:
    return day.isoformat()


def _weekday_cn(d: dt.date) -> str:
    return "周" + "一二三四五六日"[d.weekday()]


def _recovery_gap_conflicts(days: list[WeekPlanDay]) -> list[PlanConflict]:
    """连续中高强度日无间隔 + 连续两天高强度。"""
    out: list[PlanConflict] = []
    ordered = sorted(days, key=lambda d: d.date)

    # 1) 连续两天高强度
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        if prev.session and cur.session and _is_hard(prev.session) and _is_hard(cur.session):
            out.append(
                PlanConflict(
                    days=[_fmt(prev.date), _fmt(cur.date)],
                    type=ConflictType.RECOVERY_GAP,
                    severity=ConflictSeverity.BLOCK,
                    hard_block=True,
                    message=(
                        f"{_weekday_cn(prev.date)}与{_weekday_cn(cur.date)}连续两天高强度"
                        f"（{prev.session.target_hr_zone}/{cur.session.target_hr_zone}），"
                        "两次训练间隔不足 24 小时，恢复不足。建议把其中一天降为 Z2 或改为休息。"
                    ),
                )
            )

    # 2) 连续三天中高强度
    run: list[WeekPlanDay] = []
    for d in ordered:
        if d.session is not None and _is_moderate(d.session):
            run.append(d)
        else:
            if len(run) >= 3:
                out.append(_consecutive_block(run))
            run = []
    if len(run) >= 3:
        out.append(_consecutive_block(run))
    return out


def _consecutive_block(run: list[WeekPlanDay]) -> PlanConflict:
    return PlanConflict(
        days=[_fmt(run[0].date), _fmt(run[-1].date)],
        type=ConflictType.RECOVERY_GAP,
        severity=ConflictSeverity.BLOCK,
        hard_block=True,
        message=(
            f"{_weekday_cn(run[0].date)}至{_weekday_cn(run[-1].date)}连续 "
            f"{len(run)} 天中高强度训练没有恢复日，超出周内恢复间隔上限。"
            "建议至少插入一天休息或 Z1 恢复跑。"
        ),
    )


def _night_shift_conflicts(days: list[WeekPlanDay]) -> list[PlanConflict]:
    """夜班次日高强度 → 阻断；提示强度 → 仅提示。"""
    out: list[PlanConflict] = []
    by_date = {d.date: d for d in days}
    for d in sorted(days, key=lambda x: x.date):
        if not d.is_night_shift:
            continue
        nxt = by_date.get(d.date + dt.timedelta(days=1))
        if nxt is None or nxt.session is None:
            continue
        s = nxt.session
        z = _zone_of(s)
        if _is_hard(s):
            out.append(
                PlanConflict(
                    days=[_fmt(d.date), _fmt(nxt.date)],
                    type=ConflictType.NIGHT_SHIFT,
                    severity=ConflictSeverity.BLOCK,
                    hard_block=True,
                    message=(
                        f"{_weekday_cn(d.date)}夜班，次日{_weekday_cn(nxt.date)}安排"
                        f"「{s.name}」（{z} 高强度）。夜班后睡眠与恢复受压缩，"
                        "高强度训练风险高。建议改为 Z2 以内或休息。"
                    ),
                )
            )
        else:
            out.append(
                PlanConflict(
                    days=[_fmt(d.date), _fmt(nxt.date)],
                    type=ConflictType.NIGHT_SHIFT,
                    severity=ConflictSeverity.INFO,
                    hard_block=False,
                    message=(
                        f"{_weekday_cn(d.date)}夜班，次日{_weekday_cn(nxt.date)}训练已控制在中低强度，"
                        "请按晨起恢复指标（HRV / 静息心率）临时下调时长。"
                    ),
                )
            )
    return out


def _load_spike_conflicts(days: list[WeekPlanDay]) -> list[PlanConflict]:
    """单日时长相对**其余训练日**的尖峰（剔除自身，避免短周被自己抬高基准）。"""
    all_durations = [_duration(d.session) for d in days if d.session is not None]
    all_durations = [x for x in all_durations if x > 0]
    if len(all_durations) < 2:
        return []
    out: list[PlanConflict] = []
    for d in sorted(days, key=lambda x: x.date):
        if d.session is None:
            continue
        dur = _duration(d.session)
        if dur <= 0:
            continue
        others = [x for x in all_durations if x != dur] or all_durations
        median = statistics.median(others)
        if median <= 0:
            continue
        ratio = dur / median
        if ratio >= _SPIKE_HARD_RATIO and dur >= _SPIKE_HARD_MIN:
            out.append(
                PlanConflict(
                    days=[_fmt(d.date)],
                    type=ConflictType.LOAD_SPIKE,
                    severity=ConflictSeverity.BLOCK,
                    hard_block=True,
                    message=(
                        f"{_weekday_cn(d.date)}「{d.session.name}」时长 {dur} 分钟，"
                        f"是本周单日中位数（{median:.0f} 分钟）的 {ratio:.1f} 倍，"
                        "负荷尖峰过大。建议拆分到两天，或缩短至 90 分钟以内。"
                    ),
                )
            )
        elif ratio >= _SPIKE_WARN_RATIO and dur >= _SPIKE_WARN_MIN:
            out.append(
                PlanConflict(
                    days=[_fmt(d.date)],
                    type=ConflictType.LOAD_SPIKE,
                    severity=ConflictSeverity.WARN,
                    hard_block=False,
                    message=(
                        f"{_weekday_cn(d.date)}时长 {dur} 分钟，明显高于本周其余训练"
                        f"（中位数 {median:.0f} 分钟），注意次日安排恢复。"
                    ),
                )
            )
    return out


def _sport_conflicts(days: list[WeekPlanDay]) -> list[PlanConflict]:
    """连续两天同类型训练 → 提示。"""
    out: list[PlanConflict] = []
    ordered = sorted(days, key=lambda d: d.date)
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        if prev.session is None or cur.session is None:
            continue
        a, b = prev.session, cur.session
        if a.sport is not b.sport:
            continue
        if a.sport not in (Sport.STRENGTH, Sport.RUNNING):
            continue
        label = "力量" if a.sport is Sport.STRENGTH else "跑步"
        out.append(
            PlanConflict(
                days=[_fmt(prev.date), _fmt(cur.date)],
                type=ConflictType.SPORT_CONFLICT,
                severity=ConflictSeverity.WARN,
                hard_block=False,
                message=(
                    f"{_weekday_cn(prev.date)}与{_weekday_cn(cur.date)}连续安排{label}训练，"
                    "同部位/同动作模式恢复不足。建议中间插入不同类型或休息。"
                ),
            )
        )
    return out


def _goal_conflicts(
    days: list[WeekPlanDay], goal_weights: dict[str, float] | None
) -> list[PlanConflict]:
    """四项长期目标里权重最高却一周缺席的项 → 提示。"""
    weights = goal_weights or {}
    if not weights:
        return []
    presented = {d.session.sport for d in days if d.session is not None}
    out: list[PlanConflict] = []
    top = max(weights, key=lambda k: weights[k])
    for goal, sport in _GOAL_SPORTS.items():
        if weights.get(goal, 0) <= 0:
            continue
        if sport in presented:
            continue
        if goal not in ("cardio", "maxStrength") and goal != top:
            continue
        out.append(
            PlanConflict(
                days=[],
                type=ConflictType.GOAL_MISMATCH,
                severity=ConflictSeverity.INFO,
                hard_block=False,
                message=(
                    f"本周没有安排可推进「{GOAL_LABELS[goal]}」的训练"
                    f"（权重 {weights[goal]:.2f}）。若为阶段性取舍，请在下周计划说明。"
                ),
            )
        )
    return out


def check_plan(
    days: list[WeekPlanDay],
    *,
    goal_weights: dict[str, float] | None = None,
) -> list[PlanConflict]:
    """对一周计划做确定性安全校验。

    返回冲突列表（已按严重级别降序）。**任一 ``hard_block`` 即表示该计划
    不得保存为最终版**——这是安全边界，不因用户确认而放宽。
    """
    if not days:
        return []
    out: list[PlanConflict] = []
    out.extend(_recovery_gap_conflicts(days))
    out.extend(_night_shift_conflicts(days))
    out.extend(_load_spike_conflicts(days))
    out.extend(_sport_conflicts(days))
    out.extend(_goal_conflicts(days, goal_weights))
    order = {ConflictSeverity.BLOCK: 0, ConflictSeverity.WARN: 1, ConflictSeverity.INFO: 2}
    out.sort(key=lambda c: (order[c.severity], c.days))
    return out


def has_hard_block(conflicts: list[PlanConflict]) -> bool:
    return any(c.hard_block for c in conflicts)


# ============================================================
# 心率区间白名单（⑥ 去重，2026-10-26；原 coach_service / plan_service 各一份）
# ============================================================
# 与 domain/load/hr.py 的 ZONE_NAMES 同源（单一事实来源）。
VALID_ZONES: frozenset[str] = frozenset(ZONE_NAMES)


def valid_zone(value: object) -> str | None:
    """目标心率区间白名单校验：仅接受 Z1–Z5，其余（None/越界/脏值）→ None。"""
    return str(value) if value in VALID_ZONES else None


def zone_int(value: object) -> int | None:
    """``"Z3"`` → ``3``；None/脏值/越界 → None（不阻塞处方合成）。"""
    s = str(value or "")
    ok = len(s) == 2 and s[0] == "Z" and s[1].isdigit() and 1 <= int(s[1]) <= 5
    return int(s[1:]) if ok else None
