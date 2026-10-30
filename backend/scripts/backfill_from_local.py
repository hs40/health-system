"""从既有本地 Garmin 数据集回填到本项目 raw 层（V11 数据完整性）。

背景
----
既有脚本（``Garmin_Tredict/_tools/garmin-sync.js``）已在本地抓取了
**468 天**历史数据（2025-07-03 → 2026-10-13），形态为
``<dir>/<file>.json = {"data": {"YYYY-MM-DD": <payload>, ...}}``。
本项目 raw 层是**每天一个文件** ``data/raw/garmin/<dataset>/<date>.json``。

重新从 Garmin API 抓取 468 天需约 468 × 8 ≈ 3700 次调用，不现实；
而本地已有权威快照，故直接**本地回填**（无网络请求、秒级完成）。

安全性
------
严格复用 ``GarminAdapter._write_raw`` 的 **append-only** 语义：
文件已存在则跳过、**绝不覆盖**（REQ-DATA-001，有测试护栏）。

用法
----
    python scripts/backfill_from_local.py                 # 回填 raw + 标准化入库
    python scripts/backfill_from_local.py --raw-only       # 只回填 raw 文件
    python scripts/backfill_from_local.py --with-reports   # 额外逐日生成日报
    python scripts/backfill_from_local.py --start 2026-09-01 --end 2026-10-13
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from app.adapters.garmin import DATASETS, GarminAdapter
from app.config import get_settings
from app.db.session import init_db, session_scope
from app.services.report_service import generate_daily
from app.storage.standard_store import upsert_standard

SRC = Path(r"F:/WorkBuddy AI/Garmin_Tredict/garmin_data")

# 本项目 dataset → 既有数据集的相对路径
MAPPING: dict[str, str] = {
    "daily_summary": "daily/daily_summary.json",
    "sleep": "sleep/sleep.json",
    "hrv": "hrv/hrv.json",
    "heart_rate": "heart_rate/heart_rate.json",
    "body_battery": "body_battery/body_battery.json",
    "stress": "stress/stress.json",
    "spo2": "respiration_spo2/spo2.json",
    "respiration": "respiration_spo2/respiration.json",
    # 体重：standardize() 会读 `weight` 数据集，但 Garmin API 同步尚未纳入（见下方说明），
    # 故此处从既有 body_composition 回填，使 weight_kg 可用（对 Tredict 体重与月报必要）。
    "weight": "body_composition/weight.json",
    # 活动：负荷来源（D-2）。既有为**扁平列表**（非按日期字典），按开始时间分组落盘。
    "activities": "activity/activities.json",
}

# DATASETS（9 个，API 同步覆盖） + weight（仅本地回填）
ALL_DATASETS: tuple[str, ...] = DATASETS + ("weight",)

# 非每日必有的数据集（休息日无活动 / 体重非每日测量）：完整性判定不计缺口
SPARSE_DATASETS: frozenset[str] = frozenset({"weight", "activities"})


def _activities_by_date(activities: list) -> dict[str, list]:
    """活动扁平列表 → {开始日期: [活动, ...]}（按**开始时间**归属，REQ-DATA-003）。"""
    out: dict[str, list] = {}
    for a in activities:
        if not isinstance(a, dict):
            continue
        d = str(a.get("startTimeLocal") or "")[:10]
        if len(d) != 10:
            continue
        out.setdefault(d, []).append(a)
    return out


def _weight_by_date(date_weight_list: list) -> dict[str, dict]:
    """dateWeightList → {calendarDate: 当日最后一次体重记录}。"""
    out: dict[str, dict] = {}
    for w in date_weight_list:
        if not isinstance(w, dict):
            continue
        d = w.get("calendarDate")
        if not d or w.get("weight") is None:
            continue
        prev = out.get(d)
        if prev is None or (w.get("timestampGMT") or 0) > (prev.get("timestampGMT") or 0):
            out[d] = {
                "weight": w["weight"],
                "timestampGMT": w.get("timestampGMT"),
                "calendarDate": d,
                "sourceType": w.get("sourceType"),
            }
    return out


def load_by_date(path: Path, dataset: str | None = None) -> dict[str, object]:
    """加载既有数据集 → {date: payload}（按数据集形态分派）。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if dataset == "activities":
        if not isinstance(raw, list):
            raise SystemExit(f"activities 源应为列表: {path}")
        return _activities_by_date(raw)
    if isinstance(raw, dict) and isinstance(raw.get("dateWeightList"), list):
        return _weight_by_date(raw["dateWeightList"])
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        return raw["data"]
    if isinstance(raw, dict):
        return raw
    raise SystemExit(f"无法识别的数据形态: {path}")


