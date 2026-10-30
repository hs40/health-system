"""基线：301 个 FIT 的字段覆盖统计（P4 解析器的事实依据）。

为什么要先跑这个：
  上一轮踩过坑 —— 按「消息定义里列出的字段」统计，得出 record 有 15 个字段、
  device_settings 有 114 个字段，实际大量是 None。定义 ≠ 有值。
  本脚本只统计 **非空** 值，并区分：
    - 官方命名字段（如 `heart_rate`）
    - 数字字段号（如 `107`）—— 官方 profile 未定义，属于厂商扩展

输出 `data/raw/garmin/_fit_baseline.json`，只读 FIT、只写这一个报告。

用法：
    python scripts/probe_fit_baseline.py
    python scripts/probe_fit_baseline.py --limit 20     # 抽样快跑
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from garmin_fit_sdk import Decoder, Stream

ROOT = Path(__file__).resolve().parents[1]
FIT_DIR = ROOT / "data" / "raw" / "garmin" / "fit"
OUT_PATH = ROOT / "data" / "raw" / "garmin" / "_fit_baseline.json"

# 重点关注的消息（逐秒、圈、组、区间、汇总）
FOCUS = (
    "record_mesgs",
    "session_mesgs",
    "lap_mesgs",
    "set_mesgs",
    "split_mesgs",
    "split_summary_mesgs",
    "time_in_zone_mesgs",
    "zones_target_mesgs",
    "sport_mesgs",
    "activity_mesgs",
    "device_info_mesgs",
)


def is_numeric_field(key: str) -> bool:
    """数字 key = 官方 profile 未定义的字段号。"""
    return key.isdigit()


def sample_of(value: Any) -> str:
    """给字段值一个可读的类型标签，用于判断该字段该映射成什么列类型。"""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, datetime):
        return "datetime"
    if isinstance(value, (list, tuple)):
        return f"list[{len(value)}]"
    if isinstance(value, (bytes, bytearray)):
        return f"bytes[{len(value)}]"
    return type(value).__name__


def scan_file(path: Path, stats: dict[str, Any]) -> tuple[int, int]:
    """扫描单个 FIT，把结果累加进 stats。返回 (消息类型数, 错误数)。"""
    data = path.read_bytes()
    msgs, errs = Decoder(Stream.from_byte_array(bytearray(data))).read()

    for mesg_key, rows in msgs.items():
        key = str(mesg_key)
        bucket = stats.setdefault(
            key,
            {
                "files": 0,
                "records": 0,
                "fields": {},  # field -> {"nonnull": n, "types": Counter}
            },
        )
        bucket["files"] += 1
        bucket["records"] += len(rows)

        for row in rows:
            for field, value in row.items():
                if value is None:
                    continue
                f = str(field)
                fb = bucket["fields"].setdefault(f, {"nonnull": 0, "types": Counter()})
                fb["nonnull"] += 1
                fb["types"][sample_of(value)] += 1

    return len(msgs), len(errs)


def main() -> int:
    ap = argparse.ArgumentParser(description="FIT 字段覆盖基线统计")
    ap.add_argument("--limit", type=int, default=0, help="只扫前 N 个文件（调试用）")
    args = ap.parse_args()

    files = sorted(FIT_DIR.glob("*.fit"))
    if args.limit:
        files = files[: args.limit]
    if not files:
        raise SystemExit(f"未找到 FIT：{FIT_DIR}")

    print(f"[baseline] 待扫 {len(files)} 个文件")

    stats: dict[str, Any] = {}
    total_errors = 0
    failed: list[str] = []

    for i, path in enumerate(files, 1):
        try:
            _, errs = scan_file(path, stats)
            total_errors += errs
        except Exception as exc:  # noqa: BLE001 —— 单个坏文件不中断全量
            failed.append(f"{path.name}: {type(exc).__name__}: {exc}")
        if i % 50 == 0 or i == len(files):
            print(f"  {i}/{len(files)} …")

    # Counter 不能直接序列化，转成普通 dict 并按次数排序
    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files_scanned": len(files),
        "decode_errors": total_errors,
        "failed_files": failed,
        "messages": {},
    }
    for key, bucket in stats.items():
        fields = {}
        for fname, fb in bucket["fields"].items():
            fields[fname] = {
                "nonnull": fb["nonnull"],
                "types": dict(fb["types"].most_common()),
            }
        out["messages"][key] = {
            "files": bucket["files"],
            "records": bucket["records"],
            "fields": fields,
        }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 控制台摘要 ----
    print(f"\n[baseline] 文件 {len(files)}  解码错误 {total_errors}  失败 {len(failed)}")
    print(f"[baseline] 报告 -> {OUT_PATH}\n")

    known = sorted(k for k in stats if not k.isdigit())
    private = sorted((k for k in stats if k.isdigit()), key=int)

    print(f"=== 已知消息 {len(known)} 类 ===")
    for key in known:
        b = stats[key]
        n_fields = len(b["fields"])
        n_numeric = sum(1 for f in b["fields"] if is_numeric_field(f))
        print(
            f"  {key:<28} 文件={b['files']:>3}  记录={b['records']:>7}  "
            f"有值字段={n_fields:>3}（其中数字字段号 {n_numeric}）"
        )

    print(f"\n=== 私有消息 {len(private)} 类 ===")
    for key in private:
        b = stats[key]
        nums = sorted((f for f in b["fields"] if is_numeric_field(f)), key=int)
        print(
            f"  mesg {key:<5} 文件={b['files']:>3}  记录={b['records']:>6}  "
            f"字段号={nums}"
        )

    print("\n=== 重点消息字段明细 ===")
    for key in FOCUS:
        if key not in stats:
            print(f"\n[{key}] 不存在")
            continue
        b = stats[key]
        print(f"\n[{key}] 文件={b['files']} 记录={b['records']}")
        rows = sorted(
            b["fields"].items(),
            key=lambda kv: (is_numeric_field(kv[0]), -kv[1]["nonnull"]),
        )
        for fname, fb in rows:
            types = ", ".join(f"{t}×{c}" for t, c in fb["types"].most_common(3))
            mark = "  " if not is_numeric_field(fname) else "· "
            print(f"  {mark}{fname:<26} {fb['nonnull']:>7}  {types}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
