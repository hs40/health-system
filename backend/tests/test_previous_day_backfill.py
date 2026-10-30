"""日报「前一日训练回顾」+ ACWR 图的数据覆盖：补 raw 之后必须把该日负荷算出来。

背景（真实故障 2026-10-27，NAS）：
1. NAS 容器内 ``/data/raw/garmin/activities/`` 缺 ``2026-10-26.json``，日报「昨日训练
   回顾」直接读 day-1 raw activities（``daily_report._yesterday_training``）→ 空白。
2. 补同步 10-26 后，ACWR 图仍没有 10-26 —— 因为图直接读 ``training_load``，
   而 10-26 当时没生成日报 → ``training_load`` 从未计算。**补 raw ≠ 算负荷**。

修复契约（本文件锁定）：
1. day-1 raw 缺失时，``run_daily`` 必须回补（只拉 activities）；
2. 回补后 / raw 已存在但负荷缺失时，必须补算 day-1 的 ``training_load``
   （ACWR 图据此出柱），并刷新其 ACWR；
3. 已存在的 raw 不得重复拉取（幂等）。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.adapters.garmin import GarminAdapter, GarminSyncResult
from app.config import Settings
from app.schemas.domain import StandardDaily
from app.storage.derived_store import load_range

DAY = dt.date(2026, 10, 27)
YDAY = DAY - dt.timedelta(days=1)

# 与 NAS 上真实的 2026-10-26 活动同形（跑步 + 时长 + 均心率）
ACTIVITY = {
    "activityId": 655296550,
    "activityName": "安庆市 - 1026-长距离 Z2",
    "activityType": {"typeKey": "running"},
    "duration": 3861.7,
    "durationSeconds": 3861.7,
    "averageHR": 132.0,
    "maxHR": 141.0,
    "distance": 6954.1,
}


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        raw_data_dir=tmp_path / "raw",
        db_path=tmp_path / "t.db",
        audit_dir=tmp_path / "audit",
        garmin_tokenstore=tmp_path / "garmin_tokens",
    )


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _patch_min_standardize(monkeypatch: pytest.MonkeyPatch) -> None:
    """给 generate_daily 一份最小可用标准化数据（同既有同步失败测试的做法）。"""
    monkeypatch.setattr(
        GarminAdapter,
        "standardize",
        lambda self, day: StandardDaily(  # noqa: ARG005
            date=day,
            resting_heart_rate=50,
            max_heart_rate=180,
            hrv_last_night_avg=60.0,
            sleep_duration_sec=8 * 3600,
            steps=8000,
            body_battery_wake=70,
        ),
    )


def _fake_sync_day(calls, *, write_yday: bool):
    def _inner(self, day, *, datasets=None):  # noqa: ANN001
        calls.append((day, tuple(datasets) if datasets else None))
        if write_yday and day == YDAY and datasets == ["activities"]:
            p = self._raw_path("activities", YDAY)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps([ACTIVITY], ensure_ascii=False), encoding="utf-8")
        return GarminSyncResult(date=day)

    return _inner


def test_run_daily_backfills_previous_day_and_computes_load(
    monkeypatch, session, tmp_path
) -> None:
    """day-1 raw 缺失：回补后必须同时算出 day-1 负荷（ACWR 图才有柱）。"""
    from app.services.daily_pipeline import run_daily

    settings = _settings(tmp_path)
    calls: list[tuple[dt.date, tuple[str, ...] | None]] = []

    monkeypatch.setattr(GarminAdapter, "sync_day", _fake_sync_day(calls, write_yday=True))
    _patch_min_standardize(monkeypatch)

    _, report = run_daily(session, DAY, settings)

    assert (YDAY, ("activities",)) in calls, "缺失昨日 activities 时必须回补"
    assert report.yesterday_training is not None, "回补后昨日训练回顾不得为空"
    assert report.yesterday_training["date"] == YDAY.isoformat()
    assert report.yesterday_training["activities"][0]["sport"] == "running"

    loads = load_range(session, YDAY, YDAY)
    assert loads, "补了 raw 之后必须补算 day-1 的 training_load（否则 ACWR 图为 0）"
    assert loads[0].combined > 0


def test_run_daily_computes_missing_previous_day_load_without_resync(
    monkeypatch, session, tmp_path
) -> None:
    """raw 已存在但负荷缺失（用户当前状态）：只补算负荷，不得重复拉取。"""
    from app.services.daily_pipeline import run_daily

    settings = _settings(tmp_path)
    p = settings.raw_data_dir / "garmin" / "activities" / f"{YDAY}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([ACTIVITY], ensure_ascii=False), encoding="utf-8")
    assert not load_range(session, YDAY, YDAY), "前置：此时 day-1 还没有负荷"

    calls: list[tuple[dt.date, tuple[str, ...] | None]] = []
    monkeypatch.setattr(GarminAdapter, "sync_day", _fake_sync_day(calls, write_yday=False))
    _patch_min_standardize(monkeypatch)

    _, report = run_daily(session, DAY, settings)

    assert (YDAY, ("activities",)) not in calls, "昨日 raw 已存在时不应重复拉取"
    assert report.yesterday_training is not None
    loads = load_range(session, YDAY, YDAY)
    assert loads and loads[0].combined > 0, "raw 已存在但负荷缺失时必须补算"
