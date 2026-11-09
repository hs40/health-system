"""调度器（TA.14 / REQ-PLAN-005 / D17）。

任务编排（Asia/Shanghai）：
- **日任务**（默认 07:30）：Garmin 同步 → 标准化 → 日报 → LLM 分析 → 当日计划
  → （未拦截则）**上传到课程库（不排期）**。
- **周任务**（默认周一 09:00）：生成上周回顾与本周计划草稿（阶段 B）。
- **月任务**（默认每月 1 日 09:00）：生成上月月报（阶段 C）。

设计约束：
- 每步以任务状态机登记（幂等、可重试）；
- 高风险/硬拦截 → **不上传计划**（REQ-RISK-003）；
- 任一步失败不阻断后续日期的调度；
- 下游步骤失败除写入 ``out["steps"]`` 外，还**结构化登记到 Fault 表**，
  供运维台查询（ARCHITECTURE_REVIEW A.4.2）。
"""

from __future__ import annotations

import datetime as dt
import logging
import signal
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import select

from app.adapters.garmin import GarminAdapter
from app.config import get_settings
from app.db.models import DailyReportRow
from app.db.session import init_db, session_scope
from app.domain.fault_classifier import FaultKind
from app.domain.sleep import SLEEP_COMPLETE, should_defer, sleep_state
from app.logging_conf import log_context
from app.schemas.domain import StandardDaily
from app.services.coach_service import analyze_daily, build_today_plan
from app.services.daily_pipeline import generate_report, sync_daily
from app.services.dingtalk_service import push_daily_report
from app.services.fit_backfill import run_backfill
from app.services.plan_service import generate_weekly_plan, upload_session
from app.services.recovery_service import report_fault
from app.services.report_service import analyze_monthly, generate_monthly, generate_weekly
from app.services.tredict_sync_service import refresh_today_activities, run_tredict_sync
from app.storage.standard_store import get_standard

logger = logging.getLogger("hs.scheduler")

_running = True


def _record_step_fault(session, *, step: str, day: dt.date, exc: BaseException) -> None:
    """把一次下游步骤失败登记为一般技术故障（可观测性，不阻断主流程）。

    为什么是 ``generalTech``：步骤失败的后果是「今天少了日报分析 / 没推送 /
    没上传」，属于可重试的技术故障；升级成需要人工确认的重大故障，会让运维台
    每天堆满待确认条目，反而淹没真正的安全/数据可信度告警。

    登记本身失败只记日志——可观测性不能变成新的故障源。
    """
    try:
        report_fault(
            session,
            kind=FaultKind.GENERAL_TECH.value,
            impact_scope=f"{step}@{day.isoformat()}",
            fix_measures=f"{type(exc).__name__}: {exc}",
        )
    except Exception as fault_exc:  # noqa: BLE001 — Fault 写入失败不得中断日任务
        logger.warning("登记故障失败 step=%s day=%s: %s", step, day, fault_exc)



def _sync_daily(session, target, settings, *, refresh=False):
    """薄包装：便于测试替换。"""
    return sync_daily(session, target, settings, refresh=refresh)


def _get_standard(session, target):
    return get_standard(session, target)


