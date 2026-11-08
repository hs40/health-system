"""力量活动 notes 生成（v3.4.6 A 层）——从 tredict_sync_service 拆出以守住模块体量预算。

生成前先就地补 FIT 逐组明细（``fit_backfill.ensure_fit_jsonl``）：上传链路
``_ensure_fit`` 已把 ``.fit`` 落盘，这里就地解析（零额外 Garmin 请求），让 notes
走 FIT 逐组分支，而不是「总次数冒充每组次数」的无 FIT 兜底。失败一律隔离，由
notes 兜底 / ``hr_data_state`` 接住，不阻断上传主流程。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import Settings


def _is_strength(type_key: str) -> bool:
    """与旧脚本 ``upload_tredict_activity.py`` 同口径：typeKey 含 ``strength``。"""
    return "strength" in (type_key or "")


def build_strength_activity_notes(
    settings: Settings,
    ref: Any,
    limit: int,
    *,
    fit_fetcher: Callable[[str], bytes | None] | None = None,
) -> str:
    """力量活动 → notes；非力量返回空串。"""
    from app.domain.strength.notes import build_strength_notes, truncate_notes
    from app.services.fit_backfill import ensure_fit_jsonl
    from app.storage.activity_source import load_sets

    if ref is None or not _is_strength(getattr(ref, "activity_type", "")):
        return ""
    payload = getattr(ref, "payload", {}) or {}
    aid = str(getattr(ref, "activity_id", ""))
    root = Path(settings.raw_data_dir) / "garmin"  # 与 garmin_raw_dir 同口径
    ensure_fit_jsonl(root, aid, fetcher=fit_fetcher)
    text = build_strength_notes(
        aid,
        payload.get("summarizedExerciseSets"),
        load_sets(root, aid),
    )
    return truncate_notes(text, limit)
