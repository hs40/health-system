"""P7：同步校验 CLI（Garmin / 本地 raw / 本地 DB / Tredict 四方一致性巡检）。

与三个旧 ``--verify`` 的分工（Q6=A）
-----------------------------------
| 脚本 | 回答的问题 | 何时跑 |
|---|---|---|
| ``parse_fit.py --verify`` | 这次解析写对了吗 | 回填后立刻 |
| ``backfill_garmin_activity.py --verify`` | 这次回填写对了吗 | 回填后立刻 |
| ``backfill_tredict_activity.py --verify`` | 这次回填+匹配写对了吗 | 回填后立刻 |
| **``verify_sync.py``** | **现在整体还对不对** | 随时 / cron |

前三个是「写入自检」，生命周期跟着回填走；本脚本是「长期巡检」，可反复执行。
两者共用 ``app/domain/verify.py`` 的比较逻辑（V=B），避免口径漂移。

校验范围（Q2=B）
----------------
| 实体 | 条数 | 校验内容 |
|---|---:|---|
| ``garmin_activity`` | 301 | payload 哈希 + laps/sets/zones 数量对归档 |
| ``garmin_activity_record`` | 301（活动级） | 542,977 逐秒行全量比对（Q3=A） |
| ``tredict_activity`` | 298 | 哈希 + local_date + 匹配指向 |
| ``standard_daily`` | 472 | 472 天全量重算逐字段比（H1=A） |

默认**离线**（Q1=B）；``--online`` 额外拉两边活动清单比 id 与字段值，
并补上「本地健康 raw ↔ Tredict 云端健康」的键集合比对（原盲区，见 ``verify_online_health``）。

输出（R=A / E=A+C）
-------------------
- ``data/audit/verify-<YYYYMMDD-HHMM>.json`` —— **只列问题**，正常时几 KB
- ``audit_event`` 一条汇总（category=dataSource）
- 退出码：``mismatch`` / ``missing_*`` → 1；``ambiguous`` 只警告（Q5=A）；
  ``--online`` 发现差异（仅本地 / 仅云端 / 字段不符）同样 → 1

用法：
    python scripts/verify_sync.py                  # 离线全量
    python scripts/verify_sync.py --online         # 加在线清单比对
    python scripts/verify_sync.py --fast           # 跳过 542,977 行逐秒比对
    python scripts/verify_sync.py --no-write       # 只看结果，不写 ledger/报告
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sys
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.config import get_settings  # noqa: E402
from app.domain.verify import (  # noqa: E402
    FAIL_STATES,
    STANDARD_DAILY_FIELDS,
    STATE_AMBIGUOUS,
    STATE_MISMATCH,
    STATE_MISSING_LOCAL,
    STATE_MISSING_REMOTE,
    STATE_OK,
    CheckResult,
    check_standard_daily,
    check_tredict_activity,
    code_fingerprint,
    compare_online_garmin,
    compare_online_health,
    compare_online_tredict,
    exit_code,
    summarize,
)

RAW = ROOT / "data" / "raw"
GARMIN_RAW = RAW / "garmin"
TREDICT_RAW = RAW / "tredict"
FIT_DIR = GARMIN_RAW / "fit"
JSONL_DIR = GARMIN_RAW / "fit_jsonl"
ACTIVITY_DIR = GARMIN_RAW / "activities"
AUDIT_DIR = ROOT / "data" / "audit"

TZ_OFFSET = dt.timedelta(hours=8)

DIRECTION_BY_ENTITY = {
    "garmin_activity": "garmin2local",
    "garmin_activity_record": "garmin2local",
    "tredict_activity": "tredict2local",
    "standard_daily": "garmin2local",
}

NOTES = [
    "Garmin 活动 payload 有 105 字段，其中 17 个为旧 Node 管道派生值，"
    "线上接口不可复现 → 在线只比 88 个共同字段的值，不比哈希",
    "健康 raw 无 sha（不在 asset_manifest 中），source_refs.fetched_at 为文件 mtime，"
    "跨机不可比 → 只验引用文件存在性",
    "逐秒表校验以 jsonl.gz 归档为基准，用与 P4 相同的 _build_records 复现 seq/t/7 通道"
    "（归档里 timestamp 是 ISO 字符串，需先还原成 datetime）",
]


# ============================================================
# 归档读取（单次遍历，内存只留一个活动）
# ============================================================
def scan_archive(path: Path) -> tuple[int, int, int, list[dict[str, Any]]]:
    """读一个 ``jsonl.gz`` → ``(laps, sets, zones, record_rows)``。

    只遍历一次：P5 需要 lap/set/zone 的**内容**，P7 只需要**数量**，
    而逐秒行需要**原文**交给 ``_build_records`` 复现 seq。
    """
    laps = sets = zones = 0
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            obj = json.loads(line)
            name = obj.get("m")
            if name == "record":
                rows.append(obj["d"])
            elif name == "lap":
                laps += 1
            elif name == "set":
                sets += 1
            elif name == "time_in_zone":
                zones += 1
    return laps, sets, zones, rows


def _tuples(points: Any) -> list[tuple[Any, ...]]:
    return [
        (p.seq, p.t, p.hr, p.speed, p.power, p.cadence, p.altitude, p.distance)
        for p in points
    ]


def load_local_activity_payloads() -> dict[str, dict[str, Any]]:
    """``activities/<date>.json`` 里按 activityId 铺平（一天可能多活动）。"""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(ACTIVITY_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[warn] 跳过损坏文件 {path.name}")
            continue
        items = data if isinstance(data, list) else (data.get("activities") or [])
        for item in items:
            aid = item.get("activityId")
            if aid is not None:
                out[str(aid)] = item
    return out


def _len_or_none(value: Any) -> int | None:
    return len(value) if value is not None else None


# ============================================================
# 1. Garmin 活动链路
# ============================================================
def verify_garmin(*, fast: bool) -> list[CheckResult]:
    """Garmin 活动链路：**raw ↔ FIT 文件覆盖**（2026-10-21 表废弃后重写）。

    原先还比「表列 vs FIT 归档计数」「逐秒行 vs garmin_activity_record」——
    这些比对的对象是已废弃的 ``garmin_activity`` 表，随表一起退役。
    权威的在线三环校验见 ``verify_cloud.py``（raw↔云端 / raw↔Tredict / 云端↔云端）。
    """
    from app.storage.activity_source import load_activities

    t0 = time.perf_counter()
    raw_root = Path(get_settings().raw_data_dir) / "garmin"
    acts = load_activities(raw_root)
    fit_ids = {p.stem.split("_")[0] for p in FIT_DIR.glob("*.fit")}

    print(f"[garmin] raw 活动 {len(acts)} / FIT {len(fit_ids)}")
    results: list[CheckResult] = []

    # raw 有、FIT 没有 → missing_remote（FIT 未拉）；FIT 有、raw 没有 → missing_local
    for aid in sorted(set(acts) - fit_ids):
        results.append(
            CheckResult(
                "garmin_activity", aid, STATE_MISSING_REMOTE, "", {"problems": {"fit": "missing"}}
            )
        )
    for aid in sorted(fit_ids - set(acts)):
        results.append(
            CheckResult(
                "garmin_activity", aid, STATE_MISSING_LOCAL, "", {"problems": {"raw": "missing"}}
            )
        )

    print(f"[garmin] 校验 {len(results)} 条实体  {time.perf_counter() - t0:.1f}s")
    return results


# ============================================================
# 2. Tredict 链路
# ============================================================
def verify_tredict() -> list[CheckResult]:
    from sqlmodel import select

    from app.db.models import TredictActivityRow
    from app.db.session import session_scope
    from app.storage.activity_source import load_activities

    t0 = time.perf_counter()
    results: list[CheckResult] = []

    with session_scope() as s:
        snapshot = [
            {
                "tredict_id": r.tredict_id,
                "utc_datetime": r.utc_datetime,
                "local_date": r.local_date,
                "matched_garmin_id": r.matched_garmin_id,
                "match_confidence": r.match_confidence,
                "payload_sha256": r.payload_sha256,
            }
            for r in s.exec(select(TredictActivityRow)).all()
        ]
    garmin_ids = set(load_activities(Path(get_settings().raw_data_dir) / "garmin"))

    for item in snapshot:
        aid = item["tredict_id"]
        path = TREDICT_RAW / "activities" / f"{aid}.json"
        if not path.exists():
            results.append(
                CheckResult(
                    "tredict_activity",
                    aid,
                    STATE_MISSING_REMOTE,
                    "",
                    {"problems": {"raw": "missing"}},
                )
            )
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        results.append(
            check_tredict_activity(
                aid,
                payload,
                item["payload_sha256"],
                utc_datetime=item["utc_datetime"],
                local_date=item["local_date"],
                matched_garmin_id=item["matched_garmin_id"],
                match_confidence=item["match_confidence"],
                garmin_ids=garmin_ids,
                tz_offset=TZ_OFFSET,
            )
        )

    raw_ids = {p.stem for p in (TREDICT_RAW / "activities").glob("*.json")}
    known = {i["tredict_id"] for i in snapshot}
    for aid in sorted(raw_ids - known):
        results.append(
            CheckResult(
                "tredict_activity", aid, STATE_MISSING_LOCAL, "", {"problems": {"row": "missing"}}
            )
        )

    print(f"[tredict] 校验 {len(results)} 条实体  {time.perf_counter() - t0:.1f}s")
    return results


# ============================================================
# 3. 健康链路
# ============================================================
def verify_health() -> list[CheckResult]:
    from sqlmodel import select

    from app.adapters.garmin import GarminAdapter
    from app.config import get_settings
    from app.db.models import StandardDailyRow
    from app.db.session import session_scope

    t0 = time.perf_counter()
    adapter = GarminAdapter(get_settings())
    results: list[CheckResult] = []

    with session_scope() as s:
        snapshot = [
            {"date": r.date, **{f: getattr(r, f) for f in STANDARD_DAILY_FIELDS}}
            for r in s.exec(select(StandardDailyRow)).all()
        ]

    for item in snapshot:
        day = item["date"]
        try:
            recomputed = adapter.standardize(day).model_dump()
        except Exception as exc:  # noqa: BLE001 —— 单天异常不中断整批
            results.append(
                CheckResult(
                    "standard_daily",
                    day.isoformat(),
                    STATE_MISMATCH,
                    "",
                    {"problems": {"standardize": f"{type(exc).__name__}: {exc}"}},
                )
            )
            continue
        results.append(check_standard_daily(day, item, recomputed))

    # raw 有整天数据、表里却没这一行 → missing_local
    known = {i["date"] for i in snapshot}
    raw_days = {
        dt.date.fromisoformat(p.stem)
        for p in (GARMIN_RAW / "daily_summary").glob("*.json")
    }
    for day in sorted(raw_days - known):
        results.append(
            CheckResult(
                "standard_daily",
                day.isoformat(),
                STATE_MISSING_LOCAL,
                "",
                {"problems": {"row": "missing"}},
            )
        )

    print(f"[health] 校验 {len(results)} 条实体  {time.perf_counter() - t0:.1f}s")
    return results


# ============================================================
# 4. 在线比对
# ============================================================
def verify_online_garmin() -> tuple[dict[str, Any], bool]:
    from app.adapters.garmin import GarminAdapter
    from app.config import get_settings

    try:
        remote = GarminAdapter(get_settings()).client().get_activities(0, 1000)
    except Exception as exc:  # noqa: BLE001
        print(f"[online:garmin] 失败 {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}, True

    out = compare_online_garmin(remote, load_local_activity_payloads())
    print(
        f"[online:garmin] 远端 {out['remote']} / 本地 {out['local']}  "
        f"仅远端 {len(out['only_remote'])}  仅本地 {len(out['only_local'])}  "
        f"字段不符 {len(out['field_mismatch'])}  不可验证字段 {len(out['unverifiable_fields'])}"
    )
    return out, False


def verify_online_tredict() -> tuple[dict[str, Any], bool]:
    from sqlmodel import select

    from app.adapters.tredict import TredictAdapter, parse_utc_iso
    from app.config import get_settings
    from app.db.models import TredictActivityRow
    from app.db.session import session_scope

    try:
        remote = TredictAdapter(get_settings()).fetch_activity_list()
    except Exception as exc:  # noqa: BLE001
        print(f"[online:tredict] 失败 {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}, True

    with session_scope() as s:
        local = {
            r.tredict_id: {
                "utc_datetime": r.utc_datetime,
                "sport_type": r.sport_type,
                "sub_sport_type": r.sub_sport_type,
            }
            for r in s.exec(select(TredictActivityRow)).all()
        }

    out = compare_online_tredict(remote, local, parse_utc=parse_utc_iso)
    print(
        f"[online:tredict] 远端 {out['remote']} / 本地 {out['local']}  "
        f"仅远端 {len(out['only_remote'])}  仅本地 {len(out['only_local'])}  "
        f"字段不符 {len(out['field_mismatch'])}"
    )
    return out, False


def verify_online_health() -> tuple[dict[str, Any], bool]:
    """本地健康 raw ↔ Tredict 云端健康（键集合级）。

    这是 P7 原先的盲区：离线校验只比「本地库 ↔ 本地 raw」，在线校验只比活动清单，
    于是「本地有、云端没有」这类滞后在 1,375 个校验实体里**一条都发现不了**
    —— 2026-10-15~17 缺 RMSSD 正是从这条缝里漏过去的。

    本地侧用与 ``sync_tredict.py`` **完全相同**的构建函数（``build_*_records``）
    算出「应该有哪些键」，否则差异会来自口径而不是数据。
    """
    from app.adapters.garmin import load_raw_dataset, load_raw_weight_list
    from app.adapters.tredict import (
        TredictAdapter,
        build_bodyvalue_records,
        build_hrv_records,
        build_sleep_records,
        norm_ts,
        ymd,
    )
    from app.config import get_settings

    settings = get_settings()
    try:
        cloud = TredictAdapter(settings).fetch_health()
    except Exception as exc:  # noqa: BLE001
        print(f"[online:health] 失败 {type(exc).__name__}: {exc}")
        return {"error": f"{type(exc).__name__}: {exc}"}, True

    raw_root = Path(settings.raw_data_dir) / "garmin"
    hrv_records, _, _ = build_hrv_records(load_raw_dataset(raw_root, "hrv"))
    sleep_records, _, _ = build_sleep_records(load_raw_dataset(raw_root, "sleep"))
    bv_records, _ = build_bodyvalue_records(
        load_raw_dataset(raw_root, "daily_summary"), load_raw_weight_list(raw_root)
    )

    local = {
        "hrv": {ymd(r["timestamp"]) for r in hrv_records},
        # 云端按 endTimestamp 的 GMT 日期键控（见 sync_tredict.py 文件头「差集口径」）
        "sleep": {ymd(r["endTimestamp"]) for r in sleep_records},
        "bodyvalues": {norm_ts(r["timestamp"]) for r in bv_records},
    }
    remote = {
        "hrv": set(cloud.get("hrv") or {}),
        "sleep": set(cloud.get("sleep") or {}),
        "bodyvalues": {
            norm_ts(r.get("timestamp", ""))
            for r in (cloud.get("bodyvalues") or [])
            if isinstance(r, dict)
        },
    }

    out = compare_online_health(local, remote)
    for kind, info in out["kinds"].items():
        print(
            f"[online:health:{kind}] 本地 {info['local']} / 云端 {info['remote']}  "
            f"仅本地 {len(info['only_local'])}  仅云端 {len(info['only_remote'])}"
        )
        if info["only_local"]:
            print(f"  [WARN] 仅本地（滞后未上传）：{info['only_local'][:5]}")
        if info["only_remote"]:
            print(f"  [WARN] 仅云端（本地 raw 缺失）：{info['only_remote'][:5]}")
    return out, False


def _online_diff(online: dict[str, Any]) -> int:
    """在线比对发现的可疑项总数（用于退出码）。"""
    total = 0
    for block in online.values():
        if not isinstance(block, dict) or "error" in block:
            continue
        total += len(block.get("only_local") or [])
        total += len(block.get("only_remote") or [])
        total += len(block.get("field_mismatch") or [])
        for info in (block.get("kinds") or {}).values():
            total += len(info.get("only_local") or [])
            total += len(info.get("only_remote") or [])
    return total


# ============================================================
# 5. ledger 回写（S=B：状态覆盖 + first_failed_at 语义化）
# ============================================================
def write_ledger(results: list[CheckResult]) -> dict[str, int]:
    from sqlmodel import select

    from app.db.models import SyncLedgerRow
    from app.db.session import session_scope

    now = dt.datetime.now()
    counts = {"insert": 0, "update": 0, "resolved": 0}

    with session_scope() as s:
        existing = {
            (r.entity, r.entity_id, r.direction): r
            for r in s.exec(select(SyncLedgerRow)).all()
        }

        for result in results:
            direction = DIRECTION_BY_ENTITY.get(result.entity, "garmin2local")
            row = existing.get((result.entity, result.entity_id, direction))

            if row is None:
                s.add(
                    SyncLedgerRow(
                        entity=result.entity,
                        entity_id=result.entity_id,
                        direction=direction,
                        local_hash=result.local_hash,
                        remote_hash="",
                        state=result.state,
                        first_failed_at=now if result.state in FAIL_STATES else None,
                        last_checked_at=now,
                        attempts=1,
                        detail={"verify": result.detail},
                    )
                )
                counts["insert"] += 1
                continue

            detail = dict(row.detail or {})
            if result.state == STATE_OK and row.state in FAIL_STATES:
                detail["resolved_at"] = now.isoformat(timespec="seconds")
                counts["resolved"] += 1
            detail["verify"] = result.detail
            row.detail = detail
            row.state = result.state
            row.local_hash = result.local_hash
            row.last_checked_at = now
            row.attempts += 1
            row.first_failed_at = (
                None if result.state == STATE_OK else (row.first_failed_at or now)
            )
            counts["update"] += 1

    return counts


# ============================================================
# 6. 报告
# ============================================================
def build_report(
    results: list[CheckResult],
    *,
    mode: str,
    duration: float,
    timings: dict[str, float],
    online: dict[str, Any] | None,
    online_failed: bool,
    online_diff: int,
    fast: bool,
) -> dict[str, Any]:
    stats = summarize(results)
    problems = [
        {
            "entity": r.entity,
            "entity_id": r.entity_id,
            "state": r.state,
            "detail": r.detail,
        }
        for r in results
        if r.state != STATE_OK
    ]
    notes = list(NOTES)
    if fast:
        notes.append("--fast：已跳过 542,977 行逐秒比对，本报告的「0 problems」不覆盖逐秒表")
    if online_failed:
        notes.append("在线校验未完成，退出码按失败处理")
    if online_diff:
        notes.append(
            f"在线比对发现 {online_diff} 处差异（仅本地/仅云端/字段不符），退出码按失败处理"
        )
    return {
        "schema": 1,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "fast": fast,
        "duration_sec": round(duration, 1),
        "timings": {k: round(v, 1) for k, v in timings.items()},
        "code_fingerprint": code_fingerprint(),
        "summary": {
            **stats,
            "online_diff": online_diff,
            "exit_code": exit_code(
                results, online_failed=online_failed, online_diff=online_diff
            ),
        },
        "problems": problems,
        "online": online,
        "notes": notes,
    }


# ============================================================
# 主流程
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="同步一致性巡检（P7）")
    ap.add_argument("--online", action="store_true", help="额外拉 Garmin/Tredict 清单比对")
    ap.add_argument("--fast", action="store_true", help="跳过 542,977 行逐秒比对")
    ap.add_argument("--no-write", action="store_true", help="不写 ledger / 报告 / 审计")
    args = ap.parse_args()

    started = time.perf_counter()
    timings: dict[str, float] = {}
    results: list[CheckResult] = []

    t = time.perf_counter()
    results += verify_garmin(fast=args.fast)
    timings["garmin"] = time.perf_counter() - t

    t = time.perf_counter()
    results += verify_tredict()
    timings["tredict"] = time.perf_counter() - t

    t = time.perf_counter()
    results += verify_health()
    timings["health"] = time.perf_counter() - t

    online: dict[str, Any] | None = None
    online_failed = False
    online_diff = 0
    if args.online:
        t = time.perf_counter()
        garmin_online, g_failed = verify_online_garmin()
        tredict_online, t_failed = verify_online_tredict()
        health_online, h_failed = verify_online_health()
        timings["online"] = time.perf_counter() - t
        online_failed = g_failed or t_failed or h_failed
        online = {"garmin": garmin_online, "tredict": tredict_online, "health": health_online}
        online_diff = _online_diff(online)
        print(f"[online] 差异合计 {online_diff}")

    stats = summarize(results)
    duration = time.perf_counter() - started
    code = exit_code(results, online_failed=online_failed, online_diff=online_diff)

    print(
        f"\n[summary] ok {stats[STATE_OK]}  mismatch {stats[STATE_MISMATCH]}  "
        f"missing_local {stats[STATE_MISSING_LOCAL]}  "
        f"missing_remote {stats[STATE_MISSING_REMOTE]}  "
        f"ambiguous {stats[STATE_AMBIGUOUS]}"
    )
    print(f"[summary] {duration:.1f}s  exit={code}")

    for r in results:
        if r.state != STATE_OK:
            detail = json.dumps(r.detail, ensure_ascii=False)
            print(f"  [{r.state}] {r.entity}/{r.entity_id}  {detail[:160]}")

    if args.no_write:
        print("[no-write] 未写 ledger / 报告 / 审计")
        return code

    counts = write_ledger(results)
    print(
        f"[ledger] 新增 {counts['insert']}  更新 {counts['update']}  "
        f"本次修复 {counts['resolved']}"
    )

    report = build_report(
        results,
        mode="online" if args.online else "offline",
        duration=duration,
        timings=timings,
        online=online,
        online_failed=online_failed,
        online_diff=online_diff,
        fast=args.fast,
    )
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / f"verify-{dt.datetime.now():%Y%m%d-%H%M}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[report] → {path.name}  {path.stat().st_size / 1024:.1f} KB")

    from app.config import get_settings
    from app.db.session import session_scope
    from app.schemas.enums import AuditCategory
    from app.storage import audit_store

    with session_scope() as s:
        audit_store.record(
            s,
            AuditCategory.DATA_SOURCE,
            ref="sync_verify",
            detail={
                "mode": report["mode"],
                "duration_sec": report["duration_sec"],
                **stats,
                "exit_code": code,
                "report": path.name,
            },
            audit_dir=get_settings().audit_dir,
        )

    return code


if __name__ == "__main__":
    sys.exit(main())
