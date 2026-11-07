"""同步校验引擎（P7）——在线比对（O1=B 修正版：比字段值，不比哈希）。

Garmin / Tredict / 健康的**在线**侧比较。公共入口见 ``app.domain.verify``（包门面）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.domain.verify.core import _jsonable, same_value


# ============================================================
# 在线比对（O1=B 修正版：比字段值，不比哈希）
# ============================================================
def deep_diff(
    local: Any, remote: Any, *, path: str = ""
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """递归比**叶子**：``(值不符, 云端独有, 本地独有)``。

    2026-10-21 决策 Q7：只有「同一路径的叶子值不同」才算错误。
    云端新增字段（Garmin 后来给接口加字段，如 ``splitSummaries.numClimbsAttempted``）
    是 **schema 漂移**，记进 ``cloud_only`` 作信息，不得报错——否则 156 条假红灯
    会把真缺口淹掉。

    - 列表按索引比；长度不同 → 该路径整体记 ``值不符``（长度是事实差异）；
    - 数值走 ``same_value`` 的相对/绝对容差，浮点噪声不算错。
    """
    mismatches: dict[str, dict[str, Any]] = {}
    cloud_only: list[str] = []
    local_only: list[str] = []

    def walk(a: Any, b: Any, p: str) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                sub = f"{p}.{k}" if p else str(k)
                if k not in a:
                    cloud_only.append(sub)
                elif k not in b:
                    local_only.append(sub)
                else:
                    walk(a[k], b[k], sub)
            return
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                mismatches[p] = {"local": len(a), "remote": len(b)}
                return
            for i, (x, y) in enumerate(zip(a, b, strict=False)):
                walk(x, y, f"{p}[{i}]")
            return
        if not same_value(a, b):
            mismatches[p] = {"local": a, "remote": b}

    walk(local, remote, path)
    return mismatches, cloud_only, local_only


def compare_activity_coverage(
    garmin_ids: set[str], tredict_ids: set[str]
) -> dict[str, list[str]]:
    """活动**双向**覆盖（2026-10-21 决策 Q7）。

    旧校验只遍历 Tredict 侧去找 Garmin 对应 → 「Garmin 有、Tredict 没有」的
    **上传遗漏**永远不报（漏掉的那条根本没进循环）。真实故障即如此：
    Garmin 302 / Tredict 301，校验却报「已一致」。
    """
    return {
        "only_garmin": sorted(garmin_ids - tredict_ids),
        "only_tredict": sorted(tredict_ids - garmin_ids),
    }


# 非数据字段：Garmin 把**当前登录用户的权限 scope 列表**回显在每条活动 payload 里
# （``userRoles``）。Garmin 增删 scope 后，历史 raw 快照与新响应必然不同 ——
# 2026-10-25 实测 302/304 条活动全部「值不符」，日任务天天 exit 3。
# 它不是活动数据，不参与一致性校验；真数据字段仍然照报。
NON_DATA_FIELDS: frozenset[str] = frozenset({"userRoles"})


def activity_local_date(item: dict[str, Any]) -> dt.date | None:
    """活动 payload 的**本地**日期。

    优先 ``startTimeLocal``（形如 ``2026-10-20 16:10:33``），缺失回落
    ``startTimeGMT``；两者都解析不了返回 ``None``——调用方按「不豁免」处理。
    """
    for key in ("startTimeLocal", "startTimeGMT"):
        raw = item.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            return dt.datetime.fromisoformat(raw).date()
        except ValueError:
            continue
    return None


def ids_of_day(
    garmin_cloud: list[dict[str, Any]],
    local: dict[str, dict[str, Any]],
    day: dt.date,
) -> set[str]:
    """属于 ``day`` 的活动 id 集合（Garmin ``activityId`` 域，云端 ∪ 本地）。

    为什么需要：日任务 21:00 跑校验、23:30 才同步当天活动——这个时间差里
    Garmin 云端已有当天活动而本地 raw 还没有，A 环必然报「仅远端」，
    **每天** exit 3 假红（狼来了）。健康侧 ``_health_window`` 早排除当天，
    活动侧对齐同一条规则；次日 ``RAW_REFRESH_WINDOW_DAYS=5`` 滚动窗口会
    自然复核，豁免当天不丢覆盖。

    ⚠️ 解析不出日期的活动**不豁免**（``activity_local_date`` 返回 ``None``）：
    宁可漏豁免多报一次，也不误豁免一条真实缺失。
    """
    out: set[str] = set()
    for item in garmin_cloud:
        aid = item.get("activityId")
        if aid is not None and activity_local_date(item) == day:
            out.add(str(aid))
    for aid, item in local.items():
        if activity_local_date(item) == day:
            out.add(str(aid))
    return out


def compare_tredict_details(
    local: dict[str, dict], cloud: dict[str, dict]
) -> dict[str, Any]:
    """Tredict 活动详情：本地 raw 快照 ↔ 云端实时（2026-10-21 D 环）。

    为什么单立一环：B 环只比 **id 集合**，详情内容腐烂不可见。而 Tredict 在
    capacity / zones 修订后会**重算历史 effort**（``summary.effort.*`` 三源值变），
    本地快照不变 → 快照与云端不一致且无人知晓。

    口径与 A 环一致（deep_diff）：同一路径叶子值不同才算错误；云端新增字段是
    schema 漂移（记 ``drift_paths``，不报错）。

    返回 ``{sampled, value_mismatch, drift_paths, missing_remote, missing_local}``：
    - ``value_mismatch``：``{tid: {path: {local, remote}}}``
    - ``missing_remote``：云端已删（本地快照过期，应清库/标记）
    - ``missing_local``：本地没有（拉取遗漏，归 B 环管，这里只登记）
    """
    value_mismatch: dict[str, dict[str, dict[str, Any]]] = {}
    drift_paths: list[str] = []
    shared = sorted(set(local) & set(cloud))
    for tid in shared:
        mismatches, cloud_only, _ = deep_diff(local[tid], cloud[tid])
        if mismatches:
            value_mismatch[tid] = mismatches
        for path in cloud_only:
            drift_paths.append(f"{tid}:{path}")
    return {
        "sampled": len(shared),
        "value_mismatch": value_mismatch,
        "drift_paths": drift_paths,
        "missing_remote": sorted(set(local) - set(cloud)),
        "missing_local": sorted(set(cloud) - set(local)),
    }


def compare_online_garmin(
    remote: list[dict[str, Any]],
    local: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Garmin 在线：id 集合 + 88 个共同字段逐值比。

    ⚠️ **不能比哈希**：本地 payload 有 105 字段（17 个是旧 Node 管道的派生值），
    线上接口只返回 88 个原生字段，哈希永远不等。
    """
    by_id = {str(a.get("activityId")): a for a in remote if a.get("activityId") is not None}
    remote_ids = set(by_id)
    local_ids = set(local)

    common = sorted(remote_ids & local_ids)
    field_mismatch: list[dict[str, Any]] = []
    schema_drift: list[dict[str, Any]] = []
    for aid in common:
        remote_item, local_item = by_id[aid], local[aid]
        # 2026-10-21 决策 Q7：递归比**叶子**。云端新增字段只是 schema 漂移
        # （Garmin 后来给接口加字段），不算数据错误——否则假红灯会淹掉真缺口。
        # 2026-10-25：先剔除非数据字段（``userRoles`` 等，见 ``NON_DATA_FIELDS``）。
        remote_cmp = {k: v for k, v in remote_item.items() if k not in NON_DATA_FIELDS}
        local_cmp = {k: v for k, v in local_item.items() if k not in NON_DATA_FIELDS}
        diffs, cloud_only, _local_only = deep_diff(local_cmp, remote_cmp)
        if diffs:
            field_mismatch.append({"activity_id": aid, "fields": sorted(diffs)[:20]})
        if cloud_only:
            schema_drift.append({"activity_id": aid, "fields": cloud_only[:20]})

    local_only_fields = set()
    for payload in local.values():
        local_only_fields |= set(payload)
    remote_fields = set()
    for item in by_id.values():
        remote_fields |= set(item)

    return {
        "remote": len(remote_ids),
        "local": len(local_ids),
        "only_remote": sorted(remote_ids - local_ids),
        "only_local": sorted(local_ids - remote_ids),
        "field_mismatch": field_mismatch,
        "schema_drift": schema_drift,
        "unverifiable_fields": sorted(local_only_fields - remote_fields),
    }


