"""回填历史日报 payload 的 ``recovery`` 块（补 body_battery_morning / sleep_score）。

背景
----
2026-10-22 发现前端「身体电量（晨）」实际取的是 ``body_battery_wake`` ——
但它来自 ``bodyBatteryMostRecentValue``（傍晚任务运行时当天已接近耗尽），
近 60 天中位数仅 **5**，根本不是「晨起」值。真正的晨起值是
``body_battery_morning``（``bodyBatteryAtWakeTime``，同窗口中位数 **56**）。

修正后 ``generate_daily`` 的 ``recovery`` 块同时写入两个口径，但历史 477 天的
payload 里只有旧字段，前端读不到 ``body_battery_morning``。本脚本就地补写：

- ``recovery.body_battery_morning`` ← ``standard_daily.body_battery_morning``
- ``recovery.sleep_score``          ← ``standard_daily.sleep_score``
- 保留 ``recovery.body_battery_wake``（残值，供排查；展示层已改名）

幂等
----
按日期覆盖值，可反复执行；不联网、不动 raw、不动 risk_assessment。

用法
----
    python scripts/backfill_recovery_fields.py              # 回填全部
    python scripts/backfill_recovery_fields.py --dry-run    # 只统计
    python scripts/backfill_recovery_fields.py --start 2026-09-01
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import select  # noqa: E402

from app.db.models import DailyReportRow, StandardDailyRow  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="回填日报 recovery 电量/睡眠得分字段")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--start", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", help="结束日期 YYYY-MM-DD")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start) if args.start else None
    end = dt.date.fromisoformat(args.end) if args.end else None

    init_db()
    filled = skipped_nostd = already = missing_std = 0

    with session_scope() as session:
        std_rows = session.exec(select(StandardDailyRow)).all()
        std_by_date = {r.date: r for r in std_rows}

        query = select(DailyReportRow)
        if start is not None:
            query = query.where(DailyReportRow.date >= start)
        if end is not None:
            query = query.where(DailyReportRow.date <= end)
        reports = session.exec(query.order_by(DailyReportRow.date)).all()  # type: ignore[attr-defined]

        for row in reports:
            std = std_by_date.get(row.date)
            if std is None:
                missing_std += 1
                continue
            if std.body_battery_morning is None:
                skipped_nostd += 1
                continue

            payload = dict(row.payload or {})
            recovery = dict(payload.get("recovery") or {})
            if (
                recovery.get("body_battery_morning") == std.body_battery_morning
                and recovery.get("sleep_score") == std.sleep_score
            ):
                already += 1
                continue

            recovery["body_battery_morning"] = std.body_battery_morning
            recovery["sleep_score"] = std.sleep_score
            if "body_battery_wake" not in recovery and std.body_battery_wake is not None:
                recovery["body_battery_wake"] = std.body_battery_wake
            payload["recovery"] = recovery

            if not args.dry_run:
                # SQLModel JSON 列原地修改不触发脏检查，必须构造新 dict 赋值
                row.payload = payload
                session.add(row)
            filled += 1

        if not args.dry_run:
            session.commit()

    print(
        f"{'[dry-run] ' if args.dry_run else ''}"
        f"已回填 {filled} | 已是新值 {already} | "
        f"缺标准层 {missing_std} | 晨起值为空 {skipped_nostd}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
