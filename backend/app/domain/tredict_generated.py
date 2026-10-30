"""``tredict_generated`` 落库层（Tredict 自有 / 自算数据，Q23=A：只存档 + 可查）。

为什么单独一层
--------------
Tredict 的 ``capacity`` / ``zones`` / ``efforts`` / ``plannedTrainingList`` /
``equipmentList`` 都是**它自己产生的数据**，与「我们上传的」是两类东西。
表结构在 Stage 5 迁移里已经建好（``entity / entity_key / version / content_hash``），
本模块只负责按「内容变则 version+1」写入，不改表结构。

版本语义
--------
同一 ``(entity, entity_key)`` 内容变化时**新增一行**而不是覆盖：
``capacity`` / ``zones`` 本身就是「修订序列」，历史版本有分析价值。
内容未变时只刷新 ``fetched_at``，不产生新行。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from sqlmodel import Session, select

from app.db.models import TredictGeneratedRow

# 已接入的类别（新增类别只需在这里加一行 + 一个拉取函数）
ENTITY_EFFORT = "effort"
ENTITY_CAPACITY = "capacity"
ENTITY_ZONES = "zones"
ENTITY_PLANNED = "planned_workout"
ENTITY_EQUIPMENT = "equipment"

ENTITIES: tuple[str, ...] = (
    ENTITY_EFFORT,
    ENTITY_CAPACITY,
    ENTITY_ZONES,
    ENTITY_PLANNED,
    ENTITY_EQUIPMENT,
)

# 实际**拉取**的类别（2026-10-21 决策 Q4：planned/equipment 账号实测为空，
# 不再每日空拉；若将来在 Tredict 端排了计划，再单列需求接回）。
# ENTITIES 保留全量语义（表里可能有历史行，snapshot/巡检仍可查）。
PULLED_ENTITIES: tuple[str, ...] = (
    ENTITY_EFFORT,
    ENTITY_CAPACITY,
    ENTITY_ZONES,
)


def content_hash(payload: Any) -> str:
    """规范化哈希（键排序、紧凑分隔符），用于判断内容是否变化。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def upsert_generated(
    session: Session,
    entity: str,
    entity_key: str,
    payload: Any,
    *,
    remote_updated_at: dt.datetime | None = None,
) -> str:
    """写入一条；返回 ``"insert"`` / ``"update"`` / ``"same"``。

    - 首次写入 → ``insert``（version=1）
    - 内容变了 → ``update``（version = 现有最大 + 1，**保留旧版本**）
    - 内容没变 → ``same``（只刷新 ``fetched_at``）
    """
    digest = content_hash(payload)
    rows = session.exec(
        select(TredictGeneratedRow).where(
            TredictGeneratedRow.entity == entity,
            TredictGeneratedRow.entity_key == entity_key,
        )
    ).all()
    latest = max(rows, key=lambda r: r.version) if rows else None

    if latest is not None and latest.content_hash == digest:
        latest.fetched_at = dt.datetime.now()
        session.add(latest)
        session.commit()
        return "same"

    session.add(
        TredictGeneratedRow(
            entity=entity,
            entity_key=entity_key,
            version=(latest.version + 1) if latest else 1,
            content_hash=digest,
            remote_updated_at=remote_updated_at,
            payload=payload if isinstance(payload, dict) else {"value": payload},
        )
    )
    session.commit()
    return "update" if latest is not None else "insert"
def snapshot(session: Session) -> dict[str, dict[str, Any]]:
    """各类别现状摘要（供 MD 渲染 / 巡检用）。

    ``{"effort": {"keys": 301, "versions": 305, "latest_fetched_at": "..."}}``
    """
    rows = session.exec(select(TredictGeneratedRow)).all()
    out: dict[str, dict[str, Any]] = {}
    for entity in ENTITIES:
        mine = [r for r in rows if r.entity == entity]
        if not mine:
            out[entity] = {"keys": 0, "versions": 0, "latest_fetched_at": None}
            continue
        out[entity] = {
            "keys": len({r.entity_key for r in mine}),
            "versions": len(mine),
            "latest_fetched_at": max(r.fetched_at for r in mine).isoformat(
                timespec="seconds"
            ),
        }
    return out
