"""环境接口（阶段② T2.2；Plan §4.3.3；REQ §13.7/§13.8）。"""

from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.config import Settings, get_settings
from app.db.session import get_session
from app.errors import AppError
from app.schemas.api import (
    EnvironmentResponse,
    LocationsResponse,
    LocationsUpdateRequest,
)
from app.schemas.enums import ErrorCode
from app.services.environment_service import (
    fetch_environment,
    get_environment_cached,
    list_locations,
    set_locations,
)

router = APIRouter(prefix="/environment", tags=["environment"])


@router.get("/daily", response_model=EnvironmentResponse)
def daily(
    date: str = Query(..., description="目标日期 YYYY-MM-DD"),
    location: str | None = Query(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> EnvironmentResponse:
    """取指定日期的环境数据（优先读缓存；缺失则拉取）。"""
    day = _parse_date(date)
    env = get_environment_cached(session, day, location)
    if env is None:
        env = fetch_environment(session, day, settings, location=location)
    return EnvironmentResponse(**env.model_dump())


@router.post("/refresh", response_model=EnvironmentResponse)
def refresh(
    payload: dict | None = None,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> EnvironmentResponse:
    """强制刷新环境数据（``{date?, location?}``）。"""
    payload = payload or {}
    day = _parse_date(payload.get("date")) if payload.get("date") else date_cls.today()
    location = payload.get("location") or None
    env = fetch_environment(session, day, settings, location=location)
    return EnvironmentResponse(**env.model_dump())


@router.get("/locations", response_model=LocationsResponse)
def locations(session: Session = Depends(get_session)) -> LocationsResponse:
    """列出常用训练区域（REQ §13.7）。"""
    return LocationsResponse(locations=list_locations(session))


@router.put("/locations", response_model=LocationsResponse)
def update_locations(
    payload: LocationsUpdateRequest, session: Session = Depends(get_session)
) -> LocationsResponse:
    """用户手动设置常用训练区域（§13.7：AI 建议需用户确认方可新增）。"""
    set_locations(session, payload.locations)
    return LocationsResponse(locations=list_locations(session))


def _parse_date(value: str | None) -> date_cls:
    if not value:
        raise AppError(ErrorCode.VALIDATION, "缺少日期参数 date")
    try:
        return date_cls.fromisoformat(value)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION, f"日期格式错误：{value}") from exc