def _run_downstream(session, target, settings, out: dict, sync) -> None:
    """生成日报 → LLM 分析 → 当日计划 → 钉钉推送 → 上传课程（不排期）。

    日报生成是下游第一步，必须自带兜底：2026-11-09 恢复日模板写入
    ``hr_zone="Z1"`` 触发 ValueError，此步当时是唯一没有 try/except 的步骤 →
    整条日任务抛穿、当天日报缺失且无任何 Fault 记录（六次调度全部静默失败）。
    此后与其它步骤一致：失败登记 Fault + 不阻断后续步骤。
    """
    report = None
    try:
        report = generate_report(session, target, settings, sync_errors=sync.errors)
        out["steps"]["report"] = "ok"
    except Exception as e:  # noqa: BLE001 — 单步失败不阻断后续
        out["steps"]["report"] = f"failed: {e}"
        _record_step_fault(session, step="report", day=target, exc=e)
    if report is not None and report.risk is not None:
        out["steps"]["risk"] = report.risk.priority.value
        out["steps"]["data_state"] = report.risk.data_state.value

    try:
        analysis = analyze_daily(session, target, settings)
        out["steps"]["analysis"] = "ok"
        out["analysis_summary"] = analysis.get("summary")
    except Exception as e:  # noqa: BLE001
        out["steps"]["analysis"] = f"failed: {e}"
        _record_step_fault(session, step="analysis", day=target, exc=e)

    ts = None
    try:
        ts = build_today_plan(session, target, settings)
        out["steps"]["plan"] = ts.id if ts is not None else "not_generated"
    except Exception as e:  # noqa: BLE001
        ts = None
        out["steps"]["plan"] = f"failed: {e}"
        _record_step_fault(session, step="plan", day=target, exc=e)

    # 日报推送（钉钉）：放在计划生成之后，保证「今日安排」准确；
    # 无计划也推（写「无计划/休息」）；推送失败只记录，绝不阻断主流程。
    try:
        out["steps"]["dingtalk"] = push_daily_report(session, target, settings)
    except Exception as e:  # noqa: BLE001
        out["steps"]["dingtalk"] = f"failed: {e}"
        _record_step_fault(session, step="dingtalk", day=target, exc=e)

    if ts is None:
        return

    # 上传到课程库（默认不排期：每日计划只进课程库，不排到手表日历）
    try:
        result = upload_session(session, ts.id, settings, schedule=False)
        out["steps"]["upload"] = result.status
        out["garmin_workout_id"] = result.garmin_workout_id
    except Exception as e:  # noqa: BLE001
        out["steps"]["upload"] = f"failed: {e}"
        _record_step_fault(session, step="upload", day=target, exc=e)


def _run_daily_core(session, target, settings, now, *, retry: bool) -> dict:
    """日任务核心：同步 → 睡眠门控 →（满足条件才）生成日报/计划/推送。

    - 睡眠未结束（有入睡、无起床）且未到 12:00 → 只同步，下游全部延后；
    - 没有入睡时间（没有睡眠）/ 睡眠完整 / 到 12:00 → 正常跑下游；
    - ``retry=True``（每小时重试）时，当天日报已存在则直接跳过。
    """
    out: dict = {"date": target.isoformat(), "steps": {}}

    if retry:
        exists = session.exec(
            select(DailyReportRow).where(DailyReportRow.date == target)
        ).first()
        if exists is not None:
            # 「日报已存在」≠「数据已完整」：占位睡眠可能已提前生成过日报
            # （2026-10-31 故障：07:30 旧代码按全 null 占位生成日报，之后重试因
            # 日报存在而永久跳过，占位数据再也无法被完整睡眠覆盖）。
            # 只有睡眠已完整、或已过 12:00 兜底线，才真正跳过；否则继续刷新自愈。
            sd0 = _get_standard(session, target) or StandardDaily(date=target)
            st0 = sleep_state(sd0)
            if st0 == SLEEP_COMPLETE or now.hour >= settings.sleep_deadline_hour:
                out["skipped"] = "report_exists"
                return out

    # 重试走「刷新」语义：把 07:30 写下的占位睡眠/健康数据更新为完整记录
    sync = _sync_daily(session, target, settings, refresh=retry)
    out["steps"]["sync"] = sync.status

    sd = _get_standard(session, target) or StandardDaily(date=target)
    state = sleep_state(sd)
    out["steps"]["sleep"] = state
    if should_defer(state, now, deadline_hour=settings.sleep_deadline_hour):
        out["deferred"] = "sleep_ongoing"
        logger.info("睡眠未结束（有入睡、无起床），延后日报/计划/推送：%s", target)
        return out

    _run_downstream(session, target, settings, out, sync)
    return out


def run_daily_job(day: dt.date | None = None, *, now: dt.datetime | None = None) -> dict:
    """日任务（07:30）：同步；若睡眠未结束则延后生成日报/计划/推送。"""
    settings = get_settings()
    target = day or dt.date.today()
    now = now or dt.datetime.now()
    with log_context(task_id="daily", date=target), session_scope() as session:
        logger.info("日任务开始")
        return _run_daily_core(session, target, settings, now, retry=False)


def run_sleep_retry_job(*, now: dt.datetime | None = None) -> dict:
    """睡眠补同步（08:30–11:30 每小时 + 12:00 兜底）。

    当天日报已生成 → 跳过；睡眠仍无起床且未到 12:00 → 继续等下一小时；
    补到起床时间或到 12:00 → 生成日报/计划/推送。
    """
    settings = get_settings()
    target = dt.date.today()
    now = now or dt.datetime.now()
    with log_context(task_id="sleep_retry", date=target), session_scope() as session:
        return _run_daily_core(session, target, settings, now, retry=True)


