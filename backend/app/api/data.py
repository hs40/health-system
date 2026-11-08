"""数据接口（REQ-UI-003）。"""

from __future__ import annotations

from datetime import date
from datetime import timedelta as dt_timedelta

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.db.models import BaselineRow, StandardDailyRow
from app.db.session import get_session
from app.domain.algo_version import algo_versions
from app.domain.baseline import with_thresholds
from app.domain.standard_fields import GARMIN_TO_DB
from app.errors import AppError
from app.schemas.api import (
    Baseline,
    DataChangeRequest,
    LoadSeriesResponse,
    SeriesPoint,
    SeriesResponse,
    StandardDetail,
)
from app.schemas.enums import ErrorCode
from app.storage.standard_store import get_standard

router = APIRouter(prefix="/data", tags=["data"])

# 标准层全部字段（Garmin 原始字段名 → StandardDailyRow 字段）。
# 唯一事实源：``app/domain/standard_fields.py``（② 白名单收敛，2026-10-26）。
# 加一个标准层指标只需改注册表，本映射与 verify 的校验清单自动跟随。
_STANDARD_FIELDS = dict(GARMIN_TO_DB)

# ``/series`` 白名单：仅保留**数值型且趋势图有意义**的字段（时间戳/强度分钟数不进趋势图；
# hrvStatus 为枚举字符串，不能进数值序列）
_SERIES_EXCLUDED = (
    "sleepStartTs",
    "sleepEndTs",
    "intensityModerateMin",
    "intensityVigorousMin",
    "hrvStatus",
)
_METRIC_FIELDS = {k: v for k, v in _STANDARD_FIELDS.items() if k not in _SERIES_EXCLUDED}


@router.get("/series", response_model=SeriesResponse)
def get_series(
    metric: str = Query(),
    from_: date = Query(alias="from"),
    to: date = Query(),
    session: Session = Depends(get_session),
) -> SeriesResponse:
    field = _METRIC_FIELDS.get(metric)
    if field is None:
        raise AppError(ErrorCode.VALIDATION, f"不支持的指标: {metric}")
    rows = session.exec(
        select(StandardDailyRow).where(
            StandardDailyRow.date >= from_, StandardDailyRow.date <= to
        ).order_by(StandardDailyRow.date)  # type: ignore[attr-defined]
    ).all()
    points = [
        SeriesPoint(date=r.date, value=float(getattr(r, field)))
        for r in rows
        if getattr(r, field) is not None
    ]
    return SeriesResponse(metric=metric, points=points)


@router.get("/baseline", response_model=Baseline)
def get_baseline(
    metric: str = Query(),
    session: Session = Depends(get_session),
) -> Baseline:
    row = session.exec(
        select(BaselineRow).where(BaselineRow.metric == metric).order_by(BaselineRow.version.desc())  # type: ignore[attr-defined]
    ).first()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"基线不存在: {metric}")
    return with_thresholds(
        row.metric,
        Baseline(
            metric=row.metric,
            version=row.version,
            window_days=row.window_days,
            mean=row.mean,
            std=row.std,
            p10=row.p10,
            p90=row.p90,
            mature=row.mature,
            computed_at=row.computed_at,
        ),
    )


@router.get("/standard", response_model=StandardDetail)
def get_standard_detail(
    day: date = Query(alias="date"),
    session: Session = Depends(get_session),
) -> StandardDetail:
    """某日「标准层详情」：标准化取值 + 这些取值的**原始文件出处**（REQ-DATA-004）。

    只读；``source_refs`` 指向 ``raw_data_dir`` 下的相对路径（POSIX），不复制原始内容。
    """
    sd = get_standard(session, day)
    if sd is None:
        raise AppError(ErrorCode.NOT_FOUND, f"标准化数据不存在: {day}")
    return StandardDetail(
        date=sd.date,
        metrics={name: getattr(sd, field) for name, field in _STANDARD_FIELDS.items()},
        source_refs=sd.source_refs,
    )


# ============================================================
# 数据变更 → 自动重算（需求 10.1–10.4，开发计划 4.9）
# ============================================================
@router.post("/change")
def register_data_change(
    payload: DataChangeRequest,
    session: Session = Depends(get_session),
) -> dict:
    """登记数据补充/修正，**自动触发**重算（不提供手动重算入口，需求 10.1）。

    校验（不通过 → 400，不触发任何重算）：
    - 受影响日期不能晚于今天（未来日期无数据可重算）；
    - ``kind`` 只能是 ``correction`` / ``supplement``。

    重算范围与合并规则由服务端决定：从最早受影响日算到今天（10.2）；
    已有重算在执行时**合并登记**为待处理，等该任务结束后再算（10.3 / 10.4）。
    """
    from app.services import recompute_service

    if payload.kind not in ("correction", "supplement"):
        raise AppError(ErrorCode.VALIDATION, f"不支持的变更类型: {payload.kind}")
    if payload.earliest_affected_date > date.today():
        raise AppError(ErrorCode.VALIDATION, "受影响日期不能晚于今天")

    return recompute_service.request(session, payload.earliest_affected_date)


@router.get("/recompute/status")
def recompute_status(session: Session = Depends(get_session)) -> dict:
    """当前是否有重算在执行（Q4：执行期间不生成当天日报）。"""
    from app.services import recompute_service

    running = recompute_service.running_task(session)
    return {
        "running": running is not None,
        "task_id": running.id if running else None,
        "started_at": running.started_at.isoformat() if running else None,
        "checkpoint": running.checkpoint if running else None,
    }


# ============================================================
# 图表数据：逐日负荷 + ACWR（需求 4.6 / 8.x，开发计划 4.11）
# ============================================================
# 默认展示 28 天；上限 120 天 —— 图表按天渲染，一次拉全历史既无意义又拖慢响应
SERIES_DEFAULT_DAYS = 28
SERIES_MAX_DAYS = 120
# EWMA 预热窗口：与 refresh_acwr 同口径（慢性窗口 2 倍），保证图中 ACWR 与日报一致
SERIES_WARMUP_DAYS = 56


@router.get("/load-series", response_model=LoadSeriesResponse)
def get_load_series(
    start: date | None = Query(default=None, alias="from", description="起始日期（含）"),
    end: date | None = Query(default=None, alias="to", description="结束日期（含）"),
    session: Session = Depends(get_session),
) -> LoadSeriesResponse:
    """图表数据：逐日堆叠柱（分运动负荷）+ ACWR 折线。

    口径（需求 4.5 / 8.1 / 8.3）：
    - 无训练日 / 无负荷记录日 → 总负荷 0，逐日保留（EWMA 按 0 参与，不断轴）；
    - ACWR 由历史负荷**回算**（与日报 ``refresh_acwr`` 同一预热口径），
      慢性为 0 → ``acwr=null`` + ``acwr_state="not_available"``，不伪造数值；
    - 不可计算活动逐日附在 ``not_computable`` 里（含原因），不填 0、不静默丢弃；
    - 每日负荷取标准化后分值（与生产同一落库值），前端据此堆叠。
    """
    end = end or date.today()
    start = start or end - dt_timedelta(days=SERIES_DEFAULT_DAYS - 1)
    if start > end:
        raise AppError(ErrorCode.VALIDATION, f"起始日期 {start} 晚于结束日期 {end}")
    span = (end - start).days + 1
    if span > SERIES_MAX_DAYS:
        raise AppError(
            ErrorCode.VALIDATION,
            f"时间跨度 {span} 天超过上限 {SERIES_MAX_DAYS} 天，请缩小范围",
        )

    from app.domain.load import acwr_for, hr_mod
    from app.domain.load.standardize import DEFAULT_REFERENCE, REFERENCE_LOAD
    from app.storage.derived_store import load_range

    # 预热：从 start 往前多取 56 天，让 EWMA 有足够历史（缺失日按 0 参与，需求 8.3）
    warmup_start = start - dt_timedelta(days=SERIES_WARMUP_DAYS)
    loads = load_range(session, warmup_start, end)
    totals = {x.date: x.combined for x in loads}
    by_date = {x.date: x for x in loads}
    # 逐日固定 D−56 预热，与日报 refresh_acwr 同一口径：
    # 避免预热起点随「显示范围」漂移，导致同一天在 28/90 天视图出现不同 ACWR。
    states = {
        d: acwr_for(totals, d, lookback_start=d - dt_timedelta(days=SERIES_WARMUP_DAYS))
        for d in (start + dt_timedelta(days=i) for i in range(span))
    }

    days: list[dict] = []
    d = start
    while d <= end:
        row = by_date.get(d)
        st = states.get(d)
        days.append(
            {
                "date": d.isoformat(),
                "total": round(row.combined, 2) if row else 0.0,
                "by_sport": (
                    {k: round(v, 2) for k, v in sorted(row.by_sport.items())}
                    if row
                    else {}
                ),
                "acwr": round(st.ratio, 3) if st and st.ratio is not None else None,
                "acwr_state": st.zone if st else "not_available",
                "acwr_note": (st.note or None) if st else None,
                # 需求 4.5：不可计算活动随负荷一并披露，附原因
                "not_computable": list(row.not_computable) if row else [],
                # 需求 6.3：可比性未验证的运动类型 → 状态（照常计分，只披露）
                "unverified_sports": dict(row.unverified_sports) if row else {},
            }
        )
        d += dt_timedelta(days=1)

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
        # 当前生效的心率参数与五区边界（Karvonen），供图表图例/说明区复用
        "hr_params": hr_mod.params().to_dict(),
        # 显式给出默认 R：有氧类共用一个 R，前端不必猜「未列出的类型」用哪个值
        "reference_loads": {
            **{"(default)": DEFAULT_REFERENCE},
            **dict(sorted(REFERENCE_LOAD.items())),
        },
        # G-16：本图使用的口径版本（acwr 与参考负荷 R 变了要能追溯）
        "algo_versions": algo_versions(),
    }
