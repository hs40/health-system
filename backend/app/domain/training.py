"""运动训练详情纯计算（三期 §7/§8）。

本模块只做无 IO 的计算：运动类型 → 展示风格/详情模板、指标格式化、
训练效果中文标签、心率曲线、分段与力量时间轴。raw 读取与响应组装在
``services/training_service.py``。

需求 §8 把详情划为六类模板：跑步 / 室内有氧 / HIIT / 力量 / 游泳 / 其他。
§7.2 要求「未来 Garmin 增加新运动类型也应可扩展」—— 未登记类型一律
回退「其他」模板与中性配色，不虚构中文名。
"""

from __future__ import annotations

# typeKey -> (中文 label, Emoji, 日历标识色, 详情模板)
_ACTIVITY_STYLES: dict[str, tuple[str, str, str, str]] = {
    "running": ("跑步", "🏃", "#E8590C", "running"),
    "indoor_cardio": ("室内有氧", "🚴", "#2F9E44", "indoor_cardio"),
    "hiit": ("HIIT", "🔥", "#F03E3E", "hiit"),
    "strength_training": ("力量训练", "🏋️", "#7048E8", "strength"),
    "lap_swimming": ("游泳", "🏊", "#1098AD", "swimming"),
    "walking": ("步行", "🚶", "#868E96", "other"),
    "hiking": ("徒步", "🥾", "#5C940D", "other"),
    "meditation": ("冥想", "🧘", "#ADB5BD", "other"),
    "breathwork": ("呼吸训练", "🌬️", "#ADB5BD", "other"),
}

_NEUTRAL = ("🏅", "#ADB5BD", "other")


def template_of(type_key: str) -> str:
    """运动类型 → 六类详情模板之一；未登记类型回退 ``other``。"""
    return _ACTIVITY_STYLES.get(str(type_key or ""), (None, None, None, "other"))[3]


def style_of(type_key: str) -> dict:
    """运动类型 → 日历标识与详情文字（``label`` / ``emoji`` / ``color`` / ``template``）。"""
    key = str(type_key or "")
    if key in _ACTIVITY_STYLES:
        label, emoji, color, template = _ACTIVITY_STYLES[key]
    else:
        label = key
        emoji, color, template = _NEUTRAL
    return {"label": label, "emoji": emoji, "color": color, "template": template}


# ------------------------------------------------------------
# 指标格式化 / 训练效果（需求 §8.1、Q4）
# ------------------------------------------------------------
def format_pace(min_per_km: float | None) -> str | None:
    """Garmin ``averagePaceMinPerKm`` 是**小数分钟**，统一渲染为 ``分'秒"/km``（Q4）。

    缺失 / 0 / 非数值返回 ``None`` —— 力量、HIIT、室内有氧该字段为 null，
    不得虚构配速（§8.2/§8.3）。
    """
    if isinstance(min_per_km, bool) or not isinstance(min_per_km, (int, float)):
        return None
    if float(min_per_km) <= 0:
        return None
    total_sec = round(float(min_per_km) * 60)
    minutes, seconds = divmod(total_sec, 60)
    return f"{minutes}'{seconds:02d}\"/km"


# 真实 Garmin 枚举全集（本地 306 条活动统计，2026-11-06）
_EFFECT_LABELS: dict[str, str] = {
    "AEROBIC_BASE": "有氧基础",
    "TEMPO": "节奏",
    "UNKNOWN": "未知",
    "ANAEROBIC_CAPACITY": "无氧能力",
    "RECOVERY": "恢复",
    "LACTATE_THRESHOLD": "乳酸阈值",
}