def run_weekly_job() -> dict:
    """周任务（阶段 B/C）：上周回顾 + 本周计划草稿。"""
    settings = get_settings()
    today = dt.date.today()
    this_monday = today - dt.timedelta(days=today.weekday())
    out: dict = {"week_start": this_monday.isoformat(), "steps": {}}

    with log_context(task_id="weekly", date=this_monday), session_scope() as session:
        try:
            report = generate_weekly(session, this_monday - dt.timedelta(days=7), settings)
            out["steps"]["weekly_review"] = report.week_start.isoformat()
        except Exception as e:  # noqa: BLE001
            out["steps"]["weekly_review"] = f"failed: {e}"

        try:
            plan = generate_weekly_plan(session, this_monday, settings)
            out["steps"]["weekly_plan"] = f"v{plan.version}"
        except Exception as e:  # noqa: BLE001
            out["steps"]["weekly_plan"] = f"failed: {e}"

    return out


def run_tredict_sync_job() -> dict:
    """Tredict 同步任务（每日 23:00，V43）——**容器内直接函数调用**。

    V42 起子进程调独立脚本，但镜像只 ``COPY backend/app``，根本没有 scripts 目录
    → 任务静默失败 10 天。V43 改为调用 ``app/services`` 内的 ``tredict_sync_service``
    （随 app 包进镜像、路径走 settings、可单测、可观测）。

    2026-11-07 根因修复：上传前先刷新「当天」activities 快照。此前 23:00 只读
    07:30 日任务写「当天」+ 次日回补「前一天」的快照，当天 07:30 后完成的训练
    当天不可见、稳定延迟一天。刷新以回调注入服务（不改 build_scheduler 注册拓扑），
    局部容错、不连坐健康上传。

    执行：当天刷新 + 活动上传（含自动补 FIT / notes）+ 健康差集上传 +
    云端覆盖校验 → 写 audit + 失败告警。
    """
    settings = get_settings()

    def _refresher(day: dt.date) -> dict:
        return refresh_today_activities(GarminAdapter(settings), settings, day)

    try:
        with (
            log_context(task_id="tredict_sync", date=dt.date.today()),
            session_scope() as session,
        ):
            return run_tredict_sync(session, settings, today_refresher=_refresher)
    except Exception as e:  # noqa: BLE001 — 兜底，绝不崩调度器进程
        # 日志必须留在上下文块内：``with ... except`` 时 ``__exit__`` 先执行、
        # contextvar 已被 reset，在 except 里打的日志会丢掉 task_id。
        with log_context(task_id="tredict_sync", date=dt.date.today()):
            logger.error("Tredict 同步任务异常: %s", e)
        return {"ok": False, "errors": [f"{type(e).__name__}: {e}"]}


def run_fit_backfill_job() -> dict:
    """逐秒心率明细补齐任务（每日 23:30，v3.4.5）——容器内直接函数调用。

    v3.4.4 之前 fit_jsonl 只能由本机 scripts 生成后人工搬到 NAS，容器镜像
    不含 scripts、scheduler 也无解析 job，本机 daily 停跑后新活动永远缺明细。
    本任务让容器自足；服务层负责只补空 / 幂等 / 失败隔离 / ledger / 告警，
    本函数只做调度包装，绝不把异常抛穿调度器进程。
    """
    settings = get_settings()
    try:
        with log_context(task_id="fit_backfill", date=dt.date.today()):
            return run_backfill(settings)
    except Exception as e:  # noqa: BLE001 — 兜底，绝不崩调度器进程
        with log_context(task_id="fit_backfill", date=dt.date.today()):
            logger.error("逐秒心率补齐任务异常: %s", e)
        return {"ok": False, "errors": [f"{type(e).__name__}: {e}"]}


def run_monthly_job() -> dict:
    """月任务（阶段 C）：上月月报 + 长期目标偏离分析（TC.3）。"""
    settings = get_settings()
    first = dt.date.today().replace(day=1)
    prev_last = first - dt.timedelta(days=1)
    month = f"{prev_last.year:04d}-{prev_last.month:02d}"
    out: dict = {"month": month, "steps": {}}

    with log_context(task_id="monthly", date=month), session_scope() as session:
        try:
            report = generate_monthly(session, month, settings)
            out["steps"]["monthly"] = report.month
        except Exception as e:  # noqa: BLE001
            out["steps"]["monthly"] = f"failed: {e}"
            return out

        try:
            analyze_monthly(session, month, settings)
            out["steps"]["long_term_analysis"] = "ok"
        except Exception as e:  # noqa: BLE001
            out["steps"]["long_term_analysis"] = f"failed: {e}"

    return out


