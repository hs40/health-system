"""load-series 图表端点测试（需求 4.5 / 4.6 / 8.1 / 8.3，开发计划 4.11）。

断言全部来自业务规则：

| 测试组 | 对应需求 |
|---|---|
| 默认 28 天 / 上限 / 日期校验 | 图表可用性 |
| 无负荷日保留为 0，不断轴 | 8.3（缺失日按 0 参与 EWMA） |
| 图中 ACWR 与日报同口径 | 8.1（图与报告必须一致） |
| 慢性为 0 → 不可计算，不伪造 | 8.4 |
| 不可计算活动逐日披露原因 | 4.5 |
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.api.data import SERIES_DEFAULT_DAYS, SERIES_MAX_DAYS, get_load_series
from app.errors import AppError
from app.schemas.domain import TrainingLoad
from app.schemas.enums import ErrorCode
from app.services.report_service import refresh_acwr
from app.storage.derived_store import upsert_load

D = dt.date(2026, 10, 14)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_loads(session: Session, end: dt.date, days: int, *, combined: float = 18.0,
                by_sport: dict | None = None, skip: set[dt.date] | None = None) -> None:
    """连续 days 天的负荷；skip 里的日期不写库（模拟无训练日 / 缺数据日）。"""
    for i in range(days):
        day = end - dt.timedelta(days=days - 1 - i)
        if skip and day in skip:
            continue
        upsert_load(
            session,
            TrainingLoad(
                date=day,
                by_sport=by_sport or {"running": 13.0, "strength_training": 5.0},
                combined=combined,
                method="mixed",
            ),
        )
    session.commit()


def _series(session: Session, **kwargs) -> dict:
    return get_load_series(session=session, **kwargs)  # type: ignore[arg-type]


def _expect_400(**kwargs) -> None:
    with pytest.raises(AppError) as exc:
        _series(**kwargs)
    assert exc.value.code == ErrorCode.VALIDATION


# ------------------------------------------------------------
# 默认范围与参数校验
# ------------------------------------------------------------
def test_default_window_is_28_days_ending_today(session):
    """不传参：默认展示 28 天，结束日为今天。"""
    # 直接调用函数时显式传 None（HTTP 路径下由 FastAPI 依赖注入解析默认值）
    out = _series(session, start=None, end=None)
    expect_start = dt.date.today() - dt.timedelta(days=SERIES_DEFAULT_DAYS - 1)
    assert out["start"] == expect_start.isoformat()
    assert out["end"] == dt.date.today().isoformat()
    assert len(out["days"]) == SERIES_DEFAULT_DAYS


def test_span_beyond_max_is_rejected(session):
    """时间跨度超上限 → 400，避免一次拉全历史。"""
    too_long = dt.date.today() - dt.timedelta(days=SERIES_MAX_DAYS + 1)
    _expect_400(session=session, start=too_long, end=dt.date.today())


def test_start_after_end_is_rejected(session):
    """起始日期晚于结束日期 → 400。"""
    _expect_400(session=session, start=D, end=D - dt.timedelta(days=1))


# ------------------------------------------------------------
# 需求 8.3：无负荷日保留为 0；8.4：慢性 0 不可计算
# ------------------------------------------------------------
def test_rest_days_are_kept_with_zero_load(session):
    """无训练日不断轴：逐日保留，总负荷 0，by_sport 为空。"""
    _seed_loads(session, D, 30, skip={D - dt.timedelta(days=3), D - dt.timedelta(days=2)})
    out = _series(session, start=D - dt.timedelta(days=6), end=D)
    days = {d["date"]: d for d in out["days"]}
    assert len(days) == 7
    for d in (D - dt.timedelta(days=3), D - dt.timedelta(days=2)):
        assert days[d.isoformat()]["total"] == 0.0
        assert days[d.isoformat()]["by_sport"] == {}
    # 有训练日照常出数
    assert days[D.isoformat()]["total"] == pytest.approx(18.0)


def test_acwr_not_computable_without_history(session):
    """无任何负荷历史 → 慢性 EWMA 为 0，ACWR 不伪造（需求 8.4）。"""
    out = _series(session, start=D, end=D + dt.timedelta(days=1))
    for d in out["days"]:
        assert d["acwr"] is None
        assert d["acwr_state"] == "not_available"


def test_acwr_matches_daily_report(session):
    """图中 ACWR 必须与日报口径一致（同一负荷序列、同一预热窗口）。"""
    _seed_loads(session, D, 60)
    out = _series(session, start=D - dt.timedelta(days=2), end=D)
    snap = refresh_acwr(session, D)
    assert snap is not None
    chart = next(d for d in out["days"] if d["date"] == D.isoformat())
    assert chart["acwr"] == pytest.approx(round(snap.ratio, 3))
    assert chart["acwr_state"] == "sweet_spot"


def test_acwr_note_is_exposed_when_insufficient(session):
    """有效负荷日不足（慢性窗口 < 14 天）时口径提示必须随图返回，不隐藏。"""
    _seed_loads(session, D, 10)  # 近 90 天只有 10 天有负荷
    out = _series(session, start=D, end=D)
    assert out["days"][0]["acwr_note"]
    assert "有效负荷日" in out["days"][0]["acwr_note"]


# ------------------------------------------------------------
# 需求 4.5：不可计算活动逐日披露
# ------------------------------------------------------------
def test_not_computable_is_attached_per_day(session):
    """不可计算活动随当日负荷一并返回，附原因，不填 0、不静默丢弃。"""
    upsert_load(
        session,
        TrainingLoad(
            date=D,
            by_sport={},
            combined=0.0,
            method="mixed",
            not_computable=[
                {"activity_id": "x1", "sport": "strength_training",
                 "reason": "缺少组数/次数信息", "computed": False},
            ],
        ),
    )
    session.commit()
    out = _series(session, start=D, end=D)
    day = out["days"][0]
    assert day["total"] == 0.0
    assert len(day["not_computable"]) == 1
    assert day["not_computable"][0]["reason"] == "缺少组数/次数信息"


def test_unverified_sports_roundtrip_per_day(session):
    """需求 6.3：未验证类型状态随落库/读出往返，不因持久化丢失。"""
    upsert_load(
        session,
        TrainingLoad(
            date=D,
            by_sport={"hiking": 33.0},
            combined=33.0,
            method="mixed",
            unverified_sports={"hiking": "insufficient"},
        ),
    )
    session.commit()
    out = _series(session, start=D, end=D)
    day = out["days"][0]
    assert day["unverified_sports"] == {"hiking": "insufficient"}


def test_legacy_row_without_unverified_sports_yields_empty(session):
    """旧行缺 unverified_sports 键 → 空 dict，不报错也不伪造状态。"""
    upsert_load(
        session,
        TrainingLoad(
            date=D,
            by_sport={"running": 18.0},
            combined=18.0,
            method="mixed",
        ),
    )
    session.commit()
    out = _series(session, start=D, end=D)
    assert out["days"][0]["unverified_sports"] == {}


# ------------------------------------------------------------
# 响应辅助信息
# ------------------------------------------------------------
def test_hr_params_and_reference_loads_included(session):
    """响应须带心率五区与参考负荷，供前端图例/说明区复用（需求 5.4 / 4.6）。"""
    out = _series(session, start=D, end=D)
    hr = out["hr_params"]
    assert hr["max_hr"] == 180 and hr["rest_hr"] == 52
    assert hr["method"] == "karvonen" and len(hr["zones"]) == 5
    assert out["reference_loads"]["(default)"] == 130.0
    assert out["reference_loads"]["strength_training"] == 60_000.0


def test_acwr_is_independent_of_query_range(session):
    """同一日期在 28 天 / 90 天查询区间下必须给出相同 ACWR（需求 8.1：图与报告一致）。

    回归：此前 ``warmup_start = 查询起点 − 56``，预热起点随显示范围漂移，
    90 天视图比 28 天视图多预热几十天 → EWMA 递推不同 → 同一天出现两个 ACWR。
    """
    D = dt.date(2026, 10, 2)
    end = D + dt.timedelta(days=24)
    # 造有明显波动的负荷（每 9 天一个尖峰），让 EWMA 依赖预热长度；
    # 200 天历史覆盖 28/90 两种视图的预热区间。
    for i in range(200):
        day = end - dt.timedelta(days=199 - i)
        val = 40.0 if i % 9 == 0 else 6.0
        upsert_load(
            session,
            TrainingLoad(date=day, by_sport={"running": val}, combined=val, method="mixed"),
        )
    session.commit()

    short = _series(session, start=D - dt.timedelta(days=27), end=end)
    long = _series(session, start=D - dt.timedelta(days=89), end=end)
    a_short = next(d for d in short["days"] if d["date"] == D.isoformat())["acwr"]
    a_long = next(d for d in long["days"] if d["date"] == D.isoformat())["acwr"]

    assert a_short is not None and a_long is not None
    assert a_short == a_long, "同一天 ACWR 不得随查询区间变化（预热起点应固定为 D−56）"
    snap = refresh_acwr(session, D)
    assert snap is not None
    assert a_short == pytest.approx(round(snap.ratio, 3))
