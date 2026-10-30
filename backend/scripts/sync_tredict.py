"""Tredict 上传：**读回差集 → 只传缺失 → 回读校验**（V6 拉取 + 上传正确性）。

为什么必须先读回
----------------
Tredict 的 `/hrv` 与 `/bodyvalues` 是**纯追加**端点（同一 timestamp 重复 POST 会累积记录），
只有 `/sleep` 走「先删后传」保证幂等。因此直接全量重传会在 HRV / 身体数据里制造重复。
本脚本先 `fetch_health()` 读回云端现状，按日期/时间戳做差集，**只上传缺失项**。

差集口径
--------
- HRV        ：按 `ymd(timestamp)` 与云端 `YYYYMMDD` 键比对
- 睡眠       ：⚠️ 云端按 **endTimestamp 的 GMT 日期** 键控，而非 startTimestamp！
              故差集必须用 `ymd(endTimestamp)` 比对（不能用 start 日期）；
              用 start 日期会误判「跨日夜间」为缺失而重复上传，
              或漏传 start 日键已存在、但 end 日键缺失的夜间
              （跨日睡眠：start 落前一日、end 落后一日）。
- 身体       ：按 `timestamp` 前 19 位与云端记录比对

用法
----
    python scripts/sync_tredict.py               # 只上传缺失项（默认）
    python scripts/sync_tredict.py --dry-run     # 只算差集，不写入
    python scripts/sync_tredict.py --all         # 忽略差集，上传全量（有重复风险，慎用）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.adapters.garmin import load_raw_dataset, load_raw_weight_list
from app.adapters.tredict import (
    BODYVALUES_URL,
    HRV_URL,
    SLEEP_URL,
    TredictAdapter,
    bodyvalues_todo,
    build_bodyvalue_records,
    build_hrv_records,
    build_sleep_records,
    ymd,
)
from app.config import get_settings


def main() -> int:
    ap = argparse.ArgumentParser(description="Tredict 上传（读回差集，只传缺失）")
    ap.add_argument("--dry-run", action="store_true", help="只算差集，不写入")
    ap.add_argument("--all", action="store_true", help="忽略差集上传全量（有重复风险）")
    args = ap.parse_args()

    settings = get_settings()
    adapter = TredictAdapter(settings)
    raw_root = Path(settings.raw_data_dir) / "garmin"

    print("== 1/4 从本地 raw 构建记录 ==")
    hrv_raw = load_raw_dataset(raw_root, "hrv")
    sleep_raw = load_raw_dataset(raw_root, "sleep")
    daily_raw = load_raw_dataset(raw_root, "daily_summary")
    weight_list = load_raw_weight_list(raw_root)
    print(f"  hrv {len(hrv_raw)} 天 | sleep {len(sleep_raw)} 天 "
          f"| daily {len(daily_raw)} 天 | weight {len(weight_list)} 条")

    hrv_records, hrv_null, hrv_notime = build_hrv_records(hrv_raw)
    sleep_records, slp_skip, slp_future = build_sleep_records(sleep_raw)
    bv_records, bv_meta = build_bodyvalue_records(daily_raw, weight_list)
    print(f"  构建：hrv {len(hrv_records)}（跳过 空值{hrv_null}/无时间{hrv_notime}）")
    print(f"        sleep {len(sleep_records)}（跳过 {slp_skip}/未来 {slp_future}）")
    print(f"        bodyvalues {len(bv_records)}（含体重 {bv_meta['weightCount']}，"
          f"{bv_meta['earliest']}~{bv_meta['latest']}）")

    print("\n== 2/4 读回云端现状 ==")
    cloud = adapter.fetch_health()
    cloud_hrv: dict = cloud.get("hrv") or {}
    cloud_sleep: dict = cloud.get("sleep") or {}
    cloud_bv: list = cloud.get("bodyvalues") or []
    print(
        f"  云端：hrv {len(cloud_hrv)} 天 | sleep {len(cloud_sleep)} 天 "
        f"| bodyvalues {len(cloud_bv)} 条"
    )

    if args.all:
        todo_hrv, todo_sleep, todo_bv = hrv_records, sleep_records, bv_records
        print("\n  ⚠ --all：忽略差集，上传全量")
    else:
        have_hrv = set(cloud_hrv)
        have_sleep = set(cloud_sleep)
        todo_hrv = [r for r in hrv_records if ymd(r["timestamp"]) not in have_hrv]
        # 睡眠云端按 endTimestamp 的 GMT 日期键控，差集必须用 end 日期比对（见文件头「差集口径」）
        todo_sleep = [r for r in sleep_records if ymd(r["endTimestamp"]) not in have_sleep]
        # bodyvalues 必须**比内容**：体重是加进已有记录的，键不变（2026-10-21 真实故障）
        todo_bv = bodyvalues_todo(bv_records, cloud_bv)
        print(
            f"\n  差集：hrv 缺 {len(todo_hrv)} | sleep 缺 {len(todo_sleep)} "
            f"| bodyvalues 缺 {len(todo_bv)}"
        )

    if not (todo_hrv or todo_sleep or todo_bv):
        print("\n[OK] 云端已与本地一致，无需上传。")
        return 0

    for label, recs in (("hrv", todo_hrv), ("sleep", todo_sleep), ("bodyvalues", todo_bv)):
        if recs:
            print(f"    待传 {label}: {recs[0]} ... {recs[-1]}")

    if args.dry_run:
        print("\n[--dry-run] 不写入。")
        return 0

    print("\n== 3/4 上传缺失项 ==")
    total_ok = total_fail = 0
    errors: list[str] = []

    if todo_hrv:
        r = adapter._post_batch(HRV_URL, "hrv", todo_hrv)  # noqa: SLF001
        print(f"  hrv       成功 {r['ok']} / 失败 {r['fail']}")
        total_ok += r["ok"]
        total_fail += r["fail"]
        errors += r["errors"]

    if todo_sleep:
        if args.all:
            # 全量模式：走「先删后传」保证幂等（与旧脚本默认行为一致）
            s = adapter.sync_sleep(todo_sleep)
            purged = s.get("deletedCount", 0)
            up = s.get("upload") or {"ok": 0, "fail": 0, "errors": []}
            print(f"  sleep     删除范围内旧记录 {purged} 条 → 成功 {up['ok']} / 失败 {up['fail']}")
        else:
            # 差集模式：**绝不能**用 sync_sleep！
            # sleep_purge_range() 取待传记录的 min(end)~max(end) 作为删除范围，
            # 稀疏记录（如跨 2025-11-22 ~ 2026-10-14 的 2 条）会算出覆盖整段历史的范围，
            # 从而误删中间全部睡眠记录（本项目的 sync_sleep 又未做备份）。
            # 差集里的日期本就是云端缺失的，纯追加即可，无需任何删除。
            r = adapter._post_batch(SLEEP_URL, "sleep", todo_sleep)  # noqa: SLF001
            up = r
            print(f"  sleep     纯追加（差集模式不删除）→ 成功 {r['ok']} / 失败 {r['fail']}")
        total_ok += up["ok"]
        total_fail += up["fail"]
        errors += up["errors"]

    if todo_bv:
        r = adapter._post_batch(BODYVALUES_URL, "bodyvalues", todo_bv)  # noqa: SLF001
        print(f"  bodyvalues 成功 {r['ok']} / 失败 {r['fail']}")
        total_ok += r["ok"]
        total_fail += r["fail"]
        errors += r["errors"]

    print(f"\n  合计：成功 {total_ok} / 失败 {total_fail}")
    for e in errors[:5]:
        print(f"    ⚠ {e}")

    print("\n== 4/4 回读校验 ==")
    after = adapter.fetch_health()
    print(f"  云端：hrv {len(after.get('hrv') or {})} 天（原 {len(cloud_hrv)}）"
          f" | sleep {len(after.get('sleep') or {})} 天（原 {len(cloud_sleep)}）"
          f" | bodyvalues {len(after.get('bodyvalues') or [])} 条（原 {len(cloud_bv)}）")

    if total_fail:
        print("\n[FAIL] 存在失败项，见上方 errors。")
        return 1
    print("\n[OK] 上传完成，无失败项。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