_AEROBIC_ZH: dict[str, str] = {
    "NO_AEROBIC_BENEFIT": "无有氧收益",
    "MINOR_AEROBIC_BENEFIT": "轻微有氧收益",
    "RECOVERY": "恢复",
    "MAINTAINING_AEROBIC_BASE": "维持有氧基础",
    "IMPROVING_AEROBIC_BASE": "提升有氧基础",
    "MAINTAINING_AEROBIC_FITNESS": "维持有氧体能",
    "IMPROVING_AEROBIC_FITNESS": "提升有氧体能",
    "IMPROVING_AEROBIC_ENDURANCE": "提升有氧耐力",
    "MAINTAINING_TEMPO": "维持节奏能力",
    "IMPACTING_TEMPO": "影响节奏能力",
    "HIGHLY_IMPACTING_TEMPO": "显著影响节奏能力",
    "MAINTAINING_LACTATE_THRESHOLD": "维持乳酸阈值",
    "IMPROVING_LACTATE_THRESHOLD": "提升乳酸阈值",
    "HIGHLY_IMPROVING_LACTATE_THRESHOLD": "显著提升乳酸阈值",
}

_ANAEROBIC_ZH: dict[str, str] = {
    "NO_ANAEROBIC_BENEFIT": "无无氧收益",
    "MINOR_ANAEROBIC_BENEFIT": "轻微无氧收益",
    "MAINTAINING_ANAEROBIC_BASE": "维持无氧基础",
}


def _message_zh(raw: object, table: dict[str, str]) -> str | None:
    """``PREFIX_<数字>`` → 中文描述。

    末尾数字是 Garmin 内部效果值编码，含义未经核实，故**不解析成数值**
    （需求禁止虚构指标）；只取前缀映射，未登记前缀回退英文小写。
    """
    if not isinstance(raw, str) or not raw:
        return None
    prefix, _, tail = raw.rpartition("_")
    key = prefix if tail.isdigit() else raw
    return table.get(key, key.replace("_", " ").lower())


def training_effect(payload: dict) -> dict:
    """有氧 / 无氧训练效果的中文标签（§8.1「可以放在一起展示」）。"""
    label = payload.get("trainingEffectLabel")
    return {
        "label": _EFFECT_LABELS.get(label, label or "未知"),
        "label_raw": label,
        "aerobic": _message_zh(payload.get("aerobicTrainingEffectMessage"), _AEROBIC_ZH),
        "anaerobic": _message_zh(payload.get("anaerobicTrainingEffectMessage"), _ANAEROBIC_ZH),
    }


# ------------------------------------------------------------
# 图表与分段数据（需求 §8.1 / §8.4，Q8 / Q12）
# ------------------------------------------------------------
def build_hr_series(records: list[dict]) -> list[dict]:
    """逐秒 FIT ``record`` → 时间—心率折线图数据。

    心率缺失保留 ``None``（断线），**不补 0**（Q8）；顺序保持 raw 的逐秒顺序。
    """
    out: list[dict] = []
    for row in records:
        hr = row.get("heart_rate")
        if isinstance(hr, bool) or not isinstance(hr, (int, float)):
            hr = None
        out.append({"t": row.get("timestamp"), "hr": hr})
    return out


# FIT lap.intensity 的真实取值 → 分段训练内容（§8.1「分段中注明训练内容」）。
# 用 Garmin 实际字段标注，不虚构「轻松跑 / 间歇跑」这种语义推断。
_LAP_INTENSITY_ZH: dict[str, str] = {
    "active": "主动段",
    "warmup": "热身",
    "cooldown": "放松",
    "interval": "间歇",
    "recovery": "恢复",
}


def build_segments(laps: list[dict]) -> list[dict]:
    """FIT ``lap`` → 训练分段（§8.1 明确「以实际 Garmin Lap 数据为依据」）。"""
    out: list[dict] = []
    for i, lap in enumerate(laps, 1):
        intensity = lap.get("intensity")
        duration = lap.get("total_timer_time")
        if duration is None:
            duration = lap.get("total_elapsed_time")
        out.append(
            {
                "index": i,
                "label": _LAP_INTENSITY_ZH.get(intensity, "分段"),
                "intensity": intensity,
                "duration_sec": round(float(duration or 0.0), 1),
                "distance_m": round(float(lap.get("total_distance") or 0.0), 1),
                "avg_hr": lap.get("avg_heart_rate"),
                "max_hr": lap.get("max_heart_rate"),
            }
        )
    return out


