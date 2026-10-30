"""周报（REQ-REPORT-002）+ 月报（REQ-REPORT-003）+ 区间聚合。

周/月共用 ``_aggregate`` / ``_delta``；从原 ``report_service.py`` 拆出（①，2026-10-26），行为不变。
"""

from __future__ import annotations

import datetime as dt
import json

from sqlmodel import Session, select

from app.config import Settings
from app.db.models import (
    DailyReportRow,
    MonthlyReportRow,
    Plan12WeekRow,
    RiskAssessmentRow,
    TrainingSessionRow,
    WeeklyPlanRow,
)
from app.domain.adjustment import (
    DayJudgment,
    build_week_execution,
)
from app.domain.algo_version import algo_versions
from app.domain.report_link import build_strategy_link, strategy_changes_in_window
from app.errors import AppError
from app.schemas.api import MonthlyReport, WeeklyReport
from app.schemas.domain import (
    StandardDaily,
)
from app.schemas.enums import (
    AuditCategory,
    ErrorCode,
)
from app.services.reports.goal_progress import load_goal_progress
from app.services.reports.report_store import _persist_monthly, _persist_weekly
from app.storage.audit_store import record as audit
from app.storage.derived_store import (
    load_range,
)
from app.storage.standard_store import load_standard_range


# ============================================================
# 周报（TB.1 / REQ-REPORT-002）
# ============================================================
def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _aggregate(dailies: list[StandardDaily]) -> dict:
    """区间聚合指标（均值 + 有效天数）；周报 / 月报共用。"""
    def vals(attr: str) -> list[float]:
        return [float(v) for d in dailies if (v := getattr(d, attr, None)) is not None]

    sleep = vals("sleep_duration_sec")
    return {
        "days": len(dailies),
        "resting_heart_rate_avg": _avg(vals("resting_heart_rate")),
        "hrv_avg": _avg(vals("hrv_last_night_avg")),
        "sleep_avg_sec": _avg(sleep),
        "sleep_avg_hours": round(sum(sleep) / len(sleep) / 3600, 1) if sleep else None,
        "steps_total": int(sum(vals("steps"))) if vals("steps") else None,
        "active_calories_total": (
            int(sum(vals("active_calories"))) if vals("active_calories") else None
        ),
        "weight_avg_kg": _avg(vals("weight_kg")),
    }


def _delta(cur: dict, prev: dict, key: str) -> float | None:
    a, b = cur.get(key), prev.get(key)
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 2)


def _sum_by_sport(loads) -> dict[str, float]:
    """把区间内各日 ``by_sport`` 累加（周报/月报共用，消除逐字重复）。"""
    out: dict[str, float] = {}
    for x in loads:
        for s, v in x.by_sport.items():
            out[s] = round(out.get(s, 0.0) + v, 1)
    return out


def _priority_counts(priorities: list[str]) -> dict:
    """P1–P4 天数计数（缺失 → None 而非 0；周报/月报共用）。"""
    return {
        "rest_days": priorities.count("P1") if priorities else None,
        "reduce_days": priorities.count("P2") if priorities else None,
        "progress_days": priorities.count("P3") if priorities else None,
        "maintain_days": priorities.count("P4") if priorities else None,
        "assessed_days": len(priorities),
    }


def _week_execution_from_dailies(
    session: Session, week_start: dt.date, week_end: dt.date
) -> dict:
    """周计划执行块（§12.4；2026-10-20 决策 Q7）：从一周日报提取聚合。

    取数窗口：D 日的完成度落在 **D+1** 的日报里 → 本周 [week_start, week_end]
    的完成度来自 [week_start+1, week_end+1] 的 7 份日报（周一 09:00 生成上周
    周报时，最后一份日报当天 07:30 已生成，无滞后）。

    缺日报的日子进 ``missing_days`` **披露而非伪造**——判定只在日报发生一次，
    周报不重算（避免两处实现漂移）。
    """
    judgments: list[DayJudgment] = []
    missing: list[str] = []
    day = week_start
    while day <= week_end:
        row = session.exec(
            select(DailyReportRow).where(DailyReportRow.date == day + dt.timedelta(days=1))
        ).first()
        comp = (row.payload or {}).get("yesterday_completion") if row else None
        if comp is None:
            # 两种情况都算「无判定」：缺日报，或当日报记载了不判定（无计划无记录）
            if row is None:
                missing.append(day.isoformat())
        else:
            judgments.append(DayJudgment.from_dict(comp))
        day += dt.timedelta(days=1)

    out = build_week_execution(judgments)
    out["missing_days"] = missing
    return out


