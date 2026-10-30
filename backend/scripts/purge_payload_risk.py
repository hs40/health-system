"""清理 ``daily_report.payload`` 中已退役的 ``risk`` 对象（Q29 / Q30）。

背景
----
2026-10-22 风险引擎重设计：旧三级体系（R-001 ~ R-008，normal / needsAdjustment /
high）整体退役，数据结构改为 ``RiskAssessmentRow``（``priority`` + ``data_state``）。
历史 payload 里的 ``risk`` 对象（含 ``level`` / ``hard_blocks`` / ``data_sufficiency``
等旧字段）已成为死数据 —— 读取方全部改读新表，保留它只会造成「两套口径」的错觉。

处理
----
**删除整个 ``risk`` 键**（不是只删 ``level``）：旧对象内所有字段都属于已退役契约，
混用新旧字段会让人误以为旧判定仍生效。

不可逆操作
----------
用户已明确本次不保留 ``data/archive/`` 备份（Q32）。脚本仍提供 ``--dry-run``
以便先核对影响面；正式执行前请确认数据库文件已随备份策略落盘。

用法
----
    python -m scripts.purge_payload_risk --dry-run
    python -m scripts.purge_payload_risk
"""

from __future__ import annotations

import argparse
import sys

from sqlmodel import Session, select

from app.db.models import DailyReportRow
from app.db.session import engine


def purge(*, dry_run: bool) -> tuple[int, int]:
    """返回 (扫描总数, 清理数)。

    注意：SQLModel 对 JSON 列的**原地修改不触发脏检查**（``payload.pop()`` 后
    再赋回同一对象不会写库）。必须构造**新的 dict** 再赋值。
    """
    scanned = 0
    purged = 0
    with Session(engine) as session:
        rows = session.exec(select(DailyReportRow)).all()
        for row in rows:
            scanned += 1
            payload = row.payload
            if not isinstance(payload, dict) or "risk" not in payload:
                continue
            row.payload = {k: v for k, v in payload.items() if k != "risk"}
            purged += 1
            if not dry_run:
                session.add(row)
        if dry_run:
            session.rollback()
        else:
            session.commit()
    return scanned, purged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理 daily_report.payload 中的 risk 对象")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args(argv)

    scanned, purged = purge(dry_run=args.dry_run)
    mode = "试运行" if args.dry_run else "已执行"
    print(f"[{mode}] 扫描 {scanned} 份日报，清理 {purged} 份的 risk 对象")
    return 0


if __name__ == "__main__":
    sys.exit(main())
