"""同日多次运动必须全部落盘（真实故障 2026-10-27，NAS 只有 1 次）。

背景
----
``activities/<date>.json`` 由 ``sync_day`` 写入，原实现走 ``_write_raw``（append-only，
文件已存在即跳过）。于是某天首次同步后（当时只有 1 次运动），当天晚些时候新增的第 2 次
运动**永远不会进本地**——再同步也被"已存在"挡掉。NAS 10-27 只有 1 条即此因。

修复契约（本文件锁定）：
1. 同日再次同步时，按 ``activityId`` **合并**新旧活动（新覆盖旧、旧中缺失的保留），
   窗口内写盘并留 ``_superseded`` 备份；
2. Garmin 若某次只返回部分活动（少于本地），**不得丢本地已有活动**（合并而非替换）；
3. 超出 ``RAW_REFRESH_WINDOW_DAYS`` 的历史日仍冻结（不覆盖，保持 append-only 精神）。
"""

from __future__ import annotations

import datetime as dt
import json

from app.adapters.garmin import RAW_REFRESH_WINDOW_DAYS, GarminAdapter
from app.config import Settings

A = {
    "activityId": 1,
    "activityName": "morning run",
    "activityType": {"typeKey": "running"},
    "duration": 1800,
    "averageHR": 130,
}
B = {
    "activityId": 2,
    "activityName": "evening strength",
    "activityType": {"typeKey": "strength_training"},
    "duration": 2400,
    "averageHR": 110,
}


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        raw_data_dir=tmp_path / "raw",
        db_path=tmp_path / "t.db",
        audit_dir=tmp_path / "audit",
        garmin_tokenstore=tmp_path / "garmin_tokens",
    )


class _FakeClient:
    def __init__(self, activities: list[dict]):
        self._activities = activities

    def get_activities_by_date(self, start, end):  # noqa: ANN001
        return list(self._activities)


def _seed(settings: Settings, day: dt.date, activities: list[dict]) -> None:
    p = settings.raw_data_dir / "garmin" / "activities" / f"{day.isoformat()}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(activities, ensure_ascii=False), encoding="utf-8")


def _ids(adapter: GarminAdapter, day: dt.date) -> set[str]:
    raw = adapter.read_raw("activities", day) or []
    return {str(x["activityId"]) for x in raw}


def _within_window_day() -> dt.date:
    # 窗口内（最近一天），保证可刷新
    return dt.date.today() - dt.timedelta(days=1)


def test_sync_day_merges_new_same_day_activity(monkeypatch, tmp_path) -> None:
    """同日新增的第二次运动必须被合并进本地（核心故障）。"""
    settings = _settings(tmp_path)
    day = _within_window_day()
    _seed(settings, day, [A])  # 首次同步只有 1 次运动
    adapter = GarminAdapter(settings)
    monkeypatch.setattr(GarminAdapter, "client", lambda self: _FakeClient([A, B]))

    res = adapter.sync_day(day, datasets=["activities"])

    assert _ids(adapter, day) == {"1", "2"}, "同日第二次运动必须落盘"
    assert "activities" in res.written
    backups = list(
        (settings.raw_data_dir / "garmin" / "_superseded" / "activities").glob(
            f"{day.isoformat()}.*.json"
        )
    )
    assert backups, "覆盖旧版本前必须留 superseded 备份（REQ-DATA-001 可追溯）"


def test_sync_day_does_not_drop_existing_when_api_returns_fewer(monkeypatch, tmp_path) -> None:
    """Garmin 某次少返回时不得丢本地已有活动（合并而非替换）。"""
    settings = _settings(tmp_path)
    day = _within_window_day()
    _seed(settings, day, [A, B])
    adapter = GarminAdapter(settings)
    monkeypatch.setattr(GarminAdapter, "client", lambda self: _FakeClient([A]))

    adapter.sync_day(day, datasets=["activities"])

    assert _ids(adapter, day) == {"1", "2"}, "API 少返回不得导致本地活动丢失"


def test_sync_day_freezes_activities_beyond_window(monkeypatch, tmp_path) -> None:
    """超出刷新窗口的历史日仍冻结（不覆盖）。"""
    settings = _settings(tmp_path)
    day = dt.date.today() - dt.timedelta(days=RAW_REFRESH_WINDOW_DAYS + 5)
    _seed(settings, day, [A])
    adapter = GarminAdapter(settings)
    monkeypatch.setattr(GarminAdapter, "client", lambda self: _FakeClient([A, B]))

    res = adapter.sync_day(day, datasets=["activities"])

    assert _ids(adapter, day) == {"1"}, "超窗口历史日不得被覆盖"
    assert "activities" not in res.written
