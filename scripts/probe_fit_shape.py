"""探查：官方 garmin-fit-sdk 解码后的数据结构形态。

目的只有一个 —— 在写 P4 解析器之前，先确认 SDK 返回的 dict 长什么样：
  - 顶层 key 的命名规则（`record_mesgs` / `unknown_233` / `private_...`？）
  - 未知消息以什么 key 出现，值是 list 还是 dict
  - 字段名是官方名（`heart_rate`）还是字段号（`2`）

只读，不写任何文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

from garmin_fit_sdk import Decoder, Stream

FIT_DIR = Path(__file__).resolve().parents[1] / "data" / "raw" / "garmin" / "fit"


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "650769156"
    path = next(FIT_DIR.glob(f"{target}*.fit"))
    data = path.read_bytes()

    msgs, errs = Decoder(Stream.from_byte_array(bytearray(data))).read()
    print(f"file={path.name}  bytes={len(data)}  errors={len(errs)}")
    print(f"顶层 key 数量 = {len(msgs)}\n")

    for key in sorted(msgs, key=str):
        val = msgs[key]
        kind = type(val).__name__
        n = len(val) if hasattr(val, "__len__") else "?"
        print(f"{key:<32} type={kind:<6} len={n}")

    print("\n=== 抽样 ===")
    for key in ("record_mesgs", "session_mesgs", "lap_mesgs", "set_mesgs"):
        if key not in msgs:
            print(f"{key}: 不存在")
            continue
        first = msgs[key][0]
        print(f"\n[{key}] 第 1 条，字段数={len(first)}")
        for k, v in list(first.items())[:20]:
            print(f"    {k!r:<28} = {v!r}")

    # 未知消息形态：找 key 里含数字且不在官方命名规则里的
    print("\n=== 疑似私有/未知消息 ===")
    for key in sorted(msgs, key=str):
        if "unknown" in str(key).lower() or str(key).isdigit():
            sample = msgs[key][0] if msgs[key] else {}
            print(f"{key}: 条数={len(msgs[key])} 字段号={list(sample)[:8]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
