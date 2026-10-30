"""周计划卡片接口测试（§12.3 / 2026-10-20 决策 Q10-A；S4）。

需求：`GET /api/plan/weekly/{week_start}` 逐日返回 `cards`（现算、不落库）：
被日报调整的日子带「日报已调整」标识与五项详情（原安排/调整后/原因/日报/实际）；
未调整的日子 is_adjusted=False。旧 payload 无该字段不报错（读取时现算）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.adapters.garmin import GarminAdapter
from app.api.plans import get_weekly
from app.config import Settings
from app.db.models import DailyReportRow, TrainingSessionRow, WeeklyPlanRow

MON = dt.date(2026, 10, 12)  # 周一
TUE = dt.date(2026, 10, 13)  # 被调整日
WED = dt.date(2026, 10, 14)  # 普通日


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


def _day(date: dt.date, name: str, minutes: int, zone: str | None) -> dict:
    return {
        "date": date.isoformat(),
        "weekday": date.weekday(),
        "session": {
            "id": f"wp-{date.isoformat()}",
            "date": date.isoformat(),
            "sport": "running",
            "name": name,
            "target_hr_zone": zone,
            "duration_min": minutes,
            "status": "confirmed",
        },
        "is_night_shift": False,
        "note": None,
    }


@pytest.fixture()
def seeded(session):
    session.add(
        WeeklyPlanRow(
            week_start=MON,
            payload={
                "week_start": MON.isoformat(),
                "days": [
                    _day(TUE, "1013-有氧轻松跑 Z2 30min", 30, "Z2"),
                    _day(WED, "1014-力量训练 45min", 45, None),
                ],
            },
            status="confirmed",
            version=1,
        )
    )
    session.commit()
    return session


def _read_raw(self, dataset: str, day: dt.date):  # noqa: ANN001
    if dataset == "activities" and day == TUE:
        return [
            {
                "activityType": {"typeKey": "running"},
                "durationSeconds": 25 * 60,
                "averageHR": 135,
            }
        ]
    return None


def test_weekly_plan_response_carries_per_day_cards(seeded, settings, monkeypatch):
    """S4 主路径：周二被日报调整（30→25）→ 卡片带徽标与五项详情；周三原样。"""
    monkeypatch.setattr(GarminAdapter, "read_raw", _read_raw)
    seeded.add(
        TrainingSessionRow(
            id=f"session-{TUE.isoformat()}",
            date=TUE,
            sport="running",
            name="调整1013-有氧轻松跑 Z2 25min",
            payload={"sport": "running", "duration_min": 25, "target_hr_zone": "Z2"},
            status="draft",
        )
    )
    seeded.add(
        DailyReportRow(
            date=TUE,
            payload={"adjustment_reason": "HRV 偏低，减量", "load": None},
            version=1,
        )
    )
    seeded.commit()

    plan = get_weekly(week_start=MON, session=seeded)

    tue = plan.cards[TUE.isoformat()]
    assert tue["is_adjusted"] is True
    assert tue["badge"] == "日报已调整"
    assert tue["original"] == "1013-有氧轻松跑 Z2 30min"
    assert tue["adjusted"] == "调整1013-有氧轻松跑 Z2 25min"
    assert tue["reason"] == "HRV 偏低，减量"
    assert tue["report_ref"] == f"daily:{TUE.isoformat()}"
    assert "running" in tue["actual"] and "25min" in tue["actual"]

    wed = plan.cards[WED.isoformat()]
    assert wed["is_adjusted"] is False
    assert wed["badge"] is None


def test_weekly_plan_cards_empty_when_nothing_adjusted(seeded, settings, monkeypatch):
    """无任何调整 → 全部 is_adjusted=False，不出徽标（不制造噪音）。"""
    monkeypatch.setattr(GarminAdapter, "read_raw", _read_raw)
    plan = get_weekly(week_start=MON, session=seeded)
    for card in plan.cards.values():
        assert card["is_adjusted"] is False
        assert card["badge"] is None


def test_weekly_plan_cards_survive_legacy_payload(seeded, settings, monkeypatch):
    """旧 payload 无 cards 字段 → 现算照常产出（Q10-A：读取时算，不依赖存储）。"""
    monkeypatch.setattr(GarminAdapter, "read_raw", _read_raw)
    row = seeded.exec(__import__("sqlmodel").select(WeeklyPlanRow)).first()
    assert "cards" not in (row.payload or {})
    plan = get_weekly(week_start=MON, session=seeded)
    assert set(plan.cards) == {TUE.isoformat(), WED.isoformat()}
