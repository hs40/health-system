"""同步校验引擎（P7）——基础工具：序列化、哈希、值比较、行还原。

公共入口见 ``app.domain.verify``（包门面）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from app.domain.verify.states import ABS_TOL, REL_TOL, STANDARD_DAILY_FIELDS


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

