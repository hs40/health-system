"""按批次回补 Garmin 数据集（2026-10-21 决策 Q4：全量数据 + 先近 90 天）。

为什么需要它
------------
`pull_garmin_health.py` 只拉「最近 5 天滚动窗口」——它修不了历史缺口，也不会
碰新增数据集（weight / lifestyle_logging / hydration / 训练准备度 …）。
本脚本按「近端优先」的批次计划把缺口补完：**先补近 90 天，其余历史逐日补完**。

写盘语义
--------
走 ``sync_day()`` → ``_write_raw()``：**已存在的原始文件不覆盖**（raw 冻结语义），
只补缺失。因此脚本天然幂等——重复跑不会改写历史证据。

用法
----
    python scripts/backfill_garmin_datasets.py --dry-run              # 只列批次
    python scripts/backfill_garmin_datasets.py                        # 新增数据集，近 90 天
    python scripts/backfill_garmin_datasets.py --datasets weight,hydration
    python scripts/backfill_garmin_datasets.py --days 30 --batch 30   # 只补近 30 天
    python scripts/backfill_garmin_datasets.py --all                  # 全历史（分批）
    python scripts/backfill_garmin_datasets.py --from 2025-07-03 --to 2026-07-23
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.adapters.garmin import DATASETS, GarminAdapter  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.domain.sync_plan import plan_batches  # noqa: E402
from app.schemas.enums import AuditCategory  # noqa: E402
from app.storage.audit_store import record as audit  # noqa: E402
from app.storage.standard_store import upsert_standard  # noqa: E402

# 新增数据集（Q4「个人健康/训练全集」里 2026-10-21 才纳入的那批）
NEW_DATASETS: tuple[str, ...] = (
    "weight",
    "lifestyle_logging",
    "hydration",
    "training_readiness",
    "training_status",
    "max_metrics",
    "fitness_age",
    "endurance_score",
    "hill_score",
    "rhr_daily",
    "steps_data",
    "floors",
    "intensity_minutes",
    "calories_daily",
    "all_day_events",
)


def _parse_days(args: argparse.Namespace) -> tuple[dt.date, dt.date]:
    today = dt.date.today()
    end = dt.date.fromisoformat(args.to) if args.to else today
    if args.start:
        start = dt.date.fromisoformat(args.start)
    elif args.all:
        start = dt.date(2025, 7, 3)  # Garmin 账号数据起点
    else:
        start = end - dt.timedelta(days=args.days - 1)
    return start, end


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按批次回补 Garmin 数据集（近端优先）")
    ap.add_argument("--datasets", help="逗号分隔；缺省 = 新增数据集全集")
    ap.add_argument("--days", type=int, default=90, help="只补最近 N 天（默认 90）")
    ap.add_argument("--from", dest="start", help="起始日 YYYY-MM-DD")
    ap.add_argument("--to", dest="to", help="结束日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--all", action="store_true", help="全历史（2025-07-03 起）")
    ap.add_argument("--batch", type=int, default=30, help="首批之后每批天数（默认 30）")
    ap.add_argument("--dry-run", action="store_true", help="只列批次，不登录")
    ap.add_argument(
        "--standardize-only",
        action="store_true",
        help="只按区间重算 standard_daily（不拉云端）；raw 已补齐后修派生层用",
    )
    ap.add_argument(
        "--no-standardize", action="store_true", help="只写 raw，不重算 standard_daily"
    )
    args = ap.parse_args(argv)

    datasets = (
        tuple(x.strip() for x in args.datasets.split(",") if x.strip())
        if args.datasets
        else NEW_DATASETS
    )
    unknown = [d for d in datasets if d not in DATASETS]
    if unknown:
        print(f"[abort] 未知数据集：{unknown}")
        return 2

    try:
        start, end = _parse_days(args)
        batches = plan_batches(start, end, first_days=args.days, batch_days=args.batch)
    except ValueError as e:
        print(f"[abort] {e}")
        return 2

    total_days = (end - start).days + 1
    print(f"[plan] {start} ~ {end}（{total_days} 天）× {len(datasets)} 数据集")
    print(f"[plan] 批次 {len(batches)} 个（近端优先）：")
    for i, (f, t) in enumerate(batches, 1):
        print(f"  #{i} {f} ~ {t}（{(t - f).days + 1} 天）")
    if args.dry_run:
        print("[dry-run] 未登录、未拉取")
        return 0

    settings = get_settings()
    adapter = GarminAdapter(settings)

    if args.standardize_only:
        # raw 已补齐 → 只重算派生层（standard_daily），不碰云端
        done = failed = 0
        day = start
        while day <= end:
            try:
                sd = adapter.standardize(day)
                with session_scope() as s:
                    upsert_standard(s, sd)
                done += 1
            except Exception as e:  # noqa: BLE001 — 单日失败不中断
                failed += 1
                print(f"  {day}  [FAIL] {type(e).__name__}: {e}")
            day += dt.timedelta(days=1)
        print(f"[summary] standard_daily 重算 {done} 天 / 失败 {failed}")
        return 0 if not failed else 1

    print("[auth] 登录 Garmin …")
    try:
        adapter.client()
    except Exception as e:  # noqa: BLE001 — 鉴权失败是硬失败
        print(f"[abort] 登录失败 {type(e).__name__}: {e}")
        return 2
    print("[auth] OK")

    written = skipped = 0
    errors: list[str] = []
    for bi, (b_from, b_to) in enumerate(batches, 1):
        print(f"[batch {bi}/{len(batches)}] {b_from} ~ {b_to}")
        day = b_from
        while day <= b_to:
            try:
                res = adapter.sync_day(day, datasets=datasets)
            except Exception as e:  # noqa: BLE001 — 单日异常不中断整批
                errors.append(f"{day}: {type(e).__name__}: {e}")
                print(f"  {day}  [FAIL] {type(e).__name__}: {e}")
                day += dt.timedelta(days=1)
                continue
            written += len(res.written)
            skipped += len(res.skipped)
            for ds, msg in res.errors.items():
                errors.append(f"{day}/{ds}: {msg}")
            for ds in res.missing:
                errors.append(f"{day}/{ds}: 云端无数据且非可选")
            # 有写盘 → 同步刷新派生层（standard_daily），否则 weight 只进 raw 不进库
            if res.written and not args.no_standardize:
                try:
                    sd = adapter.standardize(day)
                    with session_scope() as s:
                        upsert_standard(s, sd)
                except Exception as e:  # noqa: BLE001 — 派生失败只记录，不掩盖拉取结果
                    errors.append(f"{day}/standardize: {type(e).__name__}: {e}")
            day += dt.timedelta(days=1)
        print(f"  → 写盘 {written} 次累计；跳过（已存在）{skipped} 次累计；错误 {len(errors)}")

        with session_scope() as s:
            audit(
                s,
                AuditCategory.DATA_SOURCE,
                ref=f"backfill:{b_from}~{b_to}",
                detail={
                    "datasets": list(datasets),
                    "written": written,
                    "skipped": skipped,
                    "errors": errors[-20:],
                },
                audit_dir=settings.audit_dir,
            )

    print(f"[summary] 写盘 {written} / 跳过 {skipped} / 错误 {len(errors)}")
    for e in errors[:20]:
        print(f"  ! {e}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