def backfill_raw(adapter: GarminAdapter, start: dt.date | None, end: dt.date | None) -> dict:
    """回填 raw 层（append-only）。返回每个数据集的统计。"""
    stats: dict[str, dict] = {}
    for dataset in ALL_DATASETS:
        rel = MAPPING.get(dataset)
        if rel is None:
            stats[dataset] = {"error": "未配置映射"}
            continue
        p = SRC / rel
        if not p.exists():
            stats[dataset] = {"error": f"源缺失 {p}"}
            continue
        by_date = load_by_date(p, dataset)
        written = skipped = empty = 0
        lo = hi = None
        for day_str in sorted(by_date):
            try:
                day = dt.date.fromisoformat(day_str)
            except ValueError:
                continue
            if start and day < start:
                continue
            if end and day > end:
                continue
            payload = by_date[day_str]
            if payload is None or payload == {} or payload == []:
                empty += 1
                continue
            if adapter._write_raw(dataset, day, payload):  # noqa: SLF001 — 复用 append-only 语义
                written += 1
                lo = day_str if lo is None else min(lo, day_str)
                hi = day_str if hi is None else max(hi, day_str)
            else:
                skipped += 1
        stats[dataset] = {
            "source_days": len(by_date),
            "written": written,
            "skipped_existing": skipped,
            "empty_payload": empty,
            "first": lo,
            "last": hi,
        }
    return stats


def standardize_all(adapter: GarminAdapter, start: dt.date | None, end: dt.date | None) -> dict:
    """逐日标准化并入库。返回统计。"""
    ok = no_data = failed = 0
    errors: list[str] = []
    days = _date_list(adapter, start, end)
    with session_scope() as s:
        for day in days:
            try:
                sd = adapter.standardize(day)
            except Exception as e:  # noqa: BLE001
                failed += 1
                errors.append(f"{day}: {type(e).__name__}: {e}")
                continue
            # 判定"是否有数据"：关键字段全空视为无数据
            has = any(
                getattr(sd, f) is not None
                for f in (
                    "resting_heart_rate",
                    "hrv_last_night_avg",
                    "sleep_duration_sec",
                    "steps",
                    "body_battery_wake",
                )
            )
            if not has:
                no_data += 1
                continue
            upsert_standard(s, sd)
            ok += 1
    return {"days": len(days), "standardized": ok, "no_data": no_data, "failed": failed,
            "errors": errors[:5]}


def generate_reports(start: dt.date | None, end: dt.date | None) -> dict:
    """逐日生成日报（含风险与质量），用于建立基线与周/月报。"""
    settings = get_settings()
    days = _date_list(GarminAdapter(settings), start, end)
    ok = failed = 0
    errors: list[str] = []
    with session_scope() as s:
        for day in days:
            try:
                generate_daily(s, day, settings)
                ok += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                errors.append(f"{day}: {type(e).__name__}: {e}")
    return {"days": len(days), "reports": ok, "failed": failed, "errors": errors[:5]}


def _date_list(adapter: GarminAdapter, start: dt.date | None, end: dt.date | None) -> list[dt.date]:
    """以 daily_summary 的落盘日期为准列出待处理日期。"""
    root = adapter.raw_root / "daily_summary"
    if not root.exists():
        return []
    out: list[dt.date] = []
    for p in sorted(root.glob("*.json")):
        try:
            day = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if start and day < start:
            continue
        if end and day > end:
            continue
        out.append(day)
    return out


