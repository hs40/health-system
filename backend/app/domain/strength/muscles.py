"""Garmin 官方动作 → 肌群（三期 §8.4 肌肉热力图，Q9 数据源）。

数据源：Garmin Connect 公开的 ``Exercises.json``，随代码放在本包内
``garmin_exercises.json``（Q9=A；``data/`` 不入库，故不放 raw 目录）。

结构::

    categories[CATEGORY].exercises[EXERCISE] = {primaryMuscles, secondaryMuscles}
    categories[CATEGORY]                     = {primaryMuscles, secondaryMuscles}  # 类别级默认值

键格式 ``CATEGORY/SUBCATEGORY`` 与项目动作库（``moves_zh.json``）一致。
类别级默认肌群用于「汇总项缺 subCategory」的兜底（Q9 实测 8 个真实键走此路径）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).parent
GARMIN_EXERCISES_PATH = DATA_DIR / "garmin_exercises.json"


@dataclass(frozen=True, slots=True)
class Muscle:
    """一个肌群分区：官方 code + 中文名（为主）+ 英文小字（Q11）。"""

    code: str
    zh: str
    en: str


@dataclass(frozen=True, slots=True)
class MuscleHit:
    """一个动作命中到的主/次肌群，以及命中来源。"""

    primary: tuple[Muscle, ...]
    secondary: tuple[Muscle, ...]
    source: str


# Garmin 官方 17 个有效肌群枚举 → (中文名, 英文小字)。
# 顺序按用户提供的正/背面肌肉热力标注图排列，供前端并排绘制（Q10）。
MUSCLE_LABELS: dict[str, tuple[str, str]] = {
    "CHEST": ("胸大肌", "chest"),
    "SHOULDERS": ("三角肌", "shoulders"),
    "BICEPS": ("肱二头肌", "biceps"),
    "TRICEPS": ("肱三头肌", "triceps"),
    "OBLIQUES": ("腹斜肌", "obliques"),
    "FOREARM": ("前臂肌", "forearms"),
    "ABS": ("腹直肌", "abs"),
    "HIPS": ("髋屈肌", "hip flexors"),
    "QUADS": ("股四头肌", "quads"),
    "TRAPS": ("斜方肌", "traps"),
    "LATS": ("背阔肌", "lats"),
    "LOWER_BACK": ("下背部", "lower back"),
    "GLUTES": ("臀肌", "glutes"),
    "ABDUCTORS": ("髋外展肌", "hip abductor"),
    "ADDUCTORS": ("髋内收肌", "hip adductors"),
    "HAMSTRINGS": ("腘绳肌", "hamstrings"),
    "CALVES": ("小腿肌", "calves"),
}


def official_muscles() -> tuple[Muscle, ...]:
    """全部 17 个官方肌群分区（前端据此绘制未涉及区域的灰显底图）。"""
    return tuple(Muscle(code, zh, en) for code, (zh, en) in MUSCLE_LABELS.items())


def label(code: str) -> Muscle:
    """枚举 → 带中英文标签的肌群；未登记的枚举不丢数据，回退英文原名。"""
    zh, en = MUSCLE_LABELS.get(code, (code, code.lower().replace("_", " ")))
    return Muscle(code=code, zh=zh, en=en)


@lru_cache(maxsize=1)
def _categories() -> dict:
    if not GARMIN_EXERCISES_PATH.exists():
        return {}
    doc = json.loads(GARMIN_EXERCISES_PATH.read_text(encoding="utf-8"))
    return doc.get("categories") or {}


def _codes(values: object) -> tuple[Muscle, ...]:
    if not isinstance(values, list):
        return ()
    # 官方数据有 1 处空串脏数据（PLANK/WEIGHTED_SIDE_PLANK_LIFT），不入图
    return tuple(label(str(m)) for m in values if m)


def lookup(key: str) -> MuscleHit:
    """``CATEGORY/SUBCATEGORY`` → 主/次肌群；精确命中动作级定义。"""
    cat, _, sub = str(key).partition("/")
    category = _categories().get(cat) or {}
    exercise = (category.get("exercises") or {}).get(sub)
    if exercise is None:
        # 汇总项常缺 subCategory（如 ``SQUAT/``）；用类别级默认肌群兜底
        if "primaryMuscles" in category or "secondaryMuscles" in category:
            return MuscleHit(
                primary=_codes(category.get("primaryMuscles")),
                secondary=_codes(category.get("secondaryMuscles")),
                source="category",
            )
        return MuscleHit((), (), "none")
    return MuscleHit(
        primary=_codes(exercise.get("primaryMuscles")),
        secondary=_codes(exercise.get("secondaryMuscles")),
        source="exercise",
    )


@dataclass(frozen=True, slots=True)
class MuscleGroupRow:
    """表格一行：某肌群 + 以它为主/次的本次动作（Q11）。"""

    muscle: Muscle
    primary_moves: tuple[str, ...]
    secondary_moves: tuple[str, ...]


def aggregate(moves: list[tuple[str, str]]) -> tuple[MuscleGroupRow, ...]:
    """``(CAT/SUB 键, 中文动作名)`` 列表 → 按「肌群 → 动作」聚合的表格行。

    调用方传入去重后的动作列表；无肌群数据的键不产生行。行序按正/背面
    肌肉图的分区顺序固定，便于前端「先表格后人体图」稳定渲染。
    """
    primary: dict[str, list[str]] = {}
    secondary: dict[str, list[str]] = {}
    for key, zh in moves:
        hit = lookup(key)
        for m in hit.primary:
            primary.setdefault(m.code, []).append(zh)
        for m in hit.secondary:
            secondary.setdefault(m.code, []).append(zh)
    rows: list[MuscleGroupRow] = []
    for m in official_muscles():
        p = tuple(primary.get(m.code, ()))
        s = tuple(secondary.get(m.code, ()))
        if p or s:
            rows.append(MuscleGroupRow(muscle=m, primary_moves=p, secondary_moves=s))
    return tuple(rows)
