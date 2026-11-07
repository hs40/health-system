"""同步校验引擎（P7）——逐字段比对（本地 ↔ 云端），2026-10-19 新增。

公共入口见 ``app.domain.verify``（包门面）。
"""

from __future__ import annotations

from typing import Any

from app.domain.verify.online import HEALTH_KINDS, compare_online_health

# ============================================================
# 逐字段比对（本地 ↔ 云端）—— 2026-10-19 新增
# ============================================================
# 为什么需要：原来的在线校验只比**键集合**（这一天/这一条在不在）。
# ``rmssd`` 传成 0、睡眠时长传成 0，一样是绿的。10-15~17 那次是「整段没有」，
# 被键集合接住了；但「有记录、值错了」这类问题一个都发现不了。

# ``GET /bodyvalues`` 能读回的字段。上传时带的 ``hrRestDynamic`` / ``hrMaxDynamic``
# **读不回来**，必须排除，否则天天假红。
# （与 ``app.adapters.tredict.BODYVALUES_READABLE`` 必须一致，有单测锁住。）
BODYVALUES_COMPARABLE: tuple[str, ...] = (
    "restingHeartrate",
    "bodyHeightInCentimeter",
    "weightInKilograms",
    "bodyFatInPercent",
    "bodyWaterInPercent",
    "muscleMassInPercent",
)

# 活动 ``summary`` 逐字段比对：(字段名, 绝对容差)
# 容差来自实测（2026-10-19，301 条 × 2 组时长 = 602 组）：
# 最大偏差 8.2 秒，p99 = 4.2 秒，p95 = 1.6 秒，**无一超过 10 秒**。
# 这是 Tredict 用 FIT 采样点重算时长的取整噪声（1 秒采样 + 首尾样本取舍），
# 不是数据错误；真实的错（传错文件/错活动）会差几分钟。
ACTIVITY_DURATION_TOL = 10.0
ACTIVITY_SUMMARY_TOL: tuple[tuple[str, float], ...] = (
    ("calories", 1.0),
    ("heartrate", 1.0),
    ("heartrateMax", 1.0),
)

# **时长口径按运动类型分流**（实测，2026-10-19）：
#
# | 类型 | Garmin | Tredict |
# |---|---|---|
# | 游泳 | ``movingDuration`` 1034.2 | ``duration`` 1035 |
# | 游泳 | ``duration`` 2972.9 | ``durationTotal`` 2973 |
# | 其他 | ``duration`` 2349.8 | ``duration`` 2350 |
# | 其他 | ``elapsedDuration`` 2415.6 | ``durationTotal`` 2415 |
#
# 不按类型分流会产生 51 条假红（游泳的 2973 vs 1035）。
SWIM_SUB_SPORTS: frozenset[str] = frozenset({"lap_swimming", "open_water_swimming"})

# 距离单独处理：Garmin 力量/室内活动 distance=0，Tredict 干脆不返回该键
ACTIVITY_DISTANCE_FIELD = "distance"
ACTIVITY_DISTANCE_ABS_TOL = 2.0  # 米
ACTIVITY_DISTANCE_REL_TOL = 0.005

