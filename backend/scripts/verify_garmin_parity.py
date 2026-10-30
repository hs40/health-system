"""TA.2 / V4 关键验证：Garmin 数据一致性对拍（python 适配器 vs 现有 468 天 Node 抓取）。

方法：
1. 读取现有数据集（`Garmin_Tredict/garmin_data`），其结构为 ``{date: payload}``。
2. 取抽样日期，把 payload 物化为本项目 ``raw/garmin/<dataset>/<date>.json``（临时目录）。
3. 运行 ``GarminAdapter.standardize()``，与"按既定口径直接从现有数据提取"的期望值逐字段比对。

既定口径（源自 tredict-upload.js，已在 Tredict 端验证）：
- 睡眠时长 = ``dailySleepDTO.sleepTimeSeconds``；
- 睡眠起止 = Garmin **本地**时间戳按 UTC 墙钟还原（offset=0）。
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

from app.adapters.garmin import GarminAdapter
from app.config import get_settings

SRC = Path(r"F:/WorkBuddy AI/Garmin_Tredict/garmin_data")
SAMPLES = 30  # 抽样日期数


def load_map(rel: str) -> dict:
    raw = json.loads((SRC / rel).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "data" in raw and isinstance(raw["data"], dict):
        raw = raw["data"]
    return raw


def expect(day: str, daily: dict, sleep: dict, hrv: dict) -> dict:
    s = daily.get(day, {}) or {}
    dto = (sleep.get(day, {}) or {}).get("dailySleepDTO", {}) or {}
    hs = (hrv.get(day, {}) or {}).get("hrvSummary", {}) or {}
    start = dto.get("sleepStartTimestampLocal")
    return {
        "resting_heart_rate": s.get("restingHeartRate"),
        "steps": s.get("totalSteps"),
        "active_calories": s.get("activeKilocalories"),
        "intensity_moderate_min": s.get("moderateIntensityMinutes"),
        "intensity_vigorous_min": s.get("vigorousIntensityMinutes"),
        "body_battery_wake": s.get("bodyBatteryMostRecentValue"),
        "sleep_duration_sec": dto.get("sleepTimeSeconds"),
        "hrv_last_night_avg": hs.get("lastNightAvg"),
        "_sleep_start_expected": (
            dt.datetime.fromtimestamp(start / 1000, tz=dt.UTC).replace(tzinfo=None)
            if start
            else None
        ),
    }


def main() -> int:
    daily = load_map("daily/daily_summary.json")
    sleep = load_map("sleep/sleep.json")
    hrv = load_map("hrv/hrv.json")

    dates = sorted(set(daily) & set(sleep) & set(hrv))
    if not dates:
        print("无共同日期，退出")
        return 1
    step = max(1, len(dates) // SAMPLES)
    picked = dates[::step][:SAMPLES]
    print(f"共同日期 {len(dates)} 天，抽样 {len(picked)} 天\n")

    tmp = Path(tempfile.mkdtemp(prefix="garmin_parity_"))
    try:
        settings = get_settings()
        adapter = GarminAdapter(settings)
        adapter.raw_root = tmp  # 指向临时原始目录

        mismatches: list[str] = []
        checked = 0
        for day in picked:
            d = dt.date.fromisoformat(day)
            # 物化原始文件
            for ds, payload in (
                ("daily_summary", daily.get(day)),
                ("sleep", sleep.get(day)),
                ("hrv", hrv.get(day)),
            ):
                if payload is None:
                    continue
                p = tmp / ds / f"{day}.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            got = adapter.standardize(d)
            exp = expect(day, daily, sleep, hrv)
            checked += 1
            for field in (
                "resting_heart_rate",
                "steps",
                "active_calories",
                "intensity_moderate_min",
                "intensity_vigorous_min",
                "body_battery_wake",
                "sleep_duration_sec",
                "hrv_last_night_avg",
            ):
                gv, ev = getattr(got, field), exp[field]
                # 数值归一化比较（None 视为相等）
                if gv is None and ev is None:
                    continue
                if gv is None or ev is None or float(gv) != float(ev):
                    mismatches.append(f"{day} {field}: got={gv} exp={ev}")
            if got.sleep_start_ts != exp["_sleep_start_expected"]:
                mismatches.append(
                    f"{day} sleep_start_ts: got={got.sleep_start_ts} "
                    f"exp={exp['_sleep_start_expected']}"
                )

        print(f"已比对 {checked} 天 × 9 字段")
        if mismatches:
            print(f"❌ 不一致 {len(mismatches)} 项：")
            for m in mismatches[:30]:
                print("   ", m)
            return 1
        print("✅ V4 对拍通过：字段与既定口径完全一致")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
