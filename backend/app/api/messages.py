"""系统消息接口（阶段③ T3.10；Plan §4.3.2 / REQ §10）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.db.session import get_session
from app.errors import AppError
from app.schemas.api import (
    MessageDeleteRequest,
    MessageDeleteResponse,
    MessageListResponse,
)
from app.schemas.domain import Message
from app.schemas.enums import ErrorCode
from app.services import notify_service

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("", response_model=MessageListResponse)
def list_messages(
    unreadOnly: bool = Query(default=False),  # noqa: N803
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> MessageListResponse:
    items, unread = notify_service.list_messages(
        session, unread_only=unreadOnly, category=category, q=q, limit=limit
    )
    return MessageListResponse(items=items, unread_count=unread)


@router.post("/{message_id}/read", response_model=Message)
def mark_read(message_id: str, session: Session = Depends(get_session)) -> Message:
    """标记已读（**不触发任何策略变化**，§10.3）。"""
    try:
        return notify_service.mark_read(session, message_id)
    except KeyError as exc:
        raise AppError(ErrorCode.NOT_FOUND, str(exc)) from exc


@router.post("/read-all")
def mark_all_read(session: Session = Depends(get_session)) -> dict:
    updated = notify_service.mark_all_read(session)
    return {"updated": updated}


@router.post("/{message_id}/resolve", response_model=Message)
def mark_resolved(message_id: str, session: Session = Depends(get_session)) -> Message:
    """标记已解决（与已读分离，§10.3）。"""
    try:
        return notify_service.mark_resolved(session, message_id)
    except KeyError as exc:
        raise AppError(ErrorCode.NOT_FOUND, str(exc)) from exc


@router.delete("", response_model=MessageDeleteResponse)
def delete_messages(
    payload: MessageDeleteRequest, session: Session = Depends(get_session)
) -> MessageDeleteResponse:
    """清理消息（**仅删消息行**；底层审计/故障记录不删，§10.5）。"""
    deleted = notify_service.delete_messages(session, payload.ids)
    return MessageDeleteResponse(deleted=deleted)