def _integrity(adapter: GarminAdapter, start: dt.date | None, end: dt.date | None) -> dict:
    """数据完整性检查：各数据集覆盖天数与缺口。"""
    res: dict[str, dict] = {}
    union: set[dt.date] = set()
    per: dict[str, set[dt.date]] = {}
    for dataset in ALL_DATASETS:
        root = adapter.raw_root / dataset
        days = set()
        if root.exists():
            for p in root.glob("*.json"):
                try:
                    d = dt.date.fromisoformat(p.stem)
                except ValueError:
                    continue
                if start and d < start:
                    continue
                if end and d > end:
                    continue
                days.add(d)
        per[dataset] = days
        union |= days
    for dataset in ALL_DATASETS:
        missing = sorted(union - per[dataset])
        res[dataset] = {
            "files": len(per[dataset]),
            "missing_vs_union": len(missing),
            "sample_missing": [d.isoformat() for d in missing[:5]],
        }
    res["__union__"] = {"days": len(union)}
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="从既有本地 Garmin 数据集回填")
    ap.add_argument("--raw-only", action="store_true", help="只回填 raw 文件，不做标准化")
    ap.add_argument("--with-reports", action="store_true", help="额外逐日生成日报")
    ap.add_argument("--start", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=None)
    args = ap.parse_args()

    settings = get_settings()
    init_db()
    adapter = GarminAdapter(settings)

    print(f"源目录: {SRC}")
    print(f"raw 根: {adapter.raw_root}")
    print(f"范围  : {args.start or '全部'} → {args.end or '全部'}\n")

    print("== 1/3 回填 raw（append-only）==")
    stats = backfill_raw(adapter, args.start, args.end)
    for ds in ALL_DATASETS:
        st = stats[ds]
        if "error" in st:
            print(f"  ❌ {ds:<14} {st['error']}")
        else:
            print(
                f"  ✅ {ds:<14} 源 {st['source_days']:>3} 天 | 新写 {st['written']:>3} "
                f"| 已存在跳过 {st['skipped_existing']:>3} | 空 {st['empty_payload']:>3}"
                f" | {st['first']} → {st['last']}"
            )

    if args.raw_only:
        print("\n（--raw-only：跳过标准化与日报）")
        return 0

    print("\n== 2/3 标准化入库 ==")
    st = standardize_all(adapter, args.start, args.end)
    print(
        f"  日期 {st['days']} | 标准化成功 {st['standardized']} "
        f"| 无数据 {st['no_data']} | 失败 {st['failed']}"
    )
    for e in st["errors"]:
        print(f"    ⚠ {e}")

    if args.with_reports:
        print("\n== 3/3 生成日报 ==")
        r = generate_reports(args.start, args.end)
        print(f"  日报成功 {r['reports']} | 失败 {r['failed']}")
        for e in r["errors"]:
            print(f"    ⚠ {e}")
    else:
        print("\n（跳过日报生成；加 --with-reports 可逐日生成）")

    print("\n== 数据完整性 ==")
    integ = _integrity(adapter, args.start, args.end)
    print(f"  日期并集: {integ['__union__']['days']} 天")
    for ds in ALL_DATASETS:
        i = integ[ds]
        if ds in SPARSE_DATASETS:
            # 非每日必有（体重非每日测量 / 休息日无活动），缺口属预期，不计入完整性判定
            note = "仅本地回填，非每日测量" if ds == "weight" else "休息日无活动属预期"
            print(f"  ℹ️ {ds:<14} 文件 {i['files']:>3} | {note}（缺口属预期）")
            continue
        flag = "✅" if i["missing_vs_union"] == 0 else "⚠️"
        print(
            f"  {flag} {ds:<14} 文件 {i['files']:>3} | 缺口 {i['missing_vs_union']:>3}"
            + (f" | 例: {', '.join(i['sample_missing'])}" if i["sample_missing"] else "")
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
