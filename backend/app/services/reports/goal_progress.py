"""四项长期目标进展（日报/周报/月报共用口径）。

从原 ``report_service.py`` 拆出（①，2026-10-26），行为不变。
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from app.db.models import (
    TrainingSessionRow,
)
from app.schemas.domain import (
    StandardDaily,
)
from app.storage.standard_store import load_standard_range

# ============================================================
# 四项长期目标进展（2026-10-22 需求：日报/周报/月报都要展示）
# ============================================================
# 口径声明（前端会原样展示，不得含糊）：
# - cardio       心肺功能   实测：区间平均静息心率（越低越好）→ 目标方向 down
# - fatLoss      减脂减重   实测：区间平均体重（kg）→ down
# - muscleEndurance 肌肉耐力 实测：Z2–Z3 总时长（分钟）→ up
# - maxStrength  最大力量   实测：力量训练总容量 = Σ 组数×次数×负荷（kg）→ up
# 基线 = 紧邻的前一个等长区间（周报取上一周，月报取上一月）；
# 无该区间数据时 baseline/delta 为 None —— 不编造、不以 0 代替缺失。
GOAL_SPEC: dict[str, dict] = {
    "cardio": {
        "label": "心肺功能",
        "unit": "bpm",
        "direction": "down",
        "hint": "区间平均静息心率，越低代表心肺基础越好",
    },
    "fatLoss": {
        "label": "减脂减重",
        "unit": "kg",
        "direction": "down",
        "hint": "区间平均体重",
    },
    "muscleEndurance": {
        "label": "肌肉耐力",
        "unit": "min",
        "direction": "up",
        "hint": "Z2–Z3 训练总时长",
    },
    "maxStrength": {
        "label": "最大力量",
        "unit": "kg",
        "direction": "up",
        "hint": "力量训练总容量（组×次×重量）",
    },
}


def _zone_minutes_by_date(
    session: Session, start: dt.date | None = None, end: dt.date | None = None
) -> dict[dt.date, dict[str, int]]:
    """从训练会话的处方步骤里累计各心率区间分钟数（无区间 → 不计入）。

    ``start``/``end`` 限定扫描窗口（① 性能：避免每份日报全表扫描 TrainingSessionRow）。
    """

    stmt = select(TrainingSessionRow)
    if start is not None:
        stmt = stmt.where(TrainingSessionRow.date >= start)
    if end is not None:
        stmt = stmt.where(TrainingSessionRow.date <= end)
    out: dict[dt.date, dict[str, int]] = {}
    rows = session.exec(stmt).all()
    for r in rows:
        payload = r.payload or {}
        bucket = out.setdefault(r.date, {})
        for phase in ("warmup", "main", "cooldown"):
            for step in payload.get(phase) or []:
                zone = step.get("hr_zone")
                if zone is None:
                    continue
                sec = step.get("duration_sec") or 0
                try:
                    minutes = int(round(float(sec) / 60))
                except (TypeError, ValueError):
                    continue
                key = f"Z{int(zone)}"
                bucket[key] = bucket.get(key, 0) + minutes
    return out


def _strength_volume_by_date(
    session: Session, start: dt.date | None = None, end: dt.date | None = None
) -> dict[dt.date, float]:
    """力量训练容量：Σ sets×reps×load_kg（无负荷的体重动作按 0 计入次数但不计容量）。

    ``start``/``end`` 限定扫描窗口（① 性能，同 ``_zone_minutes_by_date``）。
    """

    stmt = select(TrainingSessionRow)
    if start is not None:
        stmt = stmt.where(TrainingSessionRow.date >= start)
    if end is not None:
        stmt = stmt.where(TrainingSessionRow.date <= end)
    out: dict[dt.date, float] = {}
    rows = session.exec(stmt).all()
    for r in rows:
        payload = r.payload or {}
        total = 0.0
        for item in payload.get("strength") or []:
            sets = item.get("sets") or 0
            reps = item.get("reps") or 0
            load = item.get("load_kg")
            if load is None:
                continue
            total += float(sets) * float(reps) * float(load)
        if total > 0:
            out[r.date] = out.get(r.date, 0.0) + total
    return out


def _goal_value(
    key: str,
    dailies: list[StandardDaily],
    zone_minutes: dict[dt.date, dict[str, int]],
    strength_volume: dict[dt.date, float],
) -> tuple[float | None, str]:
    """返回 (值, 来源)；来源 insufficient 表示无数据，值为 None。"""
    if key == "cardio":
        vals = [float(d.resting_heart_rate) for d in dailies if d.resting_heart_rate is not None]
        return (round(sum(vals) / len(vals), 1) if vals else None, "measured")
    if key == "fatLoss":
        vals = [float(d.weight_kg) for d in dailies if d.weight_kg is not None]
        return (round(sum(vals) / len(vals), 1) if vals else None, "measured")
    if key == "muscleEndurance":
        dates = {d.date for d in dailies}
        minutes = sum(
            bucket.get("Z2", 0) + bucket.get("Z3", 0)
            for day, bucket in zone_minutes.items()
            if day in dates
        )
        return (float(minutes), "measured") if minutes > 0 else (None, "insufficient")
    if key == "maxStrength":
        dates = {d.date for d in dailies}
        total = sum(v for day, v in strength_volume.items() if day in dates)
        return (round(total, 0), "measured") if total > 0 else (None, "insufficient")
    return None, "insufficient"


def load_goal_progress(
    session: Session, start: dt.date, end: dt.date, *, prev_start: dt.date | None = None
) -> dict:
    """四项长期目标进展（结构化，供日报/周报/月报共用）。

    ``prev_start`` 为基线区间起点；缺省时基线区间长度与本期一致、紧邻本期之前。
    基线缺失 → ``baseline``/``delta`` 为 None。
    """
    span_days = (end - start).days + 1
    if prev_start is None:
        prev_start = start - dt.timedelta(days=span_days)
    prev_end = prev_start + dt.timedelta(days=span_days - 1)

    cur_dailies = load_standard_range(session, start, end)
    prev_dailies = load_standard_range(session, prev_start, prev_end)
    zone_minutes = _zone_minutes_by_date(session, prev_start, end)
    strength_volume = _strength_volume_by_date(session, prev_start, end)

    out: dict = {}
    for key, spec in GOAL_SPEC.items():
        value, source = _goal_value(key, cur_dailies, zone_minutes, strength_volume)
        baseline, _ = _goal_value(key, prev_dailies, zone_minutes, strength_volume)
        delta = None
        if value is not None and baseline is not None:
            delta = round(value - baseline, 2)
        limitations: list[str] = []
        if value is None:
            limitations.append("本期该指标无实测数据，无法评估进展")
        if baseline is None and value is not None:
            limitations.append("缺少上一等长区间基线，暂不显示变化量")
        out[key] = {
            **spec,
            "value": value,
            "baseline": baseline,
            "delta": delta,
            "source": source if value is not None else "insufficient",
            "baseline_start": prev_start.isoformat(),
            "baseline_end": prev_end.isoformat(),
            "range_start": start.isoformat(),
            "range_end": end.isoformat(),
            "limitations": limitations,
        }
    return out