def generate_weekly(
    session: Session, week_start: dt.date, settings: Settings
) -> WeeklyReport:
    """生成周报：计划 vs 执行、负荷、恢复、目标进展、偏差原因（D17 周起始=周一）。"""
    if week_start.weekday() != 0:
        raise AppError(ErrorCode.VALIDATION, "week_start 必须是周一（D17）")
    week_end = week_start + dt.timedelta(days=6)
    prev_start = week_start - dt.timedelta(days=7)

    cur_dailies = load_standard_range(session, week_start, week_end)
    prev_dailies = load_standard_range(session, prev_start, week_start - dt.timedelta(days=1))

    cur = _aggregate(cur_dailies)
    prev = _aggregate(prev_dailies)

    # 计划 vs 执行
    plan_row = session.exec(
        select(WeeklyPlanRow)
        .where(WeeklyPlanRow.week_start == week_start)
        .order_by(WeeklyPlanRow.version.desc())  # type: ignore[attr-defined]
    ).first()
    planned_days = (plan_row.payload or {}).get("days", []) if plan_row else []
    planned_sessions = [d for d in planned_days if d.get("session")]
    night_shifts = [d["date"] for d in planned_days if d.get("is_night_shift")]
    session_rows = session.exec(
        select(TrainingSessionRow).where(
            TrainingSessionRow.date >= week_start, TrainingSessionRow.date <= week_end
        )
    ).all()
    by_status: dict[str, int] = {}
    for r in session_rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1

    plan_vs_actual = {
        "planned_sessions": len(planned_sessions),
        "created_sessions": len(session_rows),
        "uploaded_sessions": by_status.get("uploaded", 0),
        "confirmed_sessions": by_status.get("confirmed", 0),
        "status_breakdown": by_status,
        "night_shift_days": night_shifts,
    }

    loads = load_range(session, week_start, week_end)
    total_load = round(sum(x.combined for x in loads), 1)
    by_sport = _sum_by_sport(loads)

    # Q14b：聚合改读 risk_assessment 新表（priority / data_state）。
    # 只统计「有评估记录」的日期；缺失日期返回 None 而不是 0（不把缺失伪装成安全）。
    week_rows = session.exec(
        select(RiskAssessmentRow).where(
            RiskAssessmentRow.date >= week_start, RiskAssessmentRow.date <= week_end
        )
    ).all()
    week_priorities = [r.priority for r in week_rows]
    week_states = [r.data_state for r in week_rows]

    report = WeeklyReport(
        week_start=week_start,
        review={
            "days_with_data": cur["days"],
            "priorities": week_priorities,
            "data_states": week_states,
            # P1 = 强制休息天数（原 high_risk_days 的语义承接者）
            **_priority_counts(week_priorities),
        },
        plan_vs_actual=plan_vs_actual,
        # §12.4 执行块：从一周 7 份日报提取聚合（判定只在日报发生一次）
        execution=_week_execution_from_dailies(session, week_start, week_end),
        load={
            "total_combined": total_load,
            "by_sport": by_sport,
            "days_with_load": len(loads),
        },
        recovery={
            "resting_heart_rate_avg": cur["resting_heart_rate_avg"],
            "hrv_avg": cur["hrv_avg"],
            "sleep_avg_hours": cur["sleep_avg_hours"],
            "delta_vs_prev_week": {
                "resting_heart_rate_avg": _delta(cur, prev, "resting_heart_rate_avg"),
                "hrv_avg": _delta(cur, prev, "hrv_avg"),
                "sleep_avg_sec": _delta(cur, prev, "sleep_avg_sec"),
            },
        },
        goal_progress={
            "steps_total": cur["steps_total"],
            "active_calories_total": cur["active_calories_total"],
            "steps_delta_vs_prev_week": _delta(cur, prev, "steps_total"),
            # 四项长期目标（结构化，日报页/周报页共用同一口径）
            "goals": load_goal_progress(session, week_start, week_end),
        },
        deviation_reason=None,
        # G-16：记录本周聚合使用的口径版本（旧 payload 无该键 = legacy）
        algo_versions=algo_versions(),
    )

    # 报告与策略版本关联（T4.1，§11.3）：记录生成时有效策略版本 + 本周策略变化。
    report.strategy = build_strategy_link(
        session,
        report_generated_at=dt.datetime.now(),
        data_range=(week_start, week_end),
        rule_version=settings.risk_rule_version,
        model_version=getattr(settings, "llm_model_reasoning", "") or "",
        strategy_type=settings.strategy_type,
    )
    report.strategy_changes = strategy_changes_in_window(
        session, start=week_start, end=week_end, strategy_type=settings.strategy_type
    )

    _persist_weekly(session, report, settings)
    return report


