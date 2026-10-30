"""云端三路校验：本地 ↔ Garmin 云端、本地 ↔ Tredict 云端、Garmin ↔ Tredict。

为什么需要它
------------
P7 的 ``verify_sync.py`` 只回答「本地库和本地 raw 是否一致」——它的在线部分
只比**键集合**（这一天/这一条在不在）。这留下两个盲区：

1. **值错了发现不了**：``rmssd`` 传成 0、睡眠时长传成 0，一样是绿的。
2. **Garmin 云端从没对过**：健康 raw 只有「库 ↔ raw」，raw 本身就是残缺的也无从得知
   （Garmin 会事后补数，而旧 append-only 策略让残缺永久化）。

三路各自抓什么
--------------
| 路 | 比较对象 | 能接住 |
|---|---|---|
| A | 本地 raw ↔ Garmin 云端 | 拉取遗失（云端有本地无）、Garmin 事后改数 |
| B | 本地上传物 ↔ Tredict 云端 | 上传遗失（本地有云端无）、上传错值、拉取遗失 |
| C | Garmin 原值 ↔ Tredict 存值 | **我们的转换错了**（A/B 都会通过，因为两边一致地错） |

C 是独立的：如果 ``build_hrv_records`` 把 64 算成 46 并上传，B 两边都是 46 → 绿；
只有 C 拿 Garmin 原值 64 去比才会红。

退出码
------
- ``0`` 全部一致
- ``1`` 校验没跑成（网络/鉴权失败）——「没验」不等于「验过了」
- ``3`` 有差异，**可尝试自动修复一轮**（调用方据此重跑上传后再验）

用法
----
    python scripts/verify_cloud.py --scope activity
    python scripts/verify_cloud.py --scope health
    python scripts/verify_cloud.py --scope all --full     # 健康 473 天全量
    python scripts/verify_cloud.py --scope activity --no-write
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.adapters.garmin import RAW_REFRESH_WINDOW_DAYS  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.domain.verify import (  # noqa: E402
    compare_activity_coverage,
    compare_cloud_pair,
    compare_health_fields,
    compare_health_raw,
    compare_online_garmin,
    deep_diff,
    ids_of_day,
)
from app.services.sync_status import verification_message  # noqa: E402

RAW = ROOT / "data" / "raw"
GARMIN_RAW = RAW / "garmin"
AUDIT_DIR = ROOT / "data" / "audit"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DIFF = 3


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ============================================================
# A：本地 raw ↔ Garmin 云端（活动）
# ============================================================
def load_local_activity_payloads() -> dict[str, dict[str, Any]]:
    """``activities/<date>.json`` 按 activityId 铺平（一天可能多活动）。"""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted((GARMIN_RAW / "activities").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"[warn] 跳过损坏文件 {path.name}")
            continue
        items = data if isinstance(data, list) else (data.get("activities") or [])
        for item in items:
            aid = item.get("activityId")
            if aid is not None:
                out[str(aid)] = item
    return out


def verify_tredict_details(adapter_tredict, *, sample: int) -> dict[str, Any]:
    """D 环：Tredict 详情本地快照 ↔ 云端实时（2026-10-21）。

    B 环只比 **id 集合**，详情内容腐烂不可见。而 Tredict 在 capacity/zones
    修订后会**重算历史 effort**（``summary.effort.*`` 三源值变），本地快照
    不变 → 快照与云端不一致且无人知晓。本环按 ``utc_datetime`` 取最近
    ``sample`` 条实时 GET（带 ``extraValues=1``）与本地 raw 快照做 deep_diff；
    全量一致性由 ``backfill_tredict_activity.py --refresh`` 承担。
    """
    from sqlmodel import select

    from app.adapters.tredict import trim_series_sampled
    from app.db.models import TredictActivityRow
    from app.db.session import session_scope
    from app.domain.tredict_sync import ACTIVITY_DIR
    from app.domain.verify import compare_tredict_details

    with session_scope() as s:
        recent = s.exec(
            select(TredictActivityRow)
            .order_by(TredictActivityRow.utc_datetime.desc())  # type: ignore[attr-defined]
            .limit(sample)
        ).all()
        ids = [r.tredict_id for r in recent]  # 必须在 session 内取出标量

    local: dict[str, dict] = {}
    for tid in ids:
        p = ACTIVITY_DIR / f"{tid}.json"
        if p.exists():
            local[tid] = json.loads(p.read_text(encoding="utf-8"))
    cloud: dict[str, dict] = {}
    errors: list[str] = []
    for tid in ids:
        try:
            item = adapter_tredict.fetch_activity_detail(tid)
        except Exception as e:  # noqa: BLE001 — 单条失败不中断
            errors.append(f"{tid}: {type(e).__name__}: {e}")
            continue
        if item:
            cloud[tid] = trim_series_sampled(item)  # 与本地快照同口径（裁 data）

    d = compare_tredict_details(local, cloud)
    d["errors"] = errors
    return d


def verify_activity(
    adapter_garmin,
    adapter_tredict,
    *,
    no_write: bool,
    detail_sample: int = 20,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """活动三路。

    A：本地 raw ↔ Garmin 云端（88 个共同字段）→ 拉取遗失 / 云端改数
    B：本地 ``tredict_activity`` ↔ Tredict 云端 id 集合 → 上传遗失 / 拉取遗失
    C：Garmin 云端 ↔ Tredict 云端逐字段（按运动类型分流时长口径）→ 上传错值

    ⚠️ B 与 C **不重复计数**：B 只比 id 集合，C 只比共同 id 的字段值。

    ``today`` 可注入以便测试，默认取当天。**当天的活动不计入差异**（L-12③）：
    日任务 21:00 跑校验、23:30 才同步当天活动，这个时间差里「云端有、本地无」
    是管道未走完而非数据丢失，报出来就是天天假红。健康侧 ``_health_window``
    早有同一条规则；次日 5 天滚动窗口自然复核，豁免不丢覆盖。
    """
    from sqlmodel import select

    from app.db.models import TredictActivityRow
    from app.db.session import session_scope

    local = load_local_activity_payloads()
    garmin_cloud = adapter_garmin.client().get_activities(0, 1000)

    today = today or dt.date.today()
    skip = ids_of_day(garmin_cloud, local, today)
    if skip:
        local = {k: v for k, v in local.items() if k not in skip}
        garmin_cloud = [x for x in garmin_cloud if str(x.get("activityId")) not in skip]
        print(f"[activity] 豁免当天活动 {len(skip)} 条（当日管道未走完，不计入差异）")

    print(f"[A] 本地 raw 活动 {len(local)} 条")
    a = compare_online_garmin(garmin_cloud, local)
    print(
        f"[A] Garmin 云端 {a['remote']} / 本地 {a['local']}  "
        f"仅远端 {len(a['only_remote'])}  仅本地 {len(a['only_local'])}  "
        f"值不符 {len(a['field_mismatch'])}  "
        f"schema 漂移 {len(a.get('schema_drift') or [])}（不算错误）"
    )

    tredict_list = adapter_tredict.fetch_activity_list(extended=True)
    cloud_ids = {str(x.get("id")) for x in tredict_list if x.get("id")}
    with session_scope() as s:
        rows = {
            r.tredict_id: r.matched_garmin_id
            for r in s.exec(select(TredictActivityRow)).all()
            if r.matched_garmin_id is None or str(r.matched_garmin_id) not in skip
        }
    local_ids = set(rows)
    b = {
        "local": len(local_ids),
        "remote": len(cloud_ids),
        "only_local": sorted(local_ids - cloud_ids),  # 本地有云端无 → 上传遗失
        "only_remote": sorted(cloud_ids - local_ids),  # 云端有本地无 → 拉取遗失
    }
    print(
        f"[B] 本地 tredict_activity {b['local']} / 云端 {b['remote']}  "
        f"仅本地 {len(b['only_local'])}  仅云端 {len(b['only_remote'])}"
    )

    garmin_cloud_by_id = {
        str(x.get("activityId")): x for x in garmin_cloud if x.get("activityId") is not None
    }
    tredict_items = {str(x.get("id")): x for x in tredict_list if x.get("id")}
    pairs = [(str(gid), tid) for tid, gid in rows.items() if gid]
    c = compare_cloud_pair(garmin_cloud_by_id, tredict_items, pairs)
    print(
        f"[C] 配对 {c['pairs']}  可比 {c['compared']}  跳过 {c['skipped']}  "
        f"字段不符 {c['mismatch']}"
    )

    # C0：活动**双向覆盖**（2026-10-21 决策 Q7）——旧校验只遍历 Tredict 侧，
    # 「Garmin 有、Tredict 没有」的上传遗漏永远报不出来（漏掉的那条没进循环）。
    # Tredict 的 id 是自己的字符串 id，与 Garmin 数字 id 不同域 →
    # 覆盖集合必须用镜像里的 matched_garmin_id（配对结果），不能拿云端 id 直接比。
    covered_garmin = {str(gid) for gid in rows.values() if gid}
    cov = compare_activity_coverage(set(local), covered_garmin)
    print(
        f"[C0] 覆盖 Garmin {len(local)} / 已配对 {len(covered_garmin)}  "
        f"上传遗漏 {len(cov['only_garmin'])}  多余 {len(cov['only_tredict'])}"
    )
    for aid in cov["only_garmin"][:10]:
        print(f"     ! 上传遗漏 activityId={aid}（Garmin 有、Tredict 无）")

    # D 环：Tredict 详情快照 ↔ 云端实时（抽样）——capacity/zones 修订会重算
    # 历史 effort，B 环（只比 id）看不见这种腐烂。
    d = verify_tredict_details(adapter_tredict, sample=detail_sample)
    print(
        f"[D] 详情快照抽样 {d['sampled']} 条  "
        f"值不符 {len(d['value_mismatch'])}  "
        f"schema 漂移 {len(d['drift_paths'])}（不算错误）  "
        f"云端已删 {len(d['missing_remote'])}  拉取遗漏 {len(d['missing_local'])}"
    )
    for tid, paths in list(d["value_mismatch"].items())[:5]:
        first = sorted(paths)[0]
        print(f"     ! {tid} {first}：本地 {paths[first]['local']} ≠ 云端 {paths[first]['remote']}")
    for e in d["errors"][:5]:
        print(f"     ! 详情拉取失败 {e}")

    total = (
        len(a["only_remote"])
        + len(a["only_local"])
        + len(a["field_mismatch"])
        + len(b["only_local"])
        + len(b["only_remote"])
        + c["mismatch"]
        + len(cov["only_garmin"])
        + len(cov["only_tredict"])
        + len(d["value_mismatch"])
        + len(d["errors"])
    )
    return {
        "a_local_garmin": a,
        "coverage": cov,
        "b_local_tredict": b,
        "c_garmin_tredict": c,
        "d_tredict_details": d,
        "diff": total,
    }


# ============================================================
# 健康：A（本地 raw ↔ Garmin 云端）+ B + C
# ============================================================
def _health_window(full: bool, days: int) -> list[dt.date]:
    if full:
        files = sorted((GARMIN_RAW / "daily_summary").glob("*.json"))
        if not files:
            return []
        start = dt.date.fromisoformat(files[0].stem)
        end = dt.date.fromisoformat(files[-1].stem)
        return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    today = dt.date.today()
    # **排除当天**：当天数据未走完（07:30 拉的快照 vs 16:48 的云端必然不同），
    # 这类「当日未结束」差异按设计次日刷新生效，不该天天报错（狼来了）。
    # 5 天滚动窗本就覆盖「当天 + 延迟回填」，校验只需盯已走完的日子。
    return [today - dt.timedelta(days=i) for i in range(1, days + 1)]


def verify_health(
    adapter_garmin, adapter_tredict, *, full: bool, days: int
) -> dict[str, Any]:
    from app.adapters.garmin import load_raw_dataset, load_raw_weight_list
    from app.adapters.tredict import (
        build_bodyvalue_records,
        build_hrv_records,
        build_sleep_records,
        norm_ts,
        ymd,
    )
    from app.config import get_settings

    window = _health_window(full, days)
    print(f"[health] 窗口 {window[-1] if window else '-'} ~ {window[0] if window else '-'}"
          f"（{len(window)} 天）")

    # ---- A：本地 raw ↔ Garmin 云端（逐数据集逐字段比，只读不覆盖）----
    garmin_client = adapter_garmin.client()
    a_problems: list[dict[str, Any]] = []
    checked = 0
    # 2026-10-21：把新增数据集纳入 A 环（此前只有 5 个核心集，体重缺口检不出）
    core_datasets = ("daily_summary", "sleep", "hrv", "spo2", "respiration")
    extra_datasets = ("weight", "lifestyle_logging", "hydration")
    for day in window:
        for dataset in core_datasets + extra_datasets:
            try:
                cloud = adapter_garmin._fetch(garmin_client, dataset, day)  # noqa: SLF001
            except Exception as e:  # noqa: BLE001 — 单日单集失败不中断
                a_problems.append(
                    {"date": day.isoformat(), "dataset": dataset,
                     "issue": "fetch_failed", "detail": f"{type(e).__name__}: {e}"}
                )
                continue
            path = GARMIN_RAW / dataset / f"{day.isoformat()}.json"
            checked += 1
            if not path.exists():
                if cloud:
                    a_problems.append(
                        {"date": day.isoformat(), "dataset": dataset,
                         "issue": "missing_local"}
                    )
                continue
            try:
                mine = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                a_problems.append(
                    {"date": day.isoformat(), "dataset": dataset, "issue": "local_corrupt"}
                )
                continue
            if mine != cloud:
                if dataset in core_datasets:
                    diffs = compare_health_raw(mine, cloud, dataset=dataset)
                    for d in diffs:
                        a_problems.append({"date": day.isoformat(), **d})
                else:
                    # 新数据集：递归比叶子（云端新增字段算漂移，不算错误）
                    mism, _cloud_only, _local_only = deep_diff(mine, cloud)
                    for path, vals in list(mism.items())[:10]:
                        a_problems.append(
                            {
                                "date": day.isoformat(),
                                "dataset": dataset,
                                "field": path,
                                "local": vals.get("local"),
                                "remote": vals.get("remote"),
                            }
                        )
    print(
        f"[A] 比对 {checked} 组（{len(window)} 天 × 8 数据集）"
        f"  参与计算字段差异 {len(a_problems)}"
    )

    # ---- B：本地构建的上传物 ↔ Tredict 云端（逐字段）----
    raw_root = Path(get_settings().raw_data_dir) / "garmin"
    hrv_raw = load_raw_dataset(raw_root, "hrv")
    sleep_raw = load_raw_dataset(raw_root, "sleep")
    daily_raw = load_raw_dataset(raw_root, "daily_summary")
    weight_list = load_raw_weight_list(raw_root)

    hrv_records, _, _ = build_hrv_records(hrv_raw)
    sleep_records, _, _ = build_sleep_records(sleep_raw)
    bv_records, _ = build_bodyvalue_records(daily_raw, weight_list)

    local_health = {
        "hrv": {ymd(r["timestamp"]): {"rmssd": r["rmssd"]} for r in hrv_records},
        "sleep": {
            ymd(r["endTimestamp"]): {"sleepDurationInSeconds": r["sleepDurationInSeconds"]}
            for r in sleep_records
        },
        "bodyvalues": {norm_ts(r["timestamp"]): r for r in bv_records},
    }
    cloud = adapter_tredict.fetch_health()
    b = compare_health_fields(local_health, cloud)
    for kind, info in b["kinds"].items():
        print(
            f"[B] {kind:<11} 本地 {info['local']} / 云端 {info['remote']}  "
            f"仅本地 {len(info['only_local'])}  仅云端 {len(info['only_remote'])}  "
            f"值不符 {len(info.get('mismatch') or [])}"
        )

    # ---- C：Garmin 原值 ↔ Tredict 存值（绕过我们的转换）----
    c_problems: list[dict[str, Any]] = []
    cloud_hrv = cloud.get("hrv") or {}
    for day, payload in hrv_raw.items():
        want = (payload.get("hrvSummary") or {}).get("lastNightAvg")
        got = cloud_hrv.get(day.replace("-", ""))
        if want is None or not isinstance(got, list) or not got:
            continue
        if abs(float(want) - float(got[0])) > 0.5:
            c_problems.append(
                {"kind": "hrv", "date": day, "garmin": want, "tredict": got[0]}
            )

    cloud_sleep = cloud.get("sleep") or {}
    for day, payload in sleep_raw.items():
        want = ((payload or {}).get("dailySleepDTO") or {}).get("sleepTimeSeconds")
        got = cloud_sleep.get(day.replace("-", ""))
        if want is None or not isinstance(got, list) or not got:
            continue
        if abs(float(want) - float(got[0])) > 1:
            c_problems.append(
                {"kind": "sleep", "date": day, "garmin": want, "tredict": got[0]}
            )

    cloud_bv = {
        str(r.get("timestamp", ""))[:10]: r
        for r in (cloud.get("bodyvalues") or [])
        if isinstance(r, dict)
    }
    for day, payload in daily_raw.items():
        want = (payload or {}).get("restingHeartRate")
        got = cloud_bv.get(day)
        if want is None or got is None:
            continue
        if abs(float(want) - float(got.get("restingHeartrate") or 0)) > 0.5:
            c_problems.append(
                {"kind": "bodyvalues", "date": day, "field": "restingHeartrate",
                 "garmin": want, "tredict": got.get("restingHeartrate")}
            )
    print(f"[C] Garmin 原值 ↔ Tredict 存值  差异 {len(c_problems)}")

    diff = len(a_problems) + b.get("lagging", 0) + b.get("extra", 0) + b.get("mismatch", 0)
    diff += len(c_problems)
    return {
        "window": [window[-1].isoformat(), window[0].isoformat()] if window else [],
        "a_local_garmin": {"checked": checked, "problems": a_problems[:50],
                           "count": len(a_problems)},
        "b_local_tredict": b,
        "c_garmin_tredict": {"problems": c_problems[:50], "count": len(c_problems)},
        "diff": diff,
    }


# ============================================================
# 落库 / 报告
# ============================================================
def write_ledger(report: dict[str, Any]) -> None:
    """把差异写进 ``sync_ledger``（entity 用 ``cloud_verify`` 前缀，不覆盖 P7 的结论）。"""
    from sqlmodel import select

    from app.db.models import SyncLedgerRow
    from app.db.session import session_scope

    now = dt.datetime.now()
    items: list[tuple[str, str, str, dict[str, Any]]] = []

    for block_key, entity_name in (
        ("a_local_garmin", "cloud_garmin"),
        ("b_local_tredict", "cloud_tredict"),
    ):
        block = report.get(block_key) or {}
        for key in ("only_local", "only_remote"):
            for item in block.get(key) or []:
                state = "missing_remote" if key == "only_local" else "missing_local"
                items.append((entity_name, str(item), state, {"from": block_key}))
        for issue in block.get("problems") or []:
            items.append(
                (
                    "cloud_garmin",
                    f"{issue.get('date')}/{issue.get('dataset')}",
                    "mismatch",
                    issue,
                )
            )
        for kind, info in (block.get("kinds") or {}).items():
            for key in ("only_local", "only_remote"):
                for item in info.get(key) or []:
                    state = "missing_remote" if key == "only_local" else "missing_local"
                    items.append((f"cloud_{kind}", str(item), state, {"from": block_key}))
            for bad in info.get("mismatch") or []:
                items.append(
                    (f"cloud_{kind}", str(bad.get("key")), "mismatch", bad)
                )

    c = report.get("c_garmin_tredict") or {}
    for problem in c.get("problems") or []:
        items.append(
            (
                f"cloud_pair_{problem.get('kind')}",
                str(problem.get("date")),
                "mismatch",
                problem,
            )
        )

    if not items:
        return

    with session_scope() as s:
        existing = {
            (r.entity, r.entity_id, r.direction): r
            for r in s.exec(
                select(SyncLedgerRow).where(SyncLedgerRow.direction == "cloud_verify")
            ).all()
        }
        for entity, entity_id, state, detail in items:
            row = existing.get((entity, entity_id, "cloud_verify"))
            if row is None:
                s.add(
                    SyncLedgerRow(
                        entity=entity,
                        entity_id=entity_id,
                        direction="cloud_verify",
                        local_hash="",
                        remote_hash="",
                        state=state,
                        first_failed_at=now,
                        last_checked_at=now,
                        attempts=1,
                        detail={"cloud_verify": detail},
                    )
                )
            else:
                row.state = state
                row.last_checked_at = now
                row.attempts += 1
                row.detail = {"cloud_verify": detail}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="云端三路校验")
    ap.add_argument("--scope", choices=("activity", "health", "all"), default="all")
    ap.add_argument("--full", action="store_true", help="健康校验跑全量（473 天）")
    ap.add_argument(
        "--days",
        type=int,
        default=RAW_REFRESH_WINDOW_DAYS,
        help=f"健康校验窗口天数（默认 {RAW_REFRESH_WINDOW_DAYS}，与拉取窗口一致）",
    )
    ap.add_argument("--no-write", action="store_true", help="不写 ledger / 报告")
    ap.add_argument(
        "--detail-sample",
        type=int,
        default=20,
        help="D 环详情快照抽样条数（按 utc_datetime 取最近 N 条，默认 20）",
    )
    args = ap.parse_args(argv)

    from app.adapters.garmin import GarminAdapter
    from app.adapters.tredict import TredictAdapter
    from app.config import get_settings

    settings = get_settings()
    report: dict[str, Any] = {"generated_at": _now(), "scope": args.scope,
                              "full": args.full, "days": args.days}
    started = time.perf_counter()
    failed = False

    try:
        if args.scope in ("activity", "all"):
            t = time.perf_counter()
            report["activity"] = verify_activity(
                GarminAdapter(settings), TredictAdapter(settings),
                no_write=args.no_write, detail_sample=args.detail_sample,
            )
            report["activity"]["seconds"] = round(time.perf_counter() - t, 1)
        if args.scope in ("health", "all"):
            t = time.perf_counter()
            report["health"] = verify_health(
                GarminAdapter(settings), TredictAdapter(settings),
                full=args.full, days=args.days,
            )
            report["health"]["seconds"] = round(time.perf_counter() - t, 1)
    except Exception as e:  # noqa: BLE001 — 任何失败都按「没验成」处理
        failed = True
        report["error"] = f"{type(e).__name__}: {e}"
        print(f"\n[FAIL] 校验未完成：{report['error']}")

    diff = sum(
        int((report.get(k) or {}).get("diff", 0)) for k in ("activity", "health")
    )
    report["diff"] = diff
    report["seconds"] = round(time.perf_counter() - started, 1)

    print()
    print(f"[summary] 差异合计 {diff}  {report['seconds']}s")

    if not args.no_write:
        if not failed:
            try:
                write_ledger(report)
            except Exception as e:  # noqa: BLE001 — ledger 写失败不该掩盖校验结论
                print(f"[warn] ledger 写入失败：{type(e).__name__}: {e}")
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        p = AUDIT_DIR / f"cloud-verify-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        p.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[report] → {p.name}")

        # Q8：差异 > 0 → 消息中心推送（同一报告只发一次；失败不影响校验结论）
        if diff:
            try:
                with session_scope() as s:
                    msg = verification_message(s, diff_total=diff, report_path=p.name)
                if msg is not None:
                    print(f"[msg] 已写消息中心：{msg.title}")
                else:
                    print("[msg] 该报告已通知过，跳过")
            except Exception as e:  # noqa: BLE001 — 通知失败不改校验结论
                print(f"[warn] 消息写入失败：{type(e).__name__}: {e}")

    if failed:
        return EXIT_ERROR
    return EXIT_DIFF if diff else EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
