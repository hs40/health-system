"""多源冲突检测的**降级路径**（L-8 后续，2026-10-25）。

`detect_day_conflicts` 在多来源时显式 `raise NotImplementedError`（读数未接入）。
日报生成不得因此崩：必须降级为「不判冲突 + 写入 limitations」，否则将来接入
第二个数据源的那天，整份日报会生成失败——比「没检测」严重得多。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.schemas.domain import RawDailyRef, StandardDaily
from app.services.report_service import generate_daily
from app.storage.standard_store import upsert_standard

D = dt.date(2026, 10, 20)


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


def _ref(source: str) -> RawDailyRef:
    return RawDailyRef(
        source=source,
        dataset="daily_summary",
        date=D,
        path=f"{source}/daily_summary/{D.isoformat()}.json",
        fetched_at=dt.datetime(2026, 10, 20, 7, 0),
    )


def test_multi_source_degrades_to_limitation(session, settings) -> None:
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
                source_refs=[_ref("garmin"), _ref("tredict")] if day == D else [_ref("garmin")],
            ),
        )
    session.commit()

    report = generate_daily(session, D, settings)

    assert any("多源冲突检测未接入" in item for item in report.limitations)
    assert report.quality.conflicts == []
