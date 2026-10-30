"""G-16：算法口径版本号（2026-10-24 用户决策：**只标记、不回填**）。

口径变更（改 EWMA 窗口、改参考负荷 R、改 ``REQUIRED_METRICS``、改聚合窗口）时，
旧行与新行同表不可区分 → 趋势图会静默混用两种口径。风险规则早有
``RULE_VERSION`` 并在 ``risk_assessment`` 每日落库，这三项一直没有。

规则（本文件即契约）：

- 新写入的派生行与报告 payload 带当前版本；
- **历史行一律 legacy（NULL）**，不重算——重算会抹掉「当时用旧口径」的事实，
  与追溯目标相反（同 ``report_link`` 的「策略版本变化不自动重算历史」）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import Settings
from app.db.models import AcwrRow, DailyReportRow
from app.schemas.domain import StandardDaily
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


# ------------------------------------------------------------
# 版本常量
# ------------------------------------------------------------
def test_algo_versions_exposes_four_axes() -> None:
    """负荷 / ACWR / 质量 / 聚合四条口径都要能追溯（load 键 2026-10-25 补）。"""
    from app.domain.algo_version import LOAD_VERSION, algo_versions

    v = algo_versions()
    assert set(v) == {"load", "acwr", "quality", "aggregation"}
    assert v["load"] == LOAD_VERSION
    assert all(isinstance(x, str) and x and x != "legacy" for x in v.values())


# ------------------------------------------------------------
# 派生行：新写入带版本，历史行保持 NULL
# ------------------------------------------------------------
def test_acwr_write_stamps_current_version(session) -> None:
    from app.domain.algo_version import ACWR_VERSION
    from app.schemas.domain import AcwrSnapshot
    from app.storage.derived_store import upsert_acwr

    upsert_acwr(session, AcwrSnapshot(date=D, acute=1.0, chronic=1.0, ratio=1.0))

    row = session.exec(select(AcwrRow).where(AcwrRow.date == D)).first()
    assert row is not None
    assert row.acwr_version == ACWR_VERSION


def test_legacy_acwr_rows_stay_unstamped(session) -> None:
    """迁移前写入的行保持 NULL——「不回填」是决策，不是遗漏。"""
    session.add(AcwrRow(date=D, acute=1.0, chronic=1.0, ratio=1.0))
    session.commit()

    row = session.exec(select(AcwrRow).where(AcwrRow.date == D)).first()
    assert row is not None
    assert row.acwr_version is None


# ------------------------------------------------------------
# 报告 payload / 接口
# ------------------------------------------------------------
def test_daily_report_payload_records_algo_versions(session, settings) -> None:
    from app.domain.algo_version import algo_versions

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

    generate_daily(session, D, settings)

    row = session.exec(select(DailyReportRow).where(DailyReportRow.date == D)).first()
    assert row is not None
    assert row.payload["algo_versions"] == algo_versions()


def test_load_series_reports_algo_versions(session) -> None:
    from app.api.data import get_load_series
    from app.domain.algo_version import algo_versions

    out = get_load_series(start=None, end=None, session=session)
    assert out["algo_versions"] == algo_versions()
