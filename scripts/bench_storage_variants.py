"""对比 lap/set/zone 的三种存储路径在同等场景下的真实代价（只读源数据，写临时文件）。

路径 A：全部塞进 garmin_activity 的 JSON 列，用 json_each 查询
路径 B：独立表 + 索引
路径 C：只存 jsonl.gz，用时解压

用法：
    python scripts/bench_storage_variants.py
"""

from __future__ import annotations

import gzip
import json
import os
import random
import sqlite3
import statistics
import time
from pathlib import Path

FIT_JSON_DIR = Path(r"F:/WorkBuddy AI/Garmin_Tredict/garmin_data/activity/fit_json")
TMP = Path(os.environ.get("TEMP", "."))
DB_A = TMP / "bench_variant_a.db"
GZ_DIR = TMP / "bench_gz"


def timeit(fn, repeat: int = 5) -> tuple[float, float]:
    vals = []
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        vals.append((time.perf_counter() - t) * 1000)
    return statistics.median(vals), min(vals)


def load_real() -> tuple[list[tuple[str, int, list]], list[list]]:
    """返回 [(activity_id, 逐秒点数, sets)] 与全部 sets。"""
    out = []
    all_sets = []
    for f in sorted(FIT_JSON_DIR.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        sets = d.get("sets") or []
        out.append((f.stem, len(d.get("records") or []), sets))
        all_sets.extend(sets)
    return out, all_sets


def build_a(acts) -> None:
    if DB_A.exists():
        DB_A.unlink()
    con = sqlite3.connect(DB_A)
    con.execute(
        "CREATE TABLE garmin_activity (activity_id TEXT PRIMARY KEY, "
        "date TEXT, payload TEXT, sets_json TEXT, zones_json TEXT)"
    )
    rnd = random.Random(7)
    rows = []
    for aid, n, sets in acts:
        payload = json.dumps({"activityId": aid, "n": n, "x": "y" * 200})
        rows.append((aid, "2026-01-01", payload,
                     json.dumps(sets, separators=(",", ":")), "[]"))
    con.executemany("INSERT INTO garmin_activity VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()


def build_gz(acts) -> None:
    GZ_DIR.mkdir(exist_ok=True)
    rnd = random.Random(11)
    for aid, n, _ in acts:
        path = GZ_DIR / f"{aid}.jsonl.gz"
        if path.exists():
            continue
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as fh:
            hr = 120.0
            for i in range(n):
                hr = max(80.0, min(190.0, hr + rnd.gauss(0, 2)))
                fh.write(json.dumps(
                    {"m": 20, "f": {"253": 1760000000 + i, "3": int(hr),
                                    "73": round(2.5 + rnd.random(), 3),
                                    "4": round(80 + rnd.random() * 10, 1),
                                    "78": 20.0, "5": i * 2.5}},
                    separators=(",", ":")) + "\n")


def read_gz(aid: str) -> list:
    out = []
    with gzip.open(GZ_DIR / f"{aid}.jsonl.gz", "rt", encoding="utf-8") as fh:
        for line in fh:
            out.append(json.loads(line))
    return out


def main() -> int:
    acts, all_sets = load_real()
    ids = [a for a, _, _ in acts]
    print(f"真实规模：{len(acts)} 活动 / {sum(n for _, n, _ in acts):,} 逐秒点 / "
          f"{len(all_sets):,} 个 set\n")

    build_a(acts)
    print("准备 gzip 归档（首次会写 298 个文件）…")
    build_gz(acts)
    gz_total = sum(p.stat().st_size for p in GZ_DIR.glob("*.jsonl.gz"))
    print(f"gzip 归档体积 {gz_total / 1048576:.1f} MB\n")

    con = sqlite3.connect(DB_A)
    day_id = ids[-1]
    month_ids = ids[-30:]
    ph = ",".join("?" * len(month_ids))

    results = {}

    # A：JSON 列 + json_each
    results["A 月报·动作重量趋势(json_each)"] = timeit(lambda: con.execute(
        "SELECT json_extract(j.value,'$.category') , AVG(json_extract(j.value,'$.weight')) "
        "FROM garmin_activity, json_each(garmin_activity.sets_json) j "
        f"WHERE garmin_activity.activity_id IN ({ph}) GROUP BY 1", month_ids).fetchall())

    # A：胖行代价 —— SELECT * 拖出 sets_json
    results["A 日报·SELECT * 胖行"] = timeit(lambda: con.execute(
        "SELECT * FROM garmin_activity WHERE activity_id=?", (day_id,)).fetchall())

    # A：只取需要的列
    results["A 日报·显式列名"] = timeit(lambda: con.execute(
        "SELECT activity_id,date FROM garmin_activity WHERE activity_id=?", (day_id,)).fetchall())

    # C：gzip 解压
    results["C 日报·解压 1 文件"] = timeit(lambda: read_gz(day_id))
    results["C 月报·解压 30 文件"] = timeit(
        lambda: [read_gz(a) for a in month_ids], repeat=3)
    results["C 12周·解压 84 文件"] = timeit(
        lambda: [read_gz(a) for a in ids[-84:]], repeat=3)

    print(f"{'路径 / 场景':<34} {'中位数ms':>9} {'最快ms':>8}")
    print("-" * 56)
    for name, (med, best) in results.items():
        print(f"{name:<34} {med:>9.1f} {best:>8.1f}")

    con.close()
    print(f"\n路径 A 库体积 {DB_A.stat().st_size / 1048576:.1f} MB")
    print(f"路径 C 归档体积 {gz_total / 1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
