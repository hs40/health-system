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

logger = logging.getLogger("hs.tredict_sync")

ENTITY_REF = "tredictSync"


# ============================================================
# 路径（全部来自 settings —— 容器里是 /data/...）
# ============================================================
def garmin_raw_dir(settings: Settings) -> Path:
    return Path(settings.raw_data_dir) / "garmin"


def fit_dir_for(settings: Settings) -> Path:
    return garmin_raw_dir(settings) / "fit"


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
                    logger.warning("[tredictSync] %s ZIP 内 .fit 数=%d，跳过", aid, len(names))
                    return None
                fit = z.read(names[0])
            if len(fit) < 14 or fit[8:12] != b".FIT":
                logger.warning("[tredictSync] %s FIT 头不合法，跳过", aid)
                return None
            return fit
        except Exception as e:  # noqa: BLE001 — 单条失败不阻断
            logger.warning("[tredictSync] 下载 FIT %s 失败：%s", aid, e)
            return None

    return _fetch


# ============================================================
# 子步骤：活动 / 健康 / 校验
# ============================================================
def _sync_activities(
    settings: Settings,
    tredict: Any,
    fit_fetcher: Callable[[str], bytes | None],
    activities: dict,
    cloud_list: list[dict],
) -> dict:
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
        try:
            r = tredict.upload_activity(aid, fit, name=name)
        except Exception as e:  # noqa: BLE001 — 单条失败不阻断
            failed.append({"activity_id": aid, "error": f"{type(e).__name__}: {e}"})
            continue
        if r.get("ok") or r.get("exists"):
            (exists if r.get("exists") else uploaded).append(aid)
        else:
            failed.append(
                {"activity_id": aid, "status": r.get("status"), "error": r.get("text", "")}
            )
    return {
        "local": len(activities),
        "cloud": len(cloud_list),
        "missing": missing,
        "uploaded": uploaded,
        "exists": exists,
        "failed": failed,
        "no_fit": no_fit,
    }


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
    """上传后云端清单 vs 本地活动：仍缺多少（复用 domain/verify 纯函数）。

    ``confirmed`` = 本次 ``upload_activity`` 返回 ok/exists 的活动。云端清单通常
    不会立即索引刚上传的活动（有延迟），把这些算进已覆盖，避免「刚传完就假红」。
    真正没传上去的（failed / no_fit）仍会留在 ``only_garmin`` 触发告警。
    """
    from app.domain.matching import match_activities
    from app.domain.verify import compare_activity_coverage

    grefs, trefs = _refs_for_matching(activities, cloud_list)
    outcomes = match_activities(grefs, trefs)
    covered = {o.garmin_id for o in outcomes if o.garmin_id} | confirmed
    cov = compare_activity_coverage({g.activity_id for g in grefs}, covered)
    return {"only_garmin": cov["only_garmin"], "only_tredict": cov["only_tredict"]}


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
                "errors": result["errors"],
            },
            audit_dir=Path(settings.audit_dir),
        )
    except Exception as e:  # noqa: BLE001 — audit 失败不该掩盖主结论
        logger.error("[tredictSync] 写 audit 失败：%s", e)


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
    if result["errors"]:
        reasons.append(f"异常 {len(result['errors'])} 项")
    if result["verify"]["only_garmin"]:
        reasons.append(f"覆盖仍缺 {len(result['verify']['only_garmin'])} 条")
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
        logger.error("[tredictSync] 写告警失败：%s", e)


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
) -> dict:
    """执行「活动上传 + 健康上传 + 云端校验」，返回结构化结果，不抛异常。

    ``garmin`` / ``tredict`` 缺省时由本服务构造真实 adapter（生产路径）；
    测试注入 fake。``fit_fetcher`` 缺省时用 ``default_fit_fetcher(garmin)``。
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
                       "exists": [], "failed": [], "no_fit": []},
        "health": {"hrv": 0, "sleep": 0, "bodyvalues": 0, "posted": 0, "errors": []},
        "verify": {"only_garmin": [], "only_tredict": []},
        "errors": [],
    }

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
        logger.error("[tredictSync] %s", result["errors"][-1])

    # 云端清单拉不到时**熔断上传**：无法确认「缺失」，盲传会在云端造重复活动
    # （P8 脚本同款安全阀）。此时 result["activities"] 保持全空，校验也不跑。
    if cloud_ok:
        try:
            result["activities"] = _sync_activities(
                settings, tredict, fetcher, activities, cloud_list
            )
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"活动上传失败：{type(e).__name__}: {e}")
            logger.error("[tredictSync] %s", result["errors"][-1])

    # 健康上传
    try:
        result["health"] = _sync_health(settings, tredict)
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"健康上传失败：{type(e).__name__}: {e}")
        logger.error("[tredictSync] %s", result["errors"][-1])

    # 云端校验：本次确认上传成功的活动算已覆盖（云端索引可能延迟）
    if cloud_ok:
        try:
            a = result["activities"]
            confirmed = set(a["uploaded"]) | set(a["exists"])
            after = list(tredict.fetch_activity_list(extended=True))
            result["verify"] = _verify_coverage(activities, after, confirmed)
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"云端校验失败：{type(e).__name__}: {e}")
            logger.error("[tredictSync] %s", result["errors"][-1])

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
