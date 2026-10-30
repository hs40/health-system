"""硬性环境约束（Plan §4.6 / §5 T2.4）。

**这是硬性业务约束，优先级高于季节策略，不得被季节策略覆盖**（§4.6）：
- **雨雪** → 户外改室内；
- **气温 < 5°C** → 户外改室内；
- 天气不可用 → **不放宽安全阈值、不编造**（§3.1.1 降级）。

**纯函数、无 IO**：由环境数据（`EnvironmentDaily`）→ 约束判定，可复现、可单测。

与季节策略的关系（调用顺序）：
1. 先由 `season_policy` 产出季节意图；
2. 再由本模块施加**硬性环境覆盖**（可否定户外安排）；
3. 最终计划不得违反本模块的约束。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# 硬性阈值（业务约束，§4.6）
TEMP_LOW_C = 5.0          # 低于此温度 → 户外改室内
TEMP_HIGH_STOP_OUTDOOR = 30.0  # 高于此温度 → 建议停止户外（季节策略 threshold，非硬拦截）


class ConstraintKind(StrEnum):
    RAIN_SNOW_INDOOR = "rainSnowIndoor"
    TEMP_LOW_INDOOR = "tempLowIndoor"
    WEATHER_UNAVAILABLE = "weatherUnavailable"
    TEMP_HIGH_ADVISORY = "tempHighAdvisory"  # 高温建议（非硬性）


@dataclass(slots=True, frozen=True)
class EnvironmentConstraint:
    """单条环境约束判定结果。"""

    kind: ConstraintKind
    hard: bool            # True = 硬性（不可被季节策略覆盖）
    reason: str
    force_indoor: bool = False  # 是否强制改室内


@dataclass(slots=True, frozen=True)
class EnvironmentDecision:
    """环境约束汇总（进入计划生成与报告）。"""

    constraints: tuple[EnvironmentConstraint, ...] = ()
    weather_available: bool = True

    @property
    def indoor_required(self) -> bool:
        """是否存在强制室内（硬性）约束。"""
        return any(c.hard and c.force_indoor for c in self.constraints)

    @property
    def hard_reasons(self) -> list[str]:
        return [c.reason for c in self.constraints if c.hard]

    @property
    def advisory_reasons(self) -> list[str]:
        return [c.reason for c in self.constraints if not c.hard]


def evaluate_environment(
    *,
    temp_c: float | None,
    is_rain: bool = False,
    is_snow: bool = False,
    weather_available: bool = True,
) -> EnvironmentDecision:
    """施加硬性环境约束（纯函数）。

    :param temp_c: 气温（℃）；``None`` 表示缺失。
    :param is_rain: 是否降雨。
    :param is_snow: 是否降雪。
    :param weather_available: 天气数据是否可用；``False`` → 不编造、仅标注。
    """
    constraints: list[EnvironmentConstraint] = []

    if not weather_available:
        # 天气不可用：不猜、不放宽（§3.1.1 降级）
        constraints.append(
            EnvironmentConstraint(
                kind=ConstraintKind.WEATHER_UNAVAILABLE,
                hard=False,  # 不强制室内（避免误判），但明确标注缺失
                reason="天气数据不可用：不编造、不放宽安全阈值；户外安排需谨慎并临近确认",
                force_indoor=False,
            )
        )
        return EnvironmentDecision(tuple(constraints), weather_available=False)

    # —— 硬性：雨雪 → 室内 ——
    if is_rain or is_snow:
        kind = ConstraintKind.RAIN_SNOW_INDOOR
        cond = "降雪" if is_snow else "降雨"
        constraints.append(
            EnvironmentConstraint(
                kind=kind,
                hard=True,
                reason=f"{cond}：硬性约束 → 户外改室内（§4.6）",
                force_indoor=True,
            )
        )

    # —— 硬性：气温 < 5°C → 室内 ——
    if temp_c is not None and temp_c < TEMP_LOW_C:
        constraints.append(
            EnvironmentConstraint(
                kind=ConstraintKind.TEMP_LOW_INDOOR,
                hard=True,
                reason=f"气温 {temp_c:g}°C < {TEMP_LOW_C:g}°C：硬性约束 → 户外改室内（§4.6）",
                force_indoor=True,
            )
        )

    # —— 非硬性：高温建议 ——
    if temp_c is not None and temp_c > TEMP_HIGH_STOP_OUTDOOR:
        constraints.append(
            EnvironmentConstraint(
                kind=ConstraintKind.TEMP_HIGH_ADVISORY,
                hard=False,
                reason=f"气温 {temp_c:g}°C 偏高：建议避开正午、降低强度（建议性）",
                force_indoor=False,
            )
        )

    return EnvironmentDecision(tuple(constraints), weather_available=True)
