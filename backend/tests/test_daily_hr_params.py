"""日报心率参数字段测试（需求 5.4，开发计划 4.12）。

需求 5.4：日报展示**当日生效**的 max/rest HR、Karvonen 方法、五区整数边界。
断言全部来自需求，不是实现细节：

| 测试组 | 对应需求 |
|---|---|
| 日报带齐 hr_params 四个要素 | 5.4 |
| 与负荷计算同一组常量（single source of truth） | 5.4（展示值=计算值） |
| 五区边界由 Karvonen 公式推导且连续不重叠 | 5.2 / 9.3 |
| 落库 payload 含该字段（API 读得到） | 5.4 |
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.domain.load import hr_mod
from app.domain.load.hr import ZONE_BANDS
from app.schemas.domain import StandardDaily
from app.services.report_service import generate_daily
from app.storage.standard_store import upsert_standard

D = dt.date(2026, 10, 20)

# Karvonen 五区期望边界（max=180 / rest=52，HRR=128），按需求 5.2 公式手算：
# bpm = rest + pct × (max − rest)，四舍五入取整
EXPECTED_BOUNDS = [116, 129, 142, 154, 167, 180]


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
    """两天合格标准化数据：足够生成日报（风险/基线不会因缺数据崩）。"""
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


# ------------------------------------------------------------
# 需求 5.4：日报带齐心率参数
# ------------------------------------------------------------
def test_daily_report_carries_hr_params(seeded, settings):
    """日报必须带当日生效心率参数：max/rest HR、方法、五区边界。"""
    report = generate_daily(seeded, D, settings)
    hp = report.hr_params
    assert hp is not None
    assert hp["max_hr"] == hr_mod.MAX_HR
    assert hp["rest_hr"] == hr_mod.REST_HR_BASELINE
    assert hp["method"] == "karvonen"
    assert hp["hrr"] == hp["max_hr"] - hp["rest_hr"]
    assert len(hp["zones"]) == 5


def test_hr_params_match_load_computation(seeded, settings):
    """展示值必须与负荷计算用的是同一组常量，不允许出现两套口径。"""
    report = generate_daily(seeded, D, settings)
    assert report.hr_params == hr_mod.params().to_dict()


# ------------------------------------------------------------
# 需求 5.2 / 9.3：五区由 Karvonen 公式推导，边界连续不重叠
# ------------------------------------------------------------
def test_zone_bounds_follow_karvonen_formula():
    """五个边界点 = rest + pct×HRR 四舍五入；端点即 rest 与 max。"""
    rest, mx = hr_mod.REST_HR_BASELINE, hr_mod.MAX_HR
    hrr = mx - rest
    # 五区覆盖 50%–100% HRR：Z1 下界是 rest+0.5·HRR（不是 rest），Z5 上界是 max
    pcts = [ZONE_BANDS[0][0]] + [b[1] for b in ZONE_BANDS]
    expect = [int(rest + p * hrr + 0.5) for p in pcts]
    assert expect == EXPECTED_BOUNDS
    assert expect[0] == int(rest + 0.5 * hrr + 0.5) and expect[-1] == mx


def test_zone_bounds_are_contiguous_integers(seeded, settings):
    """相邻区间共用边界值：Z[i].high == Z[i+1].low，无空档无重叠（需求 9.3）。"""
    report = generate_daily(seeded, D, settings)
    zones = report.hr_params["zones"]
    for a, b in zip(zones, zones[1:], strict=False):
        assert isinstance(a["low_bpm"], int) and isinstance(a["high_bpm"], int)
        assert a["high_bpm"] == b["low_bpm"]
    assert zones[-1]["high_bpm"] == report.hr_params["max_hr"]
    assert zones[0]["low_bpm"] == EXPECTED_BOUNDS[0]


# ------------------------------------------------------------
# 落库可读（API /reports/daily 走的是 payload）
# ------------------------------------------------------------
def test_persisted_report_payload_contains_hr_params(seeded, settings):
    """hr_params 必须随日报落库，历史日报读回来同样带这组值。"""
    generate_daily(seeded, D, settings)
    from app.db.models import DailyReportRow

    row = seeded.exec(
        __import__("sqlmodel").select(DailyReportRow).where(DailyReportRow.date == D)
    ).first()
    assert row is not None
    hp = row.payload.get("hr_params")
    assert hp is not None and hp["method"] == "karvonen"
    assert [z["low_bpm"] for z in hp["zones"]] == EXPECTED_BOUNDS[:5]
    assert [z["high_bpm"] for z in hp["zones"]] == EXPECTED_BOUNDS[1:]
