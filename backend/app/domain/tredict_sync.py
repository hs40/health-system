"""Tredict 活动同步的共享层（P6 回填 / P8 上传共用，Q14）。

为什么抽出来
------------
P6（``backfill_tredict_activity.py``）与 P8（``upload_tredict_activity.py``）用的是
**同一套** raw 落盘规则、同一套 ``tredict_activity`` 行构造、同一张 ``sync_ledger``。
如果各写一份，P8 增量回填出来的行与 P6 全量回填出来的行迟早会漂移——
而 P7 的校验引擎正是拿这两边逐字段比对的，漂移会变成假警报。

所以这里只放「不依赖业务判断」的部分：
- raw 层 append-only 读写（``write_if_absent`` / ``fetch_details``）
- ``TredictRef`` / ``TredictActivityRow`` 的构造
- ``sync_ledger`` 写入

匹配策略、报告结构、CLI 开关仍留在各自脚本里。

raw 层为什么 append-only
------------------------
``data/raw`` 是**取证层**：一旦写入就代表「那一刻远端长这样」。内容变了要显式
``--refresh``，绝不静默覆盖——否则事后无法解释「为什么昨天的报告说 298 条」。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from app.adapters.tredict import canonical_sha256, parse_utc_iso, trim_series_sampled
from app.db.models import SyncLedgerRow, TredictActivityRow
from app.db.session import session_scope
from app.domain.matching import GarminRef, TredictRef, pick_duration
from app.storage.activity_source import load_activities

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent

RAW_DIR = ROOT / "data" / "raw" / "tredict"
ACTIVITY_DIR = RAW_DIR / "activities"
LIST_PATH = RAW_DIR / "activity_list.json"

# 口径 A：Tredict 的 date 是真 UTC，本地日期 = UTC + 8h
LOCAL_TZ_OFFSET = dt.timedelta(hours=8)

ENTITY = "tredict_activity"
DIRECTION_PULL = "tredict2local"
DIRECTION_PUSH = "local2tredict"


# ============================================================
# raw 层
# ============================================================
def write_if_absent(path: Path, payload: dict | list, *, refresh: bool) -> tuple[str, bool]:
    """写 raw（append-only）→ ``(sha256, 是否新写)``。

    已存在且内容相同 → 不写；内容不同且未 ``refresh`` → 报错，绝不静默覆盖。
    """
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sha = canonical_sha256(payload)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not refresh:
        old_sha = canonical_sha256(json.loads(path.read_text(encoding="utf-8")))
        if old_sha == sha:
            return sha, False
        raise RuntimeError(
            f"{path.name} 内容已变（本地 {old_sha[:8]} ≠ 远端 {sha[:8]}）；"
            "raw 层 append-only，确认要覆盖请加 --refresh"
        )

    path.write_text(blob, encoding="utf-8")
    return sha, True


def fetch_details(
    ids: list[str], *, refresh: bool, dry_run: bool
) -> tuple[dict[str, dict], dict[str, str], list[str]]:
    """逐条取详情并落 raw → ``(详情, 哈希表, 失败列表)``。

    已存在的 raw 文件默认直接复用（天然幂等，也让失败项下次重试）。
    """
    from app.adapters.tredict import TredictAdapter
    from app.config import get_settings

    details: dict[str, dict] = {}
    hashes: dict[str, str] = {}
    failed: list[str] = []
    to_fetch: list[str] = []

    for aid in ids:
        path = ACTIVITY_DIR / f"{aid}.json"
        if path.exists() and not refresh:
            payload = json.loads(path.read_text(encoding="utf-8"))
            details[aid] = payload
            hashes[aid] = canonical_sha256(payload)
        else:
            to_fetch.append(aid)

    if not to_fetch:
        print(f"[raw] 复用已有详情 {len(details)} 条，无需联网")
        return details, hashes, failed

    if dry_run:
        print(f"[dry-run] 待抓取 {len(to_fetch)} 条详情（未联网、未写盘）")
        return details, hashes, failed

    adapter = TredictAdapter(get_settings())
    fetched_ok = 0
    for i, aid in enumerate(to_fetch, 1):
        try:
            raw = adapter.fetch_activity_detail(aid)
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断（Q18）
            print(f"  [fail] {aid} {type(exc).__name__}: {exc}")
            failed.append(aid)
            continue
        if raw is None:
            print(f"  [404] {aid} 远端已不存在")
            failed.append(aid)
            continue

        trimmed = trim_series_sampled(raw)
        try:
            sha, _ = write_if_absent(ACTIVITY_DIR / f"{aid}.json", trimmed, refresh=refresh)
        except RuntimeError as exc:
            print(f"  [conflict] {exc}")
            failed.append(aid)
            continue
        details[aid] = trimmed
        hashes[aid] = sha
        fetched_ok += 1
        if i % 25 == 0 or i == len(to_fetch):
            print(f"  {i}/{len(to_fetch)} …")

    print(f"[raw] 待抓 {len(to_fetch)} 条，成功 {fetched_ok}，失败 {len(failed)}")
    return details, hashes, failed


# ============================================================
# 库层
# ============================================================
def load_garmin_refs(raw_root: Path) -> tuple[list[GarminRef], int]:
    """读全部 Garmin 活动 → 匹配用的最小视图（``(refs, 总数)``）。

    2026-10-21：改读 raw（``garmin_activity`` 表已废弃——它只由一次性 backfill
    写入，日常管线从不更新，导致新活动永远进不了匹配/上传）。
    """
    acts = load_activities(raw_root)
    refs = [
        GarminRef(
            activity_id=r.activity_id,
            start_utc=r.start_utc,
            activity_type=r.activity_type,
            duration_sec=r.duration_sec,
        )
        for r in acts.values()
        if r.start_utc is not None
    ]
    return refs, len(acts)


def to_tredict_ref(aid: str, payload: dict[str, Any]) -> TredictRef | None:
    """Tredict 详情 → 匹配用引用；``date`` 不可解析时返回 ``None``。"""
    start = parse_utc_iso(payload.get("date"))
    if start is None:
        return None
    summary = payload.get("summary") or {}
    return TredictRef(
        tredict_id=aid,
        start_utc=start,
        sport_type=str(payload.get("sportType") or ""),
        sub_sport_type=str(payload.get("subSportType") or ""),
        duration_sec=summary.get("duration"),
        duration_total_sec=summary.get("durationTotal"),
    )


def build_row(aid: str, payload: dict[str, Any], sha: str, ref: TredictRef) -> TredictActivityRow:
    summary = payload.get("summary") or {}
    return TredictActivityRow(
        tredict_id=aid,
        utc_datetime=ref.start_utc,
        local_date=(ref.start_utc + LOCAL_TZ_OFFSET).date(),
        sport_type=ref.sport_type,
        sub_sport_type=ref.sub_sport_type,
        title=str(payload.get("title") or ""),
        duration_total_sec=ref.duration_total_sec,
        duration_sec=ref.duration_sec,
        distance_m=summary.get("distance"),
        has_notes=bool(payload.get("notes")),
        training_type=str(payload.get("trainingType") or "file"),
        payload_sha256=sha,
    )


UPDATABLE_FIELDS: tuple[str, ...] = (
    "utc_datetime",
    "local_date",
    "sport_type",
    "sub_sport_type",
    "title",
    "duration_total_sec",
    "duration_sec",
    "distance_m",
    "has_notes",
    "training_type",
    "payload_sha256",
)


def upsert_rows(
    details: dict[str, dict], hashes: dict[str, str], *, dry_run: bool
) -> dict[str, int]:
    """按 sha 幂等写 ``tredict_activity`` + ``tredict2local`` ledger。"""
    counts = {"insert": 0, "update": 0, "skip": 0, "no_date": 0}

    for aid in sorted(details):
        ref = to_tredict_ref(aid, details[aid])
        if ref is None:
            counts["no_date"] += 1
            print(f"[warn] {aid} 无可用 date，跳过")
            continue
        row = build_row(aid, details[aid], hashes[aid], ref)

        with session_scope() as s:
            existing = s.get(TredictActivityRow, aid)
            if existing is not None and existing.payload_sha256 == hashes[aid]:
                counts["skip"] += 1
                continue
            if dry_run:
                counts["insert" if existing is None else "update"] += 1
                continue

            if existing is None:
                s.add(row)
                counts["insert"] += 1
            else:
                for name in UPDATABLE_FIELDS:
                    setattr(existing, name, getattr(row, name))
                counts["update"] += 1

            upsert_ledger(
                s,
                aid,
                local_hash=hashes[aid],
                remote_hash=hashes[aid],
                state="ok",
                detail={
                    "source": f"activities/{aid}.json",
                    "sport": f"{row.sport_type}/{row.sub_sport_type}",
                    "duration_field": pick_duration(ref)[1],
                },
            )
    return counts


def upsert_ledger(
    session: Any,
    entity_id: str,
    *,
    local_hash: str,
    remote_hash: str,
    state: str,
    detail: dict[str, Any],
    detail_key: str | None = None,
    direction: str = DIRECTION_PULL,
) -> None:
    """写一条 ``sync_ledger``（同一 (entity, entity_id, direction) 只保留一行）。

    ``detail_key`` 为 ``None`` 时字段直接并到根部（P6/P8 的写法，沿用存量 298 行的形状）；
    给值时做命名空间隔离（P7 用 ``"verify"``），避免两个用途互相覆盖。
    ``first_failed_at`` 只在「首次失败」时写入，成功时清空（P7 定义的语义）。
    """
    from sqlmodel import select

    row = session.exec(
        select(SyncLedgerRow).where(
            SyncLedgerRow.entity == ENTITY,
            SyncLedgerRow.entity_id == entity_id,
            SyncLedgerRow.direction == direction,
        )
    ).first()
    now = dt.datetime.now()
    ok = state == "ok"

    def _merge(base: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base or {})
        if detail_key is None:
            merged.update(detail)
        else:
            merged[detail_key] = detail
        return merged

    if row is None:
        session.add(
            SyncLedgerRow(
                entity=ENTITY,
                entity_id=entity_id,
                direction=direction,
                local_hash=local_hash,
                remote_hash=remote_hash,
                state=state,
                first_failed_at=None if ok else now,
                last_checked_at=now,
                attempts=1,
                detail=_merge({}),
            )
        )
        return

    merged = _merge(row.detail or {})
    if ok and row.state != "ok":
        merged["resolved_at"] = now.isoformat(timespec="seconds")
    row.detail = merged
    row.local_hash = local_hash
    row.remote_hash = remote_hash
    row.state = state
    row.last_checked_at = now
    row.attempts += 1
    row.first_failed_at = None if ok else (row.first_failed_at or now)
