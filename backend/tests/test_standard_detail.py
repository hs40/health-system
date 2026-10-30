"""「标准层详情」端点回归测试（REQ-UI-003 数据浏览 / REQ-DATA-004 可回溯）。

覆盖：标准值透出（含未提供字段为 None，不伪造 0）、原始文件引用透出、缺日 404。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.api.data import _STANDARD_FIELDS, get_standard_detail
from app.errors import AppError
from app.schemas.domain import RawDailyRef, StandardDaily
from app.schemas.enums import ErrorCode
from app.storage.standard_store import upsert_standard

D = dt.date(2026, 10, 14)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _ref(dataset: str) -> RawDailyRef:
    return RawDailyRef(
        source="garmin",
        dataset=dataset,
        date=D,
        path=f"garmin/{dataset}/{D.isoformat()}.json",
        fetched_at=dt.datetime(2026, 10, 14, 8, 30),
    )


def test_standard_detail_returns_metrics_and_refs(session):
    upsert_standard(
        session,
        StandardDaily(
            date=D,
            resting_heart_rate=48,
            hrv_last_night_avg=62.5,
            sleep_duration_sec=7 * 3600,
            weight_kg=69.8,
            source_refs=[_ref("daily_summary"), _ref("sleep")],
        ),
    )

    detail = get_standard_detail(day=D, session=session)

    assert detail.date == D
    assert detail.metrics["restingHeartRate"] == 48
    assert detail.metrics["hrvLastNightAvg"] == 62.5
    assert detail.metrics["weightKg"] == 69.8
    assert detail.metrics["sleepDurationSec"] == 7 * 3600
    # 未提供的字段返回 None（不伪造 0）
    assert detail.metrics["spo2Avg"] is None
    assert [r.dataset for r in detail.source_refs] == ["daily_summary", "sleep"]
    # 路径为 POSIX 相对路径（跨机可移植）
    assert detail.source_refs[0].path == "garmin/daily_summary/2026-10-14.json"


def test_standard_detail_exposes_all_standard_fields(session):
    upsert_standard(session, StandardDaily(date=D))

    detail = get_standard_detail(day=D, session=session)

    assert set(detail.metrics) == set(_STANDARD_FIELDS)
    # 15 个一期标准字段 + 5 个 Garmin HRV 基线字段（E24）+ sleepScore / bodyBatteryMorning（Q11）
    assert len(detail.metrics) == 22
    assert {"hrvLowUpper", "hrvBalancedHigh", "hrvStatus"} <= set(detail.metrics)
    assert {"sleepScore", "bodyBatteryMorning"} <= set(detail.metrics)
    assert detail.source_refs == []


def test_standard_detail_missing_day_raises_not_found(session):
    with pytest.raises(AppError) as ei:
        get_standard_detail(day=dt.date(2020, 1, 1), session=session)

    assert ei.value.code == ErrorCode.NOT_FOUND


# ============================================================
# 身体电量两口径（2026-10-22 修正）
#   body_battery_morning ← bodyBatteryAtWakeTime   = 醒时电量（真正的「晨起」）
#   body_battery_wake    ← bodyBatteryMostRecentValue = 同步时刻残值（傍晚任务 → 低值）
# 前端「身体电量（晨）」必须取醒时值，不得用残值顶替。
# ============================================================
def test_series_body_battery_morning_points_to_wake_time_column(session):
    """`series_metric=bodyBatteryMorning` → 醒时电量列；`bodyBatteryWake` → 残值列。"""
    from app.api.data import get_series

    upsert_standard(session, StandardDaily(date=D, body_battery_wake=38, body_battery_morning=76))

    morning = get_series(metric="bodyBatteryMorning", from_=D, to=D, session=session)
    recent = get_series(metric="bodyBatteryWake", from_=D, to=D, session=session)

    assert [p.value for p in morning.points] == [76.0]
    assert [p.value for p in recent.points] == [38.0]


def test_standard_detail_labels_battery_metrics_by_true_meaning(session):
    """标准层详情：两个电量字段各自可见，且 `bodyBatteryWake` 就是残值口径。"""
    upsert_standard(
        session,
        StandardDaily(date=D, body_battery_wake=38, body_battery_morning=76, sleep_score=77),
    )

    detail = get_standard_detail(day=D, session=session)

    assert detail.metrics["bodyBatteryMorning"] == 76
    assert detail.metrics["bodyBatteryWake"] == 38
    assert detail.metrics["sleepScore"] == 77
