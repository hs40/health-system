"""重算命令行入口（供 ``daily_sync.py`` 的 activity 档调用）。

为什么不直接写脚本：需求 10.1 明确**不增加独立的手动重算入口**，重算逻辑统一在
``app.services.recompute_service``。这里只是把同一套编排暴露给命令行，
参数与 ``POST /api/data/change`` 一致——只登记「最早受影响日」，范围由服务端决定。

用法::

    python -m app.orchestrator.recompute_cli       # 默认：增量同步后，从最后有数据的日期重算
    python -m app.orchestrator.recompute_cli --from 2026-10-01
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from sqlmodel import Session, select

from app.db.models import TrainingLoadRow
from app.db.session import engine
from app.services import recompute_service


def _latest_load_date(session: Session) -> dt.date | None:
    row = session.exec(
        select(TrainingLoadRow).order_by(TrainingLoadRow.date.desc())  # type: ignore[attr-defined]
    ).first()
    return row.date if row else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="重算负荷与 ACWR（复用 recompute_service）")
    ap.add_argument(
        "--from",
        dest="start",
        help="最早受影响日 YYYY-MM-DD（缺省自动取最后有数据的日期）",
    )
    ap.add_argument("--days", type=int, default=7, help="未指定 --from 时，回溯的天数（默认 7）")
    args = ap.parse_args(argv)

    today = dt.date.today()
    with Session(engine) as session:
        if args.start:
            start = dt.date.fromisoformat(args.start)
        else:
            latest = _latest_load_date(session)
            # 滚动窗口内（最近几天）云端可能被晚到的数据覆盖，故默认回看一段
            start = max(latest or today, today - dt.timedelta(days=args.days))

        if start > today:
            start = today
        out = recompute_service.request(session, start)

    status = out.get("status", "running")
    print(
        f"[recompute] {out.get('from')} → {out.get('to')}  days={out.get('days')} "
        f"status={status} report={out.get('daily_report_generated')}"
    )
    if out.get("merged"):
        print(f"[recompute] 已有重算在执行（task={out.get('task_id')}），已合并为待处理")
        return 0
    if out.get("errors"):
        for e in out["errors"][:5]:
            print(f"  [x] {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
