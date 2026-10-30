"""TA.5 / V5 对拍：Tredict Python 重写 vs Node 原脚本（逐项一致）。

基准由 ``scripts/parity_tredict_extract.cjs`` 从 ``tredict-upload.js`` **原样抽取**函数生成，
本脚本用同一批本地数据跑 Python 构造函数，逐条比对字段。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.adapters.tredict import (
    build_bodyvalue_records,
    build_hrv_records,
    build_sleep_records,
)

SRC = Path(r"F:/WorkBuddy AI/Garmin_Tredict/garmin_data")
BASELINE = Path(__file__).resolve().parents[2] / "data" / "tredict_baseline.json"


def load(rel: str) -> dict:
    raw = json.loads((SRC / rel).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        raw = raw["data"]
    return raw


def _cmp(a: list[dict], b: list[dict], keys: tuple[str, ...], label: str, errs: list[str]) -> None:
    if len(a) != len(b):
        errs.append(f"{label}: 条数不一致 py={len(a)} node={len(b)}")
        return
    for i, (x, y) in enumerate(zip(a, b, strict=True)):
        for k in keys:
            xv, yv = x.get(k), y.get(k)
            if isinstance(xv, float) and isinstance(yv, (int, float)):
                if abs(xv - float(yv)) > 1e-9:
                    errs.append(f"{label}[{i}] {k}: py={xv} node={yv}")
            elif xv != yv:
                errs.append(f"{label}[{i}] {k}: py={xv!r} node={yv!r}")


def main() -> int:
    if not BASELINE.exists():
        print("缺少基准，请先运行 parity_tredict_extract.cjs")
        return 2
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    now_ms = base["nowMs"]

    hrv_raw = load("hrv/hrv.json")
    sleep_raw = load("sleep/sleep.json")
    daily_raw = load("daily/daily_summary.json")
    weight_raw = json.loads((SRC / "body_composition/weight.json").read_text(encoding="utf-8"))
    wlist = weight_raw.get("dateWeightList") or (weight_raw.get("data") or {}).get(
        "dateWeightList"
    ) or []

    py_hrv, _, _ = build_hrv_records(hrv_raw)
    py_sleep, _, _ = build_sleep_records(sleep_raw, now_ms)
    py_bv, py_meta = build_bodyvalue_records(daily_raw, wlist, now_ms)

    errs: list[str] = []
    _cmp(
        py_hrv,
        base["hrv"]["records"],
        ("timestamp", "timezoneOffsetInSeconds", "rmssd"),
        "hrv",
        errs,
    )
    _cmp(
        py_sleep,
        base["sleep"]["records"],
        ("startTimestamp", "endTimestamp", "timezoneOffsetInSeconds", "sleepDurationInSeconds"),
        "sleep",
        errs,
    )
    _cmp(
        py_bv,
        base["bodyvalues"]["records"],
        (
            "timestamp",
            "timezoneOffsetInSeconds",
            "restingHeartrate",
            "hrRestDynamic",
            "hrMaxDynamic",
            "weightInKilograms",
            "bodyHeightInCentimeter",
        ),
        "bodyvalues",
        errs,
    )

    # 元数据（门控/计数）一致性
    node_bv = base["bodyvalues"]
    for k in ("dailyCount", "weightCount", "withHr", "earliest", "latest", "skippedToday"):
        if py_meta.get(k) != node_bv.get(k):
            errs.append(f"bodyvalues.meta {k}: py={py_meta.get(k)} node={node_bv.get(k)}")

    print(f"HRV {len(py_hrv)} | 睡眠 {len(py_sleep)} | 身体 {len(py_bv)} 条")
    print(f"身体门控：gateToday={py_meta['gateToday']} skippedToday={py_meta['skippedToday']}")
    if errs:
        print(f"❌ 不一致 {len(errs)} 项（前 30）：")
        for e in errs[:30]:
            print("   ", e)
        return 1
    print("✅ V5 对拍通过：Python 重写与 Node 原脚本逐项一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