def compare_online_tredict(
    remote: list[dict[str, Any]],
    local: dict[str, dict[str, Any]],
    *,
    parse_utc,
) -> dict[str, Any]:
    """Tredict 在线：id 集合 + ``date`` / ``sportType`` / ``subSportType``。

    ``activityList`` 只有这四个字段，所以只能验到这层；哈希级要重取详情
    （298 次请求），留给 ``--deep-online``。
    """
    by_id = {str(a.get("id")): a for a in remote if a.get("id") is not None}
    remote_ids = set(by_id)
    local_ids = set(local)

    mismatch: list[dict[str, Any]] = []
    for tid in sorted(remote_ids & local_ids):
        remote_item, local_item = by_id[tid], local[tid]
        diffs: list[dict[str, Any]] = []
        want = parse_utc(remote_item.get("date"))
        have = local_item.get("utc_datetime")
        if want is not None and have is not None and want != have:
            diffs.append({"field": "date", "db": _jsonable(have), "remote": _jsonable(want)})
        for name in ("sportType", "subSportType"):
            key = "sport_type" if name == "sportType" else "sub_sport_type"
            remote_value = remote_item.get(name)
            local_value = local_item.get(key)
            if remote_value is not None and local_value is not None and remote_value != local_value:
                diffs.append({"field": name, "db": local_value, "remote": remote_value})
        if diffs:
            mismatch.append({"tredict_id": tid, "fields": diffs})

    return {
        "remote": len(remote_ids),
        "local": len(local_ids),
        "only_remote": sorted(remote_ids - local_ids),
        "only_local": sorted(local_ids - remote_ids),
        "field_mismatch": mismatch,
    }


