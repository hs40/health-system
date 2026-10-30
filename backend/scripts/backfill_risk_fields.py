"""回填 standard_daily 的风险引擎新字段（sleep_score / body_battery_morning）。

背景
----
2026-10-22 风险引擎重设计（Q11）新增两列，历史 477 天的值全部为空。两列的数据
**早已存在于 raw 层**（``dailySummary.bodyBatteryAtWakeTime`` /
``dailySleepDTO.sleepScores.overall.value``），只是从未解析入库。因此无需联网，
只需按 raw 重新标准化并 upsert。

幂等
----
``upsert_standard`` 按日期覆盖，可反复执行；本脚本不写 raw，不触发任何网络请求。

用法
----
    python scripts/backfill_risk_fields.py              # 回填全部日期
    python scripts/backfill_risk_fields.py --dry-run    # 只统计，不写库
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.garmin import GarminAdapter  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402
from app.storage.standard_store import upsert_standard  # noqa: E402


def _existing_dates() -> list[dt.date]:
    """以 raw/daily_summary 的文件名作为日期来源（该数据集历史最全）。"""
    root = Path(get_settings().raw_data_dir) / "garmin" / "daily_summary"
    out: list[dt.date] = []
    for p in sorted(root.glob("*.json")):
        try:
            out.append(dt.date.fromisoformat(p.stem))
        except ValueError:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 sleep_score / body_battery_morning")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--start", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", help="结束日期 YYYY-MM-DD")
    args = ap.parse_args()

    init_db()
    adapter = GarminAdapter(get_settings())
    dates = _existing_dates()
    if args.start:
        lo = dt.date.fromisoformat(args.start)
        dates = [d for d in dates if d >= lo]
    if args.end:
        hi = dt.date.fromisoformat(args.end)
        dates = [d for d in dates if d <= hi]

    written = 0
    sleep_hit = 0
    battery_hit = 0
    for day in dates:
        sd = adapter.standardize(day)
        if sd.sleep_score is not None:
            sleep_hit += 1
        if sd.body_battery_morning is not None:
            battery_hit += 1
        if not args.dry_run:
            with session_scope() as s:
                upsert_standard(s, sd)
            written += 1

    print(f"日期总数: {len(dates)}")
    print(f"sleep_score 有值: {sleep_hit}")
    print(f"body_battery_morning 有值: {battery_hit}")
    print(f"写库: {written}{'（dry-run 未写）' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
