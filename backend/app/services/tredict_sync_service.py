"""Tredict 同步服务（V43）——容器内**直接函数调用**，不再 shell-out 到 scripts。

为什么要有这一层
----------------
V42 让调度器 ``subprocess`` 调 ``scripts/daily_sync.py``，但 ``Dockerfile.single``
只 ``COPY backend/app`` —— 镜像里没有 ``scripts/``，任务静默失败且无人发现。
本模块把「活动上传 + 健康上传 + 云端校验」的编排放进 ``app/`` 包内，
**一定**会随 ``COPY backend/app`` 进镜像；路径全部来自 ``settings``（compose
把数据挂到 ``/data/...``），不再有脚本里 ``ROOT/"data"/...`` 的硬编码。

设计约束
--------
- **依赖注入**：``garmin`` / ``tredict`` / ``fit_fetcher`` 由调用方传入，便于
  单测用 fake 断言「真的调用了 upload_activity」，而不是 mock 掉 subprocess 数字符串。
- **只传缺失**：活动用 ``match_activities`` 判定云端是否已存在，避免造重复；
  健康用「读回云端 → 差集」只传缺项（HRV/bodyvalues 是纯追加端点，重复 POST 会累积）。
- **FIT 自动补齐**：本地缺 FIT 的活动由 ``fit_fetcher`` 下载并 append-only 落盘（Q2），
  首次运行即可补传历史活动。
- **失败可观测**：无论成功失败都写一条 ``tredictSync`` audit；失败/差异写消息中心
  告警。绝不静默吞异常（V42 的教训）。
- **不崩调度器**：任一子步骤异常都收敛进 ``errors``，返回结构化结果。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlmodel import Session

from app.config import Settings
from app.schemas.enums import AuditCategory, MessageCategory, MessageImportance
from app.services.strength_notes_service import build_strength_activity_notes

logger = logging.getLogger("hs.services.tredict_sync")

ENTITY_REF = "tredictSync"


# ============================================================
# 路径（全部来自 settings —— 容器里是 /data/...）
# ============================================================
def garmin_raw_dir(settings: Settings) -> Path:
    return Path(settings.raw_data_dir) / "garmin"


def fit_dir_for(settings: Settings) -> Path:
    return garmin_raw_dir(settings) / "fit"


# ============================================================
# 当天活动供给（2026-11-07 修复根因）
# ============================================================
def refresh_today_activities(
    garmin: Any,
    settings: Settings | None,
    today: dt.date,
    *,
    attempts: int = 3,
    sleeper: Callable[[], None] | None = None,
) -> dict:
    """23:00 上传前刷新「当天」activities 快照，让当天训练当天可见。

    根因：``load_activities()`` 只读 ``activities/*.json``，而该快照仅由 07:30
    日任务写「当天」+ 次日回补「前一天」；当天 07:30 后完成的训练当天 23:00
    不可见，稳定延迟一天。V42/V43 只修上传执行侧、从未碰数据供给侧。

    行为（Q3 = 任务内轻量重拉、不改调度拓扑）：
    - 只调 ``sync_day(today, datasets=["activities"])``；
    - 空返回视为「可能索引延迟」，最多重拉 ``attempts`` 次；仍空按休息日
      正常结束（``empty=True``、不告警，避免休息日误报）；
    - 任一异常收敛进 ``errors``，绝不抛出（Q1：局部容错、不连坐健康上传）。

    返回结构化结果，供 job 层并入 audit 结果。``settings`` 仅为签名对称保留，
    路径由 ``garmin`` adapter 自己从 settings 解析。
    """
    out: dict = {
        "date": today.isoformat(),
        "refreshed": False,
        "empty": False,
        "attempts": 0,
        "errors": [],
    }
    for i in range(attempts):
        out["attempts"] = i + 1
        try:
            result = garmin.sync_day(today, datasets=["activities"])
        except Exception as e:  # noqa: BLE001 — 局部容错，不连坐上传
            out["errors"].append(f"{type(e).__name__}: {e}")
            logger.warning("刷新当天 activities 第 %d 次异常：%s", i + 1, e)
            if sleeper is not None:
                sleeper()
            continue

        written = list(getattr(result, "written", None) or [])
        skipped = list(getattr(result, "skipped", None) or [])
        errors = dict(getattr(result, "errors", None) or {})
        if errors.get("activities"):
            out["errors"].append(f"activities: {errors['activities']}")
        if "activities" in written or "activities" in skipped:
            out["refreshed"] = True
            return out
        # 空返回：可能 Garmin 索引延迟 → 轻量重拉
        if sleeper is not None:
            sleeper()
    out["empty"] = True
    return out


# ============================================================
# 纯判定：哪些活动云端还没有
# ============================================================
def _refs_for_matching(activities: dict, cloud_list: list[dict]) -> tuple[list, list]:
    """本地 ``ActivityRef`` / 云端清单 → ``(GarminRef[], TredictRef[])``。"""
    from app.adapters.tredict import parse_utc_iso
    from app.domain.matching import GarminRef, TredictRef

    grefs = [
        GarminRef(
            activity_id=r.activity_id,
            start_utc=r.start_utc,
            activity_type=r.activity_type,
            duration_sec=float(r.duration_sec) if r.duration_sec else None,
        )
        for r in activities.values()
        if r.start_utc is not None
    ]
    trefs = []
    for item in cloud_list:
        if not isinstance(item, dict):
            continue
        start = parse_utc_iso(item.get("date"))
        tid = item.get("id")
        if start is None or tid is None:
            continue
        summary = item.get("summary") or {}
        trefs.append(
            TredictRef(
                tredict_id=str(tid),
                start_utc=start,
                sport_type=str(item.get("sportType") or ""),
                sub_sport_type=str(item.get("subSportType") or ""),
                duration_sec=summary.get("duration"),
                duration_total_sec=summary.get("durationTotal"),
            )
        )
    return grefs, trefs


def missing_activity_ids(activities: dict, cloud_list: list[dict]) -> list[str]:
    """本地有、云端无的 Garmin activity_id（复用 domain/matching 纯函数）。"""
    from app.domain.matching import CONFIDENCE_AMBIGUOUS, match_activities, unmatched_garmin

    grefs, trefs = _refs_for_matching(activities, cloud_list)
    outcomes = match_activities(grefs, trefs)

    # 匹配歧义的活动绝不上传（否则云端会重复）——与 P8 脚本同口径。
    ambiguous: set[str] = set()
    for o in outcomes:
        if o.confidence != CONFIDENCE_AMBIGUOUS:
            continue
        for key in ("candidates", "claimants"):
            for v in o.detail.get(key) or []:
                ambiguous.add(str(v))
        if o.detail.get("garmin_id"):
            ambiguous.add(str(o.detail["garmin_id"]))

    return [aid for aid in unmatched_garmin(grefs, outcomes) if aid not in ambiguous]


# ============================================================
# FIT：本地读 / 缺则下载并 append-only 落盘
# ============================================================
def _ensure_fit(
    aid: str, settings: Settings, fit_fetcher: Callable[[str], bytes | None]
) -> bytes | None:
    """返回 FIT 字节；本地无则 ``fit_fetcher`` 下载并落盘（不覆盖已有）。"""
    path = fit_dir_for(settings) / f"{aid}.fit"
    if path.exists():
        return path.read_bytes()
    blob = fit_fetcher(aid)
    if not blob:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)  # append-only：调用前已确认不存在
    return blob


def default_fit_fetcher(garmin: Any) -> Callable[[str], bytes | None]:
    """生产用：Garmin ``download_activity`` → 解 ZIP 取 .fit → 校验 FIT 头。

    与 ``scripts/pull_garmin_fit.py`` 同口径，但**不 import scripts**（那些脚本
    不在镜像里）。逐条失败返回 None，由上层记入 no_fit。
    """

    def _fetch(aid: str) -> bytes | None:
        import io
        import zipfile

        try:
            client = garmin.client()
            fmt = client.ActivityDownloadFormat.ORIGINAL
            blob = client.download_activity(aid, fmt)
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                names = [n for n in z.namelist() if n.lower().endswith(".fit")]
                if len(names) != 1:
                    logger.warning("%s ZIP 内 .fit 数=%d，跳过", aid, len(names))
                    return None
                fit = z.read(names[0])
            if len(fit) < 14 or fit[8:12] != b".FIT":
                logger.warning("%s FIT 头不合法，跳过", aid)
                return None
            return fit
        except Exception as e:  # noqa: BLE001 — 单条失败不阻断
            logger.warning("下载 FIT %s 失败：%s", aid, e)
            return None

    return _fetch


# ============================================================
# 子步骤：活动 / 健康 / 校验
# ============================================================
def _backfill_notes(
    settings: Settings,
    tredict: Any,
    activities: dict,
    cloud_list: list[dict],
    fit_fetcher: Callable[[str], bytes | None] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Q4：对云端已存在、notes 为空的记录补写 notes（只补空、幂等）。

    只处理力量活动（非力量没有 notes 语义）；已有 notes 的一律不覆盖。
    返回 ``(meta_updated, meta_failed)``。
    """
    from app.domain.matching import match_activities
    from app.domain.strength.notes import NOTES_LIMIT_UPDATE

    updated: list[dict] = []
    failed: list[dict] = []
    cloud_notes = {
        str(item.get("id")): str(item.get("notes") or "")
        for item in cloud_list
        if isinstance(item, dict) and item.get("id") is not None
    }
    grefs, trefs = _refs_for_matching(activities, cloud_list)
    for outcome in match_activities(grefs, trefs):
        if not outcome.garmin_id or not outcome.tredict_id:
            continue
        if cloud_notes.get(str(outcome.tredict_id)):
            continue  # 已有 notes：只补空，绝不覆盖
        want = build_strength_activity_notes(
            settings, activities.get(outcome.garmin_id), NOTES_LIMIT_UPDATE,
            fit_fetcher=fit_fetcher,
        )
        if not want:
            continue
        try:
            r = tredict.update_activity(str(outcome.tredict_id), {"notes": want})
        except Exception as e:  # noqa: BLE001 — 单条失败不阻断
            failed.append({
                "activity_id": outcome.garmin_id,
                "tredict_id": outcome.tredict_id,
                "error": f"{type(e).__name__}: {e}",
            })
            continue
        if r.get("ok"):
            updated.append({
                "activity_id": outcome.garmin_id,
                "tredict_id": outcome.tredict_id,
                "fields": ["notes"],
            })
        else:
            failed.append({
                "activity_id": outcome.garmin_id,
                "tredict_id": outcome.tredict_id,
                "status": r.get("status"),
                "error": r.get("text", ""),
            })
    return updated, failed


def _record_upload_ledger(
    session: Session, aid: str, fit: bytes, result: dict, notes_len: int
) -> None:
    """写一条 ``local2tredict`` 台账（Q2 遗留③：运维台直接读这张表）。

    失败不抛：台账是观测手段，不能反向阻断上传主流程。
    """
    import hashlib

    from app.domain.tredict_sync import DIRECTION_PUSH, upsert_ledger

    try:
        upsert_ledger(
            session,
            aid,
            local_hash=hashlib.sha256(fit).hexdigest(),
            remote_hash="",
            state="ok" if (result.get("ok") or result.get("exists")) else "missing_remote",
            direction=DIRECTION_PUSH,
            detail={
                "action": "upload",
                "ok": bool(result.get("ok")),
                "status": result.get("status"),
                "notes_len": notes_len,
                "at": dt.datetime.now().isoformat(timespec="seconds"),
            },
        )
    except Exception as e:  # noqa: BLE001 — 台账失败不阻断上传
        logger.error("写 sync_ledger 失败 %s：%s", aid, e)


def _sync_activities(
    session: Session,
    settings: Settings,
    tredict: Any,
    fit_fetcher: Callable[[str], bytes | None],
    activities: dict,
    cloud_list: list[dict],
) -> dict:
    from app.domain.strength.notes import NOTES_LIMIT_UPLOAD
    from app.storage.activity_source import load_activities

    if not activities:
        activities = load_activities(garmin_raw_dir(settings))
    missing = missing_activity_ids(activities, cloud_list)

    uploaded: list[str] = []
    exists: list[str] = []
    failed: list[dict] = []
    no_fit: list[str] = []

    for aid in missing:
        fit = _ensure_fit(aid, settings, fit_fetcher)
        if fit is None:
            no_fit.append(aid)
            continue
        ref = activities.get(aid)
        name = str(ref.name) if ref is not None else ""
        notes = build_strength_activity_notes(
            settings, ref, NOTES_LIMIT_UPLOAD, fit_fetcher=fit_fetcher
        )
        try:
            r = tredict.upload_activity(aid, fit, name=name, notes=notes)
        except Exception as e:  # noqa: BLE001 — 单条失败不阻断
            failed.append({"activity_id": aid, "error": f"{type(e).__name__}: {e}"})
            _record_upload_ledger(
                session, aid, fit,
                {"ok": False, "status": None, "text": f"{type(e).__name__}: {e}"},
                len(notes),
            )
            continue
        _record_upload_ledger(session, aid, fit, r, len(notes))
        if r.get("ok") or r.get("exists"):
            (exists if r.get("exists") else uploaded).append(aid)
        else:
            failed.append(
                {"activity_id": aid, "status": r.get("status"), "error": r.get("text", "")}
            )

    meta_updated, meta_failed = _backfill_notes(
        settings, tredict, activities, cloud_list, fit_fetcher
    )
    return {
        "local": len(activities),
        "cloud": len(cloud_list),
        "missing": missing,
        "uploaded": uploaded,
        "exists": exists,
        "failed": failed,
        "no_fit": no_fit,
        "meta_updated": meta_updated,
        "meta_failed": meta_failed,
    }


def _upsert_cloud_mirror(session: Session, cloud_list: list[dict]) -> int:
    """Q2 遗留③：把云端**新出现**的活动补进 ``tredict_activity`` 镜像。

    只做「不存在则插入」：已存在的行交给 backfill（详情 sha）维护，避免两套
    sha 口径（清单项 vs 详情）反复互相覆盖。单条失败不影响主流程。
    """
    from app.db.models import TredictActivityRow
    from app.domain.tredict_sync import build_row, to_tredict_ref
    from app.domain.verify import canonical_sha256

    inserted = 0
    for item in cloud_list:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        tid = str(item["id"])
        try:
            if session.get(TredictActivityRow, tid) is not None:
                continue
            ref = to_tredict_ref(tid, item)
            if ref is None:
                continue
            session.add(build_row(tid, item, canonical_sha256(item), ref))
            inserted += 1
        except Exception as e:  # noqa: BLE001 — 镜像写失败不阻断上传
            logger.warning("镜像写入跳过 %s：%s", tid, e)
    return inserted


def _sync_health(settings: Settings, tredict: Any) -> dict:
    """健康数据差集上传（读回云端，只传缺失）——与 sync_tredict.py 同口径。"""
    from app.adapters.garmin import load_raw_dataset, load_raw_weight_list
    from app.adapters.tredict import (
        BODYVALUES_URL,
        HRV_URL,
        SLEEP_URL,
        bodyvalues_todo,
        build_bodyvalue_records,
        build_hrv_records,
        build_sleep_records,
        ymd,
    )

    root = garmin_raw_dir(settings)
    hrv_raw = load_raw_dataset(root, "hrv")
    sleep_raw = load_raw_dataset(root, "sleep")
    daily_raw = load_raw_dataset(root, "daily_summary")
    weight_list = load_raw_weight_list(root)

    hrv_records, _, _ = build_hrv_records(hrv_raw)
    sleep_records, _, _ = build_sleep_records(sleep_raw)
    bv_records, _ = build_bodyvalue_records(daily_raw, weight_list)

    cloud = tredict.fetch_health()
    have_hrv = set(cloud.get("hrv") or {})
    have_sleep = set(cloud.get("sleep") or {})
    cloud_bv = cloud.get("bodyvalues") or []
    todo_hrv = [r for r in hrv_records if ymd(r["timestamp"]) not in have_hrv]
    todo_sleep = [r for r in sleep_records if ymd(r["endTimestamp"]) not in have_sleep]
    todo_bv = bodyvalues_todo(bv_records, cloud_bv)

    posted = 0
    errors: list[str] = []
    for url, key, records in (
        (HRV_URL, "hrv", todo_hrv),
        (SLEEP_URL, "sleep", todo_sleep),
        (BODYVALUES_URL, "bodyvalues", todo_bv),
    ):
        if not records:
            continue
        try:
            r = tredict._post_batch(url, key, records)  # noqa: SLF001
            posted += int(r.get("ok", 0))
            errors += [str(e) for e in r.get("errors", [])]
        except Exception as e:  # noqa: BLE001
            errors.append(f"{key}: {type(e).__name__}: {e}")
    return {"hrv": len(todo_hrv), "sleep": len(todo_sleep),
            "bodyvalues": len(todo_bv), "posted": posted, "errors": errors}


def _verify_coverage(
    activities: dict, cloud_list: list[dict], confirmed: set[str]
) -> dict:
    """上传后云端清单 vs 本地活动：区分「真覆盖 / 待索引 / 真缺失」。

    ``confirmed`` = 本次 ``upload_activity`` 返回 ok/exists 的活动。云端清单通常
    不会立即索引刚上传的活动（有延迟）。V43 直接把 ``confirmed`` 无条件并入
    ``covered`` → 即使云端根本没建记录也判 ok、永不告警（**假绿**，2026-11-07 修）。

    修复：只有云端清单里**真实匹配**到的才算已覆盖；本次已上传但云端尚未出现的
    记为 ``pending_cloud_index``（显式暴露 + 落告警待复核），既不静默判绿，也不
    误报为 ``only_garmin`` 硬缺失（避免刚传完就假红）。真正没传上去的
    （failed / no_fit）仍留在 ``only_garmin`` 触发告警。
    """
    from app.domain.matching import match_activities
    from app.domain.verify import compare_activity_coverage

    grefs, trefs = _refs_for_matching(activities, cloud_list)
    outcomes = match_activities(grefs, trefs)
    cloud_matched = {o.garmin_id for o in outcomes if o.garmin_id}
    pending = sorted(confirmed - cloud_matched)
    covered = cloud_matched | set(pending)
    cov = compare_activity_coverage({g.activity_id for g in grefs}, covered)
    return {
        "only_garmin": cov["only_garmin"],
        "only_tredict": cov["only_tredict"],
        "pending_cloud_index": pending,
    }


# ============================================================
# 可观测：audit + 消息中心
# ============================================================
def _write_audit(session: Session, settings: Settings, result: dict, today: dt.date) -> None:
    from app.storage.audit_store import record

    try:
        record(
            session,
            AuditCategory.TASK,
            ref=f"{ENTITY_REF}:{today.isoformat()}",
            detail={
                "ok": result["ok"],
                "activities": {k: (len(v) if isinstance(v, list) else v)
                               for k, v in result["activities"].items()},
                "health": result["health"],
                "verify": result["verify"],
                "today_refresh": result.get("today_refresh"),
                "cloud_mirror_inserted": result.get("cloud_mirror_inserted", 0),
                "errors": result["errors"],
            },
            audit_dir=Path(settings.audit_dir),
        )
    except Exception as e:  # noqa: BLE001 — audit 失败不该掩盖主结论
        logger.error("写 audit 失败：%s", e)


def _alert(session: Session, settings: Settings, result: dict, today: dt.date) -> None:
    """失败或校验有差异 → 消息中心告警（同一天同一结论只发一次）。"""
    from sqlmodel import select

    from app.db.models import MessageRow
    from app.services.notify_service import send_message

    reasons: list[str] = []
    a = result["activities"]
    if a["failed"]:
        reasons.append(f"活动上传失败 {len(a['failed'])} 条")
    if a["no_fit"]:
        reasons.append(f"缺 FIT {len(a['no_fit'])} 条")
    if a.get("meta_failed"):
        reasons.append(f"补 notes 失败 {len(a['meta_failed'])} 条")
    if result["errors"]:
        reasons.append(f"异常 {len(result['errors'])} 项")
    if result["verify"]["only_garmin"]:
        reasons.append(f"覆盖仍缺 {len(result['verify']['only_garmin'])} 条")
    if result["verify"].get("pending_cloud_index"):
        reasons.append(
            f"已上传但云端尚未索引 {len(result['verify']['pending_cloud_index'])} 条（待复核）"
        )
    if result["health"]["errors"]:
        reasons.append(f"健康上传错误 {len(result['health']['errors'])} 项")
    if not reasons:
        return

    ref_id = f"{ENTITY_REF}:{today.isoformat()}"
    try:
        exists = session.exec(
            select(MessageRow).where(
                MessageRow.ref_type == ENTITY_REF, MessageRow.ref_id == ref_id
            )
        ).first()
        if exists is not None:
            return
        send_message(
            session,
            category=MessageCategory.NEED_ACTION,
            title="Tredict 同步异常",
            body="；".join(reasons) + "。请查看运维台 audit 与容器日志。",
            ref_type=ENTITY_REF,
            ref_id=ref_id,
            link="/ops",
            importance=MessageImportance.HIGH,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("写告警失败：%s", e)


# ============================================================
# 编排入口
# ============================================================
def run_tredict_sync(
    session: Session,
    settings: Settings,
    *,
    garmin: Any = None,
    tredict: Any = None,
    fit_fetcher: Callable[[str], bytes | None] | None = None,
    today: dt.date | None = None,
    today_refresher: Callable[[dt.date], dict] | None = None,
) -> dict:
    """执行「当天刷新 + 活动上传 + 健康上传 + 云端校验」，返回结构化结果，不抛异常。

    ``garmin`` / ``tredict`` 缺省时由本服务构造真实 adapter（生产路径）；
    测试注入 fake。``fit_fetcher`` 缺省时用 ``default_fit_fetcher(garmin)``。
    ``today_refresher`` 由 job 层注入（2026-11-07 根因修复）：在读取本地快照
    **之前**刷新当天 activities，使当天 07:30 后完成的训练当天即可见。
    缺省 ``None`` 保持纯编排语义，便于既有测试注入 fake。
    """
    from app.adapters.garmin import GarminAdapter
    from app.adapters.tredict import TredictAdapter

    if garmin is None:
        garmin = GarminAdapter(settings)
    if tredict is None:
        tredict = TredictAdapter(settings)
    today = today or dt.date.today()
    fetcher = fit_fetcher if fit_fetcher is not None else default_fit_fetcher(garmin)

    result: dict[str, Any] = {
        "ok": False,
        "date": today.isoformat(),
        "activities": {"local": 0, "cloud": 0, "missing": [], "uploaded": [],
                       "exists": [], "failed": [], "no_fit": [],
                       "meta_updated": [], "meta_failed": []},
        "health": {"hrv": 0, "sleep": 0, "bodyvalues": 0, "posted": 0, "errors": []},
        "verify": {"only_garmin": [], "only_tredict": [], "pending_cloud_index": []},
        "cloud_mirror_inserted": 0,
        "today_refresh": {"date": today.isoformat(), "refreshed": False, "empty": False,
                          "attempts": 0, "errors": []},
        "errors": [],
    }

    # 根因修复：先刷新当天 activities 快照，再读本地活动 —— 顺序不可颠倒。
    # 局部容错：刷新失败只记 errors，不中断上传（Q1：不连坐健康上传）。
    if today_refresher is not None:
        try:
            result["today_refresh"] = today_refresher(today)
        except Exception as e:  # noqa: BLE001
            result["today_refresh"]["errors"].append(f"{type(e).__name__}: {e}")
            logger.error("当天活动刷新失败：%s", e)
        for err in result["today_refresh"].get("errors") or []:
            result["errors"].append(f"当天活动刷新：{err}")

    activities: dict = {}
    cloud_list: list[dict] = []
    cloud_ok = False
    from app.storage.activity_source import load_activities

    try:
        activities = load_activities(garmin_raw_dir(settings))
        cloud_list = list(tredict.fetch_activity_list(extended=True))
        cloud_ok = True
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"读取活动/云端清单失败：{type(e).__name__}: {e}")
        logger.error("%s", result["errors"][-1])

    # 云端清单拉不到时**熔断上传**：无法确认「缺失」，盲传会在云端造重复活动
    # （P8 脚本同款安全阀）。此时 result["activities"] 保持全空，校验也不跑。
    if cloud_ok:
        try:
            result["activities"] = _sync_activities(
                session, settings, tredict, fetcher, activities, cloud_list
            )
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"活动上传失败：{type(e).__name__}: {e}")
            logger.error("%s", result["errors"][-1])

    # 健康上传
    try:
        result["health"] = _sync_health(settings, tredict)
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"健康上传失败：{type(e).__name__}: {e}")
        logger.error("%s", result["errors"][-1])

    # 云端校验：本次确认上传成功的活动算已覆盖（云端索引可能延迟）
    if cloud_ok:
        try:
            a = result["activities"]
            confirmed = set(a["uploaded"]) | set(a["exists"])
            after = list(tredict.fetch_activity_list(extended=True))
            result["verify"] = _verify_coverage(activities, after, confirmed)
            result["cloud_mirror_inserted"] = _upsert_cloud_mirror(session, after)
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"云端校验失败：{type(e).__name__}: {e}")
            logger.error("%s", result["errors"][-1])

    a = result["activities"]
    result["ok"] = not (
        result["errors"]
        or a["failed"]
        or a["no_fit"]
        or result["health"]["errors"]
        or result["verify"]["only_garmin"]
    )

    _write_audit(session, settings, result, today)
    _alert(session, settings, result, today)
    return result
