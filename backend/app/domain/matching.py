"""Garmin ↔ Tredict 活动匹配（P6）。

**纯函数模块**：不碰数据库、不碰网络，输入两组轻量引用、输出配对结论，
便于单测与事后复核（``data/raw/tredict/_match_report.json``）。

为什么用**开始时间**而不是时长做主键（推翻最初的「时长差贪心」方案）
--------------------------------------------------------------------
实测 298 条 Tredict 活动与 301 条 Garmin 活动按 ``|Δt|`` 配对：

======  ======  ======  ======  ======
容差    无候选  多候选  重复占用  已配
======  ======  ======  ======  ======
±1s     0       0       0       298
±2s     0       0       0       298
±5s     0       0       0       298
±60s    0       0       0       298
======  ======  ======  ======  ======

298/298 差值 ≤1 秒，时间天然唯一；最危险的场景（2026-10-04 三条步行，
时长只差 68s）按时间也完全分离。故时间为主键、时长为校验。

时长口径必须按运动分叉（Q8）
----------------------------
游泳是系统性口径差异——Tredict 的 ``duration`` 只算「划水时间」，
``durationTotal`` 才是墙钟总时长：

============  ========  =========  ==============
日期          Garmin    duration   durationTotal
============  ========  =========  ==============
2025-07-26    4800      2036       4800 ✓
2025-07-24    3848      1489       3851 ✓
2025-07-21    2973      1035       2973 ✓
2025-09-18    1141      996        1141 ✓
============  ========  =========  ==============

反之力量训练用 ``durationTotal`` 会把「组间休息」算进去，43 条里最多差 2195s。
所以：**游泳用 durationTotal，其余用 duration**，298/298 全部落 ±5s。
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Any

# 时间容差（Q9）：实测 298/298 差值 ≤1s，取 2s 留一点抖动余量
TIME_TOLERANCE_SEC = 2
# 时长容差（Q11）：≤5s 判 high；超差不判 none，只降 medium
DURATION_TOLERANCE_SEC = 5

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_AMBIGUOUS = "ambiguous"
CONFIDENCE_NONE = "none"

# ============================================================
# 运动类型映射（Q2：显式表，未命中落 misc）
# ============================================================
# 键 = (Tredict sportType, Tredict subSportType)；值 = Garmin typeKey
SPORT_MAP: dict[tuple[str, str], str] = {
    ("running", "generic"): "running",
    ("swimming", "lap_swimming"): "lap_swimming",
    ("misc", "walking"): "walking",
    ("misc", "strength_training"): "strength_training",
    ("misc", "hiking"): "hiking",
    ("misc", "cardio_training"): "indoor_cardio",
}

# Tredict 侧归入 misc/generic 的活动，实际对应这些 Garmin 类型。
# 实测 31 条 = meditation 18 + breathwork 4 + hiit 9（另 3 条为 Garmin 独有）。
GENERIC_GARMIN: frozenset[str] = frozenset({"meditation", "breathwork", "hiit"})

# 游泳的时长口径特殊（见模块 docstring）
SWIM_SPORT = "swimming"


# ============================================================
# 输入 / 输出
# ============================================================
@dataclass(frozen=True, slots=True)
class GarminRef:
    """匹配所需的 Garmin 活动最小视图（真 UTC 开始时间）。"""

    activity_id: str
    start_utc: dt.datetime
    activity_type: str
    duration_sec: float | None = None


@dataclass(frozen=True, slots=True)
class TredictRef:
    """匹配所需的 Tredict 活动最小视图（``date`` 已是真 UTC）。"""

    tredict_id: str
    start_utc: dt.datetime
    sport_type: str
    sub_sport_type: str = ""
    duration_sec: float | None = None
    duration_total_sec: float | None = None


@dataclass(slots=True)
class MatchOutcome:
    """单条 Tredict 活动的匹配结论。"""

    tredict_id: str
    garmin_id: str | None
    confidence: str
    detail: dict[str, Any] = field(default_factory=dict)


# ============================================================
# 纯函数
# ============================================================
def garmin_type_for(sport_type: str, sub_sport_type: str) -> str:
    """Tredict 的 (sport, subSport) → Garmin ``typeKey``；未命中返回 ``misc``。"""
    return SPORT_MAP.get((sport_type or "", sub_sport_type or ""), "misc")


def types_compatible(garmin_type: str, sport_type: str, sub_sport_type: str) -> bool:
    """两侧运动类型是否兼容。

    ``misc`` 是兜底桶，其下混着 meditation / breathwork / hiit 三种 Garmin 类型，
    因此不能用等号判定，改用 ``GENERIC_GARMIN`` 白名单。
    """
    mapped = garmin_type_for(sport_type, sub_sport_type)
    if mapped == "misc":
        return garmin_type in GENERIC_GARMIN
    return garmin_type == mapped


def pick_duration(ref: TredictRef) -> tuple[float | None, str]:
    """按运动选时长字段（Q8）→ ``(秒数, 字段名)``。"""
    if ref.sport_type == SWIM_SPORT:
        return ref.duration_total_sec, "durationTotal"
    return ref.duration_sec, "duration"


def match_activities(
    garmin: list[GarminRef],
    tredict: list[TredictRef],
    *,
    time_tolerance_sec: int = TIME_TOLERANCE_SEC,
    duration_tolerance_sec: int = DURATION_TOLERANCE_SEC,
) -> list[MatchOutcome]:
    """全量重算配对（Q17：不增量，每次从头算）。

    三轮：
    1. 每条 Tredict 在 ±容差内找 Garmin 候选；
    2. 候选唯一 → 暂定配对；0 个 → ``none``；多个 → ``ambiguous``；
    3. **重复占用检测**：若同一 Garmin 被多条 Tredict 暂定占用，全部降为
       ``ambiguous``——宁可暴露问题，也不按遍历顺序随意分配。
    """
    tol = dt.timedelta(seconds=time_tolerance_sec)
    ordered = sorted(garmin, key=lambda g: g.start_utc)
    stamps = [g.start_utc for g in ordered]

    outcomes: list[MatchOutcome] = []
    tentative: dict[str, list[int]] = {}  # garmin_id → outcome 下标

    for ref in tredict:
        lo = bisect_left(stamps, ref.start_utc - tol)
        hi = bisect_right(stamps, ref.start_utc + tol)
        cands = ordered[lo:hi]

        if not cands:
            outcomes.append(
                MatchOutcome(
                    ref.tredict_id,
                    None,
                    CONFIDENCE_NONE,
                    {"reason": "no_candidate", "tolerance_sec": time_tolerance_sec},
                )
            )
            continue

        if len(cands) > 1:
            outcomes.append(
                MatchOutcome(
                    ref.tredict_id,
                    None,
                    CONFIDENCE_AMBIGUOUS,
                    {
                        "reason": "multiple_candidates",
                        "candidates": [c.activity_id for c in cands],
                    },
                )
            )
            continue

        cand = cands[0]
        outcomes.append(
            _grade(ref, cand, duration_tolerance_sec)
        )
        tentative.setdefault(cand.activity_id, []).append(len(outcomes) - 1)

    # 重复占用：一个 Garmin 被多条 Tredict 认领
    for garmin_id, idxs in tentative.items():
        if len(idxs) < 2:
            continue
        claimants = [outcomes[i].tredict_id for i in idxs]
        for i in idxs:
            outcomes[i] = MatchOutcome(
                outcomes[i].tredict_id,
                None,
                CONFIDENCE_AMBIGUOUS,
                {
                    "reason": "duplicate_claim",
                    "garmin_id": garmin_id,
                    "claimants": claimants,
                },
            )

    return outcomes


def _grade(ref: TredictRef, cand: GarminRef, duration_tolerance_sec: int) -> MatchOutcome:
    """对唯一候选定级（Q10 / Q11）。"""
    delta_sec = (ref.start_utc - cand.start_utc).total_seconds()
    duration, duration_field = pick_duration(ref)
    detail: dict[str, Any] = {
        "garmin_start_utc": cand.start_utc.isoformat(sep=" "),
        "time_delta_sec": round(delta_sec, 3),
        "duration_field": duration_field,
    }

    type_ok = types_compatible(cand.activity_type, ref.sport_type, ref.sub_sport_type)
    if not type_ok:
        detail["type_mismatch"] = {
            "garmin": cand.activity_type,
            "tredict": f"{ref.sport_type}/{ref.sub_sport_type}",
            "mapped": garmin_type_for(ref.sport_type, ref.sub_sport_type),
        }

    if duration is None or cand.duration_sec is None:
        detail["duration_missing"] = True
        return MatchOutcome(ref.tredict_id, cand.activity_id, CONFIDENCE_MEDIUM, detail)

    duration_delta = abs(float(duration) - float(cand.duration_sec))
    detail["duration_delta"] = round(duration_delta, 3)
    detail["tredict_duration"] = duration
    detail["garmin_duration"] = cand.duration_sec

    if not type_ok:
        # 类型不符 → medium（Q10：仍算匹配，但打标记）
        return MatchOutcome(ref.tredict_id, cand.activity_id, CONFIDENCE_MEDIUM, detail)

    if duration_delta <= duration_tolerance_sec:
        return MatchOutcome(ref.tredict_id, cand.activity_id, CONFIDENCE_HIGH, detail)

    # 时间命中、时长超差 → medium；**绝不判 none**（Q11）
    return MatchOutcome(ref.tredict_id, cand.activity_id, CONFIDENCE_MEDIUM, detail)


def summarize(outcomes: list[MatchOutcome]) -> dict[str, int]:
    """按置信度计数，用于报告与验收。"""
    out = {CONFIDENCE_HIGH: 0, CONFIDENCE_MEDIUM: 0, CONFIDENCE_AMBIGUOUS: 0, CONFIDENCE_NONE: 0}
    for o in outcomes:
        out[o.confidence] = out.get(o.confidence, 0) + 1
    return out


def unmatched_garmin(
    garmin: list[GarminRef], outcomes: list[MatchOutcome]
) -> list[str]:
    """Garmin 侧没被任何 Tredict 认领的 activity_id（Q12：不建占位行，只报告）。"""
    claimed = {o.garmin_id for o in outcomes if o.garmin_id}
    return sorted(g.activity_id for g in garmin if g.activity_id not in claimed)
