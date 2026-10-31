"""重算编排回归测试（需求第十章 + Q4）。

断言全部来自业务规则：

| 测试组 | 对应需求 |
|---|---|
| 变更自动触发、无手动重算入口 | 10.1 |
| 从最早受影响日重算至当前日期 | 10.2 |
| 重叠任务合并（取最早受影响日） | 10.3 |
| 执行中的任务不中途合并新变更，随后再算 | 10.4 |
| 状态机沿用 ``task_run`` | 10.5 |
| 历史日报不更新、当天日报覆盖 | 10.6 |
| 重算执行期间不生成当天日报，结束后生成 | Q4 |
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.adapters.garmin import GarminAdapter
from app.config import Settings
from app.db.models import AcwrRow, DailyReportRow, TaskRunRow
from app.domain.load import CHRONIC_DAYS, acwr_for
from app.errors import AppError
from app.orchestrator import task_state
from app.schemas.domain import StandardDaily
from app.schemas.enums import ErrorCode, TaskStatus, TaskType
from app.services import recompute_service, report_service
from app.storage.derived_store import load_range
from app.storage.standard_store import upsert_standard

TODAY = dt.date.today()
START = TODAY - dt.timedelta(days=40)  # 足够长的历史，含 EWMA 预热


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


def _seed(session: Session, start: dt.date, days: int) -> None:
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


class _FakeGarmin:
    """用可控活动表替换 Garmin 原始读取：``{date: [activities]}``。"""

    def __init__(self, activities: dict[dt.date, list[dict]]) -> None:
        self.activities = activities

    def read_raw(self, dataset: str, day: dt.date):  # noqa: ANN201
        if dataset == "activities":
            return self.activities.get(day)
        return None


@pytest.fixture()
def wire(monkeypatch, settings):
    """把服务层用到的 Settings 与原始读取都指向测试替身。"""

    def fake_settings():
        return settings

    monkeypatch.setattr(recompute_service, "_settings", fake_settings)
    # report_service 内部的 GarminAdapter 通过 read_raw 读原始活动
    state: dict[dt.date, list[dict]] = {}

    def read_raw(self, dataset: str, day: dt.date):  # noqa: ANN001, ANN201
        if dataset == "activities":
            return state.get(day)
        return None

    monkeypatch.setattr(GarminAdapter, "read_raw", read_raw)
    return state


# ============================================================
# 10.2：最早受影响日 → 今天
# ============================================================
def test_recompute_covers_earliest_affected_day_to_today(session, wire):
    """需求 10.2：重算范围从最早受影响日一直覆盖到今天。"""
    _seed(session, START, 41)
    earliest = TODAY - dt.timedelta(days=5)

    out = recompute_service.request(session, earliest)

    assert out["from"] == earliest.isoformat()
    assert out["to"] == TODAY.isoformat()
    assert out["days"] == 6  # 含首尾
    assert out["status"] == TaskStatus.SUCCESS.value


def test_recompute_backfills_missing_load_day(session, wire):
    """需求 10.2：补入的当天之前无负荷数据的日子也能被补算出来。"""
    _seed(session, START, 41)
    day = TODAY - dt.timedelta(days=3)
    wire[day] = [
        {"activityType": {"typeKey": "running"}, "duration": 1800, "averageHR": 140}
    ]
    assert load_range(session, day, day) == []

    recompute_service.request(session, day)

    rows = load_range(session, day, day)
    assert len(rows) == 1
    assert rows[0].combined > 0


# ============================================================
# 10.2：ACWR 必须回放，而不是只算当天
# ============================================================
def test_recompute_acwr_matches_full_replay(session, wire):
    """需求 10.2 / 8.1：回补后逐日 ACWR 与「全程重算」结果一致。"""
    _seed(session, START, 41)
    # 第 -20 天补一次大负荷
    big = TODAY - dt.timedelta(days=20)
    wire[big] = [{"activityType": {"typeKey": "running"}, "duration": 5400, "averageHR": 155}]
    recompute_service.request(session, big)

    # 用落库负荷从头重推，比对各日 ACWR 与重算结果
    from app.db.models import AcwrRow

    loads = {x.date: x.combined for x in load_range(session, START, TODAY)}
    for delta in (20, 15, 10, 7, 1, 0):
        d = TODAY - dt.timedelta(days=delta)
        expect = acwr_for(loads, d, lookback_start=d - dt.timedelta(days=CHRONIC_DAYS * 2))
        row = session.exec(select(AcwrRow).where(AcwrRow.date == d)).first()
        assert row is not None, f"{d} 缺 ACWR"
        assert row.acute == pytest.approx(expect.acute_ewma, rel=1e-6)
        assert row.chronic == pytest.approx(expect.chronic_ewma, rel=1e-6)


# ============================================================
# 10.3 / 10.4：合并与「执行中不中途合并」
# ============================================================
def test_overlapping_change_is_merged_not_duplicated(session, wire):
    """需求 10.3：执行中的重算任务收到新变更 → 合并到同一任务，不另起任务。"""
    _seed(session, START, 41)
    running = task_state.begin(
        session, TaskType.RECOMPUTE, "recompute:running-demo", checkpoint="from=x"
    )
    before = len(session.exec(select(TaskRunRow)).all())

    out = recompute_service.request(session, TODAY - dt.timedelta(days=2))

    assert out["merged"] is True
    assert out["task_id"] == running.id
    after = len(session.exec(select(TaskRunRow)).all())
    assert after == before, "合并时不应新建任务"


def test_change_during_run_is_queued_then_handled_later(session, wire):
    """需求 10.4：执行中新变更不改变本次范围，任务结束后再按新起点重算。"""
    _seed(session, START, 41)
    # 模拟「正在执行」的任务
    task_state.begin(session, TaskType.RECOMPUTE, "recompute:running-demo")
    queued = TODAY - dt.timedelta(days=9)
    out = recompute_service.request(session, queued)
    assert out["queued_from"] == queued.isoformat()

    # 结束该任务（模拟其跑完）
    row = session.exec(
        select(TaskRunRow).where(TaskRunRow.idempotency_key == "recompute:running-demo")
    ).first()
    task_state.finish(session, row.id, TaskStatus.SUCCESS)
    assert recompute_service.is_running(session) is False

    # 结束后的下一次触发应把 pending 起点纳入范围
    out2 = recompute_service.request(session, TODAY - dt.timedelta(days=1))
    assert out2["merged"] is False
    assert out2["from"] == queued.isoformat(), "待处理起点应被合并进新任务范围"


# ============================================================
# Q4：执行期间不生成当天日报；结束后生成
# ============================================================
def test_daily_report_blocked_while_recompute_running(session, wire):
    """Q4：重算执行期间，生成当天日报 → 409（E_TASK_IN_PROGRESS）。"""
    _seed(session, START, 41)
    task_state.begin(session, TaskType.RECOMPUTE, "recompute:running-demo")
    with pytest.raises(AppError) as ei:
        report_service.generate_daily(session, TODAY, recompute_service._settings())
    assert ei.value.code == ErrorCode.TASK_IN_PROGRESS
    assert ei.value.http_status == 409


def test_daily_report_generated_after_recompute(session, wire):
    """Q4：重算任务内部允许生成当天日报；任务结束后外部也可生成。"""
    _seed(session, START, 41)
    out = recompute_service.request(session, TODAY - dt.timedelta(days=2))
    assert out["daily_report_generated"] is True

    rows = session.exec(select(DailyReportRow).where(DailyReportRow.date == TODAY)).all()
    assert len(rows) == 1
    # 任务结束后不再被拦截，可重新生成并覆盖（10.6 当天日报）
    report_service.generate_daily(session, TODAY, recompute_service._settings())
    rows = session.exec(select(DailyReportRow).where(DailyReportRow.date == TODAY)).all()
    assert len(rows) == 1 and rows[0].version >= 2


# ============================================================
# 10.6（2026-10-31 用户修订）：负荷变化后，区间内所有日报都重算
# ============================================================
def test_history_daily_report_regenerated(session, wire):
    """2026-10-31 用户决策：不再只更新当天，历史日报也必须随负荷变化重算。"""
    _seed(session, START, 41)
    history_day = TODAY - dt.timedelta(days=10)
    report_service.generate_daily(session, history_day, recompute_service._settings())
    row = session.exec(select(DailyReportRow).where(DailyReportRow.date == history_day)).first()
    original_version = row.version

    recompute_service.request(session, history_day)

    row = session.exec(select(DailyReportRow).where(DailyReportRow.date == history_day)).first()
    assert row.version > original_version, "历史日报必须随负荷变化重算"


def test_recompute_regenerates_every_report_in_range(session, wire):
    """区间内每一天都要有日报（不只 end）。"""
    _seed(session, START, 41)
    start = TODAY - dt.timedelta(days=5)

    recompute_service.request(session, start)

    dates = {r.date for r in session.exec(select(DailyReportRow)).all()}
    expected = {start + dt.timedelta(days=i) for i in range(6)}
    assert expected <= dates, f"缺少日报：{sorted(expected - dates)}"


# ============================================================
# 10.5：沿用现有状态机
# ============================================================
def test_recompute_uses_task_run_state_machine(session, wire):
    """需求 10.5：重算以 ``task_run`` 登记，不新增专用日志表。"""
    _seed(session, START, 41)
    out = recompute_service.request(session, TODAY - dt.timedelta(days=1))
    row = session.get(TaskRunRow, out["task_id"])
    assert row.task_type == TaskType.RECOMPUTE.value
    assert row.status == TaskStatus.SUCCESS.value
    assert row.finished_at is not None


# ============================================================
# 崩溃恢复（2026-10-20 实测事故的回归防线）
# ============================================================
def test_acwr_ratio_nullable_when_chronic_zero(session):
    """需求 8.4：慢性 EWMA 为 0 时 ACWR 不可计算 —— 存 NULL，不得填 0 伪造。"""
    from app.schemas.domain import AcwrSnapshot
    from app.storage.derived_store import upsert_acwr

    day = dt.date(2026, 10, 20)
    upsert_acwr(session, AcwrSnapshot(date=day, acute=0.0, chronic=0.0, ratio=None))
    row = session.exec(select(AcwrRow).where(AcwrRow.date == day)).first()
    assert row is not None and row.ratio is None


def test_fresh_running_task_still_blocks(session):
    """未超时的执行中任务仍按 Q4 挡住日报（防止误收割正在跑的任务）。"""
    recent = task_state.begin(
        session, TaskType.RECOMPUTE, "recompute:fresh", checkpoint="from=2026-10-01"
    )
    assert recompute_service.blocking_task(session) is not None
    assert recompute_service.running_task(session) is not None
    assert recent.status == TaskStatus.RETRY.value


def test_stale_task_is_reaped_instead_of_blocking_forever(session):
    """僵尸任务（进程已死）必须被收割：否则 Q4 会永久挡住日报、重算全部空转。"""
    zombie = task_state.begin(
        session, TaskType.RECOMPUTE, "recompute:zombie", checkpoint="from=2026-10-01"
    )
    # 手工把启动时间改到 8 小时前（真实场景：进程崩溃/被杀后无人落终态）
    zombie.started_at = dt.datetime.now() - dt.timedelta(hours=8)
    session.add(zombie)
    session.commit()

    blocking = recompute_service.blocking_task(session)
    assert blocking is None  # 已被收割，不再挡路
    session.refresh(zombie)
    assert zombie.status == TaskStatus.FAILED.value
    assert zombie.finished_at is not None


def test_daily_report_unblocked_after_zombie_reaped(session, settings):
    """崩溃恢复闭环：僵尸挡住日报 → 收割后日报可正常生成。"""
    _seed(session, TODAY, 3)
    zombie = task_state.begin(
        session, TaskType.RECOMPUTE, "recompute:zombie2", checkpoint="from=2026-10-01"
    )
    zombie.started_at = dt.datetime.now() - dt.timedelta(hours=7)
    session.add(zombie)
    session.commit()

    # 闸门自己收割僵尸：7h 无终态 → 判进程已死，本次调用即放行并生成日报
    report = report_service.generate_daily(session, TODAY, settings)
    assert report.date == TODAY
    session.refresh(zombie)
    assert zombie.status == TaskStatus.FAILED.value  # 僵尸已被收割为终态
    assert zombie.finished_at is not None

    rows = session.exec(select(DailyReportRow).where(DailyReportRow.date == TODAY)).all()
    assert len(rows) == 1


def test_per_day_failure_does_not_poison_session(session, wire, monkeypatch):
    """单日 flush 失败后必须 rollback：否则后续每天抛同一个错，任务退化成僵尸。"""
    _seed(session, START, 41)
    boom = TODAY - dt.timedelta(days=10)
    good = TODAY - dt.timedelta(days=9)
    wire[good] = [
        {"activityType": {"typeKey": "running"}, "duration": 1800, "averageHR": 145}
    ]

    original = report_service.recompute_day_load

    def flaky(s, day, st):  # noqa: ANN001, ANN201
        if day == boom:
            raise RuntimeError("模拟单日写入失败")
        return original(s, day, st)

    monkeypatch.setattr(report_service, "recompute_day_load", flaky)

    out = recompute_service.request(session, boom)

    # 失败日被记录，但后续日照常重算，任务落了终态（不是僵尸）
    assert any(boom.isoformat() in e for e in out["errors"])
    assert load_range(session, good, good)  # 失败日之后的日子照样算出来了
    row = recompute_service.running_task(session)
    assert row is None  # 已落终态
    finished = session.exec(select(TaskRunRow).order_by(TaskRunRow.started_at.desc())).first()
    assert finished.status in {TaskStatus.PARTIAL.value, TaskStatus.FAILED.value}


# ------------------------------------------------------------
# 聚合层重生成（2026-10-25 缺口 1）：周/月报必须跟着重算
# ------------------------------------------------------------
def test_week_starts_covers_range():
    from app.services.recompute_service import week_starts_in_range

    assert week_starts_in_range(dt.date(2026, 10, 1), dt.date(2026, 10, 14)) == [
        dt.date(2026, 9, 28),
        dt.date(2026, 10, 5),
        dt.date(2026, 10, 12),
    ]


def test_months_in_range_covers_range():
    from app.services.recompute_service import months_in_range

    assert months_in_range(dt.date(2026, 10, 1), dt.date(2026, 11, 15)) == ["2026-10", "2026-11"]
    assert months_in_range(dt.date(2025, 12, 1), dt.date(2026, 1, 31)) == ["2025-12", "2026-01"]


def test_regenerate_aggregates_covers_all_periods(session):
    """区间内每个周一/每个月都要重生成（空数据也不抛异常）。"""
    from app.services.recompute_service import (
        months_in_range,
        regenerate_aggregates,
        week_starts_in_range,
    )

    out = regenerate_aggregates(
        session, dt.date(2026, 10, 1), dt.date(2026, 10, 14), recompute_service._settings()
    )
    assert out == {
        "weekly": len(week_starts_in_range(dt.date(2026, 10, 1), dt.date(2026, 10, 14))),
        "monthly": len(months_in_range(dt.date(2026, 10, 1), dt.date(2026, 10, 14))),
    }


# ------------------------------------------------------------
# 聚合层重生成（2026-10-25 缺口 1）：周/月报必须跟着重算
# ------------------------------------------------------------

def test_run_preserves_pending_across_finish(session, wire, monkeypatch):
    """需求 10.3：``run()`` 结束时**不得覆盖**执行期间写入的 pending。

    此前 ``run()`` 的 finish 用 ``from/to/days`` 无条件覆盖 checkpoint，把执行中
    ``_set_pending`` 追加的 ``pending`` 冲掉——下一次触发看不到更早的待处理起点，导致漏重算。
    """
    _seed(session, START, 41)
    orig = report_service.recompute_day_load

    def hook(s, day, st):  # noqa: ANN001, ANN201
        if not getattr(hook, "fired", False):
            hook.fired = True
            # 模拟「执行期间到达一次更早的变更」→ 只落 pending，不改变本次范围
            recompute_service.request(s, TODAY - dt.timedelta(days=20))
        return orig(s, day, st)

    monkeypatch.setattr(report_service, "recompute_day_load", hook)
    out = recompute_service.request(session, TODAY - dt.timedelta(days=3))

    row = session.get(TaskRunRow, out["task_id"])
    pending = recompute_service._pending_start(row)
    assert pending is not None, "finish 覆盖了执行期间写入的 pending（10.3 漏重算）"
    assert pending == TODAY - dt.timedelta(days=20)
