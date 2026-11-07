"""阈值校准命令行入口（ARCHITECTURE_REVIEW A.5.2）。

用法::

    # 只读：用当前阈值跑一遍，看基线优先级分布
    python -m app.domain.calibration_cli

    # 对照：ACWR_HIGH 从 1.5 调到 1.45，看有多少天换优先级
    python -m app.domain.calibration_cli --set P1-LOAD-HRV.acwr_high=1.45

    # 多组候选并存比较
    python -m app.domain.calibration_cli --set P1-LOAD-HRV.acwr_high=1.45
    python -m app.domain.calibration_cli --set P1-LOAD-HRV.acwr_high=1.45 \\
                                           --set P2-LOAD.acwr_high=1.25

    # 读候选阈值数据文件（A.5.2 剩余）：文件格式与运行时生效的
    # data/rule_thresholds.json 一致，可以先在这里验证，再拿去部署
    python -m app.domain.calibration_cli --file ../data/rule_thresholds.json

只读：不写库、不改全局规则集、不产生策略版本。输出 JSON 便于存档对比。
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlmodel import Session

from app.db.session import engine
from app.domain import rule_registry as rr
from app.domain.calibration import calibrate


def _parse_override(text: str) -> tuple[str, str, float]:
    """``P1-LOAD-HRV.acwr_high=1.45`` → ``("P1-LOAD-HRV", "acwr_high", 1.45)``。"""
    target, sep, raw = text.partition("=")
    rule_id, dot, key = target.partition(".")
    if not sep or not dot or not rule_id or not key:
        raise argparse.ArgumentTypeError(
            f"格式应为 RULE_ID.THRESHOLD=数值，实际收到 {text!r}"
        )
    try:
        return rule_id, key, float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"阈值必须是数值：{raw!r}") from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阈值校准对照回放（只读）")
    ap.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        type=_parse_override,
        metavar="RULE.THRESHOLD=VALUE",
        help="候选阈值，可重复指定",
    )
    ap.add_argument("--limit", type=int, default=470, help="样本天数（默认 470）")
    ap.add_argument(
        "--file",
        dest="overlay_file",
        metavar="PATH",
        help="候选阈值数据文件（与运行时 data/rule_thresholds.json 同格式）",
    )
    args = ap.parse_args(argv)

    overrides: dict[str, dict[str, float]] = {}
    for rule_id, key, value in args.overrides:
        overrides.setdefault(rule_id, {})[key] = value

    if args.overlay_file:
        try:
            from_file = rr.load_threshold_overlay(args.overlay_file)
        except rr.ThresholdOverlayError as exc:
            print(f"[calibrate] 文件错误：{exc}", file=sys.stderr)
            return 2
        for rule_id, patch in from_file.items():
            overrides.setdefault(rule_id, {}).update(patch)

    try:
        with Session(engine) as session:
            report = calibrate(session, overrides, limit=args.limit)
    except ValueError as exc:  # OverrideError
        print(f"[calibrate] 参数错误：{exc}", file=sys.stderr)
        return 2

    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
