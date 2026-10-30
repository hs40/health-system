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


def run_daily(
    session: Session, day: dt.date, settings: Settings
) -> tuple[GarminSyncResult, DailyReport]:
    """执行单日端到端流水线。"""
    adapter = GarminAdapter(settings)
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

    report = generate_daily(session, day, settings, sync_errors=sync.errors)
    return sync, report
