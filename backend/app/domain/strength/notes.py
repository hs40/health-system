"""力量训练 Notes 生成（P8）——``strength-notes.js`` 的 Python 移植。

为什么必须逐字对齐
------------------
Tredict 上 65 条力量活动**全部**带 notes（46–302 字），它们是旧 Node 脚本生成的，
是你判断「今天练了什么动作、多少组、多重」的唯一入口。
``--parity`` 模式会把本模块的输出与云端现有 notes 逐字比对——这是移植正确性的唯一证明。

三个输入各司其职
----------------
| 输入 | 来源 | 提供什么 |
|---|---|---|
| ``summarized_exercise_sets`` | Garmin 活动 payload | 动作清单、名称、最大重量（含手工修正） |
| ``fit_sets`` | FIT ``set`` 消息 | 逐组分布（次数 / 时长 / 重量），决定「10 / 8 / 5 次」这种细节 |
| 修正表 / 翻译表 | 本包内两个 JSON | 剔除规则、单侧表达、逐次覆盖、中文名 |

与 JS 版的三处刻意差异
----------------------
1. **不依赖 ``fit-file-parser``**：官方 ``garmin-fit-sdk`` 自带 52 张
   ``*_exercise_name`` 枚举表，``category_subtype`` 的整数可直接查表得名字。
2. **FIT 由调用方传入**，不在本模块读文件——这样可以用 P4 的 jsonl 归档直接喂，
   不必再解一遍 FIT，也让单测不需要真实文件。
3. **``Math.round`` 用 ``floor(x+0.5)``**：Python 的 ``round()`` 是银行家舍入，
   ``round(30.5)`` 得 30 而 JS 得 31，会逐字不一致。

无 FIT 兜底（v3.4.6 口径修正）
------------------------------
``summarizedExerciseSets[].reps`` / ``duration`` 是「N 组**总量**」，不是每组值。
FIT 逐组明细缺失时（本机 daily 停跑、NAS 新活动无 ``fit_jsonl``），旧兜底把总量
塞进「N次 × M组」的每组位置，等于把「3 组共 60 次」谎报成「每组 60 次」
（2026-10-26 起暴露）。v3.4.6 起改为不误导的总量表述
「共60次（3组，缺逐组明细）」，**绝不伪造每组值**（23/3、70/6 不整除时更危险）。
这是 Python 侧防御分支，不追求与旧 Node 版逐字一致；正确逐组值一律以 FIT
``set`` 消息为准。parity 比对只覆盖有 FIT 的历史 notes，兜底零覆盖。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent
MOVES_PATH = DATA_DIR / "moves_zh.json"
CORRECTIONS_PATH = DATA_DIR / "corrections.json"

NOTES_HEADER = "力量明细："
# 两个端点的 notes 上限不同（实测自 Node 版）
NOTES_LIMIT_UPLOAD = 1024
NOTES_LIMIT_UPDATE = 2048
TITLE_LIMIT = 255

_INF = float("inf")


# ============================================================
# 基础工具
# ============================================================
def _first(value: Any) -> Any:
    """FIT 的 ``category`` / ``category_subtype`` 是二元数组，JS 版只取第一个。"""
    return value[0] if isinstance(value, (list, tuple)) and value else value


def _num(value: Any) -> float:
    """JS ``Number(x) || 0``：None / 非数值一律当 0。"""
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def js_number(value: Any) -> str:
    """按 JS ``String(number)`` 的规则渲染：``4.0`` → ``"4"``，``4.5`` → ``"4.5"``。

    Python 的 ``str(4.0)`` 是 ``"4.0"``，直接拼进 notes 会与 Node 版逐字不一致。
    """
    f = float(value)
    if f.is_integer():
        return str(int(f))
    return repr(f)


def js_round(value: float) -> int:
    """JS ``Math.round``：.5 一律向上（Python ``round`` 是银行家舍入）。"""
    return math.floor(value + 0.5)


def js_fixed_number(value: float, digits: int) -> str:
    """``+(x).toFixed(digits)`` 的等价实现。

    两处坑：
    1. ``toFixed`` 是「就近取整、平局向上」，而 Python ``round`` 是银行家舍入
       （``round(1.125, 2) == 1.12``，JS 得 ``1.13``）；
    2. 必须用 ``Decimal(float)`` 拿**二进制真值**，不能用 ``Decimal(repr(x))``——
       后者会把 ``2.675`` 当精确值取成 ``2.68``，而 JS 得 ``2.67``。
    取整后 JS 还会 ``+`` 回转数字（``"2.00"`` → ``2``），故最后再走 ``js_number``。
    """
    quant = Decimal(1).scaleb(-digits)
    fixed = Decimal(float(value)).quantize(quant, rounding=ROUND_HALF_UP)
    return js_number(float(fixed))


def prettify(name: Any) -> str:
    """未命中翻译表时的兜底：``SIDE_LYING_LEG_RAISE`` → ``side lying leg raise``。"""
    return str(name or "").lower().replace("_", " ")


def decode_sub(category: str | None, number: Any) -> str:
    """``category_subtype`` 整数 → 动作名（大写），查官方 profile 的枚举表。

    表名规则：``{category}_exercise_name``，如 ``warm_up_exercise_name``。
    """
    if number is None or not category:
        return ""
    from garmin_fit_sdk import Profile

    table = Profile["types"].get(f"{category}_exercise_name") or {}
    try:
        name = table.get(int(number))
    except (TypeError, ValueError):
        return ""
    return str(name).upper() if name else ""


# ============================================================
# 数据表
# ============================================================
@lru_cache(maxsize=1)
def load_moves() -> dict[str, str]:
    raw = json.loads(MOVES_PATH.read_text(encoding="utf-8"))
    return dict(raw.get("moves") or {})


@lru_cache(maxsize=1)
def load_corrections() -> dict[str, Any]:
    return json.loads(CORRECTIONS_PATH.read_text(encoding="utf-8"))


def data_fingerprints() -> dict[str, str]:
    """两个数据文件的 sha256（Q7=C：与 F 盘原文件不一致时报告里能看出来）。"""
    import hashlib

    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        for p in (MOVES_PATH, CORRECTIONS_PATH)
    }


# ============================================================
# FIT 分组
# ============================================================
def fit_groups(fit_sets: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """FIT ``set`` 消息 → ``{"CAT/SUB": {"sets": [...], "start": epoch}}``。

    只保留 ``set_type == "active"``；``start`` 取该组最早一组的开始时间（无则 ``inf``），
    用于按实际训练顺序排序。
    """
    groups: dict[str, dict[str, Any]] = {}
    for row in fit_sets:
        if row.get("set_type") != "active":
            continue
        raw_cat = _first(row.get("category"))
        cat = str(raw_cat or "").upper() or "UNKNOWN"
        sub = decode_sub(raw_cat, _first(row.get("category_subtype")))
        key = f"{cat}/{sub}"
        group = groups.setdefault(key, {"sets": [], "start": _INF})
        group["sets"].append(
            {
                "reps": _num(row.get("repetitions")),
                "dur": js_round(_num(row.get("duration"))),
                "weight": _num(row.get("weight")),
            }
        )
        start = _epoch(row.get("start_time"))
        if start is not None and start < group["start"]:
            group["start"] = start
    return groups


def _epoch(value: Any) -> float | None:
    import datetime as dt

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.timestamp()


def pick_fit(
    fit: dict[str, dict[str, Any]],
    used: set[str],
    cat: str,
    sub: str,
    sum_reps: float,
    sum_dur: float,
) -> dict[str, Any] | None:
    """为一条汇总项挑对应的 FIT 组。

    先试精确键；汇总缺 ``subCategory`` 时按类别挑，多个候选取与汇总次数/时长最接近者。
    """
    exact = f"{cat}/{sub}"
    if exact in fit and exact not in used:
        used.add(exact)
        return fit[exact]

    keys = [k for k in fit if k.startswith(cat + "/") and k not in used]
    if not keys:
        return None
    if len(keys) == 1:
        used.add(keys[0])
        return fit[keys[0]]

    best: dict[str, Any] | None = None
    best_key = ""
    best_score = _INF
    for key in keys:
        group = fit[key]
        reps = sum(x["reps"] for x in group["sets"])
        dur = sum(x["dur"] for x in group["sets"])
        score = abs(reps - sum_reps) * 1000 + abs(dur - sum_dur)
        if score < best_score:
            best_score, best, best_key = score, group, key
    used.add(best_key)
    return best


def fmt_per_set(values: Sequence[float], unit: str, single_side: bool) -> str:
    """逐组值 → 文本。

    - 单侧动作：``10次/侧 × 3组``；不等时 ``10次/侧 8次/侧``
    - 双侧：全等 ``10次 × 3组``；单组 ``10次``；不等 ``10 / 8 / 5 次``
    """
    n = len(values)
    all_equal = all(v == values[0] for v in values)
    if single_side:
        groups = max(1, js_round(n / 2))
        if all_equal:
            return f"{js_number(values[0])}{unit}/侧 × {groups}组"
        return " ".join(f"{js_number(v)}{unit}/侧" for v in values)
    if all_equal and n > 1:
        return f"{js_number(values[0])}{unit} × {n}组"
    if n == 1:
        return f"{js_number(values[0])}{unit}"
    return " / ".join(js_number(v) for v in values) + " " + unit


# ============================================================
# 主函数
# ============================================================
def build_strength_notes(
    activity_id: str | int,
    summarized_sets: Sequence[dict[str, Any]] | None,
    fit_sets: Sequence[dict[str, Any]] | None = None,
    *,
    moves: dict[str, str] | None = None,
    corrections: dict[str, Any] | None = None,
) -> str:
    """生成一条力量活动的 notes；无可用动作时返回空串。"""
    corr = load_corrections() if corrections is None else corrections
    moves_map = load_moves() if moves is None else moves

    sets = summarized_sets if isinstance(summarized_sets, list) else []
    activity_corr = (corr.get("activities") or {}).get(str(activity_id)) or {}
    overrides = activity_corr.get("moves") or {}
    extra_exclude = set(activity_corr.get("exclude") or [])
    global_exclude = set(corr.get("excludeSubs") or [])
    single_side = set(corr.get("singleSide") or [])

    fit = fit_groups(fit_sets or [])
    used: set[str] = set()
    entries: list[tuple[float, str]] = []

    for item in sets:
        cat = item.get("category") or "UNKNOWN"
        if cat == "WARM_UP":  # 热身一律剔除
            continue
        sub = item.get("subCategory") or ""
        key = f"{cat}/{sub}"
        if key in global_exclude or key in extra_exclude:
            continue

        group = pick_fit(
            fit,
            used,
            cat,
            sub,
            _num(item.get("reps")),
            _num(item.get("duration")) / 1000,
        )
        start = group["start"] if group else _INF

        if overrides.get(key):
            entries.append((start, overrides[key]))
            continue

        zh = moves_map.get(key) or moves_map.get(f"{cat}/") or prettify(sub or cat)
        is_single = key in single_side

        max_weight = _num(item.get("maxWeight"))
        weight = f" @{js_fixed_number(max_weight / 1000, 2)}kg" if max_weight > 0 else ""

        if group and any(x["reps"] > 0 for x in group["sets"]):
            body = fmt_per_set([x["reps"] for x in group["sets"]], "次", is_single)
            if not weight and any(x["weight"] > 0 for x in group["sets"]):
                heaviest = max(x["weight"] for x in group["sets"])
                weight = f" @{js_number(heaviest)}kg"
        elif group and group["sets"]:
            body = fmt_per_set([x["dur"] for x in group["sets"]], "s", is_single)
        else:
            # 无 FIT 逐组明细时，汇总的 reps / duration 是「N 组总量」，不是每组值。
            # 旧版把它塞进「N次 × M组」的位置，等于把 3 组共 60 次谎报成每组 60 次
            # （2026-10-26 起暴露）。绝不伪造每组值（60/3 或 23/3 不整除时更危险），
            # 改用不误导的总量表述；@kg 重量后缀由下方 f"{zh} {body}{weight}" 保留。
            reps = _num(item.get("reps"))
            n_sets = _num(item.get("sets"))
            if reps > 0:
                body = f"共{js_number(reps)}次（{js_number(n_sets)}组，缺逐组明细）"
            else:
                seconds = js_round(_num(item.get("duration")) / 1000)
                body = f"共{js_number(seconds)}s（{js_number(n_sets)}组，缺逐组明细）"

        entries.append((start, f"{zh} {body}{weight}"))

    entries.sort(key=lambda e: e[0])
    if not entries:
        return ""
    return NOTES_HEADER + "；".join(text for _, text in entries)


def truncate_notes(notes: str, limit: int) -> str:
    """按端点上限截断（JS ``slice`` 对 BMP 字符与 Python 切片等价）。"""
    return notes[:limit]
