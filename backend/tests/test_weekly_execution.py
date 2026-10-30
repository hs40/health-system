"""周报执行块测试（§12.4 / 2026-10-20 决策 Q7；S5）。

需求：周报 ``execution`` 从一周 7 份日报的 ``yesterday_completion`` 提取聚合
（判定只在日报发生一次，周报不重算）；缺日报的日子**披露而非伪造**。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.db.models import DailyReportRow
from app.domain.adjustment import judge_day
from app.schemas.domain import StandardDaily
from app.services.report_service import generate_weekly
from app.storage.standard_store import upsert_standard

MON = dt.date(2026, 10, 12)
SUN = dt.date(2026, 10, 18)
D = dt.date(2026, 10, 16)  # 周五（样例中的被调整日）
COUNTED = frozenset(
    {"running", "strength_training", "indoor_cardio", "lap_swimming", "hiit", "hiking"}
)


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
    """一周标准化数据 + 7 份日报（各带前一日完成度 payload）。"""
    for i in range(7):
        upsert_standard(
            session,
            StandardDaily(
                date=MON + dt.timedelta(days=i),
                resting_heart_rate=50,
                hrv_last_night_avg=60.0,
                sleep_duration_sec=8 * 3600,
                steps=8000,
                body_battery_wake=70,
            ),
        )
    # 日报窗口 = 周二~下周一（周一的完成度在周二的日报里）
    specs = {
        MON: ("running", 30, "Z2", 30, 135),  # 完成
        MON + dt.timedelta(days=1): ("running", 30, "Z2", 20, 135),  # 部分：时短
        MON + dt.timedelta(days=2): ("hiit", 25, "Z4", 0, None),  # 未完成
        MON + dt.timedelta(days=3): ("running", 30, "Z2", 30, 122),  # 部分：强度 Z1≠Z2
        D: ("running", 25, "Z2", 25, 135),  # 调整后完成
        MON + dt.timedelta(days=5): (None, None, None, 0, None),  # 无计划无记录
        SUN: ("running", 30, "Z2", 32, 135),  # 完成
    }
    adjusted_days = {D}
    for day, (sport, minutes, zone, act_min, hr) in specs.items():
        acts = (
            [{"sport": sport, "minutes": act_min, "average_hr": hr}]
            if sport and act_min
            else []
        )
        j = judge_day(
            day,
            planned_sport=sport,
            planned_min=minutes,
            target_zone=zone,
            adjusted=day in adjusted_days,
            activities=acts,
            counted_sports=COUNTED,
            as_of=day,
        )
        payload = {"load": None, "acwr": None}
        if j.status is not None:
            payload["yesterday_completion"] = j.to_dict()
        session.add(
            DailyReportRow(
                date=day + dt.timedelta(days=1),  # 完成度落在次日报里
                payload=payload,
                version=1,
            )
        )
    session.commit()
    return session


def test_weekly_execution_aggregates_from_dailies(seeded, settings):
    """S5 主路径：六态计数/双量/调整日/逐日明细全部从日报提取聚合。"""
    rep = generate_weekly(seeded, MON, settings)
    ex = rep.execution
    assert ex["status_counts"] == {
        "completed": 2,       # 周一、周日
        "partial": 2,         # 周二（时短）、周四（强度不符）
        "missed": 1,          # 周三
        "adjustedCompleted": 1,  # 周五
    }
    assert ex["planned_volume_min"] == 170  # 30+30+25+30+25+30
    assert ex["actual_volume_min"] == 137  # 30+20+0+30+25+32
    assert ex["adjusted_days"] == [D.isoformat()]
    assert ex["missing_days"] == []
    reasons = {d["date"]: d.get("reason") for d in ex["day_details"]}
    assert "时长不足" in reasons[(MON + dt.timedelta(days=1)).isoformat()]
    assert "强度不符" in reasons[(MON + dt.timedelta(days=3)).isoformat()]
    assert reasons[D.isoformat()] is None  # 调整后完成无差异


def test_weekly_execution_discloses_missing_daily(seeded, settings):
    """缺日报 → missing_days 披露该日，不伪造判定、不进计数。"""
    # 删掉藏周六完成度的那份日报（周日的数据）
    row = seeded.exec(
        __import__("sqlmodel").select(DailyReportRow).where(
            DailyReportRow.date == SUN + dt.timedelta(days=1)
        )
    ).first()
    seeded.delete(row)
    seeded.commit()

    rep = generate_weekly(seeded, MON, settings)
    ex = rep.execution
    assert ex["missing_days"] == [SUN.isoformat()]
    assert SUN.isoformat() not in {d["date"] for d in ex["day_details"]}
    # 缺一天不影响其余六天聚合（周日不计入任何量）
    assert ex["planned_volume_min"] == 140
    assert ex["actual_volume_min"] == 105
