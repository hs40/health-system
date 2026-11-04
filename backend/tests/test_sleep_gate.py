"""睡眠完整性门控（2026-10-30 需求）。

需求：
- 07:30 同步时，若**有入睡时间、没有起床时间**（睡眠未结束），先不生成日报/计划/推送，
  每小时重试，直到有起床时间或到 12:00，再一次性生成日报、建课程、钉钉推送。
- 若**没有入睡时间**（没有睡眠），必须直接跳过延迟逻辑、正常生成 —— 重点防误判。

本文件锁定：
1. ``sleep_state`` 区分 none / ongoing / complete，且把 1970 占位时间戳视为「无」；
2. ``should_defer`` 只在 ongoing 且未到 12:00 时延迟；
3. 调度核心 ``_run_daily_core``：ongoing 且 <12:00 → 不跑下游；到 12:00 / complete /
   none → 跑下游；retry 且当天日报已存在 → 直接跳过（不重复同步/生成）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.adapters.garmin import GarminSyncResult
from app.config import Settings
from app.db.models import DailyReportRow
from app.domain.sleep import (
    SLEEP_COMPLETE,
    SLEEP_NONE,
    SLEEP_ONGOING,
    should_defer,
    sleep_state,
)
from app.schemas.domain import StandardDaily

DAY = dt.date(2026, 10, 30)
VALID_START = dt.datetime(2026, 10, 30, 0, 30)
VALID_END = dt.datetime(2026, 10, 30, 7, 0)
SENTINEL = dt.datetime(1970, 1, 1)  # Garmin 缺失时间戳被 _epoch_ms_to_local(0) 转成 1970


def _sd(start=None, end=None) -> StandardDaily:  # noqa: ANN001
    return StandardDaily(date=DAY, sleep_start_ts=start, sleep_end_ts=end)


# ---------------------------------------------------------------------------
# 1) sleep_state：重点覆盖「无入睡时间」与 1970 占位
# ---------------------------------------------------------------------------


def test_sleep_state_none_when_no_start() -> None:
    assert sleep_state(_sd(None, VALID_END)) == SLEEP_NONE


def test_sleep_state_none_when_start_is_sentinel() -> None:
    """1970 占位 = 无有效入睡时间，不得当成"有入睡"。"""
    assert sleep_state(_sd(SENTINEL, VALID_END)) == SLEEP_NONE


def test_sleep_state_ongoing_when_no_end() -> None:
    assert sleep_state(_sd(VALID_START, None)) == SLEEP_ONGOING


def test_sleep_state_ongoing_when_end_is_sentinel() -> None:
    assert sleep_state(_sd(VALID_START, SENTINEL)) == SLEEP_ONGOING


def test_sleep_state_complete() -> None:
    assert sleep_state(_sd(VALID_START, VALID_END)) == SLEEP_COMPLETE


# ---------------------------------------------------------------------------
# 2) should_defer：只有 ongoing 且 <12:00 才延迟
# ---------------------------------------------------------------------------


def test_should_defer_until_noon() -> None:
    assert should_defer(SLEEP_ONGOING, dt.datetime(2026, 10, 30, 8, 30)) is True
    assert should_defer(SLEEP_ONGOING, dt.datetime(2026, 10, 30, 11, 59)) is True
    assert should_defer(SLEEP_ONGOING, dt.datetime(2026, 10, 30, 12, 0)) is False


def test_should_defer_when_no_sleep_before_noon() -> None:
    """无入睡时间（Garmin 全 null 占位）也视为未落定：12:00 前延迟，到点强制生成。"""
    assert should_defer(SLEEP_NONE, dt.datetime(2026, 10, 30, 8, 0)) is True
    assert should_defer(SLEEP_NONE, dt.datetime(2026, 10, 30, 12, 0)) is False


def test_should_not_defer_when_complete() -> None:
    assert should_defer(SLEEP_COMPLETE, dt.datetime(2026, 10, 30, 8, 0)) is False


# ---------------------------------------------------------------------------
# 3) 调度核心：门控行为
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _patch(monkeypatch, sd, downstream: list, syncs: list) -> None:  # noqa: ANN001
    import app.orchestrator.scheduler as sched

    def fake_sync(session, target, settings, *, refresh=False):  # noqa: ANN001
        syncs.append((target, refresh))
        return GarminSyncResult(date=target)

    monkeypatch.setattr(sched, "_sync_daily", fake_sync)
    monkeypatch.setattr(sched, "_get_standard", lambda session, target: sd)
    monkeypatch.setattr(
        sched,
        "_run_downstream",
        lambda session, target, settings, out, sync: downstream.append(target),
    )


def test_core_defers_when_sleep_ongoing_before_noon(monkeypatch, session) -> None:
    import app.orchestrator.scheduler as sched

    downstream: list = []
    syncs: list = []
    _patch(monkeypatch, _sd(VALID_START, None), downstream, syncs)

    out = sched._run_daily_core(
        session, DAY, Settings(_env_file=None), dt.datetime(2026, 10, 30, 8, 30), retry=False
    )

    assert out.get("deferred") == "sleep_ongoing"
    assert downstream == [], "睡眠未结束不得生成日报/计划/推送"


def test_core_generates_at_noon_even_if_ongoing(monkeypatch, session) -> None:
    import app.orchestrator.scheduler as sched

    downstream: list = []
    syncs: list = []
    _patch(monkeypatch, _sd(VALID_START, None), downstream, syncs)

    out = sched._run_daily_core(
        session, DAY, Settings(_env_file=None), dt.datetime(2026, 10, 30, 12, 0), retry=False
    )

    assert not out.get("deferred")
    assert downstream == [DAY], "到 12:00 无论是否完整都必须生成"


def test_core_defers_when_no_sleep_start_before_noon(monkeypatch, session) -> None:
    """无入睡时间（Garmin 全 null 占位）也视为未落定：12:00 前延迟。"""
    import app.orchestrator.scheduler as sched

    downstream: list = []
    syncs: list = []
    _patch(monkeypatch, _sd(None, None), downstream, syncs)

    out = sched._run_daily_core(
        session, DAY, Settings(_env_file=None), dt.datetime(2026, 10, 30, 8, 0), retry=False
    )

    assert out.get("deferred") == "sleep_ongoing"
    assert downstream == [], "无入睡时间在 12:00 前不得提前生成"


def test_core_generates_when_no_sleep_start_at_noon(monkeypatch, session) -> None:
    import app.orchestrator.scheduler as sched

    downstream: list = []
    syncs: list = []
    _patch(monkeypatch, _sd(None, None), downstream, syncs)

    out = sched._run_daily_core(
        session, DAY, Settings(_env_file=None), dt.datetime(2026, 10, 30, 12, 0), retry=False
    )

    assert not out.get("deferred")
    assert downstream == [DAY], "到 12:00 无论睡眠是否完整都必须生成"


def test_core_retry_skips_when_report_exists_and_sleep_complete(monkeypatch, session) -> None:
    """日报已存在且睡眠已完整：无需再刷，直接跳过。"""
    import app.orchestrator.scheduler as sched

    session.add(DailyReportRow(date=DAY, payload={}))
    session.commit()
    downstream: list = []
    syncs: list = []
    _patch(monkeypatch, _sd(VALID_START, VALID_END), downstream, syncs)

    out = sched._run_daily_core(
        session, DAY, Settings(_env_file=None), dt.datetime(2026, 10, 30, 8, 30), retry=True
    )

    assert out.get("skipped") == "report_exists"
    assert syncs == [], "睡眠已完整时重试不得再同步"
    assert downstream == []


def test_core_retry_skips_when_report_exists_after_deadline(monkeypatch, session) -> None:
    """过了 12:00 兜底线：即便睡眠仍不完整也不再重试（兜底已生成）。"""
    import app.orchestrator.scheduler as sched

    session.add(DailyReportRow(date=DAY, payload={}))
    session.commit()
    downstream: list = []
    syncs: list = []
    _patch(monkeypatch, _sd(VALID_START, None), downstream, syncs)

    out = sched._run_daily_core(
        session, DAY, Settings(_env_file=None), dt.datetime(2026, 10, 30, 12, 0), retry=True
    )

    assert out.get("skipped") == "report_exists"
    assert syncs == [], "过截止线后不再同步"
    assert downstream == []


def test_core_retry_refreshes_when_report_exists_but_sleep_incomplete(monkeypatch, session) -> None:
    """日报已存在但睡眠未完整（占位睡眠提前生成日报的场景）：12:00 前应刷新而非跳过。

    2026-10-31 真实故障：07:30 旧代码按占位睡眠生成了日报；部署修复后，若重试因
    「日报已存在」直接跳过，占位数据永远不会被完整睡眠覆盖 → 当日数据永久不完整。
    因此「跳过」必须以「睡眠已完整 / 已过 12:00」为前提。
    """
    import app.orchestrator.scheduler as sched

    session.add(DailyReportRow(date=DAY, payload={}))
    session.commit()
    downstream: list = []
    syncs: list = []
    _patch(monkeypatch, _sd(VALID_START, None), downstream, syncs)

    out = sched._run_daily_core(
        session, DAY, Settings(_env_file=None), dt.datetime(2026, 10, 30, 8, 30), retry=True
    )

    assert syncs == [(DAY, True)], "日报已存在但睡眠未完整时，重试必须刷新"
    assert out.get("deferred") == "sleep_ongoing"
    assert downstream == [], "睡眠仍未完整，不得生成"


def test_core_retry_generates_when_sleep_complete(monkeypatch, session) -> None:
    import app.orchestrator.scheduler as sched

    downstream: list = []
    syncs: list = []
    _patch(monkeypatch, _sd(VALID_START, VALID_END), downstream, syncs)

    out = sched._run_daily_core(
        session, DAY, Settings(_env_file=None), dt.datetime(2026, 10, 30, 9, 30), retry=True
    )

    assert not out.get("deferred")
    assert downstream == [DAY], "补到起床时间后应立即生成"


def test_core_retry_requests_refresh(monkeypatch, session) -> None:
    """每小时重试必须用刷新（refresh=True），才能把占位睡眠更新为完整记录。"""
    import app.orchestrator.scheduler as sched

    downstream: list = []
    syncs: list = []
    _patch(monkeypatch, _sd(VALID_START, VALID_END), downstream, syncs)

    sched._run_daily_core(
        session, DAY, Settings(_env_file=None), dt.datetime(2026, 10, 30, 9, 30), retry=True
    )

    assert syncs == [(DAY, True)]


def test_core_initial_run_does_not_refresh(monkeypatch, session) -> None:
    import app.orchestrator.scheduler as sched

    downstream: list = []
    syncs: list = []
    _patch(monkeypatch, _sd(VALID_START, VALID_END), downstream, syncs)

    sched._run_daily_core(
        session, DAY, Settings(_env_file=None), dt.datetime(2026, 10, 30, 7, 30), retry=False
    )

    assert syncs == [(DAY, False)]
