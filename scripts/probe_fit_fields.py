"""打印指定消息的「非空字段 + 取值样本」，用于判断未定义字段的真实含义（只读）。

用法：
    python scripts/probe_fit_fields.py <fit> <global_mesg_num> [--max-samples N]
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import fitdecode


def dump(path: Path, mesg_num: int, max_samples: int = 5) -> None:
    vals: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    types: dict[int, str] = {}
    nulls: set[int] = set()
    n = 0

    with fitdecode.FitReader(str(path), check_crc=fitdecode.CrcCheck.DISABLED) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.global_mesg_num != mesg_num:
                continue
            n += 1
            for f in frame.fields:
                types.setdefault(f.def_num, getattr(f.type, "name", str(f.type)))
                if f.value is None:
                    nulls.add(f.def_num)
                else:
                    vals[f.def_num][repr(f.value)[:50]] += 1

    print(f"{path.name}  mesg {mesg_num} ({frame_name(path, mesg_num)})  条数 {n}")
    print(f"{'fld':>4} {'type':<10} {'非空':>5} {'uniq':>5}  样本")
    for num in sorted(vals):
        top = vals[num].most_common(max_samples)
        print(f"{num:>4} {types[num]:<10} {sum(vals[num].values()):>5} {len(vals[num]):>5}  {top}")
    if nulls:
        print(f"  全空字段: {sorted(nulls)}")


def frame_name(path: Path, mesg_num: int) -> str:
    with fitdecode.FitReader(str(path), check_crc=fitdecode.CrcCheck.DISABLED) as fit:
        for frame in fit:
            if isinstance(frame, fitdecode.FitDataMessage) and frame.global_mesg_num == mesg_num:
                return frame.name
    return "?"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = Path(argv[0])
    num = int(argv[1])
    max_samples = 5
    if "--max-samples" in argv:
        max_samples = int(argv[argv.index("--max-samples") + 1])
    dump(path, num, max_samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
