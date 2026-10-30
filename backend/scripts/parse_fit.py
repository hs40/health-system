"""P4：把 FIT 解析成 jsonl.gz 归档 + SQLite 逐秒表。

流程（每个活动）：

1. 算 ``.fit`` 的 sha256
2. 与索引比对：sha256 未变且 jsonl 存在 → 跳过（幂等）
3. 官方 SDK 解析 + fitdecode 交叉校验（Q33=A）
4. 交叉校验失败 → **不写库**，记 ``sync_ledger(state=mismatch)``（Q36=A）
5. 写 ``fit_jsonl/<id>.jsonl.gz``（先写临时文件再原子替换）
6. 删旧行 + 批量插入 ``garmin_activity_record``
7. 更新索引 ``fit_jsonl/_index.json``

为什么索引是 JSON 文件而不是数据库表：
``--verify`` 需要在**不连数据库**的情况下也能判断「文件与归档是否一致」，
NAS 上跑校验时不必启动 backend。

用法：
    python scripts/parse_fit.py --dry-run        # 只解析不写盘
    python scripts/parse_fit.py --limit 3        # 先跑 3 个
    python scripts/parse_fit.py                  # 全量
    python scripts/parse_fit.py --verify         # 校验已有产物
    python scripts/parse_fit.py --force          # 忽略索引重解析
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.parsers.fit import (  # noqa: E402
    FitCrossCheckError,
    FitParseError,
    ParsedFit,
    parse_fit,
    write_jsonl_gz,
)

FIT_DIR = ROOT / "data" / "raw" / "garmin" / "fit"
JSONL_DIR = ROOT / "data" / "raw" / "garmin" / "fit_jsonl"
INDEX_PATH = JSONL_DIR / "_index.json"
REPORT_DIR = ROOT / "data" / "raw" / "garmin" / "_parse_reports"

BATCH = 5000


# ============================================================
# 索引
# ============================================================
def load_index() -> dict[str, Any]:
    if not INDEX_PATH.exists():
        return {}
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"[warn] 索引损坏，按空处理：{INDEX_PATH}")
        return {}


def save_index(index: dict[str, Any]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    os.replace(tmp, INDEX_PATH)


# ============================================================
# 数据库
# ============================================================
def _record_table():  # type: ignore[no-untyped-def]
    from app.db.models import GarminActivityRecordRow

    return GarminActivityRecordRow.__table__


def ensure_table() -> None:
    """确认迁移已执行，否则给出可操作的提示而不是一堆 traceback。"""
    import sqlalchemy as sa

    from app.db.session import engine

    insp = sa.inspect(engine)
    if "garmin_activity_record" not in insp.get_table_names():
        raise SystemExit(
            "缺少表 garmin_activity_record。请先执行：\n"
            "  cd backend && .venv/Scripts/alembic upgrade head"
        )


def replace_records(activity_id: str, parsed: ParsedFit) -> int:
    """删旧行 + 批量插入。返回写入行数。"""
    import sqlalchemy as sa

    from app.db.session import engine

    table = _record_table()
    rows = [
        {
            "activity_id": activity_id,
            "seq": r.seq,
            "t": r.t,
            "hr": r.hr,
            "speed": r.speed,
            "power": r.power,
            "cadence": r.cadence,
            "altitude": r.altitude,
            "distance": r.distance,
        }
        for r in parsed.records
    ]

    with engine.begin() as conn:
        conn.execute(sa.delete(table).where(table.c.activity_id == activity_id))
        for i in range(0, len(rows), BATCH):
            conn.execute(table.insert(), rows[i : i + BATCH])
    return len(rows)


def record_failure(activity_id: str, detail: dict[str, Any], reason: str) -> None:
    """Q36=A：交叉校验失败写 sync_ledger，不阻断其他活动。"""
    from sqlmodel import select

    from app.db.models import SyncLedgerRow
    from app.db.session import session_scope

    try:
        with session_scope() as s:
            existing = s.exec(
                select(SyncLedgerRow).where(
                    SyncLedgerRow.entity == "garmin_activity_record",
                    SyncLedgerRow.entity_id == activity_id,
                    SyncLedgerRow.direction == "garmin2local",
                )
            ).first()
            now = datetime.now()
            if existing is None:
                existing = SyncLedgerRow(
                    entity="garmin_activity_record",
                    entity_id=activity_id,
                    direction="garmin2local",
                    state="mismatch",
                    first_failed_at=now,
                    last_checked_at=now,
                    attempts=1,
                    detail={"reason": reason, **detail},
                )
                s.add(existing)
            else:
                existing.state = "mismatch"
                existing.last_checked_at = now
                existing.attempts += 1
                existing.detail = {"reason": reason, **detail}
    except Exception as exc:  # noqa: BLE001 —— 记账失败不应盖过原始错误
        print(f"[warn] 写 sync_ledger 失败：{type(exc).__name__}: {exc}")


# ============================================================
# 单文件处理
# ============================================================
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def process_one(
    path: Path,
    index: dict[str, Any],
    *,
    dry_run: bool,
    force: bool,
    cross_check: bool,
) -> tuple[str, dict[str, Any]]:
    """返回 (状态, 摘要)。状态：skipped|ok|failed。"""
    aid = path.stem.split("_")[0]
    digest = sha256_of(path)

    prev = index.get(aid)
    jsonl_path = JSONL_DIR / f"{aid}.jsonl.gz"
    if (
        not force
        and prev
        and prev.get("fit_sha256") == digest
        and jsonl_path.exists()
    ):
        return "skipped", {"activity_id": aid, "reason": "sha256 未变"}

    t0 = time.perf_counter()
    try:
        parsed = parse_fit(path, cross_check=cross_check)
    except FitCrossCheckError as exc:
        if not dry_run:
            record_failure(aid, exc.detail, "cross_check_failed")
        return "failed", {
            "activity_id": aid,
            "reason": "cross_check_failed",
            "detail": str(exc),
            "mismatches": exc.detail.get("mismatches", [])[:5],
        }
    except FitParseError as exc:
        if not dry_run:
            record_failure(aid, {}, "parse_error")
        return "failed", {"activity_id": aid, "reason": "parse_error", "detail": str(exc)}

    elapsed = (time.perf_counter() - t0) * 1000

    summary = {
        "activity_id": aid,
        "fit_bytes": parsed.file_bytes,
        "records": parsed.record_count,
        "laps": len(parsed.laps),
        "sets": len(parsed.sets),
        "zones": len(parsed.zones),
        "splits": len(parsed.splits),
        "parse_ms": round(elapsed, 1),
    }

    if dry_run:
        return "ok", summary

    lines = write_jsonl_gz(parsed, jsonl_path)
    written = replace_records(aid, parsed)

    index[aid] = {
        "fit_sha256": digest,
        "fit_bytes": parsed.file_bytes,
        "jsonl_bytes": jsonl_path.stat().st_size,
        "jsonl_sha256": sha256_of(jsonl_path),
        "jsonl_lines": lines,
        "records": written,
        "laps": len(parsed.laps),
        "sets": len(parsed.sets),
        "zones": len(parsed.zones),
        "splits": len(parsed.splits),
        "parsed_at": datetime.now().isoformat(timespec="seconds"),
    }
    summary["jsonl_bytes"] = index[aid]["jsonl_bytes"]
    return "ok", summary


# ============================================================
# 校验
# ============================================================
def verify(index: dict[str, Any]) -> int:
    """校验产物一致性。返回问题数。"""
    import sqlalchemy as sa

    from app.db.session import engine

    ensure_table()
    table = _record_table()
    problems = 0

    print(f"[verify] 索引 {len(index)} 条")
    with engine.connect() as conn:
        db_counts = dict(
            conn.execute(
                sa.select(table.c.activity_id, sa.func.count()).group_by(
                    table.c.activity_id
                )
            ).all()
        )

    for aid, meta in sorted(index.items()):
        jsonl_path = JSONL_DIR / f"{aid}.jsonl.gz"
        if not jsonl_path.exists():
            print(f"  [缺归档] {aid}")
            problems += 1
            continue
        if jsonl_path.stat().st_size != meta.get("jsonl_bytes"):
            print(f"  [归档大小不符] {aid}")
            problems += 1
        if sha256_of(jsonl_path) != meta.get("jsonl_sha256"):
            print(f"  [归档 sha256 不符] {aid}")
            problems += 1

        fit_path = FIT_DIR / f"{aid}.fit"
        if fit_path.exists() and sha256_of(fit_path) != meta.get("fit_sha256"):
            print(f"  [FIT 已变更，需重解析] {aid}")
            problems += 1

        db_n = db_counts.get(aid, 0)
        if db_n != meta.get("records"):
            print(f"  [逐秒行数不符] {aid} db={db_n} index={meta.get('records')}")
            problems += 1

    # 孤儿行（库里有、索引没有）
    orphans = [aid for aid in db_counts if aid not in index]
    if orphans:
        print(f"  [孤儿行] {len(orphans)} 个活动不在索引中：{orphans[:5]}")
        problems += len(orphans)

    total_rows = sum(db_counts.values())
    print(f"[verify] 逐秒表 {total_rows} 行 / {len(db_counts)} 个活动")
    print(f"[verify] 问题 {problems} 个")
    return problems


# ============================================================
# main
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="FIT → jsonl.gz + SQLite 逐秒表")
    ap.add_argument("--dry-run", action="store_true", help="只解析，不写盘不写库")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件")
    ap.add_argument("--only", nargs="*", default=[], help="只处理指定 activityId")
    ap.add_argument("--force", action="store_true", help="忽略索引，强制重解析")
    ap.add_argument("--verify", action="store_true", help="只校验已有产物")
    ap.add_argument(
        "--no-cross-check", action="store_true", help="跳过 fitdecode 交叉校验（仅调试）"
    )
    args = ap.parse_args()

    index = load_index()

    if args.verify:
        return 1 if verify(index) else 0

    files = sorted(FIT_DIR.glob("*.fit"))
    if args.only:
        wanted = set(args.only)
        files = [p for p in files if p.stem.split("_")[0] in wanted]
    if args.limit:
        files = files[: args.limit]
    if not files:
        raise SystemExit(f"未找到 FIT：{FIT_DIR}")

    if not args.dry_run:
        ensure_table()
        JSONL_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[parse] 待处理 {len(files)} 个文件  dry_run={args.dry_run}  force={args.force}")

    t0 = time.perf_counter()
    counts = {"ok": 0, "skipped": 0, "failed": 0}
    failures: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []

    for i, path in enumerate(files, 1):
        status, summary = process_one(
            path,
            index,
            dry_run=args.dry_run,
            force=args.force,
            cross_check=not args.no_cross_check,
        )
        counts[status] += 1
        summaries.append(summary)
        if status == "failed":
            failures.append(summary)
            print(f"  [{i}/{len(files)}] FAILED {summary['activity_id']} {summary['reason']}")
        elif i % 50 == 0 or i == len(files):
            print(f"  {i}/{len(files)} … ok={counts['ok']} skip={counts['skipped']}")

    elapsed = time.perf_counter() - t0

    if not args.dry_run:
        save_index(index)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORT_DIR / f"{datetime.now():%Y%m%dT%H%M%S}.json"
        report_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "dry_run": args.dry_run,
                    "counts": counts,
                    "elapsed_sec": round(elapsed, 1),
                    "failures": failures,
                    "items": summaries,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"\n报告：{report_path}")

    total_rows = sum(s.get("records", 0) for s in summaries if "records" in s)
    print(
        f"\n[parse] 成功 {counts['ok']}  跳过 {counts['skipped']}  失败 {counts['failed']}"
        f"  用时 {elapsed:.1f}s  逐秒行 {total_rows}"
    )
    if args.dry_run:
        print("[dry-run] 未写 jsonl、未写数据库、未改索引")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
