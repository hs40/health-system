"""运动训练页面应用服务（三期 §7/§8）。

编排层：raw 读取（``storage.activity_source``）+ 纯计算（``domain``）→ API 响应字典。
raw 是唯一事实源，活动元数据不入库（AGENTS.md §7）。

契约（供 API / 前端）：
- ``calendar_month(raw_root, ym, type_filter)`` → 月历
- ``activity_detail(raw_root, date, seq)`` → 单次训练详情
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections import defaultdict
from pathlib import Path

from app.domain.training import style_of, template_of
from app.storage.activity_source import ActivityRef, load_activities


def _month_dates(ym: str) -> list[dt.date]:
    year, month = (int(x) for x in ym.split("-"))
    return [
        dt.date(year, month, day)
        for day in range(1, calendar.monthrange(year, month)[1] + 1)
    ]


def _shift_ym(ym: str, delta: int) -> str:
    """月份 ±N（跨年正确），用于上月/下月按钮。"""
    year, month = (int(x) for x in ym.split("-"))
    idx = year * 12 + (month - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _start_key(a: ActivityRef) -> tuple:
    """同日排序：按开始时间从早到晚；缺开始时间的排最后，id 兜底保稳定。"""
    return (a.start_local is None, a.start_local or dt.datetime.max, a.activity_id)


def _by_date(raw_root: Path) -> dict[dt.date, list[ActivityRef]]:
    grouped: dict[dt.date, list[ActivityRef]] = defaultdict(list)
    for a in load_activities(raw_root).values():
        if a.local_date is not None:
            grouped[a.local_date].append(a)
    return grouped


def calendar_month(raw_root: Path, ym: str, type_filter: str | None = None) -> dict:
    """完整自然月月历（§7.2）。

    同日多次训练分别显示；``seq`` 按**当天全部训练**的开始时间编号，
    不受运动类型筛选影响（§7.6 明确「切换日期后不沿用筛选」）。
    """
    grouped = _by_date(raw_root)
    days: list[dict] = []
    for d in _month_dates(ym):
        ordered = sorted(grouped.get(d, []), key=_start_key)
        seq_of = {a.activity_id: i + 1 for i, a in enumerate(ordered)}
        shown = (
            ordered
            if not type_filter
            else [a for a in ordered if a.activity_type == type_filter]
        )
        days.append(
            {
                "date": d.isoformat(),
                "items": [
                    {
                        **style_of(a.activity_type),
                        "seq": seq_of[a.activity_id],
                        "activity_id": a.activity_id,
                        "type": a.activity_type,
                    }
                    for a in shown
                ],
            }
        )
    seen = {a.activity_type for items in grouped.values() for a in items}
    return {
        "ym": ym,
        "filter": type_filter or "all",
        "prev_ym": _shift_ym(ym, -1),
        "next_ym": _shift_ym(ym, 1),
        "types": [{"key": k, **style_of(k)} for k in sorted(seen)],
        "days": days,
    }


def _build_activity(raw_root: Path, a: ActivityRef) -> dict:
    """单次活动详情（§7.4/§8）：按六类模板组装，缺失指标不虚构。"""
    from app.domain import training as tr
    from app.domain.load import hr_zones
    from app.storage.activity_source import load_fit_messages

    template = template_of(a.activity_type)
    style = style_of(a.activity_type)
    msgs = load_fit_messages(
        raw_root, a.activity_id, {"record", "session", "lap", "length", "set", "split"}
    )
    records = msgs.get("record", [])

    hrs: list[int | None] = []
    for r in records:
        v = r.get("heart_rate")
        hrs.append(int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None)

    out: dict = {
        "activity_id": a.activity_id,
        "type": a.activity_type,
        "label": style["label"],
        "template": template,
        "name": a.name,
        "duration_sec": a.duration_sec,
        "metrics": [
            {"label": "训练类型", "value": style["label"]},
            {"label": "训练名称", "value": a.name},
        ]
        + tr.build_metrics(a.payload, template),
        # Q8：逐秒 record + 项目 Karvonen 五区重算，不展示 Garmin hrTimeInZone_*
        "hr_zones": hr_zones.summarize(hrs).to_dict(),
        "hr_series": tr.build_hr_series(records),
        "training_effect": tr.training_effect(a.payload),
        "segments": [],
    }
    if template in ("running", "indoor_cardio"):
        # §8.1/§8.2 分段基于实际 Garmin Lap 数据
        out["segments"] = tr.build_segments(msgs.get("lap", []))
    elif template == "hiit":
        # §8.3 分段注明动作中文名（Q7 动作库），不复用只有时长的 lap 图
        out["segments"] = tr.build_strength_timeline(msgs.get("set", []))["segments"]
    elif template == "strength":
        # §8.4 / Q12 力量专属：结构化时间轴 + 肌群表格 + 肌肉图
        out["timeline"] = tr.build_strength_timeline(msgs.get("set", []))
        out.update(tr.build_muscle_views(out["timeline"]["segments"]))
    elif template == "swimming":
        # §8.5 只展示真实存在的游泳专属指标
        out["swimming"] = tr.build_swimming(a.payload, msgs.get("length", []))
    return out


def activity_detail(raw_root: Path, date: str, seq: int = 1) -> dict:
    """单次训练详情（§7.4/§7.5/§7.6）。

    ``seq`` 1 基，按当天全部训练的开始时间排序；跨日导航按**自然日** ±1，
    不跳过无训练日期、不沿用日历筛选。
    """
    d = dt.date.fromisoformat(date)
    ordered = sorted(_by_date(raw_root).get(d, []), key=_start_key)
    total = len(ordered)
    result: dict = {
        "date": date,
        "seq": seq,
        "total": total,
        "has_training": total > 0,
        "prev_date": (d - dt.timedelta(days=1)).isoformat(),
        "next_date": (d + dt.timedelta(days=1)).isoformat(),
        "activity": None,
    }
    if 1 <= seq <= total:
        result["activity"] = _build_activity(raw_root, ordered[seq - 1])
    return result
