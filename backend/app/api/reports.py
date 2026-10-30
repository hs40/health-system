"""报告接口（REQ-UI-001）。"""

from __future__ import annotations

import datetime as dt
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import DailyReportRow, MonthlyReportRow, WeeklyReportRow
from app.db.session import get_session
from app.domain.dates import week_start as _week_start_of
from app.errors import AppError
from app.schemas.api import DailyReport, MonthlyReport, Plan12Week, WeeklyReport
from app.schemas.enums import ErrorCode
from app.services.report_service import generate_daily, generate_monthly, generate_weekly

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/daily", response_model=DailyReport)
def get_daily(
    day: date = Query(alias="date"),
    session: Session = Depends(get_session),
) -> DailyReport:
    row = session.exec(select(DailyReportRow).where(DailyReportRow.date == day)).first()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"日报不存在: {day}")
    return DailyReport(**row.payload)


@router.post("/daily/generate", response_model=DailyReport)
def generate_daily_endpoint(
    day: date | None = Query(default=None, alias="date"),
    session: Session = Depends(get_session),
) -> DailyReport:
    """由已入库的标准化数据生成日报（不触发外部同步）。"""
    target = day or dt.date.today()
    return generate_daily(session, target, get_settings())


@router.post("/daily/analyze")
def analyze_daily_endpoint(
    day: date | None = Query(default=None, alias="date"),
    session: Session = Depends(get_session),
) -> dict:
    """对已生成的日报做 LLM 叙述分析（TA.10）。"""
    from app.services.coach_service import analyze_daily

    return analyze_daily(session, day or dt.date.today(), get_settings())


@router.post("/daily/plan")
def build_today_plan_endpoint(
    day: date | None = Query(default=None, alias="date"),
    session: Session = Depends(get_session),
) -> dict:
    """生成当天训练处方；高风险/硬拦截时不生成（REQ-RISK-003）。"""
    from app.services.coach_service import build_today_plan

    ts = build_today_plan(session, day or dt.date.today(), get_settings())
    if ts is None:
        return {"generated": False, "reason": "高风险/数据不足或模型判定不宜训练"}
    return {"generated": True, "session": ts.model_dump(mode="json")}


@router.get("/weekly", response_model=WeeklyReport)
def get_weekly(
    week_start: date | None = Query(default=None),
    session: Session = Depends(get_session),
) -> WeeklyReport:
    """周报：缺省本周；未生成时按需生成（幂等落库）。"""
    target = week_start or _week_start_of(dt.date.today())
    row = session.exec(
        select(WeeklyReportRow).where(WeeklyReportRow.week_start == target)
    ).first()
    if row is not None:
        return WeeklyReport(**row.payload)
    return generate_weekly(session, target, get_settings())


@router.post("/weekly/generate", response_model=WeeklyReport)
def generate_weekly_endpoint(
    week_start: date | None = Query(default=None),
    session: Session = Depends(get_session),
) -> WeeklyReport:
    """强制重新生成指定周的周报（覆盖落库版本）。"""
    target = week_start or _week_start_of(dt.date.today())
    return generate_weekly(session, target, get_settings())


@router.get("/monthly", response_model=MonthlyReport)
def get_monthly(
    month: str = Query(pattern=r"^\d{4}-\d{2}$"),
    session: Session = Depends(get_session),
) -> MonthlyReport:
    """月报：未生成时按需生成（幂等落库）。"""
    row = session.exec(select(MonthlyReportRow).where(MonthlyReportRow.month == month)).first()
    if row is not None:
        return MonthlyReport(**row.payload)
    return generate_monthly(session, month, get_settings())


@router.post("/monthly/generate", response_model=MonthlyReport)
def generate_monthly_endpoint(
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
    session: Session = Depends(get_session),
) -> MonthlyReport:
    """强制重新生成指定月份月报（缺省上月）。"""
    if month is None:
        first = dt.date.today().replace(day=1)
        prev_last = first - dt.timedelta(days=1)
        month = f"{prev_last.year:04d}-{prev_last.month:02d}"
    return generate_monthly(session, month, get_settings())


@router.post("/monthly/analyze", response_model=MonthlyReport)
def analyze_monthly_endpoint(
    month: str = Query(pattern=r"^\d{4}-\d{2}$"),
    session: Session = Depends(get_session),
) -> MonthlyReport:
    """TC.3：LLM 长期目标偏离分析与纠正建议，写回 long_term_adjustment。"""
    from app.services.report_service import analyze_monthly

    return analyze_monthly(session, month, get_settings())


@router.get("/index")
def reports_index(
    kind: str = Query(default="daily", pattern=r"^(daily|weekly|monthly)$"),
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    """报告索引：哪些日期/周/月已有报告（前端日期控件打点用）。

    只返回**已落库**的报告，不触发生成。``start``/``end`` 可选，用于限制范围。
    """
    if kind == "daily":
        rows = session.exec(select(DailyReportRow)).all()
        items = [
            {"key": r.date.isoformat(), "date": r.date.isoformat(), "version": r.version}
            for r in rows
        ]
    elif kind == "weekly":
        rows = session.exec(select(WeeklyReportRow)).all()
        items = [
            {
                "key": r.week_start.isoformat(),
                "date": r.week_start.isoformat(),
                "week_end": (r.week_start + dt.timedelta(days=6)).isoformat(),
                "version": r.version,
            }
            for r in rows
        ]
    else:
        rows = session.exec(select(MonthlyReportRow)).all()
        items = [{"key": r.month, "date": f"{r.month}-01", "version": r.version} for r in rows]
    if start is not None:
        items = [i for i in items if i["date"] >= start.isoformat()]
    if end is not None:
        items = [i for i in items if i["date"] <= end.isoformat()]
    items.sort(key=lambda i: i["date"], reverse=True)
    return {"kind": kind, "items": items, "count": len(items)}


@router.get("/plan12w", response_model=Plan12Week)
def get_plan12w(session: Session = Depends(get_session)) -> Plan12Week:
    """滚动 12 周计划：无记录时按本周周一生成。"""
    from app.services.plan_service import generate_plan12w, latest_plan12w

    plan = latest_plan12w(session)
    if plan is not None:
        return plan
    return generate_plan12w(session, get_settings())


@router.post("/plan12w/generate", response_model=Plan12Week)
def generate_plan12w_endpoint(
    start_date: date | None = Query(default=None),
    session: Session = Depends(get_session),
) -> Plan12Week:
    """生成/滚动更新 12 周计划（缺省本周周一，D17）。"""
    from app.services.plan_service import generate_plan12w

    return generate_plan12w(session, get_settings(), start_date=start_date)


@router.get("/daily/push-enabled")
def daily_push_enabled() -> dict:
    """前端据此决定「推送至钉钉」按钮是否禁用（未配置 webhook → 禁用）。"""
    return {"enabled": bool(get_settings().dingtalk_webhook)}


@router.post("/daily/push")
def daily_push(
    day: date = Query(alias="date"),
    session: Session = Depends(get_session),
) -> dict:
    """手动把指定日期的日报推送到钉钉（显式用户操作，不阻断主流程）。"""
    from app.services.dingtalk_service import push_daily_report

    return push_daily_report(session, day, get_settings())
