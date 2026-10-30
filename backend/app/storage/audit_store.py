"""审计层（TA.13 / REQ-AUDIT-001）：六类事件双写（DB + JSONL）。

双写理由：
- DB 便于运维台查询/筛选；
- JSONL 便于离线归档与人工核查，且不随数据库损坏而丢失。
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

from sqlmodel import Session

from app.db.models import AuditEventRow
from app.schemas.enums import AuditCategory


def _jsonl_path(audit_dir: Path, day: dt.date) -> Path:
    return audit_dir / f"{day.isoformat()}.jsonl"


def record(
    session: Session,
    category: AuditCategory,
    ref: str = "",
    detail: dict | None = None,
    *,
    audit_dir: Path | None = None,
    at: dt.datetime | None = None,
) -> str:
    """记录一条审计事件，返回事件 id。"""
    now = at or dt.datetime.now()
    event_id = uuid.uuid4().hex
    payload = detail or {}

    session.add(
        AuditEventRow(id=event_id, at=now, category=category.value, ref=ref, detail=payload)
    )
    session.commit()

    if audit_dir is not None:
        p = _jsonl_path(audit_dir, now.date())
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "id": event_id,
                "at": now.isoformat(),
                "category": category.value,
                "ref": ref,
                "detail": payload,
            },
            ensure_ascii=False,
        )
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    return event_id
