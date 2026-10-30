"""回填 ``risk_assessment`` 表：按新四级优先级规则逐日重算并落库。

背景
----
2026-10-22 风险引擎重设计时，``risk_assessment`` 表按新契约**清空重建**（Q23），
同时 ``daily_report.payload`` 里的 ``risk`` 快照被整体删除（Q29/Q30）。
两者叠加导致历史日期「既无快照、也无评估行」，读取方无从得知当时的判定。

本脚本对指定区间逐日调用 ``report_service.generate_daily``：
标准层与派生层（load / acwr / baseline）均为存量数据，**不联网、不改原始层**；
每日写一条评估记录（含 P4，Q15）与一份新 payload。

幂等
----
``generate_daily`` 按日期 upsert，可反复执行；结果只取决于当日数据与当前规则版本。

用法
----
    python -m scripts.backfill_risk_assessment --dry-run
    python -m scripts.backfill_risk_assessment --start 2026-01-01 --end 2026-10-21
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import DailyReportRow, RiskAssessmentRow
from app.db.session import engine
from app.services import report_service


def _target_dates(session: Session, lo: dt.date, hi: dt.date) -> list[dt.date]:
    rows = session.exec(
        select(DailyReportRow).where(
            DailyReportRow.date >= lo, DailyReportRow.date <= hi
        )
    ).all()
    return sorted(r.date for r in rows)


def backfill(lo: dt.date, hi: dt.date, *, dry_run: bool) -> dict[str, int]:
    """返回统计：目标天数 / 已生成 / 跳过（已有评估行）/ 失败。"""
    stats = {"target": 0, "generated": 0, "skipped": 0, "failed": 0}
    settings = get_settings()
    with Session(engine) as session:
        dates = _target_dates(session, lo, hi)
        stats["target"] = len(dates)
        existing = {
            r.date
            for r in session.exec(
                select(RiskAssessmentRow).where(
                    RiskAssessmentRow.date >= lo, RiskAssessmentRow.date <= hi
                )
            ).all()
        }
        if dry_run:
            stats["skipped"] = len(existing)
            stats["generated"] = len(dates) - len(existing)
            return stats

        for day in dates:
            if day in existing:
                stats["skipped"] += 1
                continue
            try:
                report_service.generate_daily(session, day, settings)
                stats["generated"] += 1
            except Exception as exc:  # noqa: BLE001 — 单日失败不阻断后续
                session.rollback()
                stats["failed"] += 1
                print(f"  [失败] {day}: {exc}")
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回填 risk_assessment（四级优先级）")
    ap.add_argument("--start", help="起始日期 YYYY-MM-DD（默认最早日报日）")
    ap.add_argument("--end", help="结束日期 YYYY-MM-DD（默认昨天）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args(argv)

    with Session(engine) as session:
        first = session.exec(
            select(DailyReportRow).order_by(DailyReportRow.date.asc())  # type: ignore[attr-defined]
        ).first()
    if first is None:
        print("没有日报数据，无需回填")
        return 0

    lo = dt.date.fromisoformat(args.start) if args.start else first.date
    hi = dt.date.fromisoformat(args.end) if args.end else dt.date.today() - dt.timedelta(days=1)
    stats = backfill(lo, hi, dry_run=args.dry_run)
    mode = "试运行" if args.dry_run else "已执行"
    print(
        f"[{mode}] {lo} ~ {hi}：目标 {stats['target']} 天，"
        f"生成 {stats['generated']}，跳过 {stats['skipped']}，失败 {stats['failed']}"
    )
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
