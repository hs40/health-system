"""U-8 全套重算：负荷分段口径（v2）→ ACWR → 历史日报与风险重判。

背景
----
2026-10-25 用户决策：U-8 走「**全套**」——不只重算派生层，还要重生成历史日报、
重判风险，保证全库同一口径。只重算派生层会造成「``acwr`` 表与历史日报数字不一致、
风险新日按新口径 / 旧日按旧口径」的断层。

做什么（按日回放）
------------------
1. ``recompute_day_load``：按 FIT 圈重算 ``training_load``；
2. ``refresh_acwr``：从最早受影响日回放 EWMA，重写 ``acwr``；
3. ``generate_daily(during_recompute=True)``：重生成日报——内部会重判风险、重写
   ``risk_assessment`` 与报告 payload 的 ``load`` / ``acwr`` / ``quality`` / ``limitations``。

不做：不碰原始层；不重算基线（基线只看生理指标）；不调用 LLM。

用法
----
    python scripts/recompute_segmented_load.py --from 2025-07-03 --to 2026-10-25 --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sqlmodel import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.services import report_service  # noqa: E402
from app.services.recompute_service import regenerate_aggregates  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="U-8 分段口径全套重算")
    ap.add_argument("--from", dest="start", required=True, help="起始日 YYYY-MM-DD")
    ap.add_argument("--to", dest="end", required=True, help="结束日 YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只打印范围）")
    args = ap.parse_args(argv)

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    if start > end:
        print("起始日晚于结束日")
        return 2

    settings = get_settings()
    days = (end - start).days + 1
    if not args.apply:
        print(f"[dry-run] 将重算 {start} ~ {end}（{days} 天）；加 --apply 才写库")
        return 0

    t0 = time.time()
    ok = fail = 0
    with Session(engine) as session:
        d = start
        while d <= end:
            try:
                report_service.recompute_day_load(session, d, settings)
                report_service.refresh_acwr(session, d)
                report_service.generate_daily(session, d, settings, during_recompute=True)
                ok += 1
            except Exception as exc:  # noqa: BLE001 — 单日失败不阻断后续日期
                session.rollback()
                fail += 1
                print(f"  ! {d} 失败：{type(exc).__name__}: {exc}")
            if ok and ok % 50 == 0:
                print(f"  ... 已完成 {ok} 天（{d}），用时 {time.time() - t0:.0f}s")
            d += dt.timedelta(days=1)

    # 聚合层：周报/月报跟着重生成，避免「日报新口径、周月报旧快照」
    agg = regenerate_aggregates(session, start, end, settings)
    print(f"完成：成功 {ok} 天 / 失败 {fail} 天；周报 {agg['weekly']} / 月报 {agg['monthly']}；"
          f"用时 {time.time() - t0:.0f}s")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
