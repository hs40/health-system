"""计划接口（REQ-PLAN-006 / D17 / D18）。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body, Depends, Header
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import WeeklyPlanRow
from app.db.session import get_session
from app.errors import AppError
from app.schemas.api import (
    PlanEvaluation,
    UploadResult,
    WeeklyPlan,
    WeeklyPlanGenerateRequest,
)
from app.schemas.enums import ErrorCode
from app.services import plan_service, report_service

router = APIRouter(prefix="/plan", tags=["plan"])


@router.get("/weekly/current", response_model=WeeklyPlan)
def get_current_weekly(session: Session = Depends(get_session)) -> WeeklyPlan:
    row = session.exec(
        select(WeeklyPlanRow).order_by(  # type: ignore[attr-defined]
            WeeklyPlanRow.week_start.desc(), WeeklyPlanRow.version.desc()
        )
    ).first()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "当前周计划不存在")
    plan = WeeklyPlan(**row.payload)
    plan.cards = report_service.build_week_cards(session, get_settings(), plan)
    return plan


@router.get("/weekly/{week_start}", response_model=WeeklyPlan)
def get_weekly(week_start: date, session: Session = Depends(get_session)) -> WeeklyPlan:
    """读取指定周的计划（最新版本）。

    响应逐日带 ``cards``（§12.3 卡片标识：日报已调整徽标 + 五项详情），
    读取时现算、不落库（Q10-A）。
    """
    plan = plan_service.latest_weekly_plan(session, week_start)
    if plan is None:
        raise AppError(ErrorCode.NOT_FOUND, f"周计划不存在: {week_start}")
    plan.cards = report_service.build_week_cards(session, get_settings(), plan)
    return plan


@router.put("/weekly/{week_start}", response_model=WeeklyPlan)
def put_weekly(
    week_start: date,
    plan: WeeklyPlan,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
) -> WeeklyPlan:
    """用户编辑周计划（含夜班标记）。"""
    plan.week_start = week_start
    return plan_service.save_weekly_plan(session, plan, "draft")


@router.post("/weekly/{week_start}/generate", response_model=WeeklyPlan)
def generate_weekly(
    week_start: date,
    req: WeeklyPlanGenerateRequest = Body(default_factory=WeeklyPlanGenerateRequest),
    session: Session = Depends(get_session),
) -> WeeklyPlan:
    """生成周计划草稿（D18：夜班日由用户手动告知）。"""
    return plan_service.generate_weekly_plan(
        session, week_start, get_settings(), night_shift_days=req.night_shift_days
    )


@router.post("/weekly/{week_start}/evaluate", response_model=PlanEvaluation)
def evaluate_weekly(
    week_start: date,
    session: Session = Depends(get_session),
) -> PlanEvaluation:
    """提交 AI 评估（findings / suggestions / conflicts / risk_notes）。"""
    return plan_service.evaluate_weekly_plan(session, week_start, get_settings())


@router.post("/weekly/{week_start}/confirm", response_model=WeeklyPlan)
def confirm_weekly(
    week_start: date,
    session: Session = Depends(get_session),
) -> WeeklyPlan:
    """用户确认最终版（evaluated → confirmed）。"""
    return plan_service.confirm_weekly_plan(session, week_start, get_settings())


@router.post("/session/{session_id}/upload", response_model=UploadResult)
def upload_session(
    session_id: str,
    schedule: bool = False,
    session: Session = Depends(get_session),
) -> UploadResult:
    """上传课程到 Garmin 课程库（默认不排期，``schedule=true`` 才排期）+ 幂等/未知处理（TA.11）。"""
    return plan_service.upload_session(session, session_id, get_settings(), schedule=schedule)


@router.post("/session/{session_id}/reconcile", response_model=UploadResult)
def reconcile_session(
    session_id: str,
    session: Session = Depends(get_session),
) -> UploadResult:
    """对 unknown 任务回读 Garmin 真实状态（REQ-TASK-004）。"""
    return plan_service.reconcile_unknown(session, session_id, get_settings())