# ============================================================
# 月报（TC.1 / TC.3 / REQ-REPORT-003 / REQ-PLAN-007）
# ============================================================
def _month_bounds(month: str) -> tuple[dt.date, dt.date]:
    """``YYYY-MM`` → (首日, 末日)。"""
    try:
        year, mon = (int(x) for x in month.split("-"))
        start = dt.date(year, mon, 1)
    except (ValueError, TypeError) as e:  # pragma: no cover - 由 API 层正则先挡
        raise AppError(ErrorCode.VALIDATION, f"month 格式应为 YYYY-MM: {month}") from e
    nxt = dt.date(year + 1, 1, 1) if mon == 12 else dt.date(year, mon + 1, 1)
    return start, nxt - dt.timedelta(days=1)


def _prev_month(month: str) -> str:
    start, _ = _month_bounds(month)
    prev_last = start - dt.timedelta(days=1)
    return f"{prev_last.year:04d}-{prev_last.month:02d}"


def _adherence(session: Session, start: dt.date, end: dt.date) -> dict:
    """依从性：月内各周「已确认/已上传/已完成」课程数 ÷ 计划课程数。"""
    plan_rows = session.exec(
        select(WeeklyPlanRow).where(
            WeeklyPlanRow.week_start >= start - dt.timedelta(days=6),
            WeeklyPlanRow.week_start <= end,
        )
    ).all()
    # 同一周取最新版本
    latest: dict[dt.date, WeeklyPlanRow] = {}
    for r in plan_rows:
        cur = latest.get(r.week_start)
        if cur is None or r.version > cur.version:
            latest[r.week_start] = r

    planned = 0
    for r in latest.values():
        for d in (r.payload or {}).get("days", []):
            if d.get("session"):
                planned += 1

    rows = session.exec(
        select(TrainingSessionRow).where(
            TrainingSessionRow.date >= start, TrainingSessionRow.date <= end
        )
    ).all()
    done_status = {"confirmed", "uploaded", "completed"}
    done = sum(1 for r in rows if r.status in done_status)
    created = len(rows)
    rate = round(done / planned, 3) if planned else None
    return {
        "planned_sessions": planned,
        "created_sessions": created,
        "completed_sessions": done,
        "adherence_rate": rate,
        "weeks_covered": len(latest),
    }


