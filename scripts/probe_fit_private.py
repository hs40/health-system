"""从真实 FIT 文件反推「未公开私有消息」的字段结构（只读）。

对每个未定义消息号，输出：
  - 出现次数
  - 每个字段的 field_definition_number / 基础类型 / 字节数 / 是否数组
  - 去重后的取值样本

目的：判断这些消息是否携带对训练负荷分析有价值的信息，
以及能否在不依赖 Garmin 官方文档的前提下给出稳定 schema。

用法：
    python scripts/probe_fit_private.py <fit_file> [more.fit ...]
    python scripts/probe_fit_private.py --sample 5
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import fitdecode

FIT_DIR = Path(r"F:/WorkBuddy AI/Garmin_Tredict/garmin_data/raw/fit")

PRIVATE_NUMS = (22, 113, 141, 233, 288, 325, 326, 327, 394, 499)


def describe(path: Path) -> None:
    print("=" * 78)
    print(f"FILE {path.name}")
    print("=" * 78)

    counts: collections.Counter[str] = collections.Counter()
    # mesg_key -> field_num -> list of (type_name, size, is_array, sample_values)
    meta: dict[str, dict[int, dict]] = collections.defaultdict(dict)
    values: dict[str, dict[int, collections.Counter]] = collections.defaultdict(
        lambda: collections.defaultdict(collections.Counter)
    )

    with fitdecode.FitReader(str(path), check_crc=fitdecode.CrcCheck.DISABLED) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            key = frame.name
            counts[key] += 1
            if not key.startswith("unknown_"):
                continue
            for f in frame.fields:
                slot = meta[key].setdefault(
                    f.def_num,
                    {
                        "type": f.type.name if hasattr(f.type, "name") else str(f.type),
                        "size": getattr(f, "size", None),
                        "array": isinstance(f.value, (list, tuple)),
                        "is_base": f.is_base_type,
                    },
                )
                try:
                    values[key][f.def_num][repr(f.value)[:60]] += 1
                except Exception:
                    pass

    print("\n[消息计数]")
    for name, n in counts.most_common():
        print(f"  {name:<24} {n:>6}")

    for name in sorted(meta, key=lambda s: int(s.split("_")[1])):
        print(f"\n[{name}]  出现 {counts[name]} 次")
        for num in sorted(meta[name]):
            m = meta[name][num]
            samples = values[name][num].most_common(3)
            uniq = len(values[name][num])
            print(
                f"  field {num:<3} type={m['type']:<10} size={m['size']!s:<4} "
                f"array={m['array']!s:<5} uniq={uniq:<5} samples={samples}"
            )


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--sample":
        n = int(argv[1]) if len(argv) > 1 else 3
        files = sorted(FIT_DIR.glob("*.fit"))[:n]
    else:
        files = [Path(a) for a in argv]

    for p in files:
        if not p.exists():
            print(f"[SKIP] {p} 不存在")
            continue
        describe(p)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
