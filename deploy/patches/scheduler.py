"""调度器（TA.14 / REQ-PLAN-005 / D17）。

任务编排（Asia/Shanghai）：
- **日任务**（默认 07:30）：Garmin 同步 → 标准化 → 日报 → LLM 分析 → 当日计划
  → （未拦截则）**上传到课程库（不排期）**。
- **周任务**（默认周一 09:00）：生成上周回顾与本周计划草稿（阶段 B）。
- **月任务**（默认每月 1 日 09:00）：生成上月月报（阶段 C）。

设计约束：
- 每步以任务状态机登记（幂等、可重试）；
- 高风险/硬拦截 → **不上传计划**（REQ-RISK-003）；
- 任一步失败不阻断后续日期的调度。
"""

from __future__ import annotations

import datetime as dt
import logging
import signal
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.db.session import init_db, session_scope

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("hs.scheduler")

_running = True


def run_daily_job(day: dt.date | None = None) -> dict:
    """日任务：同步 → 标准化 → 日报 → LLM 分析 → 当日计划 → 上传到课程库（不排期）。"""
    from app.services.coach_service import analyze_daily, build_today_plan
    from app.services.daily_pipeline import run_daily
    from app.services.plan_service import upload_session

    settings = get_settings()
    target = day or dt.date.today()
    out: dict = {"date": target.isoformat(), "steps": {}}

    with session_scope() as session:
        sync, report = run_daily(session, target, settings)
        out["steps"]["sync"] = sync.status
        # 风险结论由 generate_daily 必然写入（含 P4，Q15）；此处仅作防御性取值
        if report.risk is not None:
            out["steps"]["risk"] = report.risk.priority.value
            out["steps"]["data_state"] = report.risk.data_state.value

        try:
            analysis = analyze_daily(session, target, settings)
            out["steps"]["analysis"] = "ok"
            out["analysis_summary"] = analysis.get("summary")
        except Exception as e:  # noqa: BLE001
            out["steps"]["analysis"] = f"failed: {e}"

        try:
            ts = build_today_plan(session, target, settings)
            out["steps"]["plan"] = ts.id if ts is not None else "not_generated"
        except Exception as e:  # noqa: BLE001
            ts = None
            out["steps"]["plan"] = f"failed: {e}"

        # 日报推送（钉钉）：放在计划生成之后，保证「今日安排」准确；
        # 无计划也推（写「无计划/休息」）；推送失败只记录，绝不阻断主流程。
        try:
            from app.services.dingtalk_service import push_daily_report

            out["steps"]["dingtalk"] = push_daily_report(session, target, settings)
        except Exception as e:  # noqa: BLE001
            out["steps"]["dingtalk"] = f"failed: {e}"

        if ts is None:
            return out

        # 上传到课程库（默认不排期：每日计划只进课程库，不排到手表日历）
        try:
            result = upload_session(session, ts.id, settings, schedule=False)
            out["steps"]["upload"] = result.status
            out["garmin_workout_id"] = result.garmin_workout_id
        except Exception as e:  # noqa: BLE001
            out["steps"]["upload"] = f"failed: {e}"

    return out


def run_weekly_job() -> dict:
    """周任务（阶段 B/C）：上周回顾 + 本周计划草稿。"""
    from app.services.plan_service import generate_weekly_plan
    from app.services.report_service import generate_weekly

    settings = get_settings()
    today = dt.date.today()
    this_monday = today - dt.timedelta(days=today.weekday())
    out: dict = {"week_start": this_monday.isoformat(), "steps": {}}

    with session_scope() as session:
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


def run_monthly_job() -> dict:
    """月任务（阶段 C）：上月月报 + 长期目标偏离分析（TC.3）。"""
    from app.services.report_service import analyze_monthly, generate_monthly

    settings = get_settings()
    first = dt.date.today().replace(day=1)
    prev_last = first - dt.timedelta(days=1)
    month = f"{prev_last.year:04d}-{prev_last.month:02d}"
    out: dict = {"month": month, "steps": {}}

    with session_scope() as session:
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
