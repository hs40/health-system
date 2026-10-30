"""各运动类型独立验证状态测试（需求 6.2 / 6.3，D-2 口径核实后补齐）。

需求 6.3 的两条硬约束：
1. 即使跨运动类型可比性尚未验证，**仍应展示可计算的负荷分值**（不隐藏）；
2. 必须**明确披露**该运动类型的验证状态，且**各运动类型独立管理**
   （一个类型未验证不自动改变其他类型的验证状态）。

断言全部来自需求原文，不是实现细节。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.adapters.garmin import GarminAdapter
from app.config import Settings
from app.domain.load.types import (
    VS_INSUFFICIENT,
    VS_UNREGISTERED,
    VS_VERIFIED,
)
from app.schemas.domain import StandardDaily
from app.services import report_service
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


@pytest.fixture()
def seeded(session):
    """两天合格标准化数据 + 足够负荷历史。"""
    for i in range(30):
        upsert_standard(
            session,
            StandardDaily(
                date=D - dt.timedelta(days=i),
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


def _seed_activity(monkeypatch, type_key: str):  # noqa: ANN001
    def _one_run(self, dataset: str, day: dt.date):  # noqa: ANN001
        if dataset == "activities":
            return [
                {
                    "activityType": {"typeKey": type_key},
                    "duration": 3600,
                    "averageHR": 140,
                }
            ]
        return None

    monkeypatch.setattr(GarminAdapter, "read_raw", _one_run)


# ------------------------------------------------------------
# 需求 6.2 / 6.3：未验证类型的负荷照常计入，状态必须披露
# ------------------------------------------------------------
def test_insufficient_sport_disclosed_in_limitations(seeded, settings, monkeypatch):
    """徒步（样本不足）→ 负荷照常计入，但日报须显式披露未验证状态。"""
    _seed_activity(monkeypatch, "hiking")
    report = report_service.generate_daily(seeded, D, settings)

    assert report.load is not None
    assert report.load.by_sport.get("hiking", 0) > 0, "未验证类型不得隐藏分值"
    assert report.load.unverified_sports == {"hiking": VS_INSUFFICIENT}
    joined = "；".join(report.limitations)
    assert "hiking" in joined and VS_INSUFFICIENT in joined, joined


def test_verified_sport_not_disclosed(seeded, settings, monkeypatch):
    """跑步已验证（n=99 / R²=0.51）→ 不出现未验证披露，避免噪音。"""
    _seed_activity(monkeypatch, "running")
    report = report_service.generate_daily(seeded, D, settings)

    assert report.load is not None
    assert report.load.unverified_sports == {}
    joined = "；".join(report.limitations)
    assert "不可验证" not in joined and "待验证" not in joined


def test_one_unverified_sport_does_not_taint_others(seeded, settings, monkeypatch):
    """需求 6.3：一个类型未验证**不改变**其他类型的验证状态。"""
    # 同日两种活动：running（已验证）+ hiking（未验证）
    def _two_acts(self, dataset: str, day: dt.date):  # noqa: ANN001
        if dataset == "activities":
            return [
                {"activityType": {"typeKey": "running"}, "duration": 1800, "averageHR": 150},
                {"activityType": {"typeKey": "hiking"}, "duration": 3600, "averageHR": 120},
            ]
        return None

    monkeypatch.setattr(GarminAdapter, "read_raw", _two_acts)
    report = report_service.generate_daily(seeded, D, settings)

    assert report.load is not None
    # 两个类型的分值都计入
    assert report.load.by_sport.get("running", 0) > 0
    assert report.load.by_sport.get("hiking", 0) > 0
    # 但只有 hiking 被标为未验证
    assert report.load.unverified_sports == {"hiking": VS_INSUFFICIENT}


# ------------------------------------------------------------
# 登记表自身的一致性（需求 6.1：采用后固定，由用户决定变更）
# ------------------------------------------------------------
def test_verification_registry_covers_all_counted_sports():
    """counted 的六个类型必须全部有登记 —— 不靠默认值兜底（需求 6.2）。"""
    from app.domain.load import SPORT_TIERS, sports

    counted = {s for s, t in SPORT_TIERS.items() if t == "counted"}
    for sport in counted:
        status, note = sports.verification_for(sport)
        assert status in {VS_VERIFIED, VS_INSUFFICIENT}, sport
        if status != VS_VERIFIED:
            assert note, f"{sport} 未验证但缺少说明"


def test_unregistered_sport_status_is_distinct_from_insufficient():
    """未登记公式 ≠ 样本不足：两者披露文案不同（需求 6.2）。"""
    from app.domain.load import sports

    unreg_status, unreg_note = sports.verification_for("skiroing")
    insuf_status, insuf_note = sports.verification_for("hiit")
    assert unreg_status == VS_UNREGISTERED
    assert insuf_status == VS_INSUFFICIENT
    assert unreg_note != insuf_note