def generate_monthly(session: Session, month: str, settings: Settings) -> MonthlyReport:
    """生成月报：训练 / 恢复 / 依从性 / 阶段目标进展（REQ-REPORT-003）。"""
    start, end = _month_bounds(month)
    p_month = _prev_month(month)
    p_start, p_end = _month_bounds(p_month)

    cur = _aggregate(load_standard_range(session, start, end))
    prev = _aggregate(load_standard_range(session, p_start, p_end))

    loads = load_range(session, start, end)
    total_load = round(sum(x.combined for x in loads), 1)
    by_sport = _sum_by_sport(loads)

    # Q14b：同周报口径，改读新表并按「有评估记录」聚合（缺失 → None 而非 0）
    month_rows = session.exec(
        select(RiskAssessmentRow).where(
            RiskAssessmentRow.date >= start, RiskAssessmentRow.date <= end
        )
    ).all()
    priorities = [r.priority for r in month_rows]
    data_states = [r.data_state for r in month_rows]

    adherence = _adherence(session, start, end)
    report = MonthlyReport(
        month=month,
        review={
            "days_with_data": cur["days"],
            "load_total": total_load,
            "load_by_sport": by_sport,
            "priorities": priorities,
            "data_states": data_states,
            **_priority_counts(priorities),
        },
        adherence=adherence["adherence_rate"],
        goal_progress={
            "adherence": adherence,
            "steps_total": cur["steps_total"],
            "active_calories_total": cur["active_calories_total"],
            "weight_avg_kg": cur["weight_avg_kg"],
            "delta_vs_prev_month": {
                "steps_total": _delta(cur, prev, "steps_total"),
                "resting_heart_rate_avg": _delta(cur, prev, "resting_heart_rate_avg"),
                "hrv_avg": _delta(cur, prev, "hrv_avg"),
                "sleep_avg_sec": _delta(cur, prev, "sleep_avg_sec"),
                "weight_avg_kg": _delta(cur, prev, "weight_avg_kg"),
            },
            # 四项长期目标（基线取上一月）
            "goals": load_goal_progress(session, start, end, prev_start=p_start),
        },
        long_term_adjustment=None,
        # G-16：记录本月聚合使用的口径版本（旧 payload 无该键 = legacy）
        algo_versions=algo_versions(),
    )

    # 报告与策略版本关联（T4.1，§11.4）：生成时有效策略版本 + 本月策略变化。
    report.strategy = build_strategy_link(
        session,
        report_generated_at=dt.datetime.now(),
        data_range=(start, end),
        rule_version=settings.risk_rule_version,
        model_version=getattr(settings, "llm_model_reasoning", "") or "",
        strategy_type=settings.strategy_type,
    )
    report.strategy_changes = strategy_changes_in_window(
        session, start=start, end=end, strategy_type=settings.strategy_type
    )

    _persist_monthly(session, report, settings)
    return report


MONTHLY_SYSTEM = (
    "你是一名长期训练规划审阅助手。依据给定月份的聚合指标与依从性，"
    "评估阶段目标是否偏离，并给出纠正建议（调整目标/节奏/恢复优先级）。"
    "不给出医学诊断。输出必须是 JSON 对象。"
)


def analyze_monthly(
    session: Session, month: str, settings: Settings, *, model: str | None = None
) -> MonthlyReport:
    """TC.3：长期目标偏离分析与纠正建议，写回 ``long_term_adjustment``。"""
    from app.adapters.llm import DEFAULT_SCOPE, LlmAdapter

    row = session.exec(
        select(MonthlyReportRow).where(MonthlyReportRow.month == month)
    ).first()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"月报不存在，无法分析: {month}")
    report = MonthlyReport(**row.payload)

    plan_row = session.exec(
        select(Plan12WeekRow).order_by(Plan12WeekRow.start_date.desc())  # type: ignore[attr-defined]
    ).first()

    monthly_json = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    plan_json = json.dumps(plan_row.payload if plan_row else None, ensure_ascii=False)
    llm = LlmAdapter(settings)
    messages = [
        {"role": "system", "content": MONTHLY_SYSTEM},
        {
            "role": "user",
            "content": (
                f"月报聚合指标（JSON）：{monthly_json}\n"
                f"当前 12 周计划（JSON）：{plan_json}\n"
                "请输出 JSON，字段：deviation(string，目标偏离判断)、"
                "corrections(string[]，纠正建议)、adjustment(string，整体长期调整结论)。"
            ),
        },
    ]
    data = llm.chat_json(
        messages, model=model, required_keys=("deviation", "corrections", "adjustment")
    )

    report.long_term_adjustment = str(data.get("adjustment") or "")
    row.payload = report.model_dump(mode="json")
    session.add(row)
    session.commit()

    audit(
        session,
        AuditCategory.MODEL_CHANGE,
        ref=f"monthly:{month}",
        detail={
            "purpose": "monthly_long_term_analysis",
            "scope": DEFAULT_SCOPE,
            "model": model or settings.llm_model_reasoning,
            "result": "ok",
            "output": data,
        },
        audit_dir=settings.audit_dir,
    )
    return report
