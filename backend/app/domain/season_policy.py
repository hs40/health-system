"""季节策略引擎（Plan §3.5 / §5 T2.3；决策 E4/E9）。

**设计原则**：
- **纯函数、无 IO**：季节 → 策略内容，可复现、可单测、可历史回放。
- **不预设固定训练比例**（§4.5）：`endurance_ratio` / `strength_ratio` / `recovery_ratio`
  默认 `None`，表示**动态判断**；只有在季节本身有明确依据时才给出固定比例。
- **季节由日期推断**，但允许显式覆盖（测试与跨年/换季过渡用）。
- **硬性环境约束不在本模块**（见 `domain/environment_rules.py`）：本模块只产出「季节意图」，
  环境硬约束（雨雪/<5°C → 室内）**优先级更高**，不得被季节策略覆盖（§4.6）。

产出结构与 Plan §4.5 的 `SeasonPolicy` 对齐。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class Season(StrEnum):
    """四季 + 过渡期（换季）。"""

    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"
    TRANSITIONAL = "transitional"  # 换季过渡（依据不充分时保守）


# 季节推断的月份边界（北半球）。
# 与气象季节一致：春 3-5、夏 6-8、秋 9-11、冬 12-2。
_SPRING_MONTHS = (3, 4, 5)
_SUMMER_MONTHS = (6, 7, 8)
_AUTUMN_MONTHS = (9, 10, 11)
_WINTER_MONTHS = (12, 1, 2)


@dataclass(slots=True, frozen=True)
class SeasonPolicy:
    """季节策略内容（对应 Plan §4.5 ``SeasonPolicy``）。"""

    season: str
    focus: str
    # 资源分配：None 表示**动态判断**（不预设固定比例，§4.5）
    endurance_ratio: float | None = None
    strength_ratio: float | None = None
    recovery_ratio: float | None = None
    # 长期优先级排序（cardio / fatLoss / muscleEndurance / maxStrength）
    long_term_priority: tuple[str, ...] = ("cardio", "fatLoss", "muscleEndurance", "maxStrength")
    limitations: tuple[str, ...] = field(default_factory=tuple)
    # 季节意图说明（人类可读，进入报告与审计）
    rationale: str = ""
    # 候选版本（与 config.season_policy_version 对齐）
    version: str = "season-0.1.0"


def infer_season(on_date: date, *, override: Season | None = None) -> Season:
    """由日期推断季节（北半球气象季节）；允许显式覆盖。"""
    if override is not None:
        return override
    m = on_date.month
    if m in _SPRING_MONTHS:
        return Season.SPRING
    if m in _SUMMER_MONTHS:
        return Season.SUMMER
    if m in _AUTUMN_MONTHS:
        return Season.AUTUMN
    if m in _WINTER_MONTHS:
        return Season.WINTER
    return Season.TRANSITIONAL  # 理论不可达（月份 1-12）


# —— 各季节策略定义（**不预设固定训练比例**，§4.5）——
_POLICIES: dict[Season, SeasonPolicy] = {
    Season.SPRING: SeasonPolicy(
        season=Season.SPRING.value,
        focus="逐步回升训练量，重建有氧基础与力量耐力",
        long_term_priority=("cardio", "fatLoss", "muscleEndurance", "maxStrength"),
        limitations=(
            "换季阶段气温波动大，注意热身与保暖",
            "训练量回升应渐进，避免骤然加量",
        ),
        rationale="春季气温回升、日照变长，适合逐步恢复训练量并重建有氧基础。",
    ),
    Season.SUMMER: SeasonPolicy(
        season=Season.SUMMER.value,
        focus="避开高温时段，重点维持有氧与减脂，控制强度避免热应激",
        long_term_priority=("cardio", "fatLoss", "muscleEndurance", "maxStrength"),
        limitations=(
            "高温高湿：避开正午时段，注意补水与电解质",
            "热环境下降强度、延长恢复，不追求峰值表现",
        ),
        rationale="夏季高温高湿，训练以避开热应激、维持有氧与减脂为主。",
    ),
    Season.AUTUMN: SeasonPolicy(
        season=Season.AUTUMN.value,
        focus="气温适宜，适合提升强度与专项能力，可安排强度周期",
        long_term_priority=("cardio", "muscleEndurance", "maxStrength", "fatLoss"),
        limitations=(
            "秋季适宜，但昼夜温差大，注意训练后保暖",
        ),
        rationale="秋季气温适宜、空气干爽，是一年中提升强度与专项能力的窗口。",
    ),
    Season.WINTER: SeasonPolicy(
        season=Season.WINTER.value,
        focus="资源分配**动态判断**：低温下以室内训练、力量与肌耐力为主，保持有氧",
        # 冬季不预设固定比例（§4.5：资源分配动态判断）
        endurance_ratio=None,
        strength_ratio=None,
        recovery_ratio=None,
        long_term_priority=("muscleEndurance", "maxStrength", "cardio", "fatLoss"),
        limitations=(
            "低温/雨雪：户外训练受硬性环境约束限制（见 environment_rules）",
            "室内资源（力量器材）有限，训练安排需匹配实有器材",
        ),
        rationale="冬季低温，户外受限，训练资源向室内力量/肌耐力倾斜；具体比例动态判断。",
    ),
    Season.TRANSITIONAL: SeasonPolicy(
        season=Season.TRANSITIONAL.value,
        focus="换季过渡：保守维持，待季节明确后再调整重点",
        long_term_priority=("cardio", "fatLoss", "muscleEndurance", "maxStrength"),
        limitations=("季节依据不充分，采取保守策略",),
        rationale="换季过渡期，季节依据不充分，采取保守维持策略。",
    ),
}


def build_season_policy(
    on_date: date,
    *,
    version: str | None = None,
    override: Season | None = None,
) -> SeasonPolicy:
    """按日期（可覆盖）构建季节策略（纯函数）。

    :param on_date: 目标日期。
    :param version: 策略版本（默认为 ``SeasonPolicy.version``）。
    :param override: 显式指定季节（测试/换季用），否则由日期推断。
    """
    season = infer_season(on_date, override=override)
    base = _POLICIES[season]
    if version is None or version == base.version:
        return base
    # 版本覆盖：仅替换 version 字段，保持策略内容
    return SeasonPolicy(
        season=base.season,
        focus=base.focus,
        endurance_ratio=base.endurance_ratio,
        strength_ratio=base.strength_ratio,
        recovery_ratio=base.recovery_ratio,
        long_term_priority=base.long_term_priority,
        limitations=base.limitations,
        rationale=base.rationale,
        version=version,
    )
