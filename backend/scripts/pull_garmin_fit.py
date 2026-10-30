"""P3：Garmin 活动 FIT 拉取补齐（只读云端，append-only 落盘）。

背景
----
P2 已把既有 Node 管道的 298 个活动 FIT 迁入 ``data/raw/garmin/fit/``，
但本地活动清单止于 **2026-10-12**（``activity_meta/activities.json`` 实测），
云端仍有更新。P3 负责把差集补下来。

流程
----
1. 登录（复用 ``GarminAdapter.client()``：tokenstore 优先，必要时账号密码）
2. 分页拉活动列表（``get_activities``，单页上限 1000）
3. 差集 = 云端 activityId 集合 − 本地 ``fit/*.fit`` 文件名集合
4. 逐个下载 ZIP（``ActivityDownloadFormat.ORIGINAL``）→ 解出 ``.fit``
5. 校验（FIT 头 + fitdecode 至少解出 1 条消息）→ 原子落盘
6. 追加进 ``asset_manifest.json``（``origin="cloud"``）
7. 写拉取报告到 ``data/raw/garmin/_pull_reports/<时间戳>.json``

安全性
------
- **append-only**：目标 ``.fit`` 已存在则跳过，绝不覆盖（REQ-DATA-001）
- **先校验后落盘**：解压失败 / 头不合法 / 解析 0 条消息 → 不写文件，计入 failed
- **不动本地清单**：``activities.json`` 是 Node 管道的产物，本脚本只读不写

用法
----
    python scripts/pull_garmin_fit.py --dry-run          # 只列差集
    python scripts/pull_garmin_fit.py                    # 下载全部缺失
    python scripts/pull_garmin_fit.py --since 2026-10-01 # 只看该日之后
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import fitdecode

sys.path.insert(0, str(Path(__file__).resolve().parent))
from migrate_fit_assets import (  # noqa: E402
    TARGET_ROOT,
    load_manifest_index,
    save_manifest_index,
    sha256_of,
)

FIT_DIR = TARGET_ROOT / "fit"
REPORT_DIR = TARGET_ROOT / "_pull_reports"
ACTIVITY_META = TARGET_ROOT / "activity_meta" / "activities.json"

PAGE_SIZE = 1000
DOWNLOAD_INTERVAL_SEC = 0.5


def load_local_activity_ids() -> set[str]:
    """本地活动 id 集合（来自 Node 管道的活动清单，只读）。"""
    if not ACTIVITY_META.exists():
        return set()
    raw = json.loads(ACTIVITY_META.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return set()
    return {str(a["activityId"]) for a in raw if isinstance(a, dict) and a.get("activityId")}


def load_local_fit_ids() -> set[str]:
    """本地已有 FIT 的 activityId 集合。"""
    if not FIT_DIR.is_dir():
        return set()
    return {p.stem for p in FIT_DIR.glob("*.fit")}


def fetch_all_activities(client: Any) -> list[dict]:
    """分页拉全部活动。"""
    out: list[dict] = []
    start = 0
    while True:
        batch = client.get_activities(start, PAGE_SIZE)
        if not batch:
            break
        out.extend(batch)
        print(f"  已拉 {len(out)} 条（start={start}）")
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return out


def extract_fit(zip_bytes: bytes) -> tuple[str, bytes]:
    """从 Garmin 下载的 ZIP 中解出唯一的 .fit。"""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".fit")]
        if not names:
            raise ValueError(f"ZIP 内无 .fit（条目：{z.namelist()[:5]}）")
        if len(names) > 1:
            raise ValueError(f"ZIP 内含多个 .fit：{names}")
        return names[0], z.read(names[0])


def validate_fit(fit_bytes: bytes) -> int:
    """校验 FIT 结构，返回数据消息条数。"""
    if len(fit_bytes) < 14:
        raise ValueError(f"文件过短：{len(fit_bytes)} 字节")
    if fit_bytes[8:12] != b".FIT":
        raise ValueError(f"FIT 头不合法：{fit_bytes[8:12]!r}")
    n = 0
    with fitdecode.FitReader(
        io.BytesIO(fit_bytes), check_crc=fitdecode.CrcCheck.DISABLED
    ) as fit:
        for frame in fit:
            if isinstance(frame, fitdecode.FitDataMessage):
                n += 1
    if n == 0:
        raise ValueError("解析出 0 条数据消息")
    return n


def save_fit(activity_id: str, fit_bytes: bytes) -> Path:
    """原子写入（调用方已保证目标不存在）。"""
    FIT_DIR.mkdir(parents=True, exist_ok=True)
    dst = FIT_DIR / f"{activity_id}.fit"
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(fit_bytes)
    os.replace(tmp, dst)
    return dst


def manifest_entry(path: Path, activity_id: str) -> dict:
    st = path.stat()
    return {
        "path": f"fit/{path.name}",
        "sha256": sha256_of(path),
        "bytes": st.st_size,
        "mtime": dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "source": f"garmin:/download-service/files/activity/{activity_id}",
        "origin": "cloud",
    }


def write_report(report: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    p = REPORT_DIR / f"{ts}.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="P3 Garmin 活动 FIT 拉取补齐")
    ap.add_argument("--dry-run", action="store_true", help="只列差集，不下载")
    ap.add_argument("--since", help="只处理该日期（YYYY-MM-DD）之后开始的活动")
    ap.add_argument("--limit", type=int, help="最多下载多少个（调试用）")
    args = ap.parse_args(argv)

    since = dt.date.fromisoformat(args.since) if args.since else None

    from app.adapters.garmin import GarminAdapter
    from app.config import get_settings

    adapter = GarminAdapter(get_settings())
    print("[auth] 登录 Garmin …")
    client = adapter.client()
    print("[auth] OK")

    print("[list] 拉取云端活动列表 …")
    activities = fetch_all_activities(client)
    print(f"[list] 云端共 {len(activities)} 条")

    cloud: dict[str, dict] = {}
    for a in activities:
        if not isinstance(a, dict) or not a.get("activityId"):
            continue
        aid = str(a["activityId"])
        if since:
            started = str(a.get("startTimeLocal") or "")[:10]
            if not started or started < since.isoformat():
                continue
        cloud[aid] = a

    local_ids = load_local_activity_ids()
    local_fit = load_local_fit_ids()
    missing = sorted(set(cloud) - local_fit)

    print()
    print(f"云端活动（筛选后）  {len(cloud)}")
    print(f"本地活动清单        {len(local_ids)}")
    print(f"本地已有 FIT        {len(local_fit)}")
    print(f"缺失 FIT            {len(missing)}")
    if local_ids - set(cloud):
        only_local = sorted(local_ids - set(cloud))
        print(f"[WARN] 本地清单有 {len(only_local)} 个 id 不在云端返回中：{only_local[:5]}")

    if missing:
        print("\n缺失明细（最多显示 20 条）：")
        for aid in missing[:20]:
            a = cloud[aid]
            print(f"  {aid}  {str(a.get('startTimeLocal'))[:19]}  "
                  f"{(a.get('activityType') or {}).get('typeKey', '?')}  "
                  f"{a.get('activityName', '')}")
        if len(missing) > 20:
            print(f"  … 另有 {len(missing) - 20} 条")

    report = {
        "pulled_at": dt.datetime.now().isoformat(timespec="seconds"),
        "since": since.isoformat() if since else None,
        "cloud_total": len(cloud),
        "local_activity_ids": len(local_ids),
        "local_fit_files": len(local_fit),
        "missing": missing,
        "downloaded": [],
        "skipped": [],
        "failed": [],
    }

    if args.dry_run or not missing:
        print("\n[dry-run] 未下载、未写 manifest")
        if not args.dry_run:
            print("[done] 无缺失，无需下载")
        return 0

    targets = missing[: args.limit] if args.limit else missing
    print(f"\n[download] 开始下载 {len(targets)} 个 …")

    fmt = client.ActivityDownloadFormat.ORIGINAL
    for i, aid in enumerate(targets, 1):
        dst = FIT_DIR / f"{aid}.fit"
        if dst.exists():  # append-only 双保险
            report["skipped"].append(aid)
            continue
        try:
            blob = client.download_activity(aid, fmt)
            inner, fit_bytes = extract_fit(blob)
            n_msgs = validate_fit(fit_bytes)
            path = save_fit(aid, fit_bytes)
            report["downloaded"].append(
                {"activity_id": aid, "inner": inner, "bytes": path.stat().st_size,
                 "messages": n_msgs}
            )
            print(f"  [{i}/{len(targets)}] {aid}  {path.stat().st_size / 1024:.0f} KB  "
                  f"{n_msgs} 条消息")
        except Exception as e:  # noqa: BLE001 — 单个失败不阻断整体
            report["failed"].append({"activity_id": aid, "error": f"{type(e).__name__}: {e}"})
            print(f"  [{i}/{len(targets)}] {aid}  [FAIL] {type(e).__name__}: {e}")
        time.sleep(DOWNLOAD_INTERVAL_SEC)

    # 更新 manifest（仅成功项）
    if report["downloaded"]:
        index = load_manifest_index()
        for item in report["downloaded"]:
            p = FIT_DIR / f"{item['activity_id']}.fit"
            index[f"fit/{p.name}"] = manifest_entry(p, item["activity_id"])
        save_manifest_index(
            index,
            note=(
                "E29：fit_json 全量归档（不参与解析）；"
                f"Q32=B：解析以 .fit 为唯一真源；其中 {len(index)} 条含 P3 云端拉取"
            ),
        )

    rp = write_report(report)
    print()
    print(f"下载成功 {len(report['downloaded'])}  跳过 {len(report['skipped'])}  "
          f"失败 {len(report['failed'])}")
    print(f"报告：{rp}")
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
