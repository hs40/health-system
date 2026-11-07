"""同步校验引擎（P7）——四条本地链路的逐条判定。

活动（Garmin 活动 / 逐秒记录 / Tredict 活动）+ 健康（``standard_daily``）。
公共入口见 ``app.domain.verify``（包门面）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.domain.verify.core import _jsonable, canonical_sha256, diff_fields, same_value
from app.domain.verify.result import CheckResult, _result
from app.domain.verify.states import (
    RECORD_FIELDS,
    STANDARD_DAILY_FIELDS,
    STATE_MISSING_REMOTE,
)


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