# crontab 的 day_of_week 与 APScheduler 语义相差一天：
#   crontab     : 0/7=周日, 1=周一, 2=周二, ..., 6=周六
#   APScheduler : 0=周一, 1=周二, ..., 6=周日
# ``CronTrigger.from_crontab()`` 不做换算，直接使用会把周一的任务排到周二。
# 2026-10-27 真实故障：``WEEKLY_JOB_CRON=0 9 * * 1`` 实际在周二 09:00 执行。
_CRON_DOW = {
    "0": "sun",
    "1": "mon",
    "2": "tue",
    "3": "wed",
    "4": "thu",
    "5": "fri",
    "6": "sat",
    "7": "sun",
}


def _crontab_trigger(expr: str, timezone: str) -> CronTrigger:
    """按 **crontab 标准**构造触发器，修正 ``day_of_week`` 的语义差异。

    仅支持 5 段表达式与逗号分隔的星期；区间/步长（如 ``1-5``、``*/2``）显式报错，
    避免静默算错——调度错误比崩溃更难发现。

    :raises ValueError: 段数不是 5，或 day_of_week 含不支持的写法
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(
            f"crontab 需要 5 段（分 时 日 月 周），实际 {len(fields)} 段: {expr!r}"
        )
    minute, hour, dom, month, dow = fields
    if dow != "*":
        names: list[str] = []
        for item in dow.split(","):
            item = item.strip()
            if item not in _CRON_DOW:
                raise ValueError(
                    f"不支持的 day_of_week 写法: {item!r}"
                    "（仅支持 *、0-7 与逗号分隔，如 '1,3'）"
                )
            names.append(_CRON_DOW[item])
        dow = ",".join(names)
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=dom,
        month=month,
        day_of_week=dow,
        timezone=timezone,
    )


def build_scheduler() -> BackgroundScheduler:
    settings = get_settings()
    sched = BackgroundScheduler(timezone=settings.sched_tz)
    sched.add_job(
        run_daily_job,
        _crontab_trigger(settings.daily_job_cron, settings.sched_tz),
        id="daily",
        replace_existing=True,
    )
    sched.add_job(
        run_weekly_job,
        _crontab_trigger(settings.weekly_job_cron, settings.sched_tz),
        id="weekly",
        replace_existing=True,
    )
    sched.add_job(
        run_monthly_job,
        _crontab_trigger(settings.monthly_job_cron, settings.sched_tz),
        id="monthly",
        replace_existing=True,
    )
    # 睡眠补同步：08:30–11:30 每小时 + 12:00 兜底（2026-10-30 需求）
    sched.add_job(
        run_sleep_retry_job,
        _crontab_trigger(settings.sleep_retry_cron, settings.sched_tz),
        id="sleep_retry",
        replace_existing=True,
    )
    sched.add_job(
        run_sleep_retry_job,
        _crontab_trigger(settings.sleep_deadline_cron, settings.sched_tz),
        id="sleep_deadline",
        replace_existing=True,
    )
    # Tredict 同步 + 云端校验（2026-11-04 V42）：每日 23:00
    sched.add_job(
        run_tredict_sync_job,
        _crontab_trigger(settings.tredict_sync_cron, settings.sched_tz),
        id="tredict_sync",
        replace_existing=True,
    )
    # 逐秒心率明细补齐（v3.4.5）：23:30，避开 23:00 tredict_sync 的 Garmin 会话
    sched.add_job(
        run_fit_backfill_job,
        _crontab_trigger(settings.fit_backfill_cron, settings.sched_tz),
        id="fit_backfill",
        replace_existing=True,
    )
    return sched


def _stop(_signum: int, _frame: object) -> None:
    global _running
    _running = False
    logger.info("收到停止信号，准备退出")


def main() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    init_db()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    sched = build_scheduler()
    sched.start()
    logger.info(
        "调度器启动 tz=%s daily=%s weekly=%s monthly=%s",
        settings.sched_tz,
        settings.daily_job_cron,
        settings.weekly_job_cron,
        settings.monthly_job_cron,
    )
    try:
        while _running:
            time.sleep(5)
    finally:
        sched.shutdown(wait=False)
        logger.info("调度器已退出")


if __name__ == "__main__":
    main()
