"""⚠️ **当前无生产调用**：本模块是「真实多源接入」时的预留引擎（用户 2026-10-25 决定保留，L-8）。
    `report_service.detect_day_conflicts` 在多来源时会显式 `NotImplementedError` 并降级披露，
    不会调用这里；接入第二个数据源时，把逐源读数接进来即可直接使用。

    全量字段跨源冲突检测器（Plan §5 T2.5；决策 E6）。

**背景（一期缺口）**：一期 `report_service.py` 调用 `grade(..., conflicts=[])` 写死空列表，
跨源冲突识别缺失；下游「冲突 → 降级 insufficient」逻辑虽已实现，但**上游从未产出冲突**。
本模块补齐上游识别。

**设计（E6 = 全量字段跨源检测）**：
- 对 `StandardDaily` 的**全部数值字段**做跨源比对（非仅静息心率）；
- 冲突定级（`ConflictSeverity`）：`irrelevant` / `affectsConclusion` / `affectsSafety`；
- 记录**采用值、来源、选择依据与差异**（REQ §13.7 要求）；
- `affectsSafety` → 下游 `grade()` 自动降级 `insufficient`（REQ-RISK-005 / REQ-DATA-006）；
- **不得简单平均互相冲突的安全关键指标**（§13.7）—— 采用「优先级选源」而非平均。

**纯函数、无 IO**：输入多来源取值 → 冲突列表，可复现、可单测。

来源优先级（高者为准；用于"采用值"选择，非平均）：
1. 设备直采（`garmin` / 设备）
2. 第三方平台（`tredict`）
3. 手动录入（`manual`，最低，仅作参考）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.domain import DataConflict
from app.schemas.enums import ConflictResolution, ConflictSeverity

# 来源可靠性优先级（数字越大越可信；用于"采用值"选择，**不做平均**）
_SOURCE_PRIORITY: dict[str, int] = {
    "garmin": 100,   # 设备直采
    "device": 100,
    "tredict": 50,   # 第三方平台
    "manual": 10,    # 手动录入
}

# 影响**安全决策**的字段：跨源冲突 → 降级 insufficient（保守）。
# 依据：这些字段直接进入风险规则（R-001/R-002/R-003/R-004）。
SAFETY_CRITICAL_METRICS: frozenset[str] = frozenset(
    {"resting_heart_rate", "hrv_last_night_avg", "sleep_duration_sec"}
)

# 单位/口径容差：小于此比例差异视为"正常波动"，不判冲突（避免浮点噪声）。
# 说明：这是**工程容差**（候选值），非医学阈值。
DEFAULT_REL_TOLERANCE = 0.05
# 睡眠时长等以秒计的字段，绝对容差（秒）——5 分钟
SLEEP_ABS_TOLERANCE_SEC = 300


@dataclass(slots=True)
class MetricReadings:
    """单个指标的多来源读数。"""

    metric: str
    # [{source, value}]，至少两个才可能判冲突
    values: list[dict] = field(default_factory=list)


def _rel_diff(a: float, b: float) -> float:
    """相对差异（以两者均值为基准，避免除零）。"""
    denom = (abs(a) + abs(b)) / 2
    if denom == 0:
        return 0.0
    return abs(a - b) / denom


def _is_conflict(metric: str, values: list[float]) -> bool:
    """判断读数是否构成冲突（超出容差）。"""
    if len(values) < 2:
        return False
    lo, hi = min(values), max(values)
    if metric == "sleep_duration_sec":
        return (hi - lo) > SLEEP_ABS_TOLERANCE_SEC
    if lo == hi:
        return False
    return _rel_diff(hi, lo) > DEFAULT_REL_TOLERANCE


def _severity_of(metric: str) -> ConflictSeverity:
    """字段 → 冲突严重度。安全关键字段影响安全决策。"""
    return (
        ConflictSeverity.AFFECTS_SAFETY
        if metric in SAFETY_CRITICAL_METRICS
        else ConflictSeverity.AFFECTS_CONCLUSION
    )


def _pick_source(values: list[dict]) -> tuple[str, object]:
    """按来源可靠性优先级选择"采用值"（**不平均**，§13.7）。"""
    best = max(values, key=lambda v: _SOURCE_PRIORITY.get(str(v.get("source", "")), 0))
    return str(best.get("source", "")), best.get("value")


def detect_conflicts(readings: list[MetricReadings]) -> list[DataConflict]:
    """检测全量字段的跨源冲突（纯函数）。

    :param readings: 各指标的多来源读数。
    :return: 冲突列表（无冲突则空）。
    """
    conflicts: list[DataConflict] = []
    for r in readings:
        numeric = [
            v
            for v in r.values
            if isinstance(v.get("value"), (int, float)) and v["value"] is not None
        ]
        if len(numeric) < 2:
            continue
        vals = [float(v["value"]) for v in numeric]
        if not _is_conflict(r.metric, vals):
            continue

        severity = _severity_of(r.metric)
        kept_source, kept_value = _pick_source(numeric)
        resolution = (
            ConflictResolution.BLOCKED
            if severity == ConflictSeverity.AFFECTS_SAFETY
            else ConflictResolution.FLAGGED
        )
        conflicts.append(
            DataConflict(
                metric=r.metric,
                values=[
                    {
                        "source": v.get("source"),
                        "value": v.get("value"),
                        "kept": v.get("source") == kept_source,
                    }
                    for v in numeric
                ]
                + [{"kept_value": kept_value, "kept_source": kept_source}],
                severity=severity,
                resolution=resolution,
            )
        )
    return conflicts


def build_readings(
    per_source: dict[str, dict[str, object]],
    *,
    metrics: tuple[str, ...] | None = None,
) -> list[MetricReadings]:
    """由「来源 → {指标: 值}」构造读数列表（便利函数）。

    :param per_source: 如 ``{"garmin": {"resting_heart_rate": 50}, "tredict": {...}}``。
    :param metrics: 限定检测的指标；``None`` → 取所有来源出现过的指标并集（**全量字段**，E6）。
    """
    if metrics is None:
        seen: set[str] = set()
        for m in per_source.values():
            seen.update(m.keys())
        metrics = tuple(sorted(seen))

    out: list[MetricReadings] = []
    for metric in metrics:
        vals = [
            {"source": src, "value": data.get(metric)}
            for src, data in per_source.items()
            if metric in data
        ]
        out.append(MetricReadings(metric=metric, values=vals))
    return out
