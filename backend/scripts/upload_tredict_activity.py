"""P8：把 Garmin 活动上传到 Tredict，并补全 title / 力量 notes。

为什么需要这个脚本
------------------
到 P7 为止，链路上只剩一段还是 Node：

| 环节 | 现状 |
|---|---|
| Garmin 拉取 | Python（``pull_garmin_fit.py``） |
| 健康数据上传 | Python（``sync_tredict.py``） |
| **活动上传 + 元数据补全** | **只有 Node 版 ``tredict-upload.js``** |

本脚本把最后一段补上，让整条链路可以只靠 Python 跑（NAS 上不必再装 Node）。

四个模式
--------
- ``--parity``  重算 65 条力量活动的 notes，与云端现有 notes **逐字比对**，不写任何数据。
                这是「移植是否等价」的唯一证明——65/65 一致才敢用。
- 默认          **dry-run**：只打印计划，不写库、不联网写（Q3）。
- ``--apply``   真正上传/更新。
- 过滤         ``--ids 650769156,...`` / ``--only-missing`` / ``--only-strength``（Q12）。

三条安全阀
----------
1. **ledger 闸门**（Q5）：``sync_ledger`` 里 ``garmin2local`` / ``tredict2local`` 若有
   非 ok 记录，说明本地数据本身没通过 P7 校验，此时推送等于把脏数据洗白。默认拒绝，
   ``--force`` 才放行。
2. **歧义不传**：某条 Garmin 活动被多条 Tredict 活动「争抢」（``ambiguous``）时不上传，
   否则会在云端造出重复活动。
3. **只补空**（Q10）：title / notes 默认只在云端为空时写，``--force-meta`` 才覆盖。

上传成功后自动增量回填 ``tredict_activity``（Q8），并写 ``local2tredict`` ledger（Q15）。

用法：
    python scripts/upload_tredict_activity.py --parity
    python scripts/upload_tredict_activity.py                 # dry-run
    python scripts/upload_tredict_activity.py --only-missing --apply
    python scripts/upload_tredict_activity.py --ids 651169725 --apply
    python scripts/upload_tredict_activity.py --force-meta --only-strength --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.adapters.tredict import TredictAdapter  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import SyncLedgerRow  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.domain.matching import (  # noqa: E402
    CONFIDENCE_AMBIGUOUS,
    MatchOutcome,
    TredictRef,
    match_activities,
    summarize,
    unmatched_garmin,
)
from app.domain.strength.notes import (  # noqa: E402
    NOTES_LIMIT_UPDATE,
    NOTES_LIMIT_UPLOAD,
    TITLE_LIMIT,
    build_strength_notes,
    data_fingerprints,
    truncate_notes,
)
from app.domain.tredict_sync import (  # noqa: E402
    ACTIVITY_DIR,
    DIRECTION_PUSH,
    LIST_PATH,
    fetch_details,
    load_garmin_refs,
    to_tredict_ref,
    upsert_ledger,
    upsert_rows,
)
from app.storage.activity_source import load_activities, load_sets  # noqa: E402

FIT_DIR = ROOT / "data" / "raw" / "garmin" / "fit"
JSONL_DIR = ROOT / "data" / "raw" / "garmin" / "fit_jsonl"
AUDIT_DIR = ROOT / "data" / "audit"

# 闸门只看「本地是否可信」这两个方向；local2tredict 自身的失败不该反过来锁死自己
GATE_DIRECTIONS = ("garmin2local", "tredict2local")
FAIL_STATES = frozenset({"mismatch", "missing_local", "missing_remote"})


# ============================================================
# 本地数据
# ============================================================
def load_garmin_rows() -> dict[str, dict[str, Any]]:
    """Garmin 活动 → 上传所需的最小视图（2026-10-21：改读 raw，表已废弃）。"""
    acts = load_activities(Path(get_settings().raw_data_dir) / "garmin")
    return {
        aid: {
            "activity_id": ref.activity_id,
            "activity_type": ref.activity_type,
            "name": ref.name,
            "start_utc": ref.start_utc,
            "duration_sec": ref.duration_sec,
            "payload": ref.payload,
        }
        for aid, ref in acts.items()
    }


def is_strength(activity_type: str) -> bool:
    """与 Node 版同口径：``typeKey`` 里含 ``strength``。"""
    return "strength" in (activity_type or "")


def load_fit_sets(activity_id: str) -> list[dict[str, Any]]:
    """FIT ``set`` 消息（2026-10-21：统一走 ``activity_source``）。"""
    return load_sets(Path(get_settings().raw_data_dir) / "garmin", activity_id)


def build_notes(act: dict[str, Any]) -> str:
    """力量活动 → notes；非力量或无可写动作时返回空串。"""
    if not is_strength(act["activity_type"]):
        return ""
    payload = act["payload"]
    return build_strength_notes(
        act["activity_id"],
        payload.get("summarizedExerciseSets"),
        load_fit_sets(act["activity_id"]),
    )


def build_title(act: dict[str, Any]) -> str:
    return str(act["payload"].get("activityName") or act["name"] or "")[:TITLE_LIMIT]


def fit_sha256(activity_id: str) -> str:
    path = FIT_DIR / f"{activity_id}.fit"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


# ============================================================
# 远端状态
# ============================================================
def load_remote_refs(
    ids: list[str], adapter: TredictAdapter | None
) -> tuple[dict[str, dict[str, Any]], dict[str, TredictRef], list[str]]:
    """Tredict 活动 → ``(详情, 引用, 失败)``。

    详情优先读 raw 缓存（P6 已落盘 298 条）；缓存缺失的（例如手机上新建的）才联网取，
    且**不落盘**——raw 归档只由 P6/P8 的回填统一写，避免两条路径写出不同裁剪口径。
    """
    details: dict[str, dict[str, Any]] = {}
    refs: dict[str, TredictRef] = {}
    failed: list[str] = []

    for aid in ids:
        path = ACTIVITY_DIR / f"{aid}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        elif adapter is not None:
            try:
                payload = adapter.fetch_activity_detail(aid)
            except Exception as exc:  # noqa: BLE001
                print(f"  [fail] 取详情 {aid} {type(exc).__name__}: {exc}")
                failed.append(aid)
                continue
            if payload is None:
                failed.append(aid)
                continue
        else:
            failed.append(aid)
            continue

        ref = to_tredict_ref(aid, payload)
        if ref is None:
            failed.append(aid)
            continue
        details[aid] = payload
        refs[aid] = ref
    return details, refs, failed


def fetch_live_ids(adapter: TredictAdapter) -> tuple[list[str], str]:
    """当前云端活动 id 列表 → ``(ids, 来源)``；联网失败退回 P6 的清单快照。

    只读不写：``data/raw/tredict/activity_list.json`` 是 P6 的归档（manifest 记了它的
    sha256），P8 若顺手覆盖会让归档与 manifest 对不上。
    """
    try:
        items = adapter.fetch_activity_list()
        return [str(it["id"]) for it in items if it.get("id")], "live"
    except Exception as exc:  # noqa: BLE001
        if LIST_PATH.exists():
            print(f"[warn] 取活动清单失败（{type(exc).__name__}: {exc}），退回本地快照")
            items = json.loads(LIST_PATH.read_text(encoding="utf-8"))
            return [str(it["id"]) for it in items if it.get("id")], "cache"
        raise


def ambiguous_garmin_ids(outcomes: list[MatchOutcome]) -> set[str]:
    """被多条 Tredict 活动争抢的 Garmin id（这些绝不能上传，否则云端会重复）。"""
    out: set[str] = set()
    for o in outcomes:
        if o.confidence != CONFIDENCE_AMBIGUOUS:
            continue
        for key in ("candidates", "claimants"):
            for value in o.detail.get(key) or []:
                if str(value).isdigit():
                    out.add(str(value))
        if o.detail.get("garmin_id"):
            out.add(str(o.detail["garmin_id"]))
    return out


# ============================================================
# 闸门
# ============================================================
def check_gate() -> dict[str, Any]:
    """P7 校验结果是否允许推送（Q5）。"""
    from sqlmodel import select

    by_state: Counter[str] = Counter()
    blocked: list[dict[str, str]] = []
    with session_scope() as s:
        for row in s.exec(select(SyncLedgerRow)).all():
            if row.direction not in GATE_DIRECTIONS or row.state == "ok":
                continue
            by_state[row.state] += 1
            if row.state in FAIL_STATES:
                blocked.append(
                    {"entity": row.entity, "entity_id": row.entity_id, "state": row.state}
                )
    return {
        "ok": not blocked,
        "blocked": len(blocked),
        "by_state": dict(by_state),
        "sample": blocked[:10],
    }


# ============================================================
# parity：notes 逐字比对（Q9）
# ============================================================
def run_parity() -> int:
    garmin = load_garmin_rows()
    ids = sorted(p.stem for p in ACTIVITY_DIR.glob("*.json"))
    details, refs, failed = load_remote_refs(ids, None)

    strength = [
        aid
        for aid in sorted(refs)
        if refs[aid].sport_type == "misc" and refs[aid].sub_sport_type == "strength_training"
    ]
    garmin_refs, _ = load_garmin_refs(Path(get_settings().raw_data_dir) / "garmin")
    outcomes = match_activities(garmin_refs, [refs[a] for a in strength])
    matched = {o.tredict_id: o.garmin_id for o in outcomes}

    diffs: list[dict[str, Any]] = []
    same = 0
    unmatched: list[str] = []
    for aid in strength:
        gid = matched.get(aid)
        if not gid or gid not in garmin:
            unmatched.append(aid)
            continue
        cloud = str(details[aid].get("notes") or "")
        mine = build_notes(garmin[gid])
        if mine == cloud:
            same += 1
            continue
        pos = next(
            (i for i, (a, b) in enumerate(zip(cloud, mine, strict=False)) if a != b),
            min(len(cloud), len(mine)),
        )
        diffs.append(
            {
                "tredict_id": aid,
                "garmin_id": gid,
                "cloud_len": len(cloud),
                "mine_len": len(mine),
                "first_diff_at": pos,
                "cloud": cloud[max(0, pos - 20) : pos + 40],
                "mine": mine[max(0, pos - 20) : pos + 40],
            }
        )

    report = {
        "schema": 1,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "parity",
        "data_files": data_fingerprints(),
        "counts": {
            "strength_remote": len(strength),
            "same": same,
            "diff": len(diffs),
            "unmatched": len(unmatched),
            "detail_failed": len(failed),
        },
        "diffs": diffs,
        "unmatched": unmatched,
    }
    write_report(report)

    print(
        f"[parity] 云端力量活动 {len(strength)} 条 → 逐字一致 {same}，"
        f"不一致 {len(diffs)}，未配对 {len(unmatched)}"
    )
    for d in diffs[:10]:
        print(f"  [diff] {d['tredict_id']} @{d['first_diff_at']}")
        print(f"    云端: {d['cloud']}")
        print(f"    本地: {d['mine']}")
    return 0 if not diffs and not failed else 1


# ============================================================
# 主流程
# ============================================================
def select_targets(
    garmin: dict[str, dict[str, Any]],
    *,
    ids: set[str],
    only_strength: bool,
) -> list[dict[str, Any]]:
    acts = [garmin[k] for k in sorted(garmin)]
    if ids:
        acts = [a for a in acts if a["activity_id"] in ids]
    if only_strength:
        acts = [a for a in acts if is_strength(a["activity_type"])]
    return acts


def run(
    *, apply: bool, force: bool, force_meta: bool, ids: set[str], only_missing: bool,
    only_strength: bool,
) -> int:
    gate = check_gate()
    print(
        f"[gate] 校验失败 {gate['blocked']} 条"
        + (f" {gate['by_state']}" if gate["by_state"] else "")
    )
    if not gate["ok"] and apply and not force:
        print("[gate] 本地数据未通过校验，拒绝上传。先跑 verify_sync.py，或加 --force 放行。")
        for item in gate["sample"]:
            print(f"  {item['entity']} {item['entity_id']} {item['state']}")
        return 2

    garmin = load_garmin_rows()
    unknown = ids - set(garmin)
    if unknown:
        print(f"[warn] --ids 里有 {len(unknown)} 个不在库中，已忽略：{sorted(unknown)}")
    targets = select_targets(garmin, ids=ids, only_strength=only_strength)
    print(f"[local] Garmin 活动 {len(garmin)} 条，本次候选 {len(targets)} 条")

    adapter = TredictAdapter(get_settings())
    remote_ids, source = fetch_live_ids(adapter)
    print(f"[remote] Tredict 活动 {len(remote_ids)} 条（来源 {source}）")

    details, refs, failed = load_remote_refs(remote_ids, adapter)
    garmin_refs, _ = load_garmin_refs(Path(get_settings().raw_data_dir) / "garmin")
    outcomes = match_activities(garmin_refs, list(refs.values()))
    stats = summarize(outcomes)
    print(
        f"[match] high {stats['high']}  medium {stats['medium']}  "
        f"ambiguous {stats['ambiguous']}  none {stats['none']}  （取详情失败 {len(failed)}）"
    )

    # 有一条远端活动读不到详情，它就不会进入匹配，其 Garmin 孪生体就会被误判为「缺失」
    # 而重新上传 —— 这是唯一会造出重复活动的路径，故直接熔断，重跑即可。
    if failed:
        print(f"[abort] {len(failed)} 条远端详情取不到，无法确认『缺失』，本次不传：{failed[:10]}")
        return 2

    matched: dict[str, str] = {
        o.garmin_id: o.tredict_id for o in outcomes if o.garmin_id
    }
    missing = set(unmatched_garmin(garmin_refs, outcomes))
    ambiguous = ambiguous_garmin_ids(outcomes)

    uploaded: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    upload_failed: list[dict[str, Any]] = []
    missing_fit: list[str] = []

    for act in targets:
        aid = act["activity_id"]
        tid = matched.get(aid)
        label = f"{aid} {act['activity_type']} {act['name']}"

        if tid is None:
            if aid in ambiguous:
                skipped.append({"activity_id": aid, "reason": "ambiguous_claim"})
                print(f"  [skip] {label} → 匹配歧义，不传")
                continue
            if aid not in missing:
                skipped.append({"activity_id": aid, "reason": "no_ref"})
                continue
            if not (FIT_DIR / f"{aid}.fit").exists():
                missing_fit.append(aid)
                print(f"  [skip] {label} → FIT 缺失")
                continue
            notes = build_notes(act)
            payload_notes = truncate_notes(notes, NOTES_LIMIT_UPLOAD)
            title = build_title(act)
            if not apply:
                uploaded.append(
                    {
                        "activity_id": aid,
                        "dry": True,
                        "name": title,
                        "notes_len": len(payload_notes),
                    }
                )
                print(f"  [dry] {label} → 上传（notes {len(payload_notes)} 字）")
                continue

            result = adapter.upload_activity(
                aid, (FIT_DIR / f"{aid}.fit").read_bytes(), name=title, notes=payload_notes
            )
            if result["ok"]:
                uploaded.append(
                    {"activity_id": aid, "name": title, "notes_len": len(payload_notes)}
                )
                print(f"  [up] {label} → 上传成功")
            elif result["exists"]:
                skipped.append({"activity_id": aid, "reason": "FILE_EXISTS"})
                print(f"  [skip] {label} → 云端已存在（FILE_EXISTS）")
            else:
                upload_failed.append(
                    {"activity_id": aid, "status": result["status"], "text": result["text"]}
                )
                print(f"  [fail] {label} → HTTP {result['status']} {result['text'][:120]}")

            with session_scope() as s:
                upsert_ledger(
                    s,
                    aid,
                    local_hash=fit_sha256(aid),
                    remote_hash="",
                    state="ok" if result["ok"] or result["exists"] else "missing_remote",
                    direction=DIRECTION_PUSH,
                    detail={
                        "action": "upload",
                        "ok": bool(result["ok"]),
                        "status": result["status"],
                        "notes_len": len(payload_notes),
                        "at": dt.datetime.now().isoformat(timespec="seconds"),
                    },
                )
            continue

        # —— 已存在：只补空的 title / notes ——
        if only_missing:
            skipped.append({"activity_id": aid, "tredict_id": tid, "reason": "already_exists"})
            continue
        detail = details.get(tid) or {}
        patch: dict[str, str] = {}
        title = build_title(act)
        if title and (force_meta or not detail.get("title")):
            patch["title"] = title
        if is_strength(act["activity_type"]):
            want = build_notes(act)
            if want and (force_meta or not detail.get("notes")):
                patch["notes"] = truncate_notes(want, NOTES_LIMIT_UPDATE)
        if not patch:
            skipped.append({"activity_id": aid, "tredict_id": tid, "reason": "meta_complete"})
            continue
        if not apply:
            updated.append({"activity_id": aid, "tredict_id": tid, "patch": patch, "dry": True})
            print(f"  [dry] {label} → 补 {','.join(patch)}")
            continue

        result = adapter.update_activity(tid, patch)
        if result["ok"]:
            updated.append({"activity_id": aid, "tredict_id": tid, "fields": sorted(patch)})
            print(f"  [up] {label} → 补 {','.join(patch)}")
        else:
            upload_failed.append(
                {
                    "activity_id": aid,
                    "tredict_id": tid,
                    "status": result["status"],
                    "text": result["text"],
                }
            )
            print(f"  [fail] {label} → 更新 HTTP {result['status']} {result['text'][:120]}")

        with session_scope() as s:
            upsert_ledger(
                s,
                aid,
                local_hash=fit_sha256(aid),
                remote_hash="",
                state="ok" if result["ok"] else "missing_remote",
                direction=DIRECTION_PUSH,
                detail={
                    "action": "update_meta",
                    "tredict_id": tid,
                    "fields": sorted(patch),
                    "ok": bool(result["ok"]),
                    "at": dt.datetime.now().isoformat(timespec="seconds"),
                },
            )

    # —— Q8：上传成功后增量回填 tredict_activity ——
    backfill: dict[str, Any] = {}
    if apply and uploaded:
        backfill = incremental_backfill(adapter, remote_ids)
        print(
            f"[backfill] 新增 {backfill.get('insert', 0)}  更新 {backfill.get('update', 0)}  "
            f"跳过 {backfill.get('skip', 0)}"
        )

    counts = {
        "garmin_total": len(garmin),
        "targets": len(targets),
        "tredict_remote": len(remote_ids),
        "matched": len(matched),
        "missing": len(missing),
        "uploaded": len(uploaded),
        "updated": len(updated),
        "skipped": len(skipped),
        "failed": len(upload_failed),
        "missing_fit": len(missing_fit),
    }
    report = {
        "schema": 1,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "apply" if apply else "dry-run",
        "force": force,
        "force_meta": force_meta,
        "filters": {
            "ids": sorted(ids),
            "only_missing": only_missing,
            "only_strength": only_strength,
        },
        "data_files": data_fingerprints(),
        "gate": gate,
        "match": stats,
        "remote_source": source,
        "counts": counts,
        "uploaded": uploaded,
        "updated": updated,
        "skipped": skipped,
        "failed": upload_failed,
        "missing_fit": missing_fit,
        "backfill": backfill,
    }
    write_report(report)
    print(
        f"\n[result] 上传 {counts['uploaded']}  补元数据 {counts['updated']}  "
        f"跳过 {counts['skipped']}  失败 {counts['failed']}  FIT缺失 {counts['missing_fit']}"
        + ("" if apply else "（dry-run，未写入）")
    )
    return 1 if upload_failed or missing_fit else 0


def incremental_backfill(adapter: TredictAdapter, before: list[str]) -> dict[str, int]:
    """只抓「本次新增的 id」的详情并入库（Q8）。"""
    after, _ = fetch_live_ids(adapter)
    new_ids = [aid for aid in after if aid not in set(before)]
    if not new_ids:
        print("[backfill] 云端 id 集合无新增，跳过")
        return {"insert": 0, "update": 0, "skip": 0, "no_date": 0, "new_ids": 0}

    details, hashes, failed = fetch_details(new_ids, refresh=False, dry_run=False)
    counts = upsert_rows(details, hashes, dry_run=False)
    counts["new_ids"] = len(new_ids)
    if failed:
        print(f"[backfill] 详情抓取失败 {len(failed)} 条：{failed}")
    return counts


# ============================================================
# 报告
# ============================================================
def write_report(report: dict[str, Any]) -> Path:
    """写 ``data/audit/upload-<YYYYMMDD-HHMMSS>.json``。

    比 Q11 约定的 ``HHMM`` 多了秒：同一分钟内跑 ``--parity`` 和 dry-run 会互相覆盖，
    而 parity 报告是「移植等价」的证据，不能被后一次 dry-run 悄悄冲掉。
    """
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = AUDIT_DIR / f"upload-{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[report] → {path.relative_to(ROOT)}")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="上传 Garmin 活动到 Tredict 并补全元数据")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    ap.add_argument("--parity", action="store_true", help="重算力量 notes 与云端逐字比对")
    ap.add_argument("--force", action="store_true", help="忽略 ledger 闸门")
    ap.add_argument("--force-meta", action="store_true", help="无条件覆盖 title/notes")
    ap.add_argument("--ids", default="", help="逗号分隔的 Garmin activityId")
    ap.add_argument("--only-missing", action="store_true", help="只上传缺失，不补元数据")
    ap.add_argument("--only-strength", action="store_true", help="只处理力量训练")
    args = ap.parse_args()

    ids = {x.strip() for x in args.ids.split(",") if x.strip()}
    if args.parity:
        return run_parity()
    return run(
        apply=args.apply,
        force=args.force,
        force_meta=args.force_meta,
        ids=ids,
        only_missing=args.only_missing,
        only_strength=args.only_strength,
    )


if __name__ == "__main__":
    sys.exit(main())
