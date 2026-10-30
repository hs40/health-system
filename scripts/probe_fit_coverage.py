"""量化「FIT 解析器实际丢了多少字段」（只读）。

对样本 FIT 文件，逐消息比对三个集合：
  A. 文件中实际出现的字段号
  B. Garmin 官方 SDK（garmin-fit-sdk）定义
  C. fit-file-parser@6.1.2 的 profile.js 定义

输出 A - B、A - C 的差集，即「真实存在但解析器不认识」的字段。

用法：
    python scripts/probe_fit_coverage.py [--sample N]
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import fitdecode
from garmin_fit_sdk import Profile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_fit_profile import FFP_PROFILE, parse_fit_file_parser  # noqa: E402

FIT_DIR = Path(r"F:/WorkBuddy AI/Garmin_Tredict/garmin_data/raw/fit")


def official_fields() -> dict[int, dict]:
    """官方 SDK：mesg_num -> {name, fields:set[int]}"""
    out: dict[int, dict] = {}
    for num, msg in Profile["messages"].items():
        out[int(num)] = {
            "name": msg["name"],
            "fields": {int(k) for k in msg["fields"]},
        }
    return out


def observed_fields(path: Path) -> dict[int, dict]:
    """实际文件：mesg_num -> {name, fields:set[int]（仅非空）, null_fields:set[int], count}

    注意：FIT 的 local message definition 会列出所有字段，即使该条消息里没有值。
    因此必须区分「定义存在」与「实际有值」，否则会严重高估信息量。
    """
    out: dict[int, dict] = {}
    with fitdecode.FitReader(str(path), check_crc=fitdecode.CrcCheck.DISABLED) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            num = frame.global_mesg_num
            slot = out.setdefault(
                num,
                {"name": frame.name, "fields": set(), "null_fields": set(), "count": 0},
            )
            slot["count"] += 1
            for f in frame.fields:
                if f.value is None:
                    slot["null_fields"].add(f.def_num)
                else:
                    slot["fields"].add(f.def_num)
    return out


def main(argv: list[str]) -> int:
    n = 3
    if "--sample" in argv:
        n = int(argv[argv.index("--sample") + 1])

    ffp_raw = parse_fit_file_parser(FFP_PROFILE)
    ffp = {num: {"name": v["name"], "fields": set(v["fields"])} for num, v in ffp_raw.items()}
    off = official_fields()

    files = sorted(FIT_DIR.glob("*.fit"))[:n]
    print(f"样本 {len(files)} 个文件\n")

    total_lost_vs_official = 0
    total_lost_vs_ffp = 0
    for p in files:
        obs = observed_fields(p)
        print("=" * 78)
        print(f"{p.name}  消息种类 {len(obs)}")
        print("=" * 78)
        print(f"{'num':>4} {'name':<22} {'msgs':>5} {'flds':>5} {'off':>4} {'ffp':>4}  丢失字段")
        for num in sorted(obs):
            o = obs[num]
            fname = o["name"]
            off_set = off.get(num, {}).get("fields", set())
            ffp_set = ffp.get(num, {}).get("fields", set())
            lost_off = sorted(o["fields"] - off_set) if off_set else sorted(o["fields"])
            lost_ffp = sorted(o["fields"] - ffp_set) if ffp_set else sorted(o["fields"])
            if not lost_off and not lost_ffp:
                continue
            total_lost_vs_official += len(lost_off)
            total_lost_vs_ffp += len(lost_ffp)
            print(
                f"{num:>4} {fname:<22} {o['count']:>5} {len(o['fields']):>5} "
                f"{len(off_set):>4} {len(ffp_set):>4}  "
                f"vs官方{len(lost_off)}个{lost_off[:8]}  vsFFP{len(lost_ffp)}个{lost_ffp[:8]}"
            )
        print()

    print(f"合计：相对官方 SDK 未覆盖字段 {total_lost_vs_official} 个；"
          f"相对 fit-file-parser {total_lost_vs_ffp} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
