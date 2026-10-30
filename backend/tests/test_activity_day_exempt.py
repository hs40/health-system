"""活动校验「当天活动不计入差异」（总账 L-12③）。

日任务 21:00 跑校验、23:30 才同步当天活动——在这个时间差里，
Garmin 云端已经有当天的活动而本地 raw 还没有，A 环必然报「仅远端」，
于是**每天** exit 3 假红（狼来了）。健康侧 ``_health_window`` 早排除当天，
活动侧漏了同一条；次日 5 天滚动窗口（``RAW_REFRESH_WINDOW_DAYS``）会自然复核，
所以豁免当天不丢覆盖。

本文件锁两层行为：

1. **纯函数层**：``activity_local_date`` / ``ids_of_day`` 的日期解析与集合口径
   （解析不了日期的一律**不豁免**——宁可多报一次，也不误豁免真实缺失）；
2. **接线层**：``verify_activity`` 里当天的活动不进任何一路的差异计数，
   而**昨天及更早**的真缺失照报（豁免不放宽真实故障）。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, select

from app.db import models as _models  # noqa: F401  — 注册全部模型
from app.db.models import TredictActivityRow
from app.db.session import engine
from app.domain.verify import activity_local_date, ids_of_day

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_cloud.py"
_spec = importlib.util.spec_from_file_location("verify_cloud", SCRIPT)
verify_cloud = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_cloud)

TODAY = dt.date(2026, 10, 24)
YESTERDAY = TODAY - dt.timedelta(days=1)
OLDER = TODAY - dt.timedelta(days=3)


# ============================================================
# 纯函数层
# ============================================================
def test_activity_local_date_prefers_local_clock() -> None:
    assert activity_local_date({"startTimeLocal": "2026-10-24 06:30:00"}) == TODAY


def test_activity_local_date_falls_back_to_gmt() -> None:
    assert activity_local_date({"startTimeGMT": "2026-10-23 22:30:00"}) == dt.date(2026, 10, 23)


def test_activity_local_date_returns_none_when_unparseable() -> None:
    assert activity_local_date({}) is None
    assert activity_local_date({"startTimeLocal": "not-a-date"}) is None
    assert activity_local_date({"startTimeLocal": 123}) is None


def test_ids_of_day_collects_both_sides() -> None:
    cloud = [_garmin_activity(101, TODAY), _garmin_activity(100, YESTERDAY)]
    local = {
        "102": _garmin_activity(102, TODAY),
        "100": _garmin_activity(100, YESTERDAY),
    }
    assert ids_of_day(cloud, local, TODAY) == {"101", "102"}


def test_ids_of_day_never_exempts_undated_items() -> None:
    """没有可解析日期的活动**不豁免**：宁可漏豁免（多报一次），
    也不误豁免一条真实缺失。"""
    cloud = [{"activityId": 7}, {"activityId": 8, "startTimeLocal": ""}]
    assert ids_of_day(cloud, {}, TODAY) == set()


# ============================================================
# 接线层
# ============================================================
def _garmin_activity(aid: int, day: dt.date) -> dict:
    """与 ``test_cloud_pair_ignores_rounding_noise`` 同口径的假活动。"""
    return {
        "activityId": aid,
        "startTimeLocal": f"{day.isoformat()} 06:30:00",
        "duration": 2349.8,
        "elapsedDuration": 2415.6,
        "movingDuration": 1170.5,
        "calories": 200,
        "averageHR": 130,
        "maxHR": 150,
    }


def _tredict_activity(tid: str) -> dict:
    return {
        "id": tid,
        "subSportType": "generic",
        "summary": {
            "duration": 2350,
            "durationTotal": 2415,
            "calories": 200,
            "heartrate": 130,
            "heartrateMax": 150,
        },
    }


class _FakeGarminClient:
    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def get_activities(self, start: int, count: int) -> list[dict]:
        return self._items


class _FakeGarminAdapter:
    def __init__(self, items: list[dict]) -> None:
        self._client = _FakeGarminClient(items)

    def client(self) -> _FakeGarminClient:
        return self._client


class _FakeTredictAdapter:
    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def fetch_activity_list(self, *, extended: bool = False) -> list[dict]:
        return self._items

    def fetch_activity_detail(self, tid: str) -> dict:
        return {}


def _row(tid: str, garmin_id: str | None, day: dt.date) -> TredictActivityRow:
    return TredictActivityRow(
        tredict_id=tid,
        utc_datetime=dt.datetime.combine(day, dt.time(6, 30)),
        local_date=day,
        sport_type="running",
        matched_garmin_id=garmin_id,
    )


@pytest.fixture()
def tdict_db():
    """真实（临时目录里的）库：B/C/D 环要读 ``tredict_activity``。"""
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for row in s.exec(select(TredictActivityRow)).all():
            s.delete(row)
        s.commit()
    yield
    with Session(engine) as s:
        for row in s.exec(select(TredictActivityRow)).all():
            s.delete(row)
        s.commit()


def _run(
    local: dict,
    garmin_cloud: list[dict],
    tredict_cloud: list[dict],
    rows: list[TredictActivityRow],
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    monkeypatch.setattr(verify_cloud, "load_local_activity_payloads", lambda: local)
    with Session(engine) as s:
        for row in rows:
            s.add(row)
        s.commit()
    return verify_cloud.verify_activity(
        _FakeGarminAdapter(garmin_cloud),
        _FakeTredictAdapter(tredict_cloud),
        no_write=True,
        today=TODAY,
    )


def test_todays_cloud_only_activity_is_exempt(tdict_db, monkeypatch) -> None:
    """当天活动只出现在 Garmin 云端（本地还没同步）→ 不计差异、不假红。"""
    out = _run(
        local={"100": _garmin_activity(100, YESTERDAY)},
        garmin_cloud=[
            _garmin_activity(100, YESTERDAY),
            _garmin_activity(101, TODAY),  # 当天：云端有、本地无
        ],
        tredict_cloud=[_tredict_activity("t1")],
        rows=[_row("t1", "100", YESTERDAY)],
        monkeypatch=monkeypatch,
    )
    assert out["a_local_garmin"]["only_remote"] == []
    assert out["coverage"]["only_garmin"] == []
    assert out["diff"] == 0


def test_yesterdays_missing_activity_still_counts(tdict_db, monkeypatch) -> None:
    """豁免只放当天；更早的真缺失必须照报，否则校验失去意义。"""
    out = _run(
        local={"100": _garmin_activity(100, YESTERDAY)},
        garmin_cloud=[
            _garmin_activity(100, YESTERDAY),
            _garmin_activity(99, OLDER),  # 三天前：本地真缺
        ],
        tredict_cloud=[_tredict_activity("t1")],
        rows=[_row("t1", "100", YESTERDAY)],
        monkeypatch=monkeypatch,
    )
    assert out["a_local_garmin"]["only_remote"] == ["99"]
    assert out["diff"] == 1
