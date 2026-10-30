"""Tredict 指标 ↔ 项目负荷 验证（命令行，需求 6.4）。

验证**只用本地已有数据**，不联网、不写库。用法::

    python scripts/verify_load_validation.py                  # 全部历史
    python scripts/verify_load_validation.py --from 2026-01-01 --to 2026-10-20
    python scripts/verify_load_validation.py --json out.json  # 另存 JSON

报告口径见 ``app/domain/load/validation.py``：按指标分别回归 ``Y = aX + b``，
**不设统一通过阈值、不做指标排名**。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sqlmodel import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.services import validation_service  # noqa: E402

_METRIC_LABEL = {
    "trainingEfforts": "trainingEfforts（所有活动）",
    "effort.heartrate": "effort.heartrate（跑步）",
    "effort.power": "effort.power（跑步）",
    "effort.pace": "effort.pace（跑步，m/s）",
}


def _print_block(title: str, items: list[dict]) -> None:
    print(f"\n### {title}")
    for m in items:
        label = _METRIC_LABEL.get(m["metric"], m["metric"])
        scope = "跨运动类型" if m["scope"] == "crossSport" else f"运动={m['sport']}"
        print(f"\n- {label}  [{scope}]  n={m['n']}  {m['status']}")
        print(f"    {m['relation']}")
        fit = m.get("fit")
        if fit:
            print(
                f"    Y = {fit['a']:.4f}·X {fit['b']:+.2f}"
                f"   r={fit['r']:.3f}  R²={fit['r2']:.3f}  ({fit['direction']})"
            )
            if fit.get("x_per_y") is not None:
                sign = "+" if fit["b"] < 0 else "-"
                print(
                    f"    反向读法：X = (Y {sign} {abs(fit['b']):.2f})"
                    f" × {fit['x_per_y']:.4f}"
                )
        if m.get("note"):
            print(f"    ⚠ {m['note']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tredict 指标 ↔ 项目负荷 验证")
    ap.add_argument("--from", dest="start", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--to", dest="end", help="结束日期 YYYY-MM-DD")
    ap.add_argument("--json", dest="json_out", help="把完整报告另存为 JSON")
    args = ap.parse_args(argv)

    start = dt.date.fromisoformat(args.start) if args.start else None
    end = dt.date.fromisoformat(args.end) if args.end else None
    settings = get_settings()

    with Session(engine) as session:
        result = validation_service.run_validation(session, settings, start=start, end=end)

    p = result["pairing"]
    print("=" * 72)
    print("Tredict 指标 ↔ 项目负荷 验证")
    print("=" * 72)
    rng = result.get("date_range")
    print(f"日期范围 : {rng[0]} ~ {rng[1]}" if rng else "日期范围 : （无数据）")
    print(f"活动数   : {p['activities']}（覆盖 {p['days']} 天）")
    print(f"成功配对 : {p['paired']}   负荷不可计算: {p['no_load']}   "
          f"effort 数量不匹配整天跳过: {p['unmatched_days']} 天")
    print(f"说明     : {p['note']}")

    if not result["metrics"]:
        print("\n[证据不足] 没有任何可用配对数据")
    else:
        _print_block("按指标（跨运动类型整体）", result["metrics"])
        _print_block("按指标 × 运动类型", result["by_sport"])

    if result.get("warnings"):
        print("\n### 提示")
        for w in result["warnings"]:
            print(f"- {w}")

    print(f"\n{result['disclaimer']}")

    if args.json_out:
        out = Path(args.json_out)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[report] → {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