# Garmin 云端 ↔ Tredict 云端：同一场活动的客观事实（三列：标签 / Garmin 字段 / Tredict 字段）
def _num(value: Any) -> float | None:
    """数值化；布尔/字符串/None 一律视为「不可比」。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _close(a: Any, b: Any, abs_tol: float, rel_tol: float = 0.0) -> bool:
    """带绝对/相对容差的数值比较；任一侧不可数值化 → ``False``。"""
    x, y = _num(a), _num(b)
    if x is None or y is None:
        return False
    return abs(x - y) <= max(abs_tol, abs(y) * rel_tol)


def compare_health_fields(
    local: dict[str, Any],
    remote: dict[str, Any],
) -> dict[str, Any]:
    """健康三类：键集合 + **逐字段值**。

    ``local`` 形如::

        {"hrv": {"20261017": {"rmssd": 64}},
         "sleep": {"20261017": {"sleepDurationInSeconds": 27180}},
         "bodyvalues": {"2026-10-17T22:00:00": {"restingHeartrate": 56, ...}}}

    ``remote`` 是云端原始返回（**不预处理**，便于直接对照官方文档）::

        {"hrv": {"20261017": [64, 58]},      # [RMSSD, Baseline]
         "sleep": {"20261017": [27180, 26000]},  # [总睡眠秒数, Baseline]
         "bodyvalues": [{"timestamp": ..., "restingHeartrate": 56, ...}]}

    返回值在 ``compare_online_health`` 的键集合基础上，为每个共同键追加
    ``mismatch`` 列表。**只比我们发出去的字段**：云端多出来的不算错。
    """
    # 云端原始形态 → 键集合（bodyvalues 是数组，需要从 timestamp 取键）
    remote_keys = {
        "hrv": set(remote.get("hrv") or {}),
        "sleep": set(remote.get("sleep") or {}),
        "bodyvalues": {
            str(r.get("timestamp", ""))[:19]
            for r in (remote.get("bodyvalues") or [])
            if isinstance(r, dict)
        },
    }
    base = compare_online_health(
        {k: set(local.get(k) or ()) for k in HEALTH_KINDS}, remote_keys
    )

    mismatches: dict[str, list[dict[str, Any]]] = {k: [] for k in HEALTH_KINDS}

    # hrv / sleep：云端是 [值, Baseline]，取第 0 位
    for kind, name in (("hrv", "rmssd"), ("sleep", "sleepDurationInSeconds")):
        cloud = remote.get(kind) or {}
        for key, mine in (local.get(kind) or {}).items():
            want = cloud.get(key)
            if not isinstance(want, list) or not want:
                continue
            have = (mine or {}).get(name)
            if have is None:
                continue
            if not _close(have, want[0], abs_tol=1.0):
                mismatches[kind].append(
                    {"key": key, "field": name, "local": have, "remote": want[0]}
                )

    # bodyvalues：云端是数组，按 timestamp 前 19 位对齐
    cloud_bv = {
        str(r.get("timestamp", ""))[:19]: r
        for r in (remote.get("bodyvalues") or [])
        if isinstance(r, dict)
    }
    for key, mine in (local.get("bodyvalues") or {}).items():
        want = cloud_bv.get(key)
        if want is None:
            continue
        for name in BODYVALUES_COMPARABLE:
            have = (mine or {}).get(name)
            if have is None:
                continue
            if not _close(have, want.get(name), abs_tol=0.05):
                mismatches["bodyvalues"].append(
                    {"key": key, "field": name, "local": have, "remote": want.get(name)}
                )

    for kind, items in mismatches.items():
        base["kinds"][kind]["mismatch"] = items
        base["mismatch"] = base.get("mismatch", 0) + len(items)
    base.setdefault("mismatch", 0)
    return base


def duration_fields(sub_sport: str | None) -> tuple[tuple[str, str], tuple[str, str]]:
    """按运动类型给出时长映射：``((g_duration, t_duration), (g_total, t_total))``。

    实测（2026-10-19）：游泳的 Tredict ``duration`` 是**移动时长**（对应 Garmin
    ``movingDuration``），``durationTotal`` 才是总时长（对应 Garmin ``duration``）；
    其他运动则直连 ``duration`` / ``elapsedDuration``。不分流会产生 51 条假红。
    """
    if (sub_sport or "") in SWIM_SUB_SPORTS:
        return ("movingDuration", "duration"), ("duration", "durationTotal")
    return ("duration", "duration"), ("elapsedDuration", "durationTotal")


# Tredict summary 字段名 → Garmin payload 字段名（同名的不必列）
_GARMIN_SUMMARY_KEY: dict[str, str] = {
    "heartrate": "averageHR",
    "heartrateMax": "maxHR",
}
def compare_cloud_pair(
    garmin: dict[str, dict[str, Any]],
    tredict: dict[str, dict[str, Any]],
    pairs: list[tuple[str, str]],
) -> dict[str, Any]:
    """Garmin 云端 ↔ Tredict 云端：同一场活动的客观事实逐字段比。

    ``garmin``：``{garmin_activity_id: 云端 payload}``
    ``tredict``：``{tredict_id: activityList 项}``（含 ``summary`` / ``subSportType``）
    ``pairs``：``[(garmin_id, tredict_id)]``，来自本地已建的软匹配

    只比**客观事实**（时长/耗时/卡路里/心率），不比任何「负荷」类字段：
    Garmin 的 ``activityTrainingLoad`` 与 Tredict 的 ``effort`` 口径不同，
    强行比只会产出永远红的结果。

    时长口径按 ``subSportType`` 分流（见 ``duration_fields``）。
    """
    mismatch: list[dict[str, Any]] = []
    compared = skipped = 0

    for gid, tid in pairs:
        g = garmin.get(str(gid))
        t = tredict.get(str(tid))
        if g is None or t is None:
            skipped += 1
            continue
        compared += 1
        summary = t.get("summary") or {}
        sub_sport = t.get("subSportType") or ""
        fields: list[dict[str, Any]] = []

        for g_key, t_key in duration_fields(sub_sport):
            gv, tv = g.get(g_key), summary.get(t_key)
            if gv is None or tv is None:
                continue  # 任一侧没有该项 → 不可比，不报
            if not _close(gv, tv, abs_tol=ACTIVITY_DURATION_TOL):
                fields.append(
                    {"field": t_key, "garmin": gv, "tredict": tv,
                     "garmin_key": g_key, "tredict_key": t_key}
                )

        for name, tol in ACTIVITY_SUMMARY_TOL:
            gv = g.get(_GARMIN_SUMMARY_KEY.get(name, name))
            tv = summary.get(name)
            if gv is None or tv is None:
                continue
            if not _close(gv, tv, abs_tol=tol):
                fields.append(
                    {"field": name, "garmin": gv, "tredict": tv,
                     "garmin_key": _GARMIN_SUMMARY_KEY.get(name, name),
                     "tredict_key": name}
                )

        g_dist = g.get(ACTIVITY_DISTANCE_FIELD)
        t_dist = summary.get(ACTIVITY_DISTANCE_FIELD)
        if (
            _num(g_dist)
            and t_dist is not None
            and not _close(
                g_dist,
                t_dist,
                ACTIVITY_DISTANCE_ABS_TOL,
                ACTIVITY_DISTANCE_REL_TOL,
            )
        ):
            fields.append(
                {
                    "field": ACTIVITY_DISTANCE_FIELD,
                    "garmin": g_dist,
                    "tredict": t_dist,
                    "garmin_key": ACTIVITY_DISTANCE_FIELD,
                    "tredict_key": ACTIVITY_DISTANCE_FIELD,
                }
            )

        if fields:
            mismatch.append(
                {"garmin_id": str(gid), "tredict_id": str(tid),
                 "sub_sport": sub_sport, "fields": fields}
            )

    return {
        "pairs": len(pairs),
        "compared": compared,
        "skipped": skipped,
        "field_mismatch": mismatch,
        "mismatch": sum(len(m["fields"]) for m in mismatch),
    }

