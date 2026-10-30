"""探查：Tredict 活动列表 vs Garmin 活动，确定匹配用哪个时长字段。

背景（docs/tredict_api_structure.md §10.4）：
  同一活动有 ``durationTotal`` 与 ``duration`` 两个时长，实测最大差 188 秒。
  Q19 的「时长 ±5s」到底比哪个，必须先实测，否则差 188 秒的活动永远匹配不上。

本脚本只读：GET ``/activityList`` 全量 + 本地 ``garmin_activity`` 表，
算出两种时长各自的「同日同运动」差值分布，输出到
``data/raw/tredict/_probe_activity_list.json`` 供 P6 复用。
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(r"F:/WorkBuddy AI/Garmin_Tredict/_tools/tredict.config.json")
DB = ROOT / "data" / "db" / "health.db"
OUT = ROOT / "data" / "raw" / "tredict" / "_probe_activity_list.json"

API = "https://www.tredict.com/api/oauth/v2"
GAP_SEC = 0.13


def fetch_all() -> list[dict]:
    token = json.loads(CONFIG.read_text(encoding="utf-8"))["token"]
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json;charset=UTF-8",
    }
    url: str | None = f"{API}/activityList?pageSize=50"
    out: list[dict] = []
    pages = 0
    with httpx.Client(timeout=30) as client:
        while url and pages < 200:
            r = client.get(url, headers=headers)
            if r.status_code != 200:
                raise SystemExit(f"activityList 失败 HTTP {r.status_code}: {r.text[:200]}")
            j = r.json()
            out.extend((j.get("_embedded") or {}).get("activityList") or [])
            url = ((j.get("_links") or {}).get("next") or {}).get("href")
            pages += 1
            time.sleep(GAP_SEC)
    print(f"[fetch] 页数 {pages}，活动 {len(out)} 条")
    return out


def garmin_rows() -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT activity_id, start_time_gmt, start_time_local, activity_type, duration_sec, distance_m "
        "FROM garmin_activity"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def sport_bucket(tredict_type: str) -> str:
    """Tredict 的 sportType 与 Garmin 的 typeKey 不同名，先粗粒度归桶。"""
    t = (tredict_type or "").lower()
    if t in {"running", "walking", "hiking"}:
        return t
    if t in {"cycling", "biking"}:
        return "cycling"
    if t in {"swimming"}:
        return "swimming"
    return "misc"


def main() -> int:
    acts = fetch_all()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(acts, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[save] {OUT}  {OUT.stat().st_size / 1024:.0f} KB")

    if not acts:
        return 1

    # 字段覆盖
    keys: dict[str, int] = {}
    for a in acts:
        for k, v in a.items():
            if v not in (None, "", [], {}):
                keys[k] = keys.get(k, 0) + 1
    print("\n=== 字段覆盖（非空计数，共 %d 条）===" % len(acts))
    for k, n in sorted(keys.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<26} {n:>5}")

    print("\n=== 时长字段 ===")
    both = [a for a in acts if a.get("duration") and a.get("durationTotal")]
    diffs = [a["durationTotal"] - a["duration"] for a in both]
    print(f"  两者都有 {len(both)} 条")
    if diffs:
        print(
            f"  durationTotal-duration: min={min(diffs)} 中位={statistics.median(diffs)} "
            f"max={max(diffs)}  >5s 的 {sum(1 for d in diffs if d > 5)} 条"
        )

    # 与 Garmin 对拍
    g = garmin_rows()
    by_key: dict[tuple[str, str], list[dict]] = {}
    for row in g:
        day = (row["start_time_gmt"] or "")[:10]
        by_key.setdefault((day, row["activity_type"]), []).append(row)

    print("\n=== 与 Garmin 同日同运动对拍 ===")
    for field in ("durationTotal", "duration"):
        exact = within5 = total = 0
        deltas: list[float] = []
        for a in acts:
            ts = a.get("date") or a.get("startTime") or ""
            day = str(ts)[:10]
            sport = sport_bucket(a.get("sportType") or a.get("sport") or "")
            cands = by_key.get((day, sport))
            if not cands:
                continue
            val = a.get(field)
            if not val:
                continue
            best = min(cands, key=lambda r: abs((r["duration_sec"] or 0) - val))
            d = abs((best["duration_sec"] or 0) - val)
            total += 1
            deltas.append(d)
            if d == 0:
                exact += 1
            if d <= 5:
                within5 += 1
        if total:
            print(
                f"  {field:<14} 可配 {total:>4} 条  完全相等 {exact:>4}  ±5s 内 {within5:>4} "
                f"({within5 / total:.1%})  中位差 {statistics.median(deltas):.1f}s  max {max(deltas):.0f}s"
            )
        else:
            print(f"  {field:<14} 无可配对样本")

    # 日期分布
    days = sorted({str(a.get("date") or "")[:10] for a in acts if a.get("date")})
    print(f"\n=== 日期 ===\n  {len(days)} 天，{days[0] if days else '?'} → {days[-1] if days else '?'}")
    print(f"  探查时间 {datetime.now():%Y-%m-%d %H:%M}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
