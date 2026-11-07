"""处方构造与任务状态机单元测试（TA.9 / TA.12）。"""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.prescription import estimate_duration_sec, to_garmin_workout
from app.schemas.domain import TrainingSession
from app.schemas.enums import SessionStatus, Sport, TaskStatus, TaskType

D = dt.date(2026, 10, 14)


def _session(**kw) -> TrainingSession:
    base = dict(
        id="s1",
        date=D,
        sport=Sport.RUNNING,
        name="轻松跑",
        warmup=[{"name": "热身", "duration_sec": 300}],
        main=[{"name": "主项", "duration_sec": 1800, "hr_zone": 2}],
        cooldown=[{"name": "放松", "duration_sec": 300}],
        status=SessionStatus.DRAFT,
    )
    base.update(kw)
    return TrainingSession(**base)


def test_estimate_duration_sums_steps():
    assert estimate_duration_sec(_session()) == 2400
    assert estimate_duration_sec(_session(duration_min=45)) == 2700


def test_to_garmin_workout_structure():
    w = to_garmin_workout(_session())
    assert w["workoutName"] == "轻松跑"
    assert w["sportType"]["sportTypeId"] == 1
    assert w["estimatedDurationInSecs"] == 2400
    steps = w["workoutSegments"][0]["workoutSteps"]
    assert [s["stepType"]["stepTypeKey"] for s in steps] == ["warmup", "main", "cooldown"]
    # 主项带心率区间目标
    main = steps[1]
    assert main["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert main["zoneNumber"] == 2
    assert main["endCondition"]["conditionTypeKey"] == "time"
    assert main["endConditionValue"] == 1800.0


def test_to_garmin_workout_distance_step():
    s = _session(main=[{"name": "5K", "distance_m": 5000}])
    main = to_garmin_workout(s)["workoutSegments"][0]["workoutSteps"][1]
    assert main["endCondition"]["conditionTypeKey"] == "distance"
    assert main["endConditionValue"] == 5000.0


def test_to_garmin_workout_empty_steps_gets_placeholder():
    s = _session(warmup=[], main=[], cooldown=[])
    steps = to_garmin_workout(s)["workoutSegments"][0]["workoutSteps"]
    assert len(steps) == 1


def test_sport_mapping_strength_and_cardio():
    assert to_garmin_workout(_session(sport=Sport.STRENGTH))["sportType"]["sportTypeId"] == 5
    assert to_garmin_workout(_session(sport=Sport.INDOOR_CARDIO))["sportType"]["sportTypeId"] == 6


# ---------------------------- 任务状态机 ----------------------------
def test_task_state_idempotent_reuse(tmp_path, monkeypatch):
    from sqlmodel import Session, SQLModel, create_engine

    from app.storage import task_state

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        r1 = task_state.begin(s, TaskType.PLAN_UPLOAD, "k1")
        assert r1.attempts == 1
        task_state.finish(s, r1.id, TaskStatus.SUCCESS)

        # 同键再次 begin → 复用同一行、尝试次数累加
        r2 = task_state.begin(s, TaskType.PLAN_UPLOAD, "k1")
        assert r2.id == r1.id
        assert r2.attempts == 2

        ok, reason = task_state.may_retry(s, "k1")
        assert ok is True  # 被 begin 重置为 retry

        task_state.finish(s, r2.id, TaskStatus.SUCCESS)
        ok2, reason2 = task_state.may_retry(s, "k1")
        assert ok2 is False
        assert "已完成" in reason2


def test_task_state_unknown_requires_reconcile():
    from sqlmodel import Session, SQLModel, create_engine

    from app.storage import task_state

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        r = task_state.begin(s, TaskType.PLAN_UPLOAD, "k2")
        task_state.finish(s, r.id, TaskStatus.UNKNOWN)
        ok, reason = task_state.may_retry(s, "k2")
        assert ok is False
        assert "回读" in reason


def test_task_state_failed_is_retryable():
    from sqlmodel import Session, SQLModel, create_engine

    from app.storage import task_state

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        r = task_state.begin(s, TaskType.PLAN_UPLOAD, "k3")
        task_state.finish(s, r.id, TaskStatus.FAILED)
        ok, _ = task_state.may_retry(s, "k3")
        assert ok is True


def test_task_state_finish_missing_raises():
    from sqlmodel import Session, SQLModel, create_engine

    from app.storage import task_state

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s, pytest.raises(KeyError):
        task_state.finish(s, "nope", TaskStatus.SUCCESS)
