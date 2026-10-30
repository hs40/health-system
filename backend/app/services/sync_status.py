"""同步状态快照 + 校验失败通知（2026-10-21 决策 Q8）。

两件事：

1. **校验失败必须让人知道**：差异 > 0 时写一条消息中心消息（标题+摘要+报告路径），
   同一份报告**只发一次**（幂等，重复跑校验不刷屏）；无差异不写。
2. **运维台状态快照**：各数据集本地覆盖（文件数 / 最新日期）+ Tredict 镜像计数
   + 最近一次云端校验结果，供 ``GET /api/ops/sync-status``。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.schemas.enums import MessageCategory, MessageImportance
from app.services.notify_service import send_message

# 云端校验报告的归属标识（消息中心按它去重）
REF_TYPE = "cloudVerify"


def verification_message(
    session: Session, *, diff_total: int, report_path: str | None
) -> Any | None:
    """校验有差异 → 发一条消息；无差异 / 同一报告已发过 → None。"""
    if diff_total <= 0:
        return None
    ref_id = (report_path or "").strip()
    if ref_id:
        from app.db.models import MessageRow

        exists = session.exec(
            select(MessageRow).where(
                MessageRow.ref_type == REF_TYPE, MessageRow.ref_id == ref_id
            )
        ).first()
        if exists is not None:
            return None
    body = (
        f"差异合计 {diff_total} 处。请打开运维台「数据同步状态」查看，"
        f"并运行 backend/scripts/verify_cloud.py 复核；"
        f"报告：{ref_id or '未落盘'}"
    )
    return send_message(
        session,
        category=MessageCategory.NEED_ACTION,
        title="云端校验失败：本地与 Garmin/Tredict 存在差异",
        body=body,
        ref_type=REF_TYPE,
        ref_id=ref_id,
        link="/ops",
        importance=MessageImportance.HIGH,
    )


def build_sync_status(raw_root: Path) -> dict[str, Any]:
    """运维台状态快照（纯本地读取，不联网）。

    ``datasets[name] = {"files": N, "latest": "YYYY-MM-DD"}``；
    目录名以下划线开头（``_meta`` / ``_superseded`` 等）与无日期文件的目录跳过。
    """
    root = Path(raw_root)
    datasets: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return {"generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "datasets": datasets}
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        files = [p for p in d.glob("*.json") if p.stem[:4].isdigit()]
        if not files:
            continue
        datasets[d.name] = {"files": len(files), "latest": max(p.stem for p in files)}
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "datasets": datasets,
    }


__all__ = ["REF_TYPE", "build_sync_status", "verification_message"]
