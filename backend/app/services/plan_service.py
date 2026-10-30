"""计划服务：Garmin 课程上传/排期/回读（TA.11）+ 周计划生成/评估/确认（TB.1-TB.3）。

安全约束（REQ-RISK-003）：高风险或存在硬性拦截时**不上传计划**。

未知处理（REQ-TASK-004）：上传请求超时 → 状态记 ``unknown``，**不盲目重发**，
须先用 ``reconcile_unknown`` 回读 Garmin 端真实状态再决定。
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlmodel import Session, select

from app.adapters.garmin import GarminAdapter
from app.adapters.llm import LlmAdapter
from app.config import Settings
from app.db.models import Plan12WeekRow, RiskAssessmentRow, TrainingSessionRow, WeeklyPlanRow
from app.domain import equipment
from app.domain.baseline import with_thresholds
from app.domain.dates import week_start as _monday_of
from app.domain.environment_rules import evaluate_environment
from app.domain.plan_rules import check_plan, has_hard_block
from app.domain.plan_rules import valid_zone as _valid_zone
from app.domain.prescription import complete_steps as _complete_steps
from app.domain.prescription import to_garmin_workout
from app.domain.report_link import build_strategy_link
from app.domain.season_policy import build_season_policy
from app.errors import AppError
from app.orchestrator import task_state
from app.schemas.api import (
    Plan12Week,
    PlanConflict,
    PlanEvaluation,
    UploadResult,
    WeeklyPlan,
)
from app.schemas.domain import TaskError, TrainingSession, WeekPlanDay
from app.schemas.enums import (
    AuditCategory,
    ErrorCode,
    SessionStatus,
    Sport,
    TaskStatus,
    TaskType,
)
from app.storage.audit_store import record as audit
from app.storage.derived_store import all_latest_baselines
from app.storage.environment_store import get_environment
from app.storage.standard_store import load_standard_range

logger = logging.getLogger(__name__)

_SPORTS = {s.value: s for s in Sport}


def _idem(session_id: str) -> str:
    return f"garmin:plan:{session_id}"


def upload_session(
    session: Session,
    session_id: str,
    settings: Settings,
    *,
    schedule: bool = False,
) -> UploadResult:
    """上传（或更新）课程；默认**只进课程库、不排期**（需要时可显式传 ``schedule=True``）。"""
    row = session.get(TrainingSessionRow, session_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"训练处方不存在: {session_id}")
    ts = TrainingSession(**row.payload)

    # 风险门禁：高风险不得上传
    if ts.status == SessionStatus.SKIPPED:
        return UploadResult(session_id=session_id, status="skipped", message="该处方已被跳过")

    # E25：恢复日计划（R-003 命中）**仅本地可见，禁止上传** ——
    # 身体状态差时不应在 Garmin 日历/课程库里多出一个待练课程。
    if not ts.upload_allowed:
        return UploadResult(
            session_id=session_id,
            status="blocked",
            message="恢复日计划仅本地可见，不上传 Garmin",
        )

    may, reason = task_state.may_retry(session, _idem(session_id))
    if not may:
        return UploadResult(
            session_id=session_id,
            status="uploaded" if row.garmin_workout_id else "unknown",
            garmin_workout_id=row.garmin_workout_id,
            garmin_scheduled_id=row.garmin_scheduled_id,
            message=reason,
        )

    run = task_state.begin(session, TaskType.PLAN_UPLOAD, _idem(session_id))
    adapter = GarminAdapter(settings)
    payload = to_garmin_workout(ts)

    try:
        if row.garmin_workout_id:
            workout_id = adapter.update_workout(row.garmin_workout_id, payload)
        else:
            workout_id = adapter.upload_workout(payload)
    except AppError as e:
        # 超时/未知 → 记 unknown，禁止盲目重发
        guarded = e.code in (ErrorCode.EXTERNAL_TIMEOUT, ErrorCode.EXTERNAL_UNKNOWN)
        status = TaskStatus.UNKNOWN if guarded else TaskStatus.FAILED
        task_state.finish(
            session, run.id, status, error=TaskError(code=e.code.value, message=e.message)
        )
        audit(
            session,
            AuditCategory.UPLOAD,
            ref=session_id,
            detail={"system": "garmin", "op": "upload_workout", "result": status.value},
            audit_dir=settings.audit_dir,
        )
        return UploadResult(
            session_id=session_id,
            status=status.value,
            message=f"{e.message}（{'需回读核对' if guarded else '可重试'}）",
        )

    row.garmin_workout_id = workout_id
    row.status = SessionStatus.UPLOADED.value

    scheduled_id = row.garmin_scheduled_id
    if schedule:
        try:
            scheduled_id = adapter.schedule_workout(workout_id, ts.date)
            row.garmin_scheduled_id = scheduled_id
        except AppError as e:
            session.add(row)
            session.commit()
            task_state.finish(
                session,
                run.id,
                TaskStatus.PARTIAL,
                error=TaskError(code=e.code.value, message=e.message),
            )
            return UploadResult(
                session_id=session_id,
                status="unknown" if e.code == ErrorCode.EXTERNAL_TIMEOUT else "failed",
                garmin_workout_id=workout_id,
                message=f"课程已上传但排期失败：{e.message}",
            )

    session.add(row)
    session.commit()
    task_state.finish(session, run.id, TaskStatus.SUCCESS)
    audit(
        session,
        AuditCategory.PLAN_VERSION,
        ref=session_id,
        detail={
            "system": "garmin",
            "workout_id": workout_id,
            "scheduled_id": scheduled_id,
            "date": ts.date.isoformat(),
        },
        audit_dir=settings.audit_dir,
    )
    return UploadResult(
        session_id=session_id,
        status="scheduled" if schedule else "uploaded",
        garmin_workout_id=workout_id,
        garmin_scheduled_id=scheduled_id,
    )


def _sched_date(it: dict) -> str:
    """排期条目上的日期：日历端点用 ``date``，``upcoming`` 端点用 ``scheduleDate``。"""
    return str(it.get("date") or it.get("scheduleDate") or it.get("calendarDate") or "")


def _sched_id(it: dict) -> int | None:
    """排期 id：日历端点用 ``id``，``upcoming`` 端点用 ``scheduledWorkoutId``。"""
    raw = it.get("id") or it.get("scheduledWorkoutId")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def scheduled_on(settings: Settings, day: dt.date) -> list[dict]:
    """回读 Garmin 端某日已排期课程（用于未知处理与幂等核对）。

    **两个来源都要查**：日历端点（``/calendar-service``）覆盖正常排期；``upcoming``
    端点覆盖日历看不到的部分（含课程模板已删的孤儿排期，其 ``itemType`` 缺省）。
    """
    adapter = GarminAdapter(settings)
    target = day.isoformat()
    candidates = [
        *adapter.get_scheduled_workouts(day.year, day.month),
        *adapter.upcoming_schedules(),
    ]
    seen: set[int] = set()
    out: list[dict] = []
    for it in candidates:
        item_type = it.get("itemType")
        if item_type is not None and str(item_type) != "workout":
            continue
        if not _sched_date(it).startswith(target):
            continue
        sid = _sched_id(it)
        if sid is not None:
            if sid in seen:
                continue
            seen.add(sid)
        out.append(it)
    return out


def reconcile_unknown(session: Session, session_id: str, settings: Settings) -> UploadResult:
    """对 ``unknown`` 任务回读 Garmin 真实状态并落库（REQ-TASK-004）。"""
    row = session.get(TrainingSessionRow, session_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"训练处方不存在: {session_id}")
    ts = TrainingSession(**row.payload)
    items = scheduled_on(settings, ts.date)
    if items:
        first = items[0]
        row.garmin_workout_id = first.get("workoutId") or row.garmin_workout_id
        row.garmin_scheduled_id = first.get("id") or first.get("scheduledWorkoutId")
        row.status = SessionStatus.UPLOADED.value
        session.add(row)
        session.commit()
        audit(
            session,
            AuditCategory.UPLOAD,
            ref=session_id,
            detail={"op": "reconcile", "result": "found", "item": first},
            audit_dir=settings.audit_dir,
        )
        return UploadResult(
            session_id=session_id,
            status="scheduled",
            garmin_workout_id=row.garmin_workout_id,
            garmin_scheduled_id=row.garmin_scheduled_id,
            message="回读确认：Garmin 端已存在该日课程",
        )
    audit(
        session,
        AuditCategory.UPLOAD,
        ref=session_id,
        detail={"op": "reconcile", "result": "not_found"},
        audit_dir=settings.audit_dir,
    )
    return UploadResult(
        session_id=session_id, status="failed", message="回读确认：Garmin 端无该日课程，可安全重传"
    )


# ============================================================
# 周计划（TB.1/TB.2/TB.3 / REQ-PLAN-006 / D17 / D18）
# ============================================================
WEEK_SYSTEM = (
    "你是一名训练计划助手。请为给定的一周（周一至周日）安排训练，"
    "遵守：夜班日只安排恢复/轻活动或不安排；高风险日不安排强度课；"
    "每周至少 1 个完整休息日；同一天只安排一次主要训练。"
    "不要给出医学诊断。输出必须是 JSON 对象。"
)


def _plan_payload(plan: WeeklyPlan) -> dict:
    return {
        "week_start": plan.week_start.isoformat(),
        "days": [
            {
                "date": d.date.isoformat(),
                "weekday": d.weekday,
                "is_night_shift": d.is_night_shift,
                "session": (d.session.model_dump(mode="json") if d.session else None),
                "note": d.note,
            }
            for d in plan.days
        ],
        "status": plan.status,
        "version": plan.version,
        "strategy": plan.strategy,
        # 最近一次评估随计划落库：刷新后仍能看到「已评估 / 被硬阻断」状态
        "evaluation": plan.evaluation,
    }


def _strategy_link(
    session: Session, settings: Settings, start: dt.date, end: dt.date
) -> dict:
    """计划与策略版本关联（T4.1，§11.1）：记录生成时实际使用的有效策略版本。"""
    return build_strategy_link(
        session,
        report_generated_at=dt.datetime.now(),
        data_range=(start, end),
        rule_version=settings.risk_rule_version,
        model_version=getattr(settings, "llm_model_daily", "") or "",
        strategy_type=settings.strategy_type,
    )


def latest_weekly_plan(session: Session, week_start: dt.date) -> WeeklyPlan | None:
    row = session.exec(
        select(WeeklyPlanRow)
        .where(WeeklyPlanRow.week_start == week_start)
        .order_by(WeeklyPlanRow.version.desc())  # type: ignore[attr-defined]
    ).first()
    return WeeklyPlan(**row.payload) if row else None


def _next_version(session: Session, week_start: dt.date) -> int:
    prev = session.exec(
        select(WeeklyPlanRow)
        .where(WeeklyPlanRow.week_start == week_start)
        .order_by(WeeklyPlanRow.version.desc())  # type: ignore[attr-defined]
    ).first()
    return (prev.version + 1) if prev else 1


def _save_weekly_plan(session: Session, plan: WeeklyPlan, status: str) -> None:
    """落库一份新的周计划版本（版本号由库内自增，保证 (week_start, version) 唯一）。"""
    plan.status = status
    plan.version = _next_version(session, plan.week_start)
    session.add(
        WeeklyPlanRow(
            week_start=plan.week_start,
            payload=_plan_payload(plan),
            status=status,
            version=plan.version,
        )
    )
    session.commit()


def save_weekly_plan(session: Session, plan: WeeklyPlan, status: str = "draft") -> WeeklyPlan:
    """落库用户编辑后的周计划（PUT /weekly/:weekStart）。"""
    if plan.week_start.weekday() != 0:
        raise AppError(ErrorCode.VALIDATION, "week_start 必须是周一（D17）")
    _save_weekly_plan(session, plan, status)
    return plan


def generate_weekly_plan(
    session: Session,
    week_start: dt.date,
    settings: Settings,
    *,
    night_shift_days: list[dt.date] | None = None,
) -> WeeklyPlan:
    """生成周计划草稿（D17 周起始=周一；D18 夜班由用户手动告知）。"""
    if week_start.weekday() != 0:
        raise AppError(ErrorCode.VALIDATION, "week_start 必须是周一（D17）")
    nights = {d.isoformat() for d in (night_shift_days or [])}
    week_end = week_start + dt.timedelta(days=6)

    baselines = all_latest_baselines(session)
    risk_rows = session.exec(
        select(RiskAssessmentRow).where(
            RiskAssessmentRow.date >= week_start, RiskAssessmentRow.date <= week_end
        )
    ).all()
    # Q14b：改读新表的 priority / data_state；无记录时显式标 None（不伪装成正常）
    risk_by_date = {
        r.date.isoformat(): {"priority": r.priority, "data_state": r.data_state}
        for r in risk_rows
    }
    dailies = load_standard_range(session, week_start - dt.timedelta(days=7), week_end)
    recent = [
        {
            "date": d.date.isoformat(),
            "rhr": d.resting_heart_rate,
            "hrv": d.hrv_last_night_avg,
            "sleep_h": round(d.sleep_duration_sec / 3600, 1) if d.sleep_duration_sec else None,
        }
        for d in dailies[-14:]
    ]

    days_desc = []
    for i in range(7):
        ds = (week_start + dt.timedelta(days=i)).isoformat()
        days_desc.append(
            {
                "date": ds,
                "weekday": i,
                "is_night_shift": ds in nights,
                "risk": risk_by_date.get(ds),
            }
        )

    llm = LlmAdapter(settings)
    base_desc = _baseline_desc(baselines)
    equip = equipment.describe(settings.dumbbell_weights_kg, is_pair=settings.dumbbell_is_pair)
    # 季节策略 + 环境硬约束（T2.7 / T2.4）：作为生成上下文注入；
    # 硬性约束（雨雪/<5°C → 室内）**不得被季节策略覆盖**。
    env_ctx = _environment_context(session, week_start, settings)
    ask = (
        "请输出 JSON：{\"days\":[{\"date\":\"YYYY-MM-DD\","
        "\"sport\":\"running|indoorCardio|strength|null\","
        "\"name\":\"...\",\"duration_min\":int,"
        "\"target_hr_zone\":\"Z1|Z2|Z3|Z4|Z5\","
        "\"warmup\":[{\"name\":\"...\",\"duration_sec\":int}],"
        "\"main\":[{\"name\":\"...\",\"duration_sec\":int,\"hr_zone\":1-5}],"
        "\"cooldown\":[{\"name\":\"...\",\"duration_sec\":int}],"
        "\"note\":\"...\"}],"
        "\"rationale\":\"...\"}；"
        "target_hr_zone 为该训练**主阶段**的目标心率区间（Karvonen 心率储备法五区，"
        "热身/冷身不计入；强度日如间歇选 Z4/Z5，恢复日选 Z1/Z2）；"
        "warmup/main/cooldown 为可选处方步骤（秒级），main 的 hr_zone 与 target_hr_zone 一致，"
        "三步时长之和应约等于 duration_min×60；省略时由系统按规则合成"
    )
    messages = [
        {"role": "system", "content": WEEK_SYSTEM},
        {
            "role": "user",
            "content": (
                f"本周日期与状态：{json.dumps(days_desc, ensure_ascii=False)}\n"
                f"近期恢复数据（末 14 天）：{json.dumps(recent, ensure_ascii=False)}\n"
                f"个人基线：{json.dumps(base_desc, ensure_ascii=False)}\n"
                f"可用器材：{equip}（力量日只使用上述器材）\n"
                f"{env_ctx}\n"
                f"{ask}"
            ),
        },
    ]
    data = llm.chat_json(messages, required_keys=("days",))
    by_date = {d.get("date"): d for d in (data.get("days") or [])}

    plan_days: list[WeekPlanDay] = []
    for i in range(7):
        d = week_start + dt.timedelta(days=i)
        ds = d.isoformat()
        spec = by_date.get(ds) or {}
        sport_raw = spec.get("sport")
        ts = None
        if sport_raw in _SPORTS and spec.get("name"):
            ts = _complete_steps(
                TrainingSession(
                    id=f"session-{ds}",
                    date=d,
                    sport=_SPORTS[sport_raw],
                    name=str(spec["name"]),
                    # 目标心率区间（Q8-A）：白名单校验，越界值 → None
                    # （宁可降级为仅时长判定，不放行污染判定基准的脏数据）
                    target_hr_zone=_valid_zone(spec.get("target_hr_zone")),
                    duration_min=int(spec.get("duration_min") or 0) or None,
                    status=SessionStatus.DRAFT,
                    # LLM 可选返回的处方步骤（原样采用，缺失由 _complete_steps 补齐）
                    warmup=list(spec.get("warmup") or []),
                    main=list(spec.get("main") or []),
                    cooldown=list(spec.get("cooldown") or []),
                )
            )
        plan_days.append(
            WeekPlanDay(
                date=d,
                weekday=i,
                session=ts,
                is_night_shift=ds in nights,
                note=spec.get("note"),
            )
        )

    plan = WeeklyPlan(week_start=week_start, days=plan_days, status="draft", version=1)
    plan.strategy = _strategy_link(session, settings, week_start, week_end)
    _save_weekly_plan(session, plan, "draft")
    audit(
        session,
        AuditCategory.PLAN_VERSION,
        ref=f"weekly:{week_start.isoformat()}",
        detail={
            "op": "generate",
            "night_shift_days": sorted(nights),
            "sessions": sum(1 for d in plan_days if d.session),
            "rationale": data.get("rationale"),
        },
        audit_dir=settings.audit_dir,
    )
    return plan


def evaluate_weekly_plan(
    session: Session, week_start: dt.date, settings: Settings
) -> PlanEvaluation:
    """AI 评估周计划：findings / suggestions / conflicts / risk_notes（TB.3）。"""
    plan = latest_weekly_plan(session, week_start)
    if plan is None:
        raise AppError(ErrorCode.NOT_FOUND, f"周计划不存在: {week_start}")

    baselines = all_latest_baselines(session)
    llm = LlmAdapter(settings)
    base_desc = _baseline_desc(baselines)
    ask = (
        "请输出 JSON：{\"findings\":[..],\"suggestions\":[..],"
        "\"conflicts\":[..],\"risk_notes\":[..]}"
    )
    messages = [
        {
            "role": "system",
            "content": (
                "你是训练计划审阅助手。请指出该周计划与恢复数据/基线的冲突、风险与改进建议。"
                "不给出医学诊断。输出必须是 JSON 对象。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"周计划：{json.dumps(_plan_payload(plan), ensure_ascii=False)}\n"
                f"个人基线：{json.dumps(base_desc, ensure_ascii=False)}\n"
                f"{ask}"
            ),
        },
    ]
    data = llm.chat_json(messages, required_keys=("findings", "suggestions"))

    # 确定性安全规则（不依赖 LLM）：结构化冲突 + 硬性阻断判定。
    # 这是「安全边界」的唯一实现——前端据此禁用确认，确认接口再次校验。
    rule_conflicts = check_plan(plan.days)
    llm_conflicts = [
        PlanConflict(
            days=[],
            kind="goal_mismatch",  # LLM 冲突归入提示类，不参与阻断
            severity="warn",
            hard_block=False,
            message=str(x),
            source="ai",
        )
        for x in data.get("conflicts", [])
        if str(x).strip()
    ]
    structured = [PlanConflict(**c.to_dict()) for c in rule_conflicts] + llm_conflicts

    evaluation = PlanEvaluation(
        week_start=week_start,
        findings=[str(x) for x in data.get("findings", [])],
        suggestions=[str(x) for x in data.get("suggestions", [])],
        conflicts=[c.message for c in structured],
        structured_conflicts=structured,
        hard_block=has_hard_block(rule_conflicts),
        risk_notes=[str(x) for x in data.get("risk_notes", [])],
        evaluated_at=dt.datetime.now(),
    )
    plan.evaluation = evaluation.model_dump(mode="json")
    _save_weekly_plan(session, plan, "evaluated")
    audit(
        session,
        AuditCategory.PLAN_VERSION,
        ref=f"weekly:{week_start.isoformat()}",
        detail={"op": "evaluate", "evaluation": evaluation.model_dump(mode="json")},
        audit_dir=settings.audit_dir,
    )
    return evaluation


def confirm_weekly_plan(session: Session, week_start: dt.date, settings: Settings) -> WeeklyPlan:
    """用户确认周计划最终版（TB.3 状态机：evaluated → confirmed）。"""
    plan = latest_weekly_plan(session, week_start)
    if plan is None:
        raise AppError(ErrorCode.NOT_FOUND, f"周计划不存在: {week_start}")
    if plan.status not in ("evaluated", "draft"):
        raise AppError(ErrorCode.VALIDATION, f"当前状态 {plan.status} 不可确认")
    # 硬性安全边界：未通过确定性规则的计划不得确认生效——**不因用户确认而绕过**。
    blocks = [c for c in check_plan(plan.days) if c.hard_block]
    if blocks:
        raise AppError(
            ErrorCode.VALIDATION,
            f"计划存在 {len(blocks)} 项硬性安全阻断，无法保存为最终计划：{blocks[0].message}",
            detail={"hard_blocks": [c.to_dict() for c in blocks]},
        )
    _save_weekly_plan(session, plan, "confirmed")
    audit(
        session,
        AuditCategory.PLAN_VERSION,
        ref=f"weekly:{week_start.isoformat()}",
        detail={"op": "confirm", "version": plan.version},
        audit_dir=settings.audit_dir,
    )
    return plan


# ============================================================
# 滚动 12 周计划（TC.2 / TC.3 / REQ-PLAN-007）
# ============================================================
PLAN12W_WEEKS = 12

# 3 周渐进 + 1 周减量，循环 3 轮；末周（第 12 周）为评估与减量周
_LOAD_FACTORS = [1.00, 1.10, 1.20, 0.80, 1.05, 1.15, 1.25, 0.85, 1.10, 1.20, 1.30, 0.75]
_EVAL_WEEKS = {4, 8, 12}  # 每 4 周评估一次

DEFAULT_GOAL_WEIGHTS: dict[str, float] = {
    "endurance": 0.4,
    "strength": 0.3,
    "recovery": 0.3,
}


def _phase_of(index: int) -> str:
    """第 index 周（0 起）的训练阶段。"""
    if (index + 1) % 4 == 0:
        return "deload"
    if index < 4:
        return "base"
    if index < 8:
        return "build"
    return "peak"


def _baseline_desc(baselines: dict) -> dict:
    """给 LLM 的基线描述：使用**真实判定口径**（E23/E24），而非分位数。

    旧实现只给 ``p10/p90``，但 R-001/R-002/R-004 已不再按分位数判定
    （HRV 用设备基线低区上沿；静息心率/睡眠用全历史 mean±1σ），
    继续给 p10/p90 会让模型按错误的阈值推理。
    """
    out: dict = {}
    for k, b in baselines.items():
        bb = with_thresholds(k, b)
        out[k] = {
            "mean": round(bb.mean, 2),
            "std": round(bb.std, 2),
            "threshold_low": None if bb.threshold_low is None else round(bb.threshold_low, 2),
            "threshold_high": None if bb.threshold_high is None else round(bb.threshold_high, 2),
            "mature": bb.mature,
        }
    return out


def _environment_context(session: Session, week_start: dt.date, settings: Settings) -> str:
    """构造季节策略 + 环境硬约束的生成上下文（T2.4/T2.7）。

    - 季节策略由 ``domain.season_policy`` 纯计算（不预设固定比例，§4.5）；
    - 环境硬约束由 ``domain.environment_rules`` 判定（雨雪/<5°C → 室内，§4.6），
      **优先级高于季节策略**；
    - 环境数据缺失 → 明确标注，不编造。
    """
    policy = build_season_policy(week_start)
    # 环境：取本周首日已落库数据（无则标注缺失）
    env = get_environment(session, week_start)
    if env is not None:
        decision = evaluate_environment(
            temp_c=env.temp_c,
            is_rain=env.is_rain,
            is_snow=env.is_snow,
            weather_available=env.available,
        )
    else:
        decision = evaluate_environment(temp_c=None, weather_available=False)

    hard = "；".join(decision.hard_reasons) or "无"
    advisory = "；".join(decision.advisory_reasons) or "无"
    return (
        "环境与季节上下文（生成时必须遵守）：\n"
        f"- 季节：{policy.season}｜侧重：{policy.focus}\n"
        f"- 季节限制：{'；'.join(policy.limitations) or '无'}\n"
        f"- 长期优先级：{'>'.join(policy.long_term_priority)}\n"
        f"- 【硬性约束】{hard}\n"
        f"- 【建议】{advisory}\n"
        "- 硬性约束优先于季节策略：雨雪或气温<5°C 必须安排室内训练。"
    )


def latest_plan12w(session: Session) -> Plan12Week | None:
    row = session.exec(
        select(Plan12WeekRow).order_by(  # type: ignore[attr-defined]
            Plan12WeekRow.start_date.desc(), Plan12WeekRow.version.desc()
        )
    ).first()
    return Plan12Week(**row.payload) if row else None


def generate_plan12w(
    session: Session,
    settings: Settings,
    *,
    start_date: dt.date | None = None,
    goal_weights: dict[str, float] | None = None,
) -> Plan12Week:
    """生成/滚动更新 12 周计划（每周更新、每 4 周评估，REQ-PLAN-007）。

    起点默认取本周周一（D17）；重复调用即为「滚动更新」。
    """
    start = _monday_of(start_date or dt.date.today())
    weights = goal_weights or dict(DEFAULT_GOAL_WEIGHTS)

    weeks: list[dict] = []
    for i in range(PLAN12W_WEEKS):
        ws = start + dt.timedelta(days=7 * i)
        phase = _phase_of(i)
        # 季节评估（T2.7）：12 周跨越换季，**每一周按其起始日重新评估季节**（§4.5）
        policy = build_season_policy(ws)
        weeks.append(
            {
                "week_index": i + 1,
                "week_start": ws.isoformat(),
                "week_end": (ws + dt.timedelta(days=6)).isoformat(),
                "phase": phase,
                "focus": {
                    "base": "有氧基础与动作模式",
                    "build": "专项负荷渐进",
                    "peak": "强度峰值与专项耐力",
                    "deload": "减量恢复与状态评估",
                }[phase],
                "season": policy.season,
                "season_focus": policy.focus,
                "long_term_priority": list(policy.long_term_priority),
                "target_load_factor": _LOAD_FACTORS[i],
                "evaluation_due": (i + 1) in _EVAL_WEEKS,
            }
        )

    plan = Plan12Week(start_date=start, weeks=weeks, goal_weights=weights)
    plan.strategy = _strategy_link(
        session, settings, start, start + dt.timedelta(days=7 * PLAN12W_WEEKS - 1)
    )
    session.add(
        Plan12WeekRow(start_date=start, payload=plan.model_dump(mode="json"), version=1)
    )
    session.commit()
    audit(
        session,
        AuditCategory.PLAN_VERSION,
        ref=f"plan12w:{start.isoformat()}",
        detail={
            "op": "generate",
            "weeks": PLAN12W_WEEKS,
            "eval_weeks": sorted(_EVAL_WEEKS),
            "goal_weights": weights,
            "seasons": [w["season"] for w in weeks],
        },
        audit_dir=settings.audit_dir,
    )
    return plan
