"""负荷 / ACWR 接入日报的回归测试。

覆盖（需求 4.1 / 4.4 / 8.1）：
- 活动 → 原始负荷（Banister TRIMP）→ 标准化 0–100 → 日汇总；
- ``generate_daily`` 回填 ``report.load`` 与 ``report.acwr``，并落派生层；
- 无活动 → 不产出负荷、ACWR 不可用（不伪造 0 值）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.adapters.garmin import GarminAdapter
from app.config import Settings
from app.schemas.domain import StandardDaily
from app.services import report_service
from app.storage.standard_store import upsert_standard

D = dt.date(2026, 10, 14)


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


def _seed_standard(session: Session, start: dt.date, days: int) -> None:
    for i in range(days):
        upsert_standard(
            session,
            StandardDaily(
                date=start + dt.timedelta(days=i),
                resting_heart_rate=50,
                max_heart_rate=180,
                hrv_last_night_avg=60.0,
                sleep_duration_sec=8 * 3600,
                steps=8000,
                body_battery_wake=70,
            ),
        )


def _one_run(self, dataset: str, day: dt.date):  # noqa: ANN001
    if dataset == "activities":
        return [{"activityType": {"typeKey": "running"}, "duration": 1800, "averageHR": 140}]
    return None


def test_standardize_activities_uses_banister_trimp(settings, monkeypatch):
    """30min @ HR140（rest 52 / max 180）→ Banister TRIMP → 标准化分值。"""
    monkeypatch.setattr(GarminAdapter, "read_raw", _one_run)
    a = GarminAdapter(settings)
    sd = StandardDaily(date=D, resting_heart_rate=50, max_heart_rate=180)
    daily = a.standardize_activities(D, sd)
    assert len(daily.contributions) == 1
    c = daily.contributions[0]
    assert c.sport == "running"
    # HRR = (140-52)/128 = 0.6875；TRIMP = 30 × 0.6875 × 0.64 × e^(1.92×0.6875)
    expected_raw = 30 * 0.6875 * 0.64 * 2.718281828 ** (1.92 * 0.6875)
    assert c.raw == pytest.approx(expected_raw, rel=0.02)
    assert c.score == pytest.approx(expected_raw / 130 * 100, rel=0.02)
    assert daily.total == pytest.approx(c.score, rel=1e-6)


def test_standardize_activities_empty_is_no_load(settings, monkeypatch):
    monkeypatch.setattr(GarminAdapter, "read_raw", lambda self, dataset, day: [])
    a = GarminAdapter(settings)
    daily = a.standardize_activities(D, StandardDaily(date=D))
    assert daily.total == 0.0
    assert daily.contributions == ()


def test_daily_report_fills_load_and_acwr(session, settings, monkeypatch):
    start = D - dt.timedelta(days=30)
    _seed_standard(session, start, 31)
    monkeypatch.setattr(GarminAdapter, "read_raw", _one_run)

    # 按时间顺序逐日生成（负荷在派生层累积，ACWR 才有 28 天历史可用）
    rep = None
    d = start
    while d <= D:
        rep = report_service.generate_daily(session, d, settings)
        d += dt.timedelta(days=1)

    assert rep is not None
    assert rep.load is not None
    assert rep.load.method == "mixed_banister_strength_v2"
    assert rep.load.combined > 0
    # 恒定负荷下 ratio 应落在甜蜜点；EWMA 冷启动期慢性尚未收敛（τ=28，
    # 31 天不足 3 个时间常数），ratio 会略高于 1 —— 这是 EWMA 的固有行为，
    # 需求 8.2 也允许从现有历史开始计算并标注数据不足。
    assert rep.acwr is not None
    assert rep.acwr.ratio is not None
    assert 0.9 <= rep.acwr.ratio <= 1.3, "恒定负荷应落在甜蜜点附近"


def test_daily_report_without_activities_has_no_acwr(session, settings, monkeypatch):
    _seed_standard(session, D - dt.timedelta(days=30), 31)
    monkeypatch.setattr(GarminAdapter, "read_raw", lambda self, dataset, day: None)

    rep = report_service.generate_daily(session, D, settings)

    assert rep.load is None
    assert rep.acwr is None
    assert any("ACWR" in x for x in rep.limitations)
