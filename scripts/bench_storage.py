"""在真实数据规模上对比三种 lap/set/zone 存储方案的查询代价（只读源数据，写临时库）。

临时库建在系统 TEMP，不触碰项目 data/。

用法：
    python scripts/bench_storage.py
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import statistics
import time
from pathlib import Path

FIT_JSON_DIR = Path(r"F:/WorkBuddy AI/Garmin_Tredict/garmin_data/activity/fit_json")
DB = Path(os.environ.get("TEMP", ".")) / "bench_storage.db"


def real_shape() -> tuple[list[tuple[str, int]], int, int]:
    """从真实 fit_json 读出每个活动的逐秒点数与 set 数。"""
    acts: list[tuple[str, int]] = []
    n_sets = 0
    for f in sorted(FIT_JSON_DIR.glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        n = len(d.get("records") or [])
        if n:
            acts.append((f.stem, n))
        n_sets += len(d.get("sets") or [])
    return acts, sum(n for _, n in acts), n_sets


def build(acts: list[tuple[str, int]], n_records: int, n_sets: int) -> None:
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """CREATE TABLE garmin_activity_record (
               activity_id TEXT NOT NULL, t INTEGER NOT NULL,
               hr INTEGER, speed REAL, power REAL, cadence REAL,
               altitude REAL, distance REAL)"""
    )
    con.execute(
        """CREATE TABLE garmin_activity_set (
               activity_id TEXT NOT NULL, set_index INTEGER NOT NULL,
               exercise TEXT, weight REAL, reps INTEGER, duration REAL,
               set_type TEXT)"""
    )
    con.execute(
        """CREATE TABLE garmin_activity (
               activity_id TEXT PRIMARY KEY, date TEXT, payload TEXT,
               laps_json TEXT, zones_json TEXT)"""
    )

    rnd = random.Random(42)
    rows = []
    for aid, n in acts:
        t0 = 1_760_000_000
        hr = 120.0
        for i in range(n):
            hr = max(80.0, min(190.0, hr + rnd.gauss(0, 2)))
            rows.append((aid, t0 + i, int(hr), 2.5 + rnd.random(),
                         None, 80.0 + rnd.random() * 10, 20.0, i * 2.5))
    con.executemany("INSERT INTO garmin_activity_record VALUES (?,?,?,?,?,?,?,?)", rows)

    srows = []
    ex = ["Bench Press", "Squat", "Deadlift", "Pull Up", "Row", "Shoulder Press"]
    for k in range(n_sets):
        aid = acts[k % 75][0]
        srows.append((aid, k % 40, rnd.choice(ex), 20.0 + rnd.random() * 60,
                      rnd.randint(5, 12), 30.0, "active"))
    con.executemany("INSERT INTO garmin_activity_set VALUES (?,?,?,?,?,?,?)", srows)

    con.executemany(
        "INSERT INTO garmin_activity VALUES (?,?,?,?,?)",
        [(aid, "2026-01-01", "{}", "[]", "[]") for aid, _ in acts],
    )

    con.execute("CREATE INDEX ix_rec ON garmin_activity_record(activity_id, t)")
    con.execute("CREATE INDEX ix_set ON garmin_activity_set(exercise, activity_id)")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


def timeit(fn, repeat: int = 5) -> tuple[float, float]:
    vals = []
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        vals.append((time.perf_counter() - t) * 1000)
    return statistics.median(vals), min(vals)


def bench(acts: list[tuple[str, int]]) -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    ids = [a for a, _ in acts]
    day_id = ids[-1]
    month_ids = ids[-30:]
    quarter_ids = ids[-84:]
    ph = ",".join("?" * len(month_ids))
    ph2 = ",".join("?" * len(quarter_ids))

    cases = {}

    cases["日报：当日逐秒序列"] = lambda: con.execute(
        "SELECT t,hr,speed,power,cadence,altitude,distance FROM garmin_activity_record "
        "WHERE activity_id=? ORDER BY t", (day_id,)).fetchall()

    cases["月报：30 活动均值聚合"] = lambda: con.execute(
        f"SELECT activity_id, AVG(hr), AVG(speed), MAX(hr) FROM garmin_activity_record "
        f"WHERE activity_id IN ({ph}) GROUP BY activity_id", month_ids).fetchall()

    cases["12周：84 活动均值聚合"] = lambda: con.execute(
        f"SELECT activity_id, AVG(hr), AVG(speed), MAX(hr) FROM garmin_activity_record "
        f"WHERE activity_id IN ({ph2}) GROUP BY activity_id", quarter_ids).fetchall()

    # TRIMP 类：需要逐秒 HR 做指数加权（用 SUM 近似，验证扫描代价）
    cases["12周：逐秒 TRIMP 类扫描"] = lambda: con.execute(
        f"SELECT activity_id, SUM(hr*hr)/100000.0 FROM garmin_activity_record "
        f"WHERE activity_id IN ({ph2}) GROUP BY activity_id", quarter_ids).fetchall()

    cases["月报：动作重量趋势（独立表）"] = lambda: con.execute(
        "SELECT exercise, AVG(weight) FROM garmin_activity_set "
        "WHERE activity_id IN (" + ",".join("?" * 30) + ") GROUP BY exercise",
        month_ids).fetchall()

    print(f"{'场景':<30} {'中位数ms':>9} {'最快ms':>8}")
    print("-" * 52)
    for name, fn in cases.items():
        med, best = timeit(fn)
        print(f"{name:<30} {med:>9.1f} {best:>8.1f}")

    n_rec = con.execute("SELECT COUNT(*) FROM garmin_activity_record").fetchone()[0]
    print(f"\n逐秒行数 {n_rec:,}")
    con.close()
    print(f"库体积 {DB.stat().st_size / 1048576:.1f} MB（含 WAL 已 checkpoint）")


def main() -> int:
    acts, n_records, n_sets = real_shape()
    print(f"真实规模：{len(acts)} 个活动 / {n_records:,} 逐秒点 / {n_sets:,} 个 set")
    build(acts, n_records, n_sets)
    bench(acts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
