"""探查：Tredict 活动详情（298 条）→ 时长口径与 Garmin 对齐率。

只读 GET。结果缓存到 ``data/raw/tredict/_probe_details.json``，
避免反复打接口（298 次详情 + 6 次列表）。
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(r"F:/WorkBuddy AI/Garmin_Tredict/_tools/tredict.config.json")
LIST = ROOT / "data" / "raw" / "tredict" / "_probe_activity_list.json"
OUT = ROOT / "data" / "raw" / "tredict" / "_probe_details.json"
DB = ROOT / "data" / "db" / "health.db"

API = "https://www.tredict.com/api/oauth/v2"
GAP_SEC = 0.13


def headers() -> dict[str, str]:
    token = json.loads(CONFIG.read_text(encoding="utf-8"))["token"]
    return {
        "Authorization": "Bearer " + token,
        "Accept": "application/json;charset=UTF-8",
    }


def fetch_details(ids: list[str]) -> dict[str, dict]:
    if OUT.exists():
        cached = json.loads(OUT.read_text(encoding="utf-8"))
        if set(cached) >= set(ids):
            print(f"[cache] 复用 {OUT.name}（{len(cached)} 条）")
            return cached
    out: dict[str, dict] = {}
    h = headers()
    with httpx.Client(timeout=30) as c:
        for i, aid in enumerate(ids, 1):
            r = c.get(f"{API}/activity/{aid}", headers=h)
            if r.status_code == 200:
                out[aid] = r.json()
            elif r.status_code == 404:
                print(f"  [{i}/{len(ids)}] {aid} 404")
            else:
                print(f"  [{i}/{len(ids)}] {aid} HTTP {r.status_code}")
            if i % 50 == 0 or i == len(ids):
                print(f"  {i}/{len(ids)} …")
            time.sleep(GAP_SEC)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[save] {OUT}  {OUT.stat().st_size / 1024:.0f} KB")
    return out


def main() -> int:
    if not LIST.exists():
        raise SystemExit("先跑 probe_tredict_activities.py")
    acts = json.loads(LIST.read_text(encoding="utf-8"))
    ids = [a["id"] for a in acts]
    details = fetch_details(ids)
    print(f"[detail] 成功 {len(details)}/{len(ids)}")

    import sqlite3

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    g = [
        dict(r)
        for r in con.execute(
            "SELECT activity_id, start_time_gmt, activity_type, duration_sec, distance_m "
            "FROM garmin_activity"
        ).fetchall()
    ]
    con.close()

    # ---- 时长分布 ----
    pairs = []
    for aid, d in details.items():
        s = d.get("summary") or {}
        if s.get("duration") and s.get("durationTotal"):
            pairs.append((s["duration"], s["durationTotal"]))
    diffs = [b - a for a, b in pairs]
    print(f"\n=== durationTotal − duration（{len(diffs)} 条）===")
    if diffs:
        print(
            f"  min={min(diffs)}  中位={statistics.median(diffs)}  max={max(diffs)}  "
            f"相等 {sum(1 for x in diffs if x == 0)}  >5s {sum(1 for x in diffs if x > 5)}"
        )

    # ---- 与 Garmin 对拍 ----
    by_day: dict[str, list[dict]] = {}
    for r in g:
        day = (r["start_time_gmt"] or "")[:10]
        by_day.setdefault(day, []).append(r)

    print("\n=== 与 Garmin 同日同运动对拍 ===")
    for field in ("durationTotal", "duration"):
        exact = within5 = total = 0
        deltas: list[float] = []
        for aid, d in details.items():
            day = str(d.get("date") or "")[:10]
            sport = d.get("sportType") or ""
            cands = [r for r in by_day.get(day, []) if r["activity_type"] == sport]
            if not cands:
                continue
            val = (d.get("summary") or {}).get(field)
            if not val:
                continue
            best = min(cands, key=lambda r: abs((r["duration_sec"] or 0) - val))
            delta = abs((best["duration_sec"] or 0) - val)
            total += 1
            deltas.append(delta)
            exact += delta == 0
            within5 += delta <= 5
        if total:
            print(
                f"  {field:<14} 可配 {total:>4}  相等 {exact:>4}  ±5s {within5:>4} "
                f"({within5 / total:.1%})  中位差 {statistics.median(deltas):.1f}s  max {max(deltas):.0f}s"
            )
        else:
            print(f"  {field:<14} 无可配对样本")

    # ---- 时区与训练类型 ----
    tz: dict[str, int] = {}
    tt: dict[str, int] = {}
    for d in details.values():
        tz[str(d.get("timezone"))] = tz.get(str(d.get("timezone")), 0) + 1
        tt[str(d.get("trainingType"))] = tt.get(str(d.get("trainingType")), 0) + 1
    print("\n=== 时区 ===", tz)
    print("=== trainingType ===", tt)
    print(f"\n探查时间 {datetime.now():%Y-%m-%d %H:%M}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
