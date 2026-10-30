"""拉取 Garmin 健康数据（日任务步骤：只搬数据，不生成日报）。

为什么单独成脚本（Q1=A）
------------------------
P8 之前健康数据只有两条入口：前端「同步」按钮（``POST /api/ops/sync`` →
``run_daily()``）与 APScheduler —— 而后者**不随 ``main.py`` 启动**。
于是「活动 FIT 每天在拉、健康 raw 停在某天」可以长期无人察觉：
``sync_tredict.py`` 面对过期的本地 raw 只会「成功地上传 0 条」并 exit 0。

本脚本把「拉健康」从 ``run_daily()`` 里摘出来，只做三件事：

    ``refresh_day()`` → ``standardize()`` → ``upsert_standard()``

**不生成日报、不调 LLM**（那是 ``run_daily()`` / APScheduler 的职责）。

滚动 5 天窗口（REQ-DATA-001 修订，2026-10-19）
--------------------------------------------
为什么不能只拉一次：``daily_summary`` 的 ``totalSteps`` / ``activeKilocalories``
是**当天累加**的。旧策略「首次写入即冻结」导致 10-13 那天本地永远停在 **37 步**，
而云端是 7180 步。

为什么窗口是 **5**（实测）：抽查 2025-08 ~ 2026-09 的历史日期，与云端逐字段完全
一致 —— **历史数据是稳定的**，差异全部落在最近几天。窗口取 5 天，覆盖「当天 +
移动端延迟回填的若干天」的余量；每晚 45 个请求（5 天 × 9 数据集），约 30 秒。

- 最近 5 天可被云端覆盖，覆盖前旧文件存 ``raw/garmin/_superseded/``（可回溯）
- 更早的日期**冻结**，不再覆盖
- **先比后覆盖**：差异先记进报告（这就是「本地 ↔ Garmin 云端」校验），再写盘
- ``--force``：一次性修复用，允许覆盖窗口外的日期（历史残缺快照）

用法
----
    python scripts/pull_garmin_health.py                  # 最近 5 天（默认）
    python scripts/pull_garmin_health.py --days 10
    python scripts/pull_garmin_health.py --since 2026-10-13 --force   # 修复历史残缺
    python scripts/pull_garmin_health.py --dry-run        # 只列计划，不登录
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.adapters.garmin import RAW_REFRESH_WINDOW_DAYS, GarminAdapter  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.schemas.enums import AuditCategory  # noqa: E402
from app.storage.audit_store import record as audit  # noqa: E402
from app.storage.standard_store import upsert_standard  # noqa: E402

AUDIT_DIR = ROOT / "data" / "audit"


def resolve_days(days: int, since: str | None, until: str | None) -> list[dt.date]:
    """解析目标日期区间（升序、含首尾）。``--since`` 优先于 ``--days``。"""
    end = dt.date.fromisoformat(until) if until else dt.date.today()
    start = (
        dt.date.fromisoformat(since)
        if since
        else end - dt.timedelta(days=max(days, 1) - 1)
    )
    if start > end:
        raise ValueError(f"--since {start} 晚于 --until {end}")
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def raw_exists(adapter: GarminAdapter, day: dt.date) -> bool:
    """该日 ``daily_summary`` 原始文件是否存在。"""
    return (adapter.raw_root / "daily_summary" / f"{day.isoformat()}.json").exists()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="拉取 Garmin 健康数据（滚动窗口，不生成日报）")
    ap.add_argument(
        "--days",
        type=int,
        default=RAW_REFRESH_WINDOW_DAYS,
        help=f"最近 N 天（默认 {RAW_REFRESH_WINDOW_DAYS}）",
    )
    ap.add_argument("--since", help="起始日期 YYYY-MM-DD（优先于 --days）")
    ap.add_argument("--until", help="结束日期 YYYY-MM-DD（默认今天）")
    ap.add_argument(
        "--force",
        action="store_true",
        help="允许覆盖窗口外的日期（一次性修复历史残缺快照，旧版仍留痕）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只列计划，不登录、不拉取")
    args = ap.parse_args(argv)

    try:
        days = resolve_days(args.days, args.since, args.until)
    except ValueError as e:
        print(f"[abort] {e}")
        return 2

    settings = get_settings()
    adapter = GarminAdapter(settings)
    window = len(days) if args.force else RAW_REFRESH_WINDOW_DAYS
    cutoff = dt.date.today() - dt.timedelta(days=RAW_REFRESH_WINDOW_DAYS - 1)
    print(f"[plan] {days[0]} ~ {days[-1]}（{len(days)} 天）")
    if args.force:
        print(f"[plan] --force：覆盖窗口放宽到 {window} 天（历史修复模式）")
    else:
        print(f"[plan] 滚动窗口自 {cutoff} 起，更早的日期冻结")

    if args.dry_run:
        for day in days:
            state = "已有" if raw_exists(adapter, day) else "缺失"
            frozen = "" if (args.force or day >= cutoff) else "  [冻结]"
            print(f"  {day}  daily_summary {state}{frozen}")
        print("[dry-run] 未登录、未拉取、未写库")
        return 0

    print("[auth] 登录 Garmin …")
    try:
        adapter.client()
    except Exception as e:  # noqa: BLE001 — 鉴权失败是硬失败，不掩盖
        print(f"[abort] 登录失败 {type(e).__name__}: {e}")
        return 2
    print("[auth] OK")

    written = superseded = frozen = same = no_data = 0
    errors: list[str] = []
    drift: list[dict] = []

    for day in days:
        try:
            result, changed = adapter.refresh_day(day, window_days=window)
        except Exception as e:  # noqa: BLE001 — 单天异常不中断整批
            errors.append(f"{day}: {type(e).__name__}: {e}")
            print(f"  {day}  [FAIL] {type(e).__name__}: {e}")
            continue

        written += len(result.written)
        superseded += len(result.superseded)
        frozen += len(result.frozen)
        same += len(result.skipped)
        for dataset, message in result.errors.items():
            errors.append(f"{day}/{dataset}: {message}")

        for dataset, (old, new) in changed.items():
            drift.append(
                {
                    "date": day.isoformat(),
                    "dataset": dataset,
                    "old_bytes": len(json.dumps(old, ensure_ascii=False)),
                    "new_bytes": len(json.dumps(new, ensure_ascii=False)),
                }
            )

        detail = {
            "source": "garmin",
            "written": result.written,
            "superseded": result.superseded,
            "frozen": result.frozen,
            "same": result.skipped,
            "missing": result.missing,
            "errors": result.errors,
        }

        if not raw_exists(adapter, day):
            no_data += 1
            print(f"  {day}  [skip] 无 daily_summary")
            with session_scope() as s:
                audit(s, AuditCategory.DATA_SOURCE, ref=day.isoformat(), detail=detail,
                      audit_dir=settings.audit_dir)
            continue

        try:
            sd = adapter.standardize(day)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{day}/standardize: {type(e).__name__}: {e}")
            print(f"  {day}  [FAIL] standardize {type(e).__name__}: {e}")
            continue

        with session_scope() as s:
            upsert_standard(s, sd)
            audit(s, AuditCategory.DATA_SOURCE, ref=day.isoformat(), detail=detail,
                  audit_dir=settings.audit_dir)

        print(
            f"  {day}  新写 {len(result.written)} 覆盖 {len(result.superseded)} "
            f"冻结 {len(result.frozen)} 未变 {len(result.skipped)} "
            f"缺 {len(result.missing)} 错 {len(result.errors)}"
        )

    print()
    print(
        f"[done] 新写 {written}  覆盖 {superseded}  冻结 {frozen}  "
        f"未变 {same}  无摘要 {no_data}  错误 {len(errors)}"
    )
    print(f"[drift] 云端与本地的差异数据集 {len(drift)} 个")

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "window": [days[0].isoformat(), days[-1].isoformat()],
        "refresh_cutoff": cutoff.isoformat(),
        "written": written,
        "superseded": superseded,
        "frozen": frozen,
        "same": same,
        "no_data": no_data,
        "drift": drift,
        "errors": errors,
    }
    p = AUDIT_DIR / f"health-pull-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[report] → {p.name}")

    for item in errors[:5]:
        print(f"  [FAIL] {item}")
    if errors:
        return 1
    print("[OK] 健康数据已更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