def _first(value: object) -> object:
    """FIT 的 ``category`` / ``category_subtype`` 是二元数组，只取第一个。"""
    return value[0] if isinstance(value, (list, tuple)) and value else value


def build_strength_timeline(fit_sets: list[dict]) -> dict:
    """FIT ``set`` → 力量训练时间轴（§8.4 / Q12）。

    - ``active`` 组：动作 + 中文名 + 次数 + 重量（kg）+ 持续秒数；
    - ``rest`` 组：只显示持续秒数，**统一用秒**，不写「X分X秒」；
    - 正式动作数量按项目既有 ``exercises.json`` 清单的 ``counted`` 口径，
      热身 / 拉伸（``WARM_UP``）不计入（§8.4）。
    """
    from app.domain.strength import exercises, notes

    moves = notes.load_moves()
    rows = sorted(fit_sets, key=lambda r: str(r.get("start_time") or "9999"))
    segments: list[dict] = []
    active_sec = rest_sec = 0.0
    formal: set[str] = set()

    for row in rows:
        set_type = row.get("set_type")
        duration = float(row.get("duration") or 0.0)
        if set_type == "rest":
            rest_sec += duration
            segments.append({"kind": "rest", "duration_sec": round(duration)})
            continue
        if set_type != "active":
            continue

        raw_cat = _first(row.get("category"))
        cat = str(raw_cat or "").upper() or "UNKNOWN"
        sub = notes.decode_sub(raw_cat, _first(row.get("category_subtype")))
        key = f"{cat}/{sub}"
        zh = moves.get(key) or moves.get(f"{cat}/") or notes.prettify(sub or cat)

        segment: dict = {
            "kind": "exercise",
            "key": key,
            "name_zh": zh,
            "duration_sec": round(duration),
        }
        reps = row.get("repetitions")
        if isinstance(reps, (int, float)) and not isinstance(reps, bool) and reps > 0:
            segment["reps"] = int(reps)
        weight = float(row.get("weight") or 0.0)
        if weight > 0:
            segment["weight_kg"] = round(weight, 2)

        active_sec += duration
        segments.append(segment)
        if cat != "WARM_UP" and exercises.entry(key)["tier"] == exercises.TIER_COUNTED:
            formal.add(key)

    return {
        "segments": segments,
        "active_seconds": round(active_sec, 1),
        "rest_seconds": round(rest_sec, 1),
        "formal_exercise_count": len(formal),
        "formal_exercises": sorted(formal),
    }


