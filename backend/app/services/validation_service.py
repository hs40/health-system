"""Tredict 验证服务（需求第七章 / 6.4，开发计划 4.10）。

职责：把**库里已有的**数据整理成 ``Sample``（同一活动的项目负荷 ↔ Tredict 指标），
交给 ``app.domain.load.validation`` 做逐指标回归。本服务**不联网、不写库**：

- 需求 6.4：验证由用户手动触发，不自动周期执行；
- 需求 6.4：**不要求保存每一次验证运行的独立历史记录** —— 结果只在当次返回；
- 需求 7.4：Tredict 指标只用于验证，**不得**并入项目负荷或替代 ACWR。

配对来源（全部读本地库，不重新拉取）：

| 数据 | 位置 | 用途 |
|---|---|---|
| 活动 payload | ``raw/garmin/activities/*.json``（10-21 起直接读） | 负荷 / 心率 / 功率 / 配速 |
| 努力值 | ``tredict_generated(entity='effort')`` | ``trainingEfforts`` |

关于 ``trainingEfforts`` 的配对（重要）：Tredict 官方接口**不返回 activityId**，
只按日期返回「当天 N 个 effort」。实测本地 301 条活动 / 218 天的**活动数与 effort
数逐日完全一致**，故按「同一天内按开始时间顺序」一一配对。某天数量不一致时
整天跳过并计入 ``unmatched_days`` —— **宁可不配对，也不做错的关联**。
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from pathlib import Path

from sqlmodel import Session, select

from app.config import Settings
from app.db.models import TredictGeneratedRow
from app.domain import load as load_domain
from app.domain.load import validation as val
from app.storage.activity_source import load_activities
from app.storage.standard_store import load_standard_range

_POWER_KEYS = ("avgPower", "averagePower")
_SPEED_KEYS = ("avgSpeed", "averageSpeed")


def _first_float(payload: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = payload.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _efforts_by_date(session: Session) -> dict[dt.date, list[float]]:
    """读本地努力值：``{日期: [effort, ...]}``（保持 Tredict 返回顺序）。"""
    rows = session.exec(
        select(TredictGeneratedRow).where(TredictGeneratedRow.entity == "effort")
    ).all()
    out: dict[dt.date, list[float]] = {}
    for r in rows:
        payload = r.payload or {}
        raw_key = str(payload.get("date") or r.entity_key or "")
        try:
            day = dt.date(int(raw_key[:4]), int(raw_key[4:6]), int(raw_key[6:8]))
        except (ValueError, IndexError):
            continue
        efforts: list[float] = []
        for item in payload.get("efforts") or []:
            # 结构为 [[value, {isRace}?], ...]，只取第一个位置的数值
            if isinstance(item, (list, tuple)) and item:
                try:
                    efforts.append(float(item[0]))
                except (TypeError, ValueError):
                    continue
        if efforts:
            out[day] = efforts
    return out


def _weights_by_day(
    session: Session, start: dt.date, end: dt.date
) -> dict[dt.date, float]:
    """活动日的体重（力量负荷需要）。

    与生产链路同一口径：当日没有实测体重要**前向沿用最近实测值**（最多回看 180 天），
    否则自重动作整片变「不可计算」。不用估算、不用均值；超窗口仍无 → 不填。
    """
    rows = load_standard_range(session, start - dt.timedelta(days=180), end)
    known = {sd.date: float(sd.weight_kg) for sd in rows if sd.weight_kg}
    out: dict[dt.date, float] = {}
    day = start
    last: float | None = None
    for sd in sorted(rows, key=lambda x: x.date):
        if sd.date < start and sd.weight_kg:
            last = float(sd.weight_kg)
    while day <= end:
        if day in known:
            last = known[day]
        if last is not None:
            out[day] = last
        day += dt.timedelta(days=1)
    return out


def _activity_load(payload: dict, day: dt.date, *, weight: float | None) -> float | None:
    """单条活动的项目负荷（标准化 0–100 分值）；不可计算返回 ``None``。

    与生产链路走**同一个** ``compute_day``（内部 TRIMP/volume + 标准化），
    不另写一套公式 —— 否则验证的就不是生产口径。
    """
    daily = load_domain.compute_day([payload], day, body_weight_kg=weight)
    for c in daily.contributions:
        if c.computed:
            return float(c.score)
    return None


def _group_by_day(
    raw_root: Path, start: dt.date | None, end: dt.date | None
) -> dict[dt.date, list[tuple[str, dt.datetime, str, dict]]]:
    """按日分组（2026-10-21：改读 raw，``garmin_activity`` 表已废弃）。"""
    acts: dict[dt.date, list[tuple[str, dt.datetime, str, dict]]] = defaultdict(list)
    for ref in load_activities(raw_root).values():
        day = ref.local_date
        if day is None:
            continue
        if (start and day < start) or (end and day > end):
            continue
        started = ref.start_local or dt.datetime.combine(day, dt.time.min)
        acts[day].append((ref.activity_id, started, ref.activity_type, ref.payload))
    return acts


def collect_samples(
    session: Session,
    settings: Settings,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> tuple[list[val.Sample], dict]:
    """构建配对样本，返回 ``(samples, info)``。

    ``info`` 记录可披露的配对情况：``activities`` / ``paired`` / ``no_load`` /
    ``unmatched_days`` / ``days``。
    """
    _ = settings  # 当前不依赖外部连接；保留签名以便将来接入在线补数
    acts = _group_by_day(Path(settings.raw_data_dir) / "garmin", start, end)
    if not acts:
        return [], {"activities": 0, "paired": 0, "no_load": 0, "unmatched_days": 0, "days": 0}

    weights = _weights_by_day(session, min(acts), max(acts))
    efforts = _efforts_by_date(session)

    samples: list[val.Sample] = []
    stats = {"activities": 0, "paired": 0, "no_load": 0, "unmatched_days": 0, "days": len(acts)}
    for day, items in sorted(acts.items()):
        items.sort(key=lambda x: (x[1], x[0]))  # 同日按开始时间，并列时按 id 稳定排序
        stats["activities"] += len(items)

        day_efforts = efforts.get(day)
        pair_effort = day_efforts is not None and len(day_efforts) == len(items)
        if not pair_effort:
            stats["unmatched_days"] += 1

        for idx, (aid, _started, sport, payload) in enumerate(items):
            load = _activity_load(payload, day, weight=weights.get(day))
            if load is None:
                stats["no_load"] += 1
                continue

            def add(
                metric: str,
                y: float | None,
                *,
                sport: str = sport,
                aid: str = aid,
                load: float = load,
            ) -> None:
                if y is None or not val.metric_allowed(metric, sport):
                    return
                samples.append(
                    val.Sample(
                        metric=metric, activity_id=aid, sport=sport, x=load, y=y
                    )
                )

            if pair_effort:
                add(val.METRIC_EFFORT, day_efforts[idx])

            # effort.heartrate / power / pace：需求 7.1 限定「仅跑步活动」
            if sport == "running":
                hr = payload.get("averageHR")
                add(val.METRIC_HEARTRATE, float(hr) if hr else None)
                add(val.METRIC_POWER, _first_float(payload, _POWER_KEYS))
                add(val.METRIC_PACE, _first_float(payload, _SPEED_KEYS))

    stats["paired"] = len({s.activity_id for s in samples})
    stats["sample_rows"] = len(samples)  # 一个活动可同时贡献多个指标
    return samples, stats


def _days_of(raw_root: Path, samples: list[val.Sample]) -> list[dt.date]:
    """样本涉及的日期（报告里如实标注证据覆盖范围）。"""
    ids = {s.activity_id for s in samples}
    days = [d for aid, ref in load_activities(raw_root).items()
            if aid in ids and (d := ref.local_date) is not None]
    return sorted(set(days))


def run_validation(
    session: Session,
    settings: Settings,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> dict:
    """执行一次验证并返回报告字典（不落库，需求 6.4）。"""
    samples, stats = collect_samples(session, settings, start=start, end=end)
    days = _days_of(Path(settings.raw_data_dir) / "garmin", samples) if samples else []
    report = val.build_report(
        samples,
        date_range=(days[0].isoformat(), days[-1].isoformat()) if days else None,
    )
    out = report.to_dict()
    out["pairing"] = {
        **stats,
        "note": (
            "trainingEfforts 不返回 activityId：按同日「开始时间顺序」配对，"
            "当日活动数与 effort 数不一致时整天跳过"
        ),
    }
    out["units"] = {
        "x": "项目负荷（标准化 0–100 分值）",
        "trainingEfforts": "Tredict trainingEfforts 原值",
        "effort.heartrate": "平均心率 bpm（Garmin 活动聚合值）",
        "effort.power": "平均功率 W",
        "effort.pace": "平均速度 m/s（Garmin avgSpeed；越大越快）",
    }
    return out
