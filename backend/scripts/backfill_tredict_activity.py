"""P6：回填 ``tredict_activity`` + 与 Garmin 活动匹配。

三条产出
--------
1. ``data/raw/tredict/activity_list.json`` —— 全量活动清单（分页合并后的原样响应）
2. ``data/raw/tredict/activities/<id>.json`` —— 单条详情，**裁掉 seriesSampled.data**
3. ``data/raw/tredict/_match_report.json`` —— 逐条配对结论，可人工复核（Q13）

raw 层严格 append-only：已存在且 sha256 相同 → 复用；不同 → 报错不覆盖
（要重取请显式 ``--refresh``）。

为什么裁 ``seriesSampled.data``（Q14）
-------------------------------------
实测 298 条详情 16.93 MB，裁掉后 0.26 MB —— 逐点曲线占 **98.4%**。
P6 只做匹配与追溯，不需要曲线；需要时按 ``_id`` 回源重取。

匹配规则
--------
见 ``app/domain/matching.py`` 模块 docstring：时间为主键（±2s）、时长为校验、
游泳用 ``durationTotal`` 其余用 ``duration``、时长超差不判 ``none`` 只降 ``medium``。

用法：
    python scripts/backfill_tredict_activity.py --dry-run
    python scripts/backfill_tredict_activity.py
    python scripts/backfill_tredict_activity.py --refresh   # 强制重取全部详情
    python scripts/backfill_tredict_activity.py --verify
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.adapters.tredict import canonical_sha256  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import SyncLedgerRow, TredictActivityRow  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.domain.matching import (  # noqa: E402
    CONFIDENCE_HIGH,
    DURATION_TOLERANCE_SEC,
    TIME_TOLERANCE_SEC,
    GarminRef,
    MatchOutcome,
    match_activities,
    summarize,
    unmatched_garmin,
)
from app.domain.tredict_sync import (  # noqa: E402
    ACTIVITY_DIR,
    ENTITY,
    LIST_PATH,
    LOCAL_TZ_OFFSET,
    RAW_DIR,
    fetch_details,
    load_garmin_refs,
    to_tredict_ref,
    upsert_rows,
    write_if_absent,
)
from app.storage.activity_source import load_activities  # noqa: E402

MANIFEST_PATH = RAW_DIR / "asset_manifest.json"
REPORT_PATH = RAW_DIR / "_match_report.json"

DIRECTION = "tredict2local"


# ============================================================
# raw 层
# ============================================================
def build_manifest(hashes: dict[str, str]) -> dict[str, Any]:
    """与 Garmin ``asset_manifest.json`` 同构（Q16）。"""
    entries = []
    total_bytes = 0
    for aid in sorted(hashes):
        path = ACTIVITY_DIR / f"{aid}.json"
        size = path.stat().st_size if path.exists() else 0
        total_bytes += size
        entries.append(
            {
                "path": f"activities/{aid}.json",
                "sha256": hashes[aid],
                "bytes": size,
                "source": f"GET /activity/{aid}",
            }
        )
    if LIST_PATH.exists():
        entries.insert(
            0,
            {
                "path": LIST_PATH.name,
                "sha256": canonical_sha256(json.loads(LIST_PATH.read_text(encoding="utf-8"))),
                "bytes": LIST_PATH.stat().st_size,
                "source": "GET /activityList (paginated)",
            },
        )
        total_bytes += LIST_PATH.stat().st_size

    return {
        "schema": 1,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source_root": "https://www.tredict.com/api/oauth/v2",
        "target_root": str(RAW_DIR),
        "policy": "append-only (REQ-DATA-001)：已存在且 sha256 相同则跳过，不同则报错不覆盖",
        "note": "Q14：裁掉 seriesSampled.data（占 98.4% 体积），保留 sampleSize / endOffset",
        "total_files": len(entries),
        "total_bytes": total_bytes,
        "counts": {"activity_list": 1, "activities": len(hashes)},
        "entries": entries,
    }


# ============================================================
# 库层
# ============================================================
def apply_matches(outcomes: list[MatchOutcome], *, dry_run: bool) -> int:
    """把配对结论写回 ``tredict_activity``（Q6：不加反向列）。"""
    if dry_run:
        return 0
    updated = 0
    with session_scope() as s:
        for o in outcomes:
            row = s.get(TredictActivityRow, o.tredict_id)
            if row is None:
                continue
            row.matched_garmin_id = o.garmin_id
            row.match_confidence = o.confidence
            updated += 1
    return updated


# ============================================================
# 报告
# ============================================================
def build_report(
    outcomes: list[MatchOutcome],
    garmin_refs: list[GarminRef],
    garmin_total: int,
    details: dict[str, dict],
    failed: list[str],
) -> dict[str, Any]:
    by_id = {o.tredict_id: o for o in outcomes}
    garmin_only = unmatched_garmin(garmin_refs, outcomes)
    tredict_only = [o.tredict_id for o in outcomes if o.garmin_id is None]

    types = Counter(
        f"{p.get('sportType')}/{p.get('subSportType')}" for p in details.values()
    )

    return {
        "schema": 1,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "tolerance": {"time_sec": TIME_TOLERANCE_SEC, "duration_sec": DURATION_TOLERANCE_SEC},
        "duration_policy": "游泳取 durationTotal，其余取 duration（Q8）",
        "duration_compare": "原始浮点差值，未取整（Garmin 为 float 秒，Tredict 为整数秒）",
        "counts": {
            "garmin_total": garmin_total,
            "garmin_with_start_time": len(garmin_refs),
            "tredict_total": len(outcomes),
            "garmin_only": len(garmin_only),
            "tredict_only": len(tredict_only),
            "failed": len(failed),
            **summarize(outcomes),
        },
        "type_distribution": dict(sorted(types.items(), key=lambda kv: -kv[1])),
        "garmin_only": garmin_only,
        "tredict_only": tredict_only,
        "failed": failed,
        "pairs": [
            {
                "tredict_id": o.tredict_id,
                "garmin_id": o.garmin_id,
                "confidence": o.confidence,
                "detail": o.detail,
            }
            for o in sorted(outcomes, key=lambda x: x.tredict_id)
        ],
        "unmatched_detail": [
            {"tredict_id": k, "detail": by_id[k].detail} for k in tredict_only
        ],
    }


# ============================================================
# 主流程
# ============================================================
def load_raw_details(ids: list[str]) -> tuple[dict[str, dict], dict[str, str], list[str]]:
    """只读本地 raw 详情（不联网）→ ``(详情, 哈希, 缺详情的 id)``。

    ``--list-only`` 用它：日常只需「清单刷新 + 重算匹配」时，没必要为 301 条详情
    每条花 3 秒（实测 ``--refresh`` 全量 15 分 31 秒）。
    """
    details: dict[str, dict] = {}
    hashes: dict[str, str] = {}
    missing: list[str] = []
    for aid in ids:
        path = ACTIVITY_DIR / f"{aid}.json"
        if not path.exists():
            missing.append(aid)
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        details[aid] = payload
        hashes[aid] = canonical_sha256(payload)
    return details, hashes, missing


def run(dry_run: bool, refresh: bool, list_only: bool = False) -> int:
    from app.adapters.tredict import TredictAdapter
    from app.config import get_settings

    # 1) 活动清单（有缓存则复用；--list-only 强制取最新）
    if LIST_PATH.exists() and not refresh and not list_only:
        activities = json.loads(LIST_PATH.read_text(encoding="utf-8"))
        print(f"[list] 复用 {LIST_PATH.name}：{len(activities)} 条")
    elif dry_run and not list_only:
        if not LIST_PATH.exists():
            print("[dry-run] 无活动清单缓存，无法继续（先不带 --dry-run 跑一次）")
            return 1
        activities = json.loads(LIST_PATH.read_text(encoding="utf-8"))
        print(f"[list] 复用 {LIST_PATH.name}：{len(activities)} 条")
    else:
        activities = TredictAdapter(get_settings()).fetch_activity_list()
        write_if_absent(LIST_PATH, activities, refresh=True)
        print(f"[list] 抓取 {len(activities)} 条 → {LIST_PATH.name}")

    ids = [str(a["id"]) for a in activities if a.get("id")]
    if len(ids) != len(activities):
        print(f"[warn] 清单中 {len(activities) - len(ids)} 条缺 id，已忽略")

    # 2) 详情（逐条，单条失败不中断；--list-only 只读本地 raw）
    if list_only:
        details, hashes, missing_raw = load_raw_details(ids)
        failed = []
        print(f"[list-only] 复用本地详情 {len(details)} 条，不联网取详情")
        if missing_raw:
            print(f"  [warn] {len(missing_raw)} 条无本地详情，本次不参与匹配：{missing_raw[:10]}")
    else:
        details, hashes, failed = fetch_details(ids, refresh=refresh, dry_run=dry_run)

    if not dry_run:
        manifest = build_manifest(hashes)
        MANIFEST_PATH.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(
            f"[manifest] {manifest['total_files']} 个文件 / "
            f"{manifest['total_bytes'] / 1024 / 1024:.2f} MB"
        )

    # 3) 入库
    counts = upsert_rows(details, hashes, dry_run=dry_run)
    print(
        f"\n[db] 新增 {counts['insert']}  更新 {counts['update']}  "
        f"跳过 {counts['skip']}  无日期 {counts['no_date']}"
    )

    # 4) 匹配（每次全量重算，Q17）
    garmin_refs, garmin_total = load_garmin_refs(Path(get_settings().raw_data_dir) / "garmin")
    tredict_refs = [
        ref for aid in sorted(details) if (ref := to_tredict_ref(aid, details[aid]))
    ]
    outcomes = match_activities(garmin_refs, tredict_refs)
    updated = apply_matches(outcomes, dry_run=dry_run)
    stats = summarize(outcomes)
    print(
        f"[match] Garmin {garmin_total} 条 / Tredict {len(outcomes)} 条 → "
        f"high {stats['high']}  medium {stats['medium']}  "
        f"ambiguous {stats['ambiguous']}  none {stats['none']}  （写回 {updated}）"
    )

    garmin_only = unmatched_garmin(garmin_refs, outcomes)
    print(f"[match] Garmin 独有 {len(garmin_only)} 条：{garmin_only}")

    # 5) 报告
    report = build_report(outcomes, garmin_refs, garmin_total, details, failed)
    if dry_run:
        print("[dry-run] 未写库、未写报告")
        return 0
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"[report] → {REPORT_PATH.name}")

    for o in outcomes:
        if o.confidence != CONFIDENCE_HIGH:
            print(f"  [{o.confidence}] {o.tredict_id} → {o.garmin_id}  {o.detail}")

    return 0 if not failed else 1


# ============================================================
# 校验
# ============================================================
def verify() -> int:
    import sqlalchemy as sa
    from sqlmodel import select

    from app.db.session import engine

    problems = 0
    with session_scope() as s:
        rows = [
            {
                "tredict_id": r.tredict_id,
                "utc_datetime": r.utc_datetime,
                "local_date": r.local_date,
                "sport_type": r.sport_type,
                "sub_sport_type": r.sub_sport_type,
                "duration_sec": r.duration_sec,
                "duration_total_sec": r.duration_total_sec,
                "matched_garmin_id": r.matched_garmin_id,
                "match_confidence": r.match_confidence,
                "payload_sha256": r.payload_sha256,
            }
            for r in s.exec(select(TredictActivityRow)).all()
        ]
    garmin_ids = set(load_activities(Path(get_settings().raw_data_dir) / "garmin"))
    print(f"[verify] tredict_activity {len(rows)} 条；garmin_activity {len(garmin_ids)} 条")

    raw_files = sorted(ACTIVITY_DIR.glob("*.json"))
    print(f"[verify] raw 详情 {len(raw_files)} 个文件")
    if len(raw_files) != len(rows):
        print(f"  [数量不符] raw={len(raw_files)} 表={len(rows)}")
        problems += 1

    seen: set[str] = set()
    for r in rows:
        aid = r["tredict_id"]
        seen.add(aid)
        path = ACTIVITY_DIR / f"{aid}.json"
        if not path.exists():
            print(f"  [缺 raw] {aid}")
            problems += 1
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if canonical_sha256(payload) != r["payload_sha256"]:
            print(f"  [哈希不符] {aid}")
            problems += 1
        if "data" in (payload.get("seriesSampled") or {}):
            print(f"  [未裁剪] {aid} 仍含 seriesSampled.data")
            problems += 1
        if (r["utc_datetime"] + LOCAL_TZ_OFFSET).date() != r["local_date"]:
            print(f"  [local_date 错] {aid}")
            problems += 1
        if r["matched_garmin_id"] and r["matched_garmin_id"] not in garmin_ids:
            print(f"  [指向不存在的 Garmin] {aid} → {r['matched_garmin_id']}")
            problems += 1
        if r["match_confidence"] == CONFIDENCE_HIGH and not r["matched_garmin_id"]:
            print(f"  [high 但未配] {aid}")
            problems += 1

    extra = {p.stem for p in raw_files} - seen
    if extra:
        print(f"  [raw 多余] {sorted(extra)[:10]}")
        problems += len(extra)

    if REPORT_PATH.exists():
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        want = report["counts"]
        have = Counter(r["match_confidence"] for r in rows)
        for level in ("high", "medium", "ambiguous", "none"):
            if want.get(level) != have.get(level, 0):
                print(f"  [报告与库不符] {level} 报告={want.get(level)} 库={have.get(level, 0)}")
                problems += 1
        if len(report.get("garmin_only") or []) != want.get("garmin_only"):
            print("  [报告内部不一致] garmin_only 列表长度与计数不符")
            problems += 1
    else:
        print(f"  [缺报告] {REPORT_PATH}")
        problems += 1

    with engine.connect() as conn:
        # 只数自己这个方向：P8 会在同一 entity 上写 local2tredict 行，混在一起数会误报
        ledger = conn.execute(
            sa.select(sa.func.count())
            .select_from(SyncLedgerRow.__table__)
            .where(
                SyncLedgerRow.__table__.c.entity == ENTITY,
                SyncLedgerRow.__table__.c.direction == DIRECTION,
            )
        ).scalar()
    print(f"[verify] sync_ledger({ENTITY}/{DIRECTION}) {ledger} 条")
    if ledger != len(rows):
        print(f"  [ledger 数量不符] ledger={ledger} 表={len(rows)}")
        problems += 1

    print(f"[verify] 问题 {problems} 个")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 tredict_activity 并匹配 Garmin")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="强制重取清单与全部详情")
    ap.add_argument(
        "--list-only",
        action="store_true",
        help="只刷新活动清单并重算匹配，详情全用本地 raw（约 10 秒，供日任务使用）",
    )
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if args.verify:
        return 1 if verify() else 0
    return run(args.dry_run, args.refresh, args.list_only)


if __name__ == "__main__":
    sys.exit(main())