# ------------------------------------------------------------
# 指标表格（需求 §8.1 / §8.2 / §8.3）
# ------------------------------------------------------------
def format_duration(seconds: float | None) -> str | None:
    """秒 → ``H:MM:SS`` / ``M:SS``；缺失或非正数返回 ``None``。"""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if float(seconds) <= 0:
        return None
    total = round(float(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_distance_km(meters: float | None) -> str | None:
    """米 → ``X.XX km``；缺失 / 0 返回 ``None``（不虚构距离）。"""
    if isinstance(meters, bool) or not isinstance(meters, (int, float)):
        return None
    if float(meters) <= 0:
        return None
    return f"{float(meters) / 1000.0:.2f} km"


_UNSUPPORTED = "该运动不支持"


def build_metrics(payload: dict, template: str) -> list[dict]:
    """指标表格（§8.1 跑步结构；§8.2/§8.3 复用）。

    只展示实际存在且有意义的指标：缺字段直接省略；室内有氧 / HIIT 的距离与
    配速按需求明确标注「该运动不支持」，不虚构（§8.2/§8.3）。训练类型 /
    训练名称由 service 单独插入（来自 ``ActivityRef``，不在 payload）。
    """
    rows: list[dict] = []

    def add(label: str, value: str | None) -> None:
        if value is not None:
            rows.append({"label": label, "value": value})

    add("训练时长", format_duration(payload.get("duration")))
    if template in ("indoor_cardio", "hiit"):
        add("距离", _UNSUPPORTED)
        add("平均配速", _UNSUPPORTED)
        add("最佳配速", _UNSUPPORTED)
    else:
        add("距离", format_distance_km(payload.get("distance")))
        add("平均配速", format_pace(payload.get("averagePaceMinPerKm")))
        add("最佳配速", format_pace(payload.get("maxPaceMinPerKm")))

    avg_hr = payload.get("averageHR")
    max_hr = payload.get("maxHR")
    if avg_hr is not None or max_hr is not None:
        avg_text = avg_hr if avg_hr is not None else "—"
        max_text = max_hr if max_hr is not None else "—"
        add("平均心率 / 最高心率（bpm）", f"{avg_text} / {max_text}")
    return rows


def build_muscle_views(segments: list[dict]) -> dict:
    """力量时间轴动作 → 肌群表格 + 肌肉图分区高亮（Q10/Q11，需求 §8.4）。

    表格按「肌群 → 本次涉及动作」聚合；肌肉图返回全部 17 个官方分区，
    每个标记 ``primary`` / ``secondary`` / ``none``，供前端按 tokens.css 上色。
    """
    from app.domain.strength import muscles as muscles_mod

    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for seg in segments:
        if seg.get("kind") != "exercise":
            continue
        key = seg.get("key")
        if not key or key in seen:
            continue
        seen.add(key)
        pairs.append((key, str(seg.get("name_zh") or key)))

    rows = muscles_mod.aggregate(pairs)
    table = [
        {
            "code": r.muscle.code,
            "zh": r.muscle.zh,
            "en": r.muscle.en,
            "primary_moves": list(r.primary_moves),
            "secondary_moves": list(r.secondary_moves),
        }
        for r in rows
    ]
    primary = {r.muscle.code for r in rows if r.primary_moves}
    secondary = {r.muscle.code for r in rows if r.secondary_moves}
    regions = [
        {
            "code": m.code,
            "zh": m.zh,
            "en": m.en,
            "level": (
                "primary" if m.code in primary else ("secondary" if m.code in secondary else "none")
            ),
        }
        for m in muscles_mod.official_muscles()
    ]
    return {"muscle_table": table, "muscles": regions}


_SWIM_STROKE_ZH: dict[str, str] = {
    "freestyle": "自由泳",
    "backstroke": "仰泳",
    "breaststroke": "蛙泳",
    "butterfly": "蝶泳",
    "mixed": "混合泳",
    "drill": "技术练习",
}


def build_swimming(payload: dict, lengths: list[dict]) -> dict:
    """游泳专属指标（§8.5）：只展示真实存在的字段，泳池长度按官方 factor 换算。

    ``poolLength`` 是带单位的整数（2500 + factor 100 → 25 m），不能当米直接展示。
    """
    pool = payload.get("poolLength")
    unit = payload.get("unitOfPoolLength")
    factor = float(unit.get("factor") or 100.0) if isinstance(unit, dict) else 100.0
    pool_m = None
    if (
        isinstance(pool, (int, float))
        and not isinstance(pool, bool)
        and pool > 0
        and factor > 0
    ):
        pool_m = round(float(pool) / factor, 1)

    stroke_types: list[str] = []
    length_rows: list[dict] = []
    for row in lengths:
        stroke = row.get("swim_stroke")
        if stroke and stroke not in stroke_types:
            stroke_types.append(stroke)
        length_rows.append(
            {
                "stroke": _SWIM_STROKE_ZH.get(stroke, stroke),
                "duration_sec": round(float(row.get("total_elapsed_time") or 0.0), 1),
                "strokes": row.get("total_strokes"),
                "avg_speed": row.get("avg_speed"),
                "length_type": row.get("length_type"),
            }
        )

    return {
        "distance_m": payload.get("distance"),
        "pool_length_m": pool_m,
        "strokes": payload.get("strokes"),
        "active_lengths": payload.get("activeLengths"),
        "average_swolf": payload.get("averageSwolf"),
        "stroke_types": [_SWIM_STROKE_ZH.get(s, s) for s in stroke_types],
        "lengths": length_rows,
    }
