"""对比「现有 fit_json」与「FIT 原始文件」的字段覆盖，量化解析损失（只读）。

方法：
  1. 用 fitdecode 读 FIT，统计每个消息的非空字段号集合
  2. 读同名 fit_json，统计每个消息对象实际输出的键数量
  3. 按消息名对齐，输出 缺失字段数 / 覆盖率

用法：
    python scripts/probe_fit_json_loss.py [--sample N]
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import fitdecode

FIT_DIR = Path(r"F:/WorkBuddy AI/Garmin_Tredict/garmin_data/raw/fit")
JSON_DIR = Path(r"F:/WorkBuddy AI/Garmin_Tredict/garmin_data/activity/fit_json")

# fitdecode 消息名 -> fit_json 顶层键
NAME_MAP = {
    "record": "records",
    "session": "sessions",
    "lap": "laps",
    "length": "lengths",
    "set": "sets",
    "event": "events",
    "device_info": "device_infos",
    "user_profile": "user_profile",
    "zones_target": "zones_target",
    "sport": "sports",
    "activity": "activity",
    "device_settings": "device_settings",
    "time_in_zone": "time_in_zone",
    "activity_metrics": "activity_metrics",
    "user_metrics": "user_metrics",
    "gps_metadata": "gps_metadata",
    "timestamp_correlation": "timestamp_correlation",
    "hrv": "hrv",
    "file_creator": "file_creator",
    "file_id": "file_ids",
}


def fit_field_counts(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with fitdecode.FitReader(str(path), check_crc=fitdecode.CrcCheck.DISABLED) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            slot = out.setdefault(frame.name, {"nonnull": set(), "msgs": 0})
            slot["msgs"] += 1
            for f in frame.fields:
                if f.value is not None:
                    slot["nonnull"].add(f.def_num)
    return out


def json_key_counts(path: Path) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    for k, v in data.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            union: set[str] = set()
            for item in v:
                if isinstance(item, dict):
                    union |= set(item.keys())
            out[k] = len(union)
        elif isinstance(v, dict):
            out[k] = len(v)
    return out


def main(argv: list[str]) -> int:
    n = 3
    if "--sample" in argv:
        n = int(argv[argv.index("--sample") + 1])

    files = sorted(FIT_DIR.glob("*.fit"))[:n]
    grand_lost = 0
    grand_have = 0

    for fp in files:
        jp = JSON_DIR / f"{fp.stem}.json"
        if not jp.exists():
            print(f"[SKIP] 无 fit_json: {fp.name}")
            continue
        fitc = fit_field_counts(fp)
        jsonc = json_key_counts(jp)

        print("=" * 74)
        print(f"{fp.stem}  ({fp.stat().st_size / 1024:.0f} KB)  "
              f"json {jp.stat().st_size / 1024:.0f} KB")
        print("=" * 74)
        print(f"{'FIT消息':<22} {'条数':>6} {'FIT非空':>7} {'JSON键':>6} {'丢失':>5}  覆盖率")
        lost_total = 0
        for name in sorted(fitc, key=lambda s: -fitc[s]["msgs"]):
            slot = fitc[name]
            key = NAME_MAP.get(name)
            if key is None:
                continue
            have = jsonc.get(key, 0)
            real = len(slot["nonnull"])
            lost = max(0, real - have)
            lost_total += lost
            cov = (have / real * 100) if real else 0.0
            flag = "  <<<" if lost > 0 else ""
            print(f"{name:<22} {slot['msgs']:>6} {real:>7} {have:>6} {lost:>5}  {cov:5.1f}%{flag}")
        grand_lost += lost_total
        print(f"  小计：非空字段 {sum(len(v['nonnull']) for v in fitc.values())} 个，"
              f"未覆盖 {lost_total} 个")
        print()

    print(f"合计未覆盖字段：{grand_lost} 个（样本 {len(files)} 个活动）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
