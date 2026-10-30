"""拉取 Tredict **自生成**数据（capacity / zones / efforts）。

为什么要这一步
--------------
到 P8 为止，本地只有「我们上传给 Tredict 的东西」和「Tredict 回显的活动」，
没有「Tredict 自己算出来的东西」。这几类数据在官方 API 里都有读端点：
https://www.tredict.com/blog/oauth_docs/

| 端点 | 内容 | 实测（2026-10-21 复核） |
|---|---|---|
| ``GET /efforts`` | 每日训练努力值数组 | 有数据（按日期分组） |
| ``GET /capacity`` | ``hrMax/hrLth/ftp/ftpa`` 修订序列 | running 3 个修订 |
| ``GET /zones`` | 心率/配速区间修订序列 | running 2 类 |

2026-10-21 决策（Q4）：``plannedTrainingList`` / ``equipmentList`` **不再拉取**——
端点存在但账号实测为空（从未排过计划、无装备），每日空拉无存档价值；
若将来在 Tredict 端排了计划，再单列需求接入。``sleep-list`` / ``hrv-list``
的次元素（Tredict 自算 2 周基线）按用户 10-17 决策同样不拉。

落库：沿用 Stage 5 已建好的 ``tredict_generated`` 表（Q23=A：**只存档 + 可查，
不进计算层**），内容变则 ``version+1``，历史版本保留。

``efforts`` 的特殊处理
----------------------
官方**不返回 activityId**：``{"20251026": [[57], [69], [11]]}`` 只知道「这天有 3 个
effort」，不知道该属于哪条活动。因此按 **日期整组存**，不按下标硬对活动——
硬对会产生「这条活动的 effort 其实是另一条的」这类假关联，比不关联更糟。

用法
----
    python scripts/pull_tredict_generated.py --dry-run
    python scripts/pull_tredict_generated.py
    python scripts/pull_tredict_generated.py --since 2025-01-01   # efforts 起点
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.adapters.tredict import TredictAdapter  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.domain import tredict_generated as gen  # noqa: E402

RAW_DIR = ROOT / "data" / "raw" / "tredict" / "generated"
DEFAULT_EFFORT_SINCE = dt.date(2024, 1, 1)


def write_raw(name: str, payload: Any) -> Path:
    """落一份 JSON 快照（原始层只留最新，历史版本在库里）。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    p = RAW_DIR / f"{name}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def flatten_capacity(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{sport: [修订...]}`` → ``{"sport|timestamp": 修订}``。"""
    out: dict[str, dict[str, Any]] = {}
    for sport, revisions in (raw or {}).items():
        for rev in revisions or []:
            if not isinstance(rev, dict):
                continue
            ts = rev.get("timestamp")
            if not ts:
                continue
            out[f"{sport}|{ts}"] = {"sportType": sport, **rev}
    return out


def flatten_zones(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{sport: {zoneType: {ts: [...]}}}`` → ``{"sport|zoneType|ts": {...}}``。"""
    out: dict[str, dict[str, Any]] = {}
    for sport, by_type in (raw or {}).items():
        for zone_type, by_ts in (by_type or {}).items():
            for ts, bands in (by_ts or {}).items():
                out[f"{sport}|{zone_type}|{ts}"] = {
                    "sportType": sport,
                    "zoneType": zone_type,
                    "timestamp": ts,
                    "bands": bands,
                }
    return out


def collect(adapter: TredictAdapter, *, effort_since: dt.date) -> dict[str, Any]:
    """拉齐三类（effort / capacity / zones）；单类失败不影响其余。

    2026-10-21 决策 Q4：planned / equipment 不再拉取（账号实测为空）。
    """
    today = dt.date.today()
    out: dict[str, Any] = {}

    def _try(entity: str, fn) -> None:
        try:
            out[entity] = (fn(), None)
        except Exception as e:  # noqa: BLE001 — 单个端点失败不阻断整批
            out[entity] = (None, f"{type(e).__name__}: {e}")

    _try(gen.ENTITY_EFFORT, lambda: adapter.fetch_efforts(since=effort_since, until=today))
    _try(gen.ENTITY_CAPACITY, adapter.fetch_capacity)
    _try(gen.ENTITY_ZONES, adapter.fetch_zones)
    return out


def persist(collected: dict[str, Any], *, dry_run: bool) -> dict[str, dict[str, int]]:
    """写入 ``tredict_generated``；返回 ``{entity: {insert/update/same}}``。"""
    stats: dict[str, dict[str, int]] = {}

    def bump(entity: str, kind: str) -> None:
        stats.setdefault(entity, {"insert": 0, "update": 0, "same": 0})
        stats[entity][kind] += 1

    with session_scope() as s:
        for entity, (payload, error) in collected.items():
            if error or payload is None:
                continue

            if entity == gen.ENTITY_EFFORT:
                items = {
                    day: {"date": day, "efforts": efforts}
                    for day, efforts in (payload or {}).items()
                }
            elif entity == gen.ENTITY_CAPACITY:
                items = flatten_capacity(payload)
            else:  # zones
                items = flatten_zones(payload)

            for key, value in items.items():
                if dry_run:
                    bump(entity, "insert")
                    continue
                bump(entity, gen.upsert_generated(s, entity, key, value))
    return stats


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="拉取 Tredict 自生成数据")
    ap.add_argument("--dry-run", action="store_true", help="只拉不写（仍会联网）")
    ap.add_argument("--since", help="efforts 起点 YYYY-MM-DD（默认 2024-01-01）")
    args = ap.parse_args(argv)

    effort_since = (
        dt.date.fromisoformat(args.since) if args.since else DEFAULT_EFFORT_SINCE
    )
    adapter = TredictAdapter(get_settings())

    print(f"[pull] Tredict 自生成数据（efforts 自 {effort_since} 起）")
    collected = collect(adapter, effort_since=effort_since)

    errors = 0
    for entity, (payload, error) in collected.items():
        if error:
            errors += 1
            print(f"  {entity:<16} [FAIL] {error}")
            continue
        size = len(payload) if hasattr(payload, "__len__") else 0
        print(f"  {entity:<16} {size} 项")
        if not args.dry_run:
            write_raw(entity, payload)

    stats = persist(collected, dry_run=args.dry_run)
    print()
    for entity in gen.PULLED_ENTITIES:
        s = stats.get(entity)
        if not s:
            continue
        print(
            f"  {entity:<16} 新增 {s['insert']}  更新 {s['update']}  未变 {s['same']}"
        )

    if args.dry_run:
        print("\n[dry-run] 未写库、未落 raw")
        return 0
    if errors:
        print(f"\n[FAIL] {errors} 个端点失败")
        return 1
    print("\n[OK] Tredict 自生成数据已更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
