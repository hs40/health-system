"""排期加固回归测试（孤儿排期防治 + 上传默认不排期）。

覆盖：
- ``plan_service.scheduled_on`` 合并「日历 + upcoming」两个来源并按排期 id 去重；
- ``GarminAdapter.delete_workout`` 删除课程前**先取消排期**；
- ``plan_service.upload_session`` **默认不排期**（只进课程库），显式 ``schedule=True`` 才排期；
- 自动日任务 ``scheduler.run_daily_job`` 以 ``schedule=False`` 上传。
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.adapters.garmin import GarminAdapter
from app.config import Settings
from app.db.models import TrainingSessionRow
from app.schemas.domain import TrainingSession
from app.schemas.enums import SessionStatus, Sport
from app.services import plan_service

D = dt.date(2026, 10, 14)


@pytest.fixture()
def settings(tmp_path):
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        raw_data_dir=tmp_path / "raw",
        db_path=tmp_path / "t.db",
        audit_dir=tmp_path / "audit",
    )


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_session(session: Session) -> str:
    ts = TrainingSession(
        id="session-2026-10-14",
        date=D,
        sport=Sport.RUNNING,
        name="轻松跑",
        main=[{"name": "主项", "duration_sec": 1800, "hr_zone": 2}],
        status=SessionStatus.DRAFT,
    )
    session.add(
        TrainingSessionRow(
            id=ts.id,
            date=ts.date,
            sport=ts.sport.value,
            name=ts.name,
            payload=ts.model_dump(mode="json"),
            status=ts.status.value,
        )
    )
    session.commit()
    return ts.id


def test_scheduled_on_merges_calendar_and_upcoming(monkeypatch, settings):
    """日历看不到的孤儿排期（无 itemType / 用 scheduleDate / scheduledWorkoutId）也要被找到。"""

    def fake_calendar(self, year, month):  # noqa: ANN001
        return [
            {"itemType": "workout", "id": 111, "workoutId": 900, "date": D.isoformat()},
            {"itemType": "workout", "id": 222, "workoutId": 901, "date": "2026-10-20"},
            {"itemType": "weight", "id": 333, "date": D.isoformat()},
        ]

    def fake_upcoming(self):  # noqa: ANN001
        return [
            {
                "scheduledWorkoutId": 444,
                "workoutId": 902,
                "workoutName": "[HS-TEST] 冒烟课程",
                "scheduleDate": D.isoformat(),
            },
            # 与日历同一条（应被去重）
            {"scheduledWorkoutId": 111, "workoutId": 900, "scheduleDate": D.isoformat()},
        ]

    monkeypatch.setattr(GarminAdapter, "get_scheduled_workouts", fake_calendar)
    monkeypatch.setattr(GarminAdapter, "upcoming_schedules", fake_upcoming)

    got = plan_service.scheduled_on(settings, D)

    ids = sorted(it.get("id") or it.get("scheduledWorkoutId") for it in got)
    assert ids == [111, 444]  # 222 日期不符、weight 非课程、重复的 111 已去重


def test_delete_workout_unschedules_first(monkeypatch, settings):
    """删课程前必须先把能查到的排期取消（否则 Garmin 侧留下不可删除的孤儿排期）。"""
    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        GarminAdapter,
        "upcoming_schedules",
        lambda self: [
            {"scheduledWorkoutId": 555, "workoutId": 777, "scheduleDate": D.isoformat()},
            {"scheduledWorkoutId": 556, "workoutId": 888, "scheduleDate": D.isoformat()},
        ],
    )
    monkeypatch.setattr(GarminAdapter, "get_scheduled_workouts", lambda self, y, m: [])
    monkeypatch.setattr(
        GarminAdapter, "unschedule_workout", lambda self, sid: calls.append(("unschedule", sid))
    )

    class _FakeClient:
        def delete_workout(self, workout_id: str) -> None:
            calls.append(("delete", int(workout_id)))

    monkeypatch.setattr(GarminAdapter, "client", lambda self: _FakeClient())

    GarminAdapter(settings).delete_workout(777)

    # 只取消该课程（777）的排期，且**在删除课程之前**
    assert calls == [("unschedule", 555), ("delete", 777)]


def test_upload_default_does_not_schedule(monkeypatch, session, settings):
    """``upload_session`` 默认**只上传课程库、不排期**（每日计划规则）。"""
    sid = _seed_session(session)
    scheduled: list[int] = []

    monkeypatch.setattr(GarminAdapter, "upload_workout", lambda self, payload: 1001)
    monkeypatch.setattr(
        GarminAdapter,
        "schedule_workout",
        lambda self, wid, day: scheduled.append(wid) or 2002,
    )

    res = plan_service.upload_session(session, sid, settings)

    assert res.status == "uploaded"
    assert res.garmin_workout_id == 1001
    assert res.garmin_scheduled_id is None
    assert scheduled == [], "默认不得调用排期"


def test_upload_with_schedule_true_schedules(monkeypatch, session, settings):
    """显式 ``schedule=True`` 时才排期（能力保留）。"""
    sid = _seed_session(session)
    scheduled: list[tuple[int, dt.date]] = []

    monkeypatch.setattr(GarminAdapter, "upload_workout", lambda self, payload: 1001)
    monkeypatch.setattr(
        GarminAdapter,
        "schedule_workout",
        lambda self, wid, day: scheduled.append((wid, day)) or 2002,
    )

    res = plan_service.upload_session(session, sid, settings, schedule=True)

    assert res.status == "scheduled"
    assert res.garmin_scheduled_id == 2002
    assert scheduled == [(1001, D)]


def test_daily_job_uploads_without_schedule(monkeypatch, session):
    """自动日任务以 ``schedule=False`` 上传（每日计划不排期）。"""
    from app.orchestrator import scheduler as sched_mod
    from app.services import coach_service, daily_pipeline
    from app.services import plan_service as ps

    captured: dict = {}

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(sched_mod, "session_scope", _scope)
    monkeypatch.setattr(
        daily_pipeline,
        "run_daily",
        lambda s, d, st: (
            SimpleNamespace(status="ok"),
            SimpleNamespace(
                risk=SimpleNamespace(
                    priority=SimpleNamespace(value="P4"),
                    data_state=SimpleNamespace(value="A"),
                )
            ),
        ),
    )
    monkeypatch.setattr(coach_service, "analyze_daily", lambda s, d, st: {"summary": "ok"})
    monkeypatch.setattr(
        coach_service,
        "build_today_plan",
        lambda s, d, st: SimpleNamespace(id="session-2026-10-14"),
    )

    def _upload(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["schedule"] = kwargs.get("schedule", "<missing>")
        return SimpleNamespace(status="uploaded", garmin_workout_id=1)

    monkeypatch.setattr(ps, "upload_session", _upload)

    out = sched_mod.run_daily_job(D)

    assert out["steps"]["upload"] == "uploaded"
    assert captured["schedule"] is False
