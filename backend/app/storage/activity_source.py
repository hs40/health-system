"""活动原始数据读取（2026-10-21 决策 Q5=B：废弃 ``garmin_activity`` 表）。

**raw 是唯一事实源**：

- ``raw/garmin/activities/<date>.json`` —— 按日铺平的活动列表（Garmin 列表接口原文）
- ``raw/garmin/fit_jsonl/<activity_id>.jsonl.gz`` —— FIT 消息（力量动作 ``set`` 在这里）

原 ``garmin_activity`` 表只由一次性 backfill 写入，日常管线从不更新 → 表冻结、
上传永远「已一致」。本模块替代该表的读取职责，供上传/匹配/负荷验证/校验使用。
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ActivityRef:
    """一条 Garmin 活动的只读视图（字段口径与原表一致）。"""

    activity_id: str
    activity_type: str
    name: str
    start_local: dt.datetime | None
    start_utc: dt.datetime | None
    duration_sec: int
    payload: dict

    @property
    def local_date(self) -> dt.date | None:
        """归属自然日（按开始时间；缺失时 None，由调用方跳过）。"""
        return self.start_local.date() if self.start_local else None


def _parse_dt(value: object) -> dt.datetime | None:
    """``"YYYY-MM-DD HH:MM:SS"`` / ISO → datetime；不可解析返回 None。"""
    if not value:
        return None
    text = str(value).strip().replace("T", " ")[:19]
    try:
        return dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _duration_sec(payload: dict) -> int:
    for key in ("durationSeconds", "duration", "movingDuration", "elapsedDuration"):
        v = payload.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 0


def load_activities(raw_root: Path) -> dict[str, ActivityRef]:
    """铺平 ``activities/*.json`` → ``{activity_id: ActivityRef}``。

    缺失字段不抛异常（置 None / 0）：raw 是外部来源，容忍脏数据比崩溃重要。
    """
    root = Path(raw_root) / "activities"
    out: dict[str, ActivityRef] = {}
    if not root.exists():
        return out
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            aid = item.get("activityId")
            if aid is None:
                continue
            out[str(aid)] = ActivityRef(
                activity_id=str(aid),
                activity_type=str((item.get("activityType") or {}).get("typeKey") or ""),
                name=str(item.get("activityName") or ""),
                start_local=_parse_dt(item.get("startTimeLocal")),
                start_utc=_parse_dt(item.get("startTimeGMT")),
                duration_sec=_duration_sec(item),
                payload=item,
            )
    return out


def load_sets(raw_root: Path, activity_id: str) -> list[dict[str, Any]]:
    """FIT jsonl 中的 ``set`` 消息（力量动作明细）；缺失/坏行返回空表。"""
    path = Path(raw_root) / "fit_jsonl" / f"{activity_id}.jsonl.gz"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("m") == "set":
                    d = obj.get("d")
                    if isinstance(d, dict):
                        out.append(d)
    except (OSError, EOFError):
        return out
    return out


def load_laps(raw_root: Path, activity_id: str) -> list[dict[str, Any]]:
    """FIT jsonl 中的 ``lap`` 消息（分段计时长 + 平均心率）；缺失/坏行返回空表。

    2026-10-25（U-8）：负荷分段计算的数据源。**不用** Garmin 列表接口的
    ``splitSummaries``——它同时含 ``RWD_*``（走跑识别）与 ``INTERVAL_*``
    两套互相重叠的分组，直接求和会双倍计时（实测 3101.5s vs 活动 1551s）。
    """
    path = Path(raw_root) / "fit_jsonl" / f"{activity_id}.jsonl.gz"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("m") == "lap":
                    d = obj.get("d")
                    if isinstance(d, dict):
                        out.append(d)
    except (OSError, EOFError):
        return out
    return out


@dataclass(frozen=True, slots=True)
class FitMessages:
    """FIT jsonl 读取结果。

    ``source`` 明确区分「明细文件缺失」（``missing``）与「文件存在、只是本类
    消息为空」（``present``）。2026-11-08 之前本函数缺文件时静默返回裸 ``{}``，
    详情页无法分辨「NAS 上 FIT 明细没同步」和「这次运动本来就没逐秒心率」，
    两张心率图一律显示「本次无逐秒心率数据」——这是跑步详情页两图同时空白
    却查不出原因的根因。
    """

    by_type: dict[str, list[dict[str, Any]]]
    source: str  # present | missing


def load_fit_messages(
    raw_root: Path,
    activity_id: str,
    types: set[str] | None = None,
) -> FitMessages:
    """一次解压 FIT jsonl，按消息类型分组（详情页一次拿齐所需消息）。

    ``types`` 为 ``None`` 时保留全部类型；指定时只保留给定类型。
    坏行、非 dict 的 ``d`` 一律跳过 —— raw 是外部来源，容忍脏数据比崩溃重要。
    文件不存在返回 ``source="missing"``，绝不静默退化成空 dict。
    """
    path = Path(raw_root) / "fit_jsonl" / f"{activity_id}.jsonl.gz"
    out: dict[str, list[dict[str, Any]]] = {}
    if not path.exists():
        return FitMessages(by_type=out, source="missing")
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                m = obj.get("m")
                if not isinstance(m, str):
                    continue
                if types is not None and m not in types:
                    continue
                d = obj.get("d")
                if isinstance(d, dict):
                    out.setdefault(m, []).append(d)
    except (OSError, EOFError):
        pass  # 保留已读到的部分；文件存在即 present
    return FitMessages(by_type=out, source="present")


__all__ = [
    "ActivityRef",
    "FitMessages",
    "load_activities",
    "load_fit_messages",
    "load_laps",
    "load_sets",
]
