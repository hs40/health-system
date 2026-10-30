"""同步校验引擎（P7）——**纯函数层**。

三层数据（云端 / 本地 raw / 本地 DB）是否仍然一致，由这里判定；
``scripts/verify_sync.py`` 负责 IO，本模块只做比较，便于单测与事后复核。

四条设计约束（来自 P7 设计树）
------------------------------
1. **只读**：本模块不写任何文件、不写库。发现缺失只报告，不自动补拉（O2=C）——
   自动补拉会让「校验」变成有副作用的写操作，cron 里跑就成了「每晚自动改数据」。
2. **逐条判定**：每个实体一条 ``CheckResult``，脚本据此回写 ``sync_ledger``（Q4=A）。
3. **三态空值判定**（H2=A）：``都空`` → ok；``表空/重算有`` → mismatch；``表有/重算空`` → mismatch。
   只有这样才能区分「数据源本来就没有」和「有数据但没取到」。
4. **代码指纹**（C=A）：健康链路依赖 ``GarminAdapter.standardize()``，谁改了算法，
   校验就会报「数据不一致」。指纹让「算法变了」和「数据变了」可区分。

已知不可验证项（写进报告 ``notes``，不假装它们不存在）
------------------------------------------------------
- Garmin 活动的 ``payload`` 有 **105 字段**，其中 **17 个是旧 Node 管道算的派生值**
  （``distanceMeters`` / ``durationSeconds`` / ``averageHeartRate`` …），
  线上接口只返回 88 个原生字段，**派生字段无法在线复现**。
- 因此在线校验**不能比哈希**（哈希永远不等），只能比 88 个共同字段的**值**。
  实测 301/301 零差异。
- 健康 raw（``daily_summary`` / ``sleep`` / ``hrv`` …）**不在 ``asset_manifest.json`` 里**，
  没有 sha 可比；``source_refs.fetched_at`` 是文件 mtime，跨机不可比（M=A 故不比对）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
from dataclasses import dataclass, field
from typing import Any

from app.domain.standard_fields import DB_FIELDS

# ============================================================
# 状态与容差
# ============================================================
STATE_OK = "ok"
STATE_MISMATCH = "mismatch"
STATE_MISSING_LOCAL = "missing_local"
STATE_MISSING_REMOTE = "missing_remote"
STATE_AMBIGUOUS = "ambiguous"

# 判定「失败」的状态（Q5=A）：ambiguous 只警告，不失败
FAIL_STATES = frozenset({STATE_MISMATCH, STATE_MISSING_LOCAL, STATE_MISSING_REMOTE})

# 浮点比较：Garmin 的 float 经过 JSON 往返应逐位相等，留极小容差吸收实现差异
REL_TOL = 1e-9
ABS_TOL = 1e-9

# ``standard_daily`` 参与比对的字段（排除 id / created_at / source_refs）。
# 唯一事实源：``app/domain/standard_fields.py``（② 白名单收敛，2026-10-26）。
# 此前手写 20 项，漏了 sleep_score / body_battery_morning → 云端校验静默跳过这 2 字段。
STANDARD_DAILY_FIELDS: tuple[str, ...] = DB_FIELDS

# 逐秒表参与比对的 8 个通道（与 ``garmin_activity_record`` 列一一对应）
RECORD_FIELDS: tuple[str, ...] = (
    "seq",
    "t",
    "hr",
    "speed",
    "power",
    "cadence",
    "altitude",
    "distance",
)


# ============================================================
# 基础工具
# ============================================================
def _jsonable(value: Any) -> Any:
    """把 datetime / date / set 转成可 JSON 序列化的形式。"""
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def canonical_sha256(payload: Any) -> str:
    """规范哈希：键排序 + 紧凑分隔符 + 不转义中文，跨进程稳定。"""
    blob = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def same_value(a: Any, b: Any, *, rel: float = REL_TOL, abs_: float = ABS_TOL) -> bool:
    """值相等判定。数值走相对容差，其余走 ``==``。

    布尔必须先于数值判断（Python 里 ``True == 1``）。
    """
    if isinstance(a, bool) or isinstance(b, bool):
        # 布尔与数值不可混同（Python 里 True == 1），必须两边都是布尔才比较
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        fa, fb = float(a), float(b)
        return abs(fa - fb) <= max(abs_, rel * max(abs(fa), abs(fb)))
    return a == b


def diff_fields(
    local: dict[str, Any],
    reference: dict[str, Any],
    fields: tuple[str, ...] = STANDARD_DAILY_FIELDS,
    *,
    rel: float = REL_TOL,
    abs_: float = ABS_TOL,
) -> list[dict[str, Any]]:
    """三态逐字段比对（H2=A）→ 差异列表（空列表 = 一致）。

    - ``local`` = 库里的值，``reference`` = 从 raw 重算的值
    - 两边都 None → 跳过（数据源本来就没有，不算问题）
    - 库里 None、重算有值 → ``db_missing``（有数据但没取到）
    - 库里有值、重算 None → ``db_only``（库里有、raw 推不出来）
    - 两边都有但不等 → ``value``
    """
    out: list[dict[str, Any]] = []
    for name in fields:
        have = local.get(name)
        want = reference.get(name)
        if have is None and want is None:
            continue
        if have is None:
            out.append({"field": name, "reason": "db_missing", "recomputed": _jsonable(want)})
        elif want is None:
            out.append({"field": name, "reason": "db_only", "db": _jsonable(have)})
        elif not same_value(have, want, rel=rel, abs_=abs_):
            out.append(
                {
                    "field": name,
                    "reason": "value",
                    "db": _jsonable(have),
                    "recomputed": _jsonable(want),
                }
            )
    return out


def rehydrate_record_row(row: dict[str, Any]) -> dict[str, Any]:
    """把归档里的 ``record`` 行还原成 ``_build_records`` 能吃的形状。

    ``jsonl.gz`` 里 ``timestamp`` 是 ISO 字符串（``_jsonable`` 的产物），
    而 ``_epoch`` 只认 ``datetime``。不还原，所有 record 都会被当成
    「没有时间戳」丢掉 —— 实测 301 个活动全部变成 0 行，看起来像整表丢失。
    """
    ts = row.get("timestamp")
    if isinstance(ts, str):
        try:
            return {**row, "timestamp": dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))}
        except ValueError:
            return row
    return row


def code_fingerprint() -> dict[str, str]:
    """参与校验的三段逻辑的源码指纹（C=A）。

    没有 git，源码哈希是唯一可用的版本标识。指纹变了而数据「不一致」，
    第一嫌疑就是算法改了，而不是数据坏了。
    """
    from app.adapters.garmin import GarminAdapter
    from app.domain.matching import match_activities
    from app.parsers.fit import _build_records

    targets = {
        "standardize": GarminAdapter.standardize,
        "build_records": _build_records,
        "match_activities": match_activities,
    }
    return {
        name: hashlib.sha256(inspect.getsource(fn).encode("utf-8")).hexdigest()[:16]
        for name, fn in targets.items()
    }


# ============================================================
# 结果
# ============================================================
@dataclass(slots=True)
class CheckResult:
    entity: str
    entity_id: str
    state: str
    local_hash: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK


def _result(
    entity: str,
    entity_id: str,
    problems: dict[str, Any],
    local_hash: str,
) -> CheckResult:
    return CheckResult(
        entity=entity,
        entity_id=entity_id,
        state=STATE_MISMATCH if problems else STATE_OK,
        local_hash=local_hash,
        detail={"problems": problems} if problems else {},
    )


# ============================================================
# 活动链路
# ============================================================
def check_garmin_activity(
    activity_id: str,
    payload: dict[str, Any],
    stored_sha256: str,
    *,
    laps_count: int | None,
    sets_count: int | None,
    zones_count: int | None,
    archive_counts: tuple[int, int, int] | None,
) -> CheckResult:
    """``garmin_activity``：payload 哈希 + 三列数量对归档。

    ``archive_counts`` 为 ``None`` 表示归档文件缺失 —— 这是 ``missing_remote``，
    不是 ``mismatch``（表里有、raw 没有）。
    """
    sha = canonical_sha256(payload)
    problems: dict[str, Any] = {}

    if not stored_sha256:
        problems["payload_sha256"] = "empty"
    elif sha != stored_sha256:
        problems["payload_sha256"] = {"db": stored_sha256[:12], "recomputed": sha[:12]}

    if archive_counts is None:
        return CheckResult(
            entity="garmin_activity",
            entity_id=activity_id,
            state=STATE_MISSING_REMOTE,
            local_hash=sha,
            detail={"problems": {"archive": "missing"}},
        )

    for name, have, want in (
        ("laps", laps_count, archive_counts[0]),
        ("sets", sets_count, archive_counts[1]),
        ("zones", zones_count, archive_counts[2]),
    ):
        if have is None:
            problems[name] = "null"
        elif have != want:
            problems[name] = {"db": have, "archive": want}

    return _result("garmin_activity", activity_id, problems, sha)


def check_activity_records(
    activity_id: str,
    db_rows: list[tuple[Any, ...]],
    archive_rows: list[tuple[Any, ...]],
) -> CheckResult:
    """``garmin_activity_record``：逐秒行全量比对（Q3=A）。

    两边都是 ``(seq, t, hr, speed, power, cadence, altitude, distance)`` 元组，
    已按 ``seq`` 升序。比较到第一处不同就停，但**行数不同会单独报**——
    行数差往往是最先暴露的问题。
    """
    local_hash = canonical_sha256(db_rows)
    problems: dict[str, Any] = {}

    if len(db_rows) != len(archive_rows):
        problems["rows"] = {"db": len(db_rows), "archive": len(archive_rows)}

    limit = min(len(db_rows), len(archive_rows))
    for i in range(limit):
        a, b = db_rows[i], archive_rows[i]
        if a == b:  # C 层快速路径：绝大多数行在这里就过掉
            continue
        diffs = [
            {"field": RECORD_FIELDS[j], "db": _jsonable(a[j]), "archive": _jsonable(b[j])}
            for j in range(len(RECORD_FIELDS))
            if not same_value(a[j], b[j])
        ]
        if not diffs:
            continue  # 差异落在容差内，不算问题
        problems["first_diff"] = {"row": i, "seq": _jsonable(a[0]), "fields": diffs}
        break

    return _result("garmin_activity_record", activity_id, problems, local_hash)


def check_tredict_activity(
    tredict_id: str,
    payload: dict[str, Any],
    stored_sha256: str,
    *,
    utc_datetime: dt.datetime,
    local_date: dt.date,
    matched_garmin_id: str | None,
    match_confidence: str,
    garmin_ids: set[str],
    tz_offset: dt.timedelta,
) -> CheckResult:
    """``tredict_activity``：哈希 + 本地日期 + 匹配指向。"""
    sha = canonical_sha256(payload)
    problems: dict[str, Any] = {}

    if not stored_sha256:
        problems["payload_sha256"] = "empty"
    elif sha != stored_sha256:
        problems["payload_sha256"] = {"db": stored_sha256[:12], "recomputed": sha[:12]}

    want_date = (utc_datetime + tz_offset).date()
    if want_date != local_date:
        problems["local_date"] = {"db": local_date.isoformat(), "recomputed": want_date.isoformat()}

    if matched_garmin_id and matched_garmin_id not in garmin_ids:
        problems["matched_garmin_id"] = {
            "value": matched_garmin_id,
            "reason": "not_in_garmin_activity",
        }

    if match_confidence == "high" and not matched_garmin_id:
        problems["match_confidence"] = {"value": "high", "reason": "no_matched_garmin_id"}

    return _result("tredict_activity", tredict_id, problems, sha)


# ============================================================
# 健康链路
# ============================================================
def check_standard_daily(
    day: dt.date,
    db_values: dict[str, Any],
    recomputed_values: dict[str, Any],
) -> CheckResult:
    """``standard_daily``：472 天全量重算逐字段比（H1=A）+ 三态空值（H2=A）。"""
    diffs = diff_fields(db_values, recomputed_values)
    local_hash = canonical_sha256({k: db_values.get(k) for k in STANDARD_DAILY_FIELDS})
    return _result(
        "standard_daily",
        day.isoformat(),
        {"fields": diffs} if diffs else {},
        local_hash,
    )


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


# Garmin 健康原始载荷里**参与计算**的字段（``standardize()`` 实际读取的那些）。
#
# 为什么不能整份 JSON 比：Garmin 健康接口有一批**滚动/实时字段**
# （``latestRespirationValue``、``restStressPercentage``、``floorsAscendedInMeters``、
# ``latestSpO2``、``*TimestampGMT`` 等），它们随时间变化，
# 任何快照都不可能与之永久相等 —— 整份比会**每天永久红**，把真问题淹掉。
#
# 分层：``(外层键, 内层键)``，内层为 ``None`` 表示取外层的标量。
HEALTH_RAW_COMPARABLE: dict[str, tuple[tuple[str, str | None], ...]] = {
    "daily_summary": (
        ("restingHeartRate", None),
        ("minHeartRate", None),
        ("maxHeartRate", None),
        ("totalSteps", None),
        ("activeKilocalories", None),
        ("moderateIntensityMinutes", None),
        ("vigorousIntensityMinutes", None),
        ("bodyBatteryMostRecentValue", None),
        ("weight", None),
    ),
    "sleep": (
        ("dailySleepDTO", "sleepTimeSeconds"),
        ("dailySleepDTO", "sleepStartTimestampLocal"),
        ("dailySleepDTO", "sleepEndTimestampLocal"),
    ),
    "hrv": (
        ("hrvSummary", "lastNightAvg"),
        ("hrvSummary", "weeklyAvg"),
        ("hrvSummary", "status"),
        ("hrvSummary", "baseline"),
    ),
    "spo2": (("averageSpO2", None),),
    "respiration": (
        ("avgRespirationRate", None),
        ("avgWakingRespirationValue", None),
    ),
}

# ``standardize()`` 另有回退链：呼吸优先取 ``daily_summary.avgWakingRespirationValue``；
# 因此 ``respiration`` 的这两个键也要在 ``daily_summary`` 上比一次。
HEALTH_RAW_ALIASES: dict[str, tuple[tuple[str, str | None], ...]] = {
    "daily_summary": (("avgWakingRespirationValue", None),),
}


def health_field_value(payload: Any, outer: str, inner: str | None) -> Any:
    """按 ``(outer, inner)`` 取值；``inner=None`` 表示取标量。"""
    if not isinstance(payload, dict):
        return None
    value = payload.get(outer)
    if inner is None:
        return value
    return value.get(inner) if isinstance(value, dict) else None


def compare_health_raw(
    local: Any,
    remote: Any,
    *,
    dataset: str,
    abs_tol: float = 0.0,
) -> list[dict[str, Any]]:
    """本地 raw 与 Garmin 云端**只比参与计算的字段**。

    返回差异列表；空列表表示「云端改动的要么没有，要么落在不参与计算的滚动字段上」。
    """
    fields = HEALTH_RAW_COMPARABLE.get(dataset, ())
    fields += HEALTH_RAW_ALIASES.get(dataset, ())
    out: list[dict[str, Any]] = []
    for outer, inner in fields:
        mine = health_field_value(local, outer, inner)
        cloud = health_field_value(remote, outer, inner)
        if mine is None and cloud is None:
            continue
        if mine is None or cloud is None:
            out.append(
                {"dataset": dataset, "field": f"{outer}.{inner or ''}".rstrip("."),
                 "local": mine, "remote": cloud, "kind": "missing_side"}
            )
            continue
        if not _close(mine, cloud, abs_tol=abs_tol) and mine != cloud:
            out.append(
                {"dataset": dataset, "field": f"{outer}.{inner or ''}".rstrip("."),
                 "local": mine, "remote": cloud, "kind": "value"}
            )
    return out


# ============================================================
# 汇总
# ============================================================
def summarize(results: list[CheckResult]) -> dict[str, int]:
    out = {
        STATE_OK: 0,
        STATE_MISMATCH: 0,
        STATE_MISSING_LOCAL: 0,
        STATE_MISSING_REMOTE: 0,
        STATE_AMBIGUOUS: 0,
    }
    for r in results:
        out[r.state] = out.get(r.state, 0) + 1
    return out


def exit_code(
    results: list[CheckResult],
    *,
    online_failed: bool = False,
    online_diff: int = 0,
    needs_repair: bool = False,
) -> int:
    """Q5=A + D=A：``mismatch`` / ``missing_*`` 才失败；``ambiguous`` 只警告。

    在线校验没跑成也算失败——「没验」不等于「验过了」，不能给假绿灯。
    在线校验**跑成了但有差异**同样失败：只记录不报错的巡检，等于把盲区
    从「没人看」改成「看了不说」（2026-10-15~17 缺 RMSSD 事故的教训）。

    ``needs_repair=True`` 返回 **3**（可修复）：调用方据此触发「重跑一轮上传」，
    修完再验一次；与 1（需人工处理）区分开，避免自动修复把真故障也吞掉。
    """
    if online_failed:
        return 1
    if online_diff:
        return 3 if needs_repair else 1
    return 1 if any(r.state in FAIL_STATES for r in results) else 0
