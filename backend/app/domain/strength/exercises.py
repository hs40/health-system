"""力量训练动作清单与原始负荷（volume load 统一形式）。

清单的作用（2026-10-20 用户确认）
--------------------------------
与 Garmin 同步力量活动时，`summarizedExerciseSets` 会不断出现**新动作**。
清单让每条动作的归属显式可查：

- ``tier``：``counted`` 计入 / ``excluded`` 剔除（热身拉伸）/ ``pending`` 待定（不计入）
- ``mode``：``machine`` / ``bodyweight`` / ``isometric`` / ``none``
- ``bw_factor``：自重动作的体重比例（**逐动作小类**，不按大类共用）
- ``iso_factor``：等长动作的相对强度系数

新动作默认 ``pending``（不计入）并进报告，等补口径后才计入——对应需求
「缺少关键字段时不得假定已获得完整负荷」。

单位（已用用户手写 notes 三组零例外验证，2026-10-19）
--------------------------------------------------
| 字段 | 原值 | 实际 | 换算 |
|---|---|---|---|
| ``maxWeight`` | 13000 | 13 kg | ÷1000（克） |
| ``volume`` | 299000 | 299 kg·reps | ÷1000（克×次） |

原始负荷口径（用户 2026-10-20 确认）
------------------------------------
| 场景 | 公式 |
|---|---|
| 器械（``volume>0``） | ``volume / 1000`` —— Garmin 已算好的 kg·reps |
| 自重（``reps>0``） | ``sets × reps × 体重kg × bw_factor`` |
| 静力（``reps=0``） | ``秒 × 体重kg × iso_factor ÷ 3``（duration 已含全部组，不乘组数） |

静力用「机械功当量」：一次重复的向心+离心约 3 秒，故 3 秒等长 ≈ 1 次重复的机械功。
**不表述为折算成次数**（需求 4.2 禁止「虚构成动态重复次数」）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent
EXERCISES_PATH = DATA_DIR / "exercises.json"

TIER_COUNTED = "counted"
TIER_EXCLUDED = "excluded"
TIER_PENDING = "pending"

MODE_MACHINE = "machine"
MODE_BODYWEIGHT = "bodyweight"
MODE_ISOMETRIC = "isometric"
MODE_NONE = "none"

DEFAULT_TIER = TIER_PENDING
DEFAULT_MODE = MODE_NONE

# 静力 → kg·reps 的机械功当量：3 秒等长 ≈ 1 次重复
ISOMETRIC_SEC_PER_REP = 3.0

FORMULA = "strength_volume_v1"
UNIT = "kg_reps"


def _key_of(category: str, sub_category: str | None) -> str:
    cat = str(category or "").strip()
    sub = str(sub_category or "").strip()
    return f"{cat}/{sub}" if sub else f"{cat}/"


@lru_cache(maxsize=1)
def _load() -> dict[str, dict[str, Any]]:
    if not EXERCISES_PATH.exists():
        return {}
    doc = json.loads(EXERCISES_PATH.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for key, value in (doc.get("exercises") or {}).items():
        if not isinstance(value, dict):
            continue
        out[key] = {
            "tier": value.get("tier", DEFAULT_TIER),
            "mode": value.get("mode", DEFAULT_MODE),
            "bw_factor": float(value.get("bw_factor") or 0.0),
            "iso_factor": float(value.get("iso_factor") or 0.0),
            "why": value.get("why", ""),
        }
    return out


def entry(key: str) -> dict[str, Any]:
    """该动作的归属；清单外返回 ``pending``（不计入）。"""
    return _load().get(
        key,
        {
            "tier": DEFAULT_TIER,
            "mode": DEFAULT_MODE,
            "bw_factor": 0.0,
            "iso_factor": 0.0,
            "why": "清单未登记，默认待定不计入",
        },
    )


def classify(sets: list[dict]) -> list[dict]:
    """给每个 ``summarizedExerciseSets`` 条目附上 ``exercise_key`` / ``tier`` / ``mode``。"""
    for item in sets:
        key = _key_of(item.get("category"), item.get("subCategory"))
        info = entry(key)
        item["exercise_key"] = key
        item["tier"] = info["tier"]
        item["mode"] = info["mode"]
        item["bw_factor"] = info["bw_factor"]
        item["iso_factor"] = info["iso_factor"]
    return sets


def unknown_keys(sets: list[dict]) -> list[dict]:
    """清单外的动作（含组数/次数/时长），供同步时报告。"""
    known = _load()
    agg: dict[str, dict[str, Any]] = {}
    for item in sets:
        key = _key_of(item.get("category"), item.get("subCategory"))
        if key in known:
            continue
        d = agg.setdefault(
            key,
            {
                "key": key,
                "category": item.get("category"),
                "sub_category": item.get("subCategory"),
                "sets": 0,
                "reps": 0,
                "duration_sec": 0.0,
                "volume": 0,
            },
        )
        d["sets"] += int(item.get("sets") or 0)
        d["reps"] += int(item.get("reps") or 0)
        d["duration_sec"] += float(item.get("duration") or 0) / 1000.0
        d["volume"] += int(item.get("volume") or 0)
    for d in agg.values():
        d["duration_sec"] = round(d["duration_sec"], 1)
    return sorted(agg.values(), key=lambda x: -x["sets"])


def estimate_load(
    sets: list[dict],
    body_weight_kg: float | None,
) -> tuple[float, dict[str, Any]]:
    """计算一次力量活动的原始负荷（kg·reps）与明细。

    **体重缺失时**：器械动作照常可算（用 Garmin volume）；
    自重/静力动作标记不可计算，**不用假体重顶上**。
    """
    body = float(body_weight_kg or 0.0)
    total = 0.0
    by_mode: dict[str, float] = {}
    excluded: dict[str, int] = {}
    not_computable: list[str] = []
    counted_sets = excluded_sets = 0

    for item in classify(list(sets)):
        key = item["exercise_key"]
        tier = item["tier"]
        mode = item["mode"]
        sets_n = int(item.get("sets") or 0)
        reps = int(item.get("reps") or 0)
        secs = float(item.get("duration") or 0) / 1000.0
        volume_g = float(item.get("volume") or 0)

        if tier != TIER_COUNTED:
            excluded[key] = excluded.get(key, 0) + sets_n
            excluded_sets += sets_n
            continue

        if mode == MODE_MACHINE and volume_g > 0:
            value = volume_g / 1000.0
        elif mode == MODE_BODYWEIGHT and reps > 0:
            if body <= 0:
                not_computable.append(f"{key}（缺体重，无法换算自重负荷）")
                continue
            value = sets_n * reps * body * float(item.get("bw_factor") or 0.0)
        elif mode == MODE_ISOMETRIC and secs > 0:
            if body <= 0:
                not_computable.append(f"{key}（缺体重，无法换算等长负荷）")
                continue
            # Garmin 的 duration 是该动作的**累计**保持时间（含所有组），
            # 故不再乘 sets —— 否则会把组数重复计入。
            value = secs * body * float(item.get("iso_factor") or 0.0) / ISOMETRIC_SEC_PER_REP
        else:
            excluded[key] = excluded.get(key, 0) + sets_n
            excluded_sets += sets_n
            continue

        counted_sets += sets_n
        by_mode[mode] = by_mode.get(mode, 0.0) + value
        total += value

    detail: dict[str, Any] = {
        "formula": FORMULA,
        "unit": UNIT,
        "by_mode": {k: round(v, 1) for k, v in by_mode.items()},
        "counted_sets": counted_sets,
        "excluded_sets": excluded_sets,
        "excluded": dict(sorted(excluded.items())),
        "body_weight_kg": body or None,
        "isometric_sec_per_rep": ISOMETRIC_SEC_PER_REP,
    }
    if not_computable:
        detail["not_computable"] = not_computable
    return round(total, 1), detail


def add_exercise(
    key: str,
    *,
    tier: str = TIER_COUNTED,
    mode: str = MODE_NONE,
    bw_factor: float = 0.0,
    iso_factor: float = 0.0,
    why: str = "用户补充",
) -> bool:
    """把新动作追加进清单；已存在则不动。"""
    if not key or "/" not in key:
        return False
    doc = json.loads(EXERCISES_PATH.read_text(encoding="utf-8"))
    exercises = doc.get("exercises") or {}
    if key in exercises:
        return False
    exercises[key] = {
        "tier": tier,
        "mode": mode,
        "bw_factor": bw_factor,
        "iso_factor": iso_factor,
        "why": why,
        "sets": 0,
        "reps": 0,
        "duration_sec": 0.0,
        "activities": 0,
    }
    doc["exercises"] = dict(sorted(exercises.items()))
    EXERCISES_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    _load.cache_clear()
    return True