# ============================================================
# 在线比对：健康三类（键集合级）
# ============================================================
HEALTH_KINDS: tuple[str, ...] = ("hrv", "sleep", "bodyvalues")


def compare_online_health(
    local: dict[str, Any],
    remote: dict[str, Any],
) -> dict[str, Any]:
    """健康三类（``hrv`` / ``sleep`` / ``bodyvalues``）的**键集合**在线比对。

    为什么只比键
    ------------
    Tredict 的健康端点没有「按 id 取回详情」的能力，也没有哈希可对；能验的只有
    「这一天的记录在不在」。而「在不在」正是 2026-10-15~17 那次事故的形态：
    本地 raw 齐全、云端连续三天没有 RMSSD，而当时的 1,375 个校验实体一个都没报。

    键的构造口径由调用方负责（必须与 ``sync_tredict.py`` 的差集口径**逐字一致**，
    否则本函数只会把口径差异当成数据差异），domain 层保持纯集合运算。

    参数
    ----
    local / remote
        ``{kind: Iterable[str]}``，键为：
        - ``hrv``        → ``YYYYMMDD``（按 ``timestamp``）
        - ``sleep``      → ``YYYYMMDD``（按 **``endTimestamp``**，云端即按此键控）
        - ``bodyvalues`` → ``YYYY-MM-DDTHH:MM:SS``（``timestamp`` 前 19 位）

    返回
    ----
    ``{"kinds": {kind: {local, remote, only_local, only_remote}}, "lagging", "extra"}``

    - ``only_local``  = 本地有、云端没有 → **滞后未上传**
    - ``only_remote`` = 云端有、本地没有 → 本地 raw 缺失或历史残留
    """
    out: dict[str, Any] = {"kinds": {}, "lagging": 0, "extra": 0}
    for kind in HEALTH_KINDS:
        have = {k for k in (local.get(kind) or ()) if k}
        cloud = {k for k in (remote.get(kind) or ()) if k}
        only_local = sorted(have - cloud)
        only_remote = sorted(cloud - have)
        out["kinds"][kind] = {
            "local": len(have),
            "remote": len(cloud),
            "only_local": only_local,
            "only_remote": only_remote,
        }
        out["lagging"] += len(only_local)
        out["extra"] += len(only_remote)
    return out

