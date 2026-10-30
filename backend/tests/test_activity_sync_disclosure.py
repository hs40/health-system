"""G-7 第一步：活动同步失败只**披露**，不进质量分级、不进拦截（2026-10-24 决策）。

为什么只能先披露：活动侧「真休息日」与「没同步到」在数据层**同形**——
休息日 ``sync_day`` 拿到空列表就不写 raw 文件（``garmin.py:379-385``），
所以「文件缺失」不能当作「未同步」。唯一诚实的信号是**同步错误态**
（``GarminSyncResult.errors['activities']``），由日流水线传入。

硬约束：这条披露**不得**改 ``quality.grade``、不得影响 ``may_generate_plan``——
否则每个同步出错的休息日都会拦掉计划生成，伤到日常使用。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.domain.quality import may_generate_plan
from app.schemas.domain import StandardDaily
from app.services.report_service import generate_daily
from app.storage.standard_store import upsert_standard

D = dt.date(2026, 10, 20)
ERROR = {"activities": "ReadTimeout: timed out"}


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def settings(tmp_path):
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        raw_data_dir=tmp_path / "raw",
        db_path=tmp_path / "t.db",
        audit_dir=tmp_path / "audit",
    )


@pytest.fixture()
def seeded(session):
    for day in (D - dt.timedelta(days=1), D):
        upsert_standard(
            session,
            StandardDaily(
                date=day,
                resting_heart_rate=50,
                max_heart_rate=180,
                hrv_last_night_avg=60.0,
                sleep_duration_sec=8 * 3600,
                steps=8000,
                body_battery_wake=70,
            ),
        )
    session.commit()
    return session


def test_activity_sync_error_is_disclosed(seeded, settings) -> None:
    report = generate_daily(seeded, D, settings, sync_errors=ERROR)
    assert any("活动数据同步失败" in item for item in report.limitations)


def test_activity_sync_error_does_not_change_grade_or_plan_gate(seeded, settings) -> None:
    """披露而已：质量分级与计划生成闸门必须与无错时一致。"""
    clean = generate_daily(seeded, D, settings)
    dirty = generate_daily(seeded, D, settings, sync_errors=ERROR)

    assert dirty.quality.grade == clean.quality.grade
    assert dirty.quality.missing == clean.quality.missing
    assert may_generate_plan(dirty.quality) == may_generate_plan(clean.quality)


def test_other_dataset_error_is_not_misattributed(seeded, settings) -> None:
    report = generate_daily(seeded, D, settings, sync_errors={"sleep": "boom"})
    assert not any("活动数据同步失败" in item for item in report.limitations)


def test_regenerated_report_restores_sync_disclosure_from_audit(seeded, settings) -> None:
    """重生成日报（不传 sync_errors）时，从当天审计事件恢复披露。

    否则「活动同步失败」只在第一次生成时存在，重算/手动重生成即静默消失——
    而这行正是防止把「没同步」误读成「休息日」的唯一提示。
    """
    from app.schemas.enums import AuditCategory
    from app.storage.audit_store import record as audit

    audit(
        seeded,
        AuditCategory.DATA_SOURCE,
        ref=D.isoformat(),
        detail={"source": "garmin", "errors": {"activities": "ReadTimeout: timeout"}},
        audit_dir=settings.audit_dir,
    )

    report = generate_daily(seeded, D, settings)  # 不传 sync_errors
    assert any("活动数据同步失败" in item for item in report.limitations)