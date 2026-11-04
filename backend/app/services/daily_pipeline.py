"""日流水线（TA.14 的同步步 / 阶段 A 端到端最小闭环）。

顺序（REQ-SYNC-001）：Garmin 同步 → 标准化 → 入库 → 质量 → 基线 → 风险 → 日报。
任一步失败不静默：同步结果与错误随返回值上抛，由调用方记入任务状态。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlmodel import Session

from app.adapters.garmin import GarminAdapter, GarminSyncResult
from app.config import Settings
from app.errors import AppError
from app.schemas.domain import DailyReport
from app.schemas.enums import AuditCategory
from app.services.report_service import (
    generate_daily,
    recompute_day_load,
    refresh_acwr,
)
from app.storage.audit_store import record as audit
from app.storage.standard_store import upsert_standard

logger = logging.getLogger("hs.daily")


def _ensure_previous_day_activities(
    session: Session, adapter: GarminAdapter, day: dt.date, settings: Settings
) -> bool:
    """确保 ``day - 1`` 的 activities raw 与 training_load 就绪（幂等，best-effort）。

    两件事缺一不可：

    - 日报「前一日训练回顾」直接读 day-1 的 raw activities
      （``reports/daily_report._yesterday_training``）；
    - ACWR 图/风险判定直接读 day-1 的 ``training_load``。

    07:30 日任务只同步「当天」；若宿主 23:00 的活动补同步未运行，day-1 的 raw 会缺失
    （真实故障 2026-10-27，NAS 缺 ``2026-10-26.json``）。即便 raw 事后补上，若当时没
    生成日报，``training_load`` 仍为空 → ACWR 图该日为 0。故这里同时补齐 raw 与负荷。

    返回是否补算了 day-1 的负荷。
    """
    y = day - dt.timedelta(days=1)
    try:
        raw = adapter.read_raw("activities", y)
    except AppError:
        # 文件损坏：权威快照不在此处覆盖，交给下游按既有错误处理
        return False

    if raw is None:
        result = adapter.sync_day(y, datasets=["activities"])
        if result.errors:
            logger.warning(
                "回补前一日 activities %s 失败: %s", y, result.errors.get("activities")
            )
            return False
        logger.info(
            "回补前一日 activities %s: status=%s written=%d",
            y,
            result.status,
            len(result.written),
        )
        try:
            raw = adapter.read_raw("activities", y)
        except AppError:
            return False

    if not raw:
        return False  # 休息日：无活动即无负荷

    # 有活动但缺负荷 → 补算并刷新 ACWR（ACWR 图直接读 training_load）
    from app.storage.derived_store import load_range

    if load_range(session, y, y):
        return False
    load = recompute_day_load(session, y, settings)
    if load is None:
        return False
    refresh_acwr(session, y)
    return True


def sync_daily(
    session: Session, day: dt.date, settings: Settings, *, refresh: bool = False
) -> GarminSyncResult:
    """同步 + 标准化 + 入库（**不生成日报**）。

    ``refresh=True``：用滚动窗口刷新（占位睡眠/健康数据可被完整记录覆盖），
    供「睡眠未结束」的每小时重试使用；默认 append-only（首次写盘不覆盖）。
    """
    adapter = GarminAdapter(settings)
    if refresh:
        sync, _changed = adapter.refresh_day(day)
    else:
        sync = adapter.sync_day(day)
    # 日报「昨日训练回顾」依赖 day-1 的 raw activities；ACWR 图依赖 day-1 的
    # training_load。07:30 任务只同步当天，这里幂等补齐（含负荷），避免回顾空白
    # 与 ACWR 图缺柱。
    _ensure_previous_day_activities(session, adapter, day, settings)

    audit(
        session,
        AuditCategory.DATA_SOURCE,
        ref=day.isoformat(),
        detail={
            "source": "garmin",
            "status": sync.status,
            "written": sync.written,
            "skipped": sync.skipped,
            "missing": sync.missing,
            "errors": sync.errors,
        },
        audit_dir=settings.audit_dir,
    )

    sd = adapter.standardize(day)
    upsert_standard(session, sd)
    return sync


def generate_report(
    session: Session,
    day: dt.date,
    settings: Settings,
    *,
    sync_errors: dict[str, str] | None = None,
) -> DailyReport:
    """生成并落库日报（供调度器在睡眠完整/到 12:00 时调用）。"""
    return generate_daily(session, day, settings, sync_errors=sync_errors)


def run_daily(
    session: Session, day: dt.date, settings: Settings, *, refresh: bool = False
) -> tuple[GarminSyncResult, DailyReport]:
    """执行单日端到端流水线（手动同步/运维端点使用，**始终生成日报**）。

    ``refresh=True``：先按滚动窗口覆盖 raw 再生成日报。手动「同步数据」必须用它，
    否则 07:30 写下的占位睡眠（全 null）因 append-only 永远不会被完整记录覆盖。
    """
    sync = sync_daily(session, day, settings, refresh=refresh)
    report = generate_report(session, day, settings, sync_errors=sync.errors)
    return sync, report

