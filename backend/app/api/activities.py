"""运动训练页面接口（三期 §7/§8）。

只做参数校验与编排；业务逻辑在 ``services.training_service``，纯计算在
``domain``，raw 读取在 ``storage``。raw 是唯一事实源，本层不触碰 DB。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from app.config import Settings, get_settings
from app.errors import AppError
from app.schemas.api import CalendarResponse, DetailResponse
from app.schemas.enums import ErrorCode
from app.services.training_service import activity_detail, calendar_month

router = APIRouter(prefix="/activities", tags=["activities"])

_YM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _raw_root(settings: Settings) -> Path:
    return Path(settings.raw_data_dir) / "garmin"


@router.get("/calendar", response_model=CalendarResponse)
def get_calendar(
    ym: str = Query(...),
    type_: str = Query("all", alias="type"),
    settings: Settings = Depends(get_settings),
) -> dict:
    """月历（§7.2）：``ym=YYYY-MM``，``type`` 省略或 ``all`` 表示全部运动类型。"""
    if not _YM.match(ym):
        raise AppError(ErrorCode.VALIDATION, "ym 必须是 YYYY-MM 且月份为 01-12")
    type_filter = None if type_ == "all" else type_
    return calendar_month(_raw_root(settings), ym, type_filter)


@router.get("/detail", response_model=DetailResponse)
def get_detail(
    date: str = Query(...),
    seq: int = Query(1, ge=1),
    settings: Settings = Depends(get_settings),
) -> dict:
    """单次训练详情（§7.4/§7.5/§7.6）：``date=YYYY-MM-DD``，``seq`` 1 基。"""
    if not _DATE.match(date):
        raise AppError(ErrorCode.VALIDATION, "date 必须是 YYYY-MM-DD")
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise AppError(ErrorCode.VALIDATION, f"无效日期: {date}") from None
    return activity_detail(_raw_root(settings), date, seq)
