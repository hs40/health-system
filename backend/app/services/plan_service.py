"""计划服务 · 周计划生成/评估/确认（TB.1-TB.3 / REQ-PLAN-006）。

原 733 行单体已按职责拆分（ARCHITECTURE_REVIEW A.1.2）：

- ``plan_upload``   —— Garmin 课程上传/排期/回读（``upload_session`` /
  ``scheduled_on`` / ``reconcile_unknown``）；
- ``plan12w_service`` —— 12 周滚动计划（``generate_plan12w`` / ``latest_plan12w``）；
- 本模块 —— 周计划与上述两块的**兼容门面**，保持既有调用方
  ``from app.services.plan_service import ...`` 不变。

安全约束（REQ-RISK-003）：高风险或存在硬性拦截时**不上传计划**。
未知处理（REQ-TASK-004）：上传请求超时 → 状态记 ``unknown``，**不盲目重发**，
须先用 ``reconcile_unknown`` 回读 Garmin 端真实状态再决定。
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlmodel import Session, select

from app.adapters.llm import LlmAdapter
from app.config import Settings
from app.db.models import RiskAssessmentRow, WeeklyPlanRow
from app.domain import equipment
from app.domain.plan_rules import check_plan, has_hard_block
from app.domain.plan_rules import valid_zone as _valid_zone
from app.domain.prescription import complete_steps as _complete_steps
from app.errors import AppError
from app.schemas.api import (
    PlanConflict,
    PlanEvaluation,
    WeeklyPlan,
)
from app.schemas.domain import TrainingSession, WeekPlanDay
from app.schemas.enums import (
    AuditCategory,
    ErrorCode,
    SessionStatus,
    Sport,
)
from app.services.plan_context import baseline_desc, environment_context, strategy_link
from app.storage.audit_store import record as audit
from app.storage.derived_store import all_latest_baselines
from app.storage.standard_store import load_standard_range

logger = logging.getLogger("hs.services.plan")

_SPORTS = {s.value: s for s in Sport}


def _idem(session_id: str) -> str:
    return f"garmin:plan:{session_id}"



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
        # 最近一次评估随计划落库：刷新后仍能看到「已评估 / 被硬阻断」状态。
        # ``evaluation`` 在模型上是 ``PlanEvaluation``，落库前必须显式 dump，
        # 否则 JSON 列会拿到 pydantic 对象直接报 not JSON serializable。
        "evaluation": (
            plan.evaluation.model_dump(mode="json")
            if isinstance(plan.evaluation, PlanEvaluation)
            else plan.evaluation
        ),
    }


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
    base_desc = baseline_desc(baselines)
    equip = equipment.describe(settings.dumbbell_weights_kg, is_pair=settings.dumbbell_is_pair)
    # 季节策略 + 环境硬约束（T2.7 / T2.4）：作为生成上下文注入；
    # 硬性约束（雨雪/<5°C → 室内）**不得被季节策略覆盖**。
    env_ctx = environment_context(session, week_start, settings)
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
    plan.strategy = strategy_link(session, settings, week_start, week_end)
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
    base_desc = baseline_desc(baselines)
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
# 兼容导出：既有调用方从 plan_service 导入，拆分后保持不变
# ============================================================
from app.services.plan12w_service import (  # noqa: E402
    PLAN12W_WEEKS,
    generate_plan12w,
    latest_plan12w,
)
from app.services.plan_upload import (  # noqa: E402
    reconcile_unknown,
    scheduled_on,
    upload_session,
)

__all__ = [
    "PLAN12W_WEEKS",
    "confirm_weekly_plan",
    "evaluate_weekly_plan",
    "generate_plan12w",
    "generate_weekly_plan",
    "latest_plan12w",
    "latest_weekly_plan",
    "reconcile_unknown",
    "save_weekly_plan",
    "scheduled_on",
    "upload_session",
]
