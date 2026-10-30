"""日报「前一日训练完成度」测试（§12.4 / 2026-10-20 决策 Q7；S3）。

需求：日报 payload 带 `yesterday_completion`（DayJudgment.to_dict() + reason），
周报从一周 7 份日报提取聚合、免重算。判定口径见 `judge_day`：

- 调整日与**调整后**计划比；强度同区才算达标（候选口径 L-11）；
- 无强度目标（历史计划）→ 只判时长并披露；未到期/无计划无记录 → 不判定。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.adapters.garmin import GarminAdapter
from app.config import Settings
from app.db.models import DailyReportRow, TrainingSessionRow, WeeklyPlanRow
from app.schemas.domain import StandardDaily
from app.services.report_service import generate_daily
from app.storage.standard_store import upsert_standard

D = dt.date(2026, 10, 16)  # 周五；前一日 10-15 周四
Y = D - dt.timedelta(days=1)


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
    """两天合格标准化数据（生成日报的最低配）。"""
    for day in (Y, D):
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


def _week_plan_payload(session_obj) -> dict:
    """本周计划：周四原安排 = 跑步 30min Z2。"""
    monday = D - dt.timedelta(days=D.weekday())
    return {
        "week_start": monday.isoformat(),
        "days": [
            {
                "date": Y.isoformat(),
                "weekday": Y.weekday(),
                "session": {
                    "id": f"wp-{Y.isoformat()}",
                    "date": Y.isoformat(),
                    "sport": "running",
                    "name": "1015-有氧轻松跑 Z2 30min",
                    "target_hr_zone": "Z2",
                    "duration_min": 30,
                    "status": "confirmed",
                },
                "is_night_shift": False,
                "note": None,
            }
        ],
    }


def _seed_week_plan(session: Session) -> None:
    payload = _week_plan_payload(session)
    session.add(
        WeeklyPlanRow(
            week_start=dt.date.fromisoformat(payload["week_start"]),
            payload=payload,
            status="confirmed",
            version=1,
        )
    )
    session.commit()


def _seed_daily_session(
    session: Session, *, name: str, duration_min: int, target_zone: str | None = None
) -> None:
    """日报调整后的当日训练（coach_service 覆盖写的那条）。"""
    session.add(
        TrainingSessionRow(
            id=f"session-{Y.isoformat()}",
            date=Y,
            sport="running",
            name=name,
            payload={
                "id": f"session-{Y.isoformat()}",
                "date": Y.isoformat(),
                "sport": "running",
                "name": name,
                "target_hr_zone": target_zone,
                "duration_min": duration_min,
                "warmup": [],
                "main": [],
                "cooldown": [],
                "status": "draft",
            },
            status="draft",
        )
    )
    session.commit()


def _activities(monkeypatch, acts: list[dict]) -> None:  # noqa: ANN001
    def _read_raw(self, dataset: str, day: dt.date):  # noqa: ANN001
        if dataset == "activities" and day == Y:
            return acts
        return None

    monkeypatch.setattr(GarminAdapter, "read_raw", _read_raw)


# ------------------------------------------------------------
# S3：日报 payload 带前一日完成度
# ------------------------------------------------------------
def test_daily_report_carries_yesterday_completion(seeded, settings, monkeypatch):
    """主路径：周四计划 30min Z2 被日报调到 25min，实际跑 25min 均心 135（Z2）
    → 日报（周五）带「日报调整后完成」，计划分钟取调整后值。"""
    _seed_week_plan(seeded)
    _seed_daily_session(seeded, name="调整1015-有氧轻松跑 Z2 25min", duration_min=25,
                        target_zone="Z2")
    _activities(
        monkeypatch,
        [{
            "activityType": {"typeKey": "running"},
            "duration": 25 * 60,
            "durationSeconds": 25 * 60,
            "averageHR": 135,
        }],
    )

    rep = generate_daily(seeded, D, settings)

    comp = rep.yesterday_completion
    assert comp is not None
    assert comp["date"] == Y.isoformat()
    assert comp["status"] == "adjustedCompleted"
    assert comp["adjusted"] is True
    assert comp["planned_min"] == 25, "调整日必须与调整后计划比（不是原计划 30）"
    assert comp["actual_min"] == 25
    assert comp["target_zone"] == "Z2" and comp["actual_zone"] == "Z2"
    assert comp["reason"] is None, "无差异不造原因"


def test_yesterday_completion_none_when_no_plan_and_no_record(seeded, settings, monkeypatch):
    """无计划无记录 → 不判定（None），不伪造状态。"""
    _activities(monkeypatch, [])
    rep = generate_daily(seeded, D, settings)
    assert rep.yesterday_completion is None


def test_yesterday_completion_persisted_in_payload(seeded, settings, monkeypatch):
    """完成度必须随日报落库（周报从 payload 提取，不重算）。"""
    _seed_week_plan(seeded)
    _activities(
        monkeypatch,
        [{
            "activityType": {"typeKey": "running"},
            "durationSeconds": 30 * 60,
            "averageHR": 135,
        }],
    )
    generate_daily(seeded, D, settings)
    row = seeded.exec(
        __import__("sqlmodel").select(DailyReportRow).where(DailyReportRow.date == D)
    ).first()
    assert row is not None
    comp = row.payload.get("yesterday_completion")
    assert comp is not None and comp["status"] == "completed"


def test_garmin_typekey_normalized_before_sport_comparison(seeded, settings, monkeypatch):
    """计划 sport=strength（LLM 缩写）vs 实际 typeKey=strength_training（Garmin）——
    两套词汇必须归一后再比，否则同一种运动被误判成「替代训练」。"""
    payload = _week_plan_payload(seeded)
    payload["days"][0]["session"]["sport"] = "strength"
    payload["days"][0]["session"]["name"] = "1015-力量训练 45min"
    # 本用例只验词汇归一：置空区间，避免强度判定干扰（历史计划场景）
    payload["days"][0]["session"]["target_hr_zone"] = None
    seeded.add(
        WeeklyPlanRow(
            week_start=dt.date.fromisoformat(payload["week_start"]),
            payload=payload,
            status="confirmed",
            version=1,
        )
    )
    seeded.commit()
    _activities(
        monkeypatch,
        [{
            "activityType": {"typeKey": "strength_training"},
            "durationSeconds": 45 * 60,
            "averageHR": 120,
        }],
    )

    rep = generate_daily(seeded, D, settings)

    comp = rep.yesterday_completion
    assert comp is not None
    assert comp["status"] != "substituted", "同一种运动不得因词汇差异误判为替代"
    assert comp["status"] == "completed"
    assert comp["planned_min"] == 30 and comp["actual_min"] == 45


def test_daily_report_carries_yesterday_training(seeded, settings, monkeypatch):
    """前一日实际训练必须进 ``yesterday_training``（2026-10-26 修复）。

    此前 ``generate_daily`` 硬编码 ``yesterday_training=None``，前端
    「前一日运动训练回顾」永远显示「无训练记录」——即便 raw 活动已同步、完成度也已判定。
    """
    _activities(
        monkeypatch,
        [
            {
                "activityId": 654942926,
                "activityName": "晨跑",
                "activityType": {"typeKey": "running"},
                "duration": 30 * 60,
                "averageHR": 135,
            }
        ],
    )
    rep = generate_daily(seeded, D, settings)
    yt = rep.yesterday_training
    assert yt is not None and yt["date"] == Y.isoformat()
    acts = yt["activities"]
    assert len(acts) == 1
    a = acts[0]
    assert a["sport"] == "running"
    assert a["name"] == "晨跑"
    assert a["duration_sec"] == 30 * 60
    assert a["load"] is not None and a["load"] > 0

