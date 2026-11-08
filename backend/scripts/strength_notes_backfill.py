"""力量 notes 历史回填（v3.4.6 B 层）——离线受控清单 + 白名单回填。

背景：2026-10-26 本机 daily 停跑后，容器每天写新 activities 却缺
``fit_jsonl``，``tredict_sync`` 23:00 生成 notes 时走无 FIT 兜底，把「3 组共
60 次」固化成「60次 × 3组」并写到 Tredict 云端。补 ``fit_jsonl`` 不会自愈
（notes 只补空、绝不覆盖已有），必须显式回填。

本工具**不进镜像、不改「只补空」常驻契约**（判定逻辑不进生产代码）：

1. ``--plan``：离线比对云端 notes 镜像与「用 FIT 逐组重算的 want」，按三重判据
   （日期 ≥ ``SINCE`` + 以 ``HEADER`` 开头 + ``have != want``，且**必须有 FIT
   逐组明细**能证明正确值）产出带 diff 预览的候选清单，交人工逐条确认；
2. ``--apply``：仅对命令行显式给出的 ``tredict_id`` 白名单调
   ``update_activity`` 覆盖，绝不扩散到清单外、绝不动手写 / 旧 Node / 已正确 /
   空 notes 记录。

空 notes 仍走既有「只补空」路径，不进候选清单。可审计、可回滚。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

# 以脚本方式运行时 backend/ 不在 sys.path 上，补一下（app.* 全在函数内延迟 import）。
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# 时间窗：本机 daily 停跑、错误兜底开始出现的边界。
SINCE = dt.date(2026, 10, 26)
# 只有本系统生成的 notes 才以此开头；旧 Node 版「3组×60次」与用户手写均不含。
HEADER = "力量明细："


def _date_of(value: Any) -> dt.date:
    """``2026-10-27T...Z`` / ``date`` → ``date``；无法解析返回 ``date.min``（保守排除）。"""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return dt.date.min


def want_notes(garmin_root: Path, aid: str, summarized_sets: Any) -> str:
    """用 FIT ``set`` 逐组明细重算该活动「应有」的 notes；无逐组明细返回空串。

    没有 FIT 逐组明细时绝不用 C 层兜底文案（「共N次，缺逐组明细」）去回填——
    那只是「不误导」，不是「正确值」。返回空串让 ``plan_candidates`` 跳过该条。
    """
    from app.domain.strength.notes import (
        NOTES_LIMIT_UPDATE,
        build_strength_notes,
        truncate_notes,
    )
    from app.storage.activity_source import load_sets

    fit_sets = load_sets(Path(garmin_root), aid)
    if not fit_sets:
        return ""
    return truncate_notes(
        build_strength_notes(aid, summarized_sets, fit_sets),
        NOTES_LIMIT_UPDATE,
    )


def plan_candidates(
    pairs: list[dict],
    garmin_root: Path,
    *,
    since: dt.date = SINCE,
) -> list[dict]:
    """三重判据 → 候选清单（含 ``have`` / ``want`` diff），**绝不写云端**。

    ``pairs`` 每项含 ``tredict_id`` / ``garmin_id`` / ``date`` / ``have`` /
    ``summarized_sets``，由 ``build_pairs`` 从云端镜像与本地 activities 组装。
    判据：① 日期 ≥ since ② ``have`` 以 HEADER 开头 ③ ``have != want``，
    且 ``want`` 必须由 FIT 逐组明细重算得出（非空）。
    """
    root = Path(garmin_root)
    out: list[dict] = []
    for p in pairs:
        tid = str(p.get("tredict_id") or "")
        gid = str(p.get("garmin_id") or "")
        have = str(p.get("have") or "")
        if not tid or not gid:
            continue
        if not have.startswith(HEADER):
            continue  # 手写 / 旧 Node（非本系统生成）/ 空 notes
        if _date_of(p.get("date")) < since:
            continue  # 时间窗外
        want = want_notes(root, gid, p.get("summarized_sets"))
        if not want:
            continue  # 无 FIT 逐组明细，无法证明正确值 → 不回填
        if have == want:
            continue  # 已正确（Python FIT 分支），无需回填
        out.append(
            {
                "tredict_id": tid,
                "garmin_id": gid,
                "date": str(p.get("date") or ""),
                "have": have,
                "want": want,
            }
        )
    return out


def build_pairs(cloud_items: list[dict], garmin_root: Path) -> list[dict]:
    """**实时**云端清单（``fetch_activity_list(extended=True)``）+ 本地 activities → ``pairs``。

    复用上传侧同款 ``match_activities`` 做 garmin↔tredict 配对（云端没有 Garmin
    id，只能按时间/类型匹配）；歧义匹配一律排除，避免误写他人记录。

    数据源警告：``data/raw/tredict/activities/*.json`` 是本机 daily 时代的**详情
    缓存**，2026-10-26 daily 停跑后即过时（实测最新只到 10-22），**不能**作为
    回填判据的数据源；必须用实时云端清单，否则 10-26 之后的错误 notes 全被漏掉。
    """
    from app.domain.matching import CONFIDENCE_AMBIGUOUS, match_activities
    from app.services.tredict_sync_service import _refs_for_matching
    from app.storage.activity_source import load_activities

    cloud_list: list[dict] = []
    for item in cloud_items:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row.setdefault("id", row.get("_id") or "")
        cloud_list.append(row)

    activities = load_activities(Path(garmin_root))
    grefs, trefs = _refs_for_matching(activities, cloud_list)
    outcomes = match_activities(grefs, trefs)
    cloud_by_id = {str(it.get("id")): it for it in cloud_list}

    pairs: list[dict] = []
    for o in outcomes:
        if not o.garmin_id or not o.tredict_id:
            continue
        if o.confidence == CONFIDENCE_AMBIGUOUS:
            continue  # 与上传侧同口径：歧义绝不写
        ref = activities.get(str(o.garmin_id))
        cloud = cloud_by_id.get(str(o.tredict_id))
        if ref is None or cloud is None:
            continue
        payload = getattr(ref, "payload", {}) or {}
        pairs.append(
            {
                "tredict_id": str(o.tredict_id),
                "garmin_id": str(o.garmin_id),
                "date": str(cloud.get("date") or "")[:10],
                "have": str(cloud.get("notes") or ""),
                "summarized_sets": payload.get("summarizedExerciseSets"),
            }
        )
    return pairs


def apply_backfill(tredict: Any, candidates: list[dict], *, ids: list[str]) -> dict:
    """仅对显式 ``ids`` 白名单回填；绝不扩散到清单外，幂等，单条失败隔离。

    ``ids`` 是人工逐条确认后的 tredict_id 列表。只遍历白名单（不遍历整份
    候选清单），因此即便清单里混入手写 / 旧格式记录也不会被写；``have == want``
    的跳过保证重跑幂等；单条 ``update_activity`` 失败不影响其余。
    """
    by_id = {str(c.get("tredict_id")): c for c in candidates}
    updated: list[str] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    for tid in ids:
        c = by_id.get(str(tid))
        if c is None:
            skipped.append({"tredict_id": tid, "reason": "不在候选清单，跳过"})
            continue
        if str(c.get("have") or "") == str(c.get("want") or ""):
            skipped.append({"tredict_id": tid, "reason": "幂等：已与 want 一致"})
            continue
        try:
            r = tredict.update_activity(str(tid), {"notes": c["want"]})
        except Exception as e:  # noqa: BLE001 — 单条失败不阻断其余
            failed.append({"tredict_id": tid, "error": f"{type(e).__name__}: {e}"})
            continue
        if r.get("ok"):
            updated.append(str(tid))
        else:
            failed.append(
                {"tredict_id": tid, "status": r.get("status"), "error": r.get("text", "")}
            )
    return {"updated": updated, "failed": failed, "skipped": skipped}


def main() -> int:
    ap = argparse.ArgumentParser(description="力量 notes 历史回填（离线受控清单）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--cloud-dir", help="云端 notes 镜像目录（*.json，可能过时）")
    src.add_argument(
        "--cloud-list",
        help="实时云端清单 JSON 数组（fetch_activity_list(extended=True) 导出，推荐）",
    )
    ap.add_argument(
        "--garmin-root", required=True,
        help="本地 garmin raw 根（含 activities/ 与 fit_jsonl/）",
    )
    ap.add_argument("--plan", help="把候选清单写到该 JSON 文件")
    ap.add_argument(
        "--apply", nargs="*", default=None,
        help="仅对这些 tredict_id 回填（须先 --plan 并人工逐条确认）",
    )
    ap.add_argument("--plan-file", help="--apply 时读取的候选清单 JSON")
    args = ap.parse_args()

    cloud_items: list[dict] = []
    if args.cloud_list:
        cloud_items = json.loads(Path(args.cloud_list).read_text(encoding="utf-8"))
    else:
        for f in sorted(Path(args.cloud_dir).glob("*.json")):
            item = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(item, dict):
                item.setdefault("id", item.get("_id") or f.stem)
                cloud_items.append(item)

    root = Path(args.garmin_root)
    pairs = build_pairs(cloud_items, root)
    candidates = plan_candidates(pairs, root)

    if args.plan:
        Path(args.plan).write_text(
            json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"配对 {len(pairs)} 条；候选 {len(candidates)} 条（SINCE={SINCE}）")
    for c in candidates:
        print(f"\n[{c['tredict_id']}] {c['date']} garmin={c['garmin_id']}")
        print(f"  have: {c['have'][:220]}")
        print(f"  want: {c['want'][:220]}")

    if args.apply is not None:
        if not args.apply:
            print("--apply 至少需要一个 tredict_id", file=sys.stderr)
            return 2
        if not args.plan_file:
            print("--apply 需要 --plan-file（先 --plan 并人工确认清单）", file=sys.stderr)
            return 2
        listed = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        from app.adapters.tredict import TredictAdapter
        from app.config import get_settings

        result = apply_backfill(TredictAdapter(get_settings()), listed, ids=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
