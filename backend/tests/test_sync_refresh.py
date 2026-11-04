"""手动「同步数据」必须能覆盖占位睡眠（2026-10-31 故障）。

07:30 写入的睡眠 raw 是**全 null 占位**；append-only 的 ``sync_day`` 遇到已存在文件
不会覆盖，导致「同步数据」按钮虽然从 Garmin 拉到了完整睡眠，却写不进 raw、日报仍为空。

手动同步必须走 ``refresh``（滚动窗口覆盖），才能把占位更新为完整记录。
本文件锁定：``run_daily(..., refresh=True)`` 确实把 refresh 透传给 ``sync_daily``。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.adapters.garmin import GarminSyncResult
from app.config import Settings
from app.services import daily_pipeline

DAY = dt.date(2026, 10, 31)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _capture(monkeypatch) -> dict:  # noqa: ANN001
    seen: dict = {}

    def fake_sync(session, day, settings, *, refresh=False):  # noqa: ANN001
        seen["refresh"] = refresh
        return GarminSyncResult(date=day)

    monkeypatch.setattr(daily_pipeline, "sync_daily", fake_sync)
    monkeypatch.setattr(daily_pipeline, "generate_report", lambda *a, **k: object())
    return seen


def test_run_daily_forwards_refresh(session, monkeypatch) -> None:
    seen = _capture(monkeypatch)
    daily_pipeline.run_daily(session, DAY, Settings(_env_file=None), refresh=True)
    assert seen["refresh"] is True, "手动同步必须走 refresh 覆盖占位"


def test_run_daily_defaults_to_append_only(session, monkeypatch) -> None:
    seen = _capture(monkeypatch)
    daily_pipeline.run_daily(session, DAY, Settings(_env_file=None))
    assert seen["refresh"] is False, "默认仍为 append-only，避免误覆盖历史"
