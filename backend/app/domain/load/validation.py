"""Tredict 指标 ↔ 项目负荷 的验证（需求第七章，开发计划 4.10）。

**这份模块只做「测量与报告」，不参与任何生产负荷计算。**

口径（需求 7.1–7.4，用户 2026-10-20 确认）：

- 允许的指标只有四个：``trainingEfforts``（所有提供它的活动）、
  ``effort.heartrate`` / ``effort.power`` / ``effort.pace``（仅跑步活动）；
- **每个指标单独回归**，``Y = aX + b``，**允许非零截距**；
- 每个指标都要覆盖两组：**各运动类型内部** 与 **跨运动类型整体**；
- 不同指标**不混在同一组数据**里，指标之间**不做优劣排名**；
- **不设统一通过/不通过阈值**，只报告「是否存在可识别的线性关系」及拟合结果；
- 数据不足 → 明确写「证据不足」，不硬凑结论；
- Tredict 指标**不是**生理负荷真值，只用于验证与比较（需求 7.4）。

变量方向（需求 7.2 要求「实现时统一并明确」）：统一取

    X = 项目负荷（该活动标准化后的 0–100 分值）
    Y = Tredict 指标值

即回归回答「项目负荷变化时，Tredict 指标如何变化」。反过来读（用 Tredict 预测
项目负荷）只需把斜率取倒数，报告里同时给出两种读法的说明。

为什么用标准化分值而不是原始负荷（TRIMP / kg·reps）：原始负荷量纲随类型不同，
跨类型回归根本没有意义（需求 4.1）。标准化分值正是为跨类型可比而设计的。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ------------------------------------------------------------
# 允许的指标（需求 7.1）—— 白名单，不在表里的指标一律不验证
# ------------------------------------------------------------
METRIC_EFFORT = "trainingEfforts"
METRIC_HEARTRATE = "effort.heartrate"
METRIC_POWER = "effort.power"
METRIC_PACE = "effort.pace"

ALLOWED_METRICS: tuple[str, ...] = (
    METRIC_EFFORT,
    METRIC_HEARTRATE,
    METRIC_POWER,
    METRIC_PACE,
)

# 只对这些运动类型适用的指标（其余指标对所有运动类型适用）
METRIC_SPORTS: dict[str, frozenset[str]] = {
    METRIC_EFFORT: frozenset(),  # 空 = 不限运动类型
    METRIC_HEARTRATE: frozenset({"running"}),
    METRIC_POWER: frozenset({"running"}),
    METRIC_PACE: frozenset({"running"}),
}

# 需求 7.1 明确的**禁用**指标：不得作为跨类型统一验证指标
FORBIDDEN_METRICS: tuple[str, ...] = (
    "garmin.hr_zone_duration",   # Garmin 心率区间训练时长
    "tredict.intensityDistribution",
    "tredict.zonesDistribution",
)

# 样本量门槛：低于此值只报「证据不足」，不给 a/b 结论
MIN_SAMPLES_EFFECT = 5      # 低于此值：证据不足，不出结果
MIN_SAMPLES_REPORT = 10     # 低于此值：结果不稳定，需附提示


def metric_allowed(metric: str, sport: str | None = None) -> bool:
    """指标是否允许用于验证（需求 7.1 + 7.2 的运动类型限制）。"""
    if metric in FORBIDDEN_METRICS:
        return False
    if metric not in ALLOWED_METRICS:
        return False
    if sport is None:
        return True
    sports = METRIC_SPORTS.get(metric) or frozenset()
    return not sports or sport in sports


# ------------------------------------------------------------
# 线性回归
# ------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Sample:
    """一个配对样本：同一活动的项目负荷与**某一个** Tredict 指标值。

    ``metric`` 是样本的必填归属：需求 7.2 要求「不同指标不混在同一组数据」，
    靠这个字段而不是调用顺序来保证 —— 谁传错了也混不进来。
    """

    metric: str
    activity_id: str
    sport: str
    x: float          # 项目负荷（标准化分值）
    y: float          # Tredict 指标值


@dataclass(frozen=True, slots=True)
class Regression:
    """最小二乘 ``Y = aX + b`` 及其解释性统计量。"""

    n: int
    slope: float          # a
    intercept: float      # b
    r: float              # 相关系数（允许负相关，不取绝对值）
    r2: float
    x_mean: float
    y_mean: float

    @property
    def direction(self) -> str:
        if self.r > 0:
            return "positive"
        if self.r < 0:
            return "negative"
        return "none"

    @property
    def x_per_y(self) -> float | None:
        """反向读法：``X = (Y - b) / a``。斜率为 0 时无意义，返回 ``None``。"""
        return None if self.slope == 0 else 1.0 / self.slope

    def predict(self, x: float) -> float:
        return self.slope * x + self.intercept


@dataclass(frozen=True, slots=True)
class MetricResult:
    """单个指标在某一组数据上的验证结果（需求 7.3 的报告单元）。"""

    metric: str
    sport: str | None          # None = 跨运动类型整体
    n: int
    status: str                # ok | insufficient | forbidden
    regression: Regression | None = None
    relation: str = ""         # 是否存在可识别的线性关系（文字结论）
    note: str = ""

    @property
    def scope(self) -> str:
        return "crossSport" if self.sport is None else "bySport"

    def to_dict(self) -> dict:
        out: dict = {
            "metric": self.metric,
            "scope": self.scope,
            "sport": self.sport,
            "n": self.n,
            "status": self.status,
            "relation": self.relation,
            "note": self.note,
        }
        if self.regression is not None:
            g = self.regression
            out["fit"] = {
                "formula": "Y = aX + b",
                "a": round(g.slope, 6),
                "b": round(g.intercept, 4),
                "r": round(g.r, 4),
                "r2": round(g.r2, 4),
                "direction": g.direction,
                "x_per_y": None if g.x_per_y is None else round(g.x_per_y, 6),
                "x_mean": round(g.x_mean, 3),
                "y_mean": round(g.y_mean, 3),
            }
        return out


def fit(samples: list[Sample]) -> Regression | None:
    """最小二乘拟合 ``Y = aX + b``；样本为空或 X 无方差 → ``None``。"""
    n = len(samples)
    if n < 2:
        return None
    xs = [s.x for s in samples]
    ys = [s.y for s in samples]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    sxx = sum((x - x_mean) ** 2 for x in xs)
    if sxx == 0:
        return None  # X 全相同，斜率无定义
    sxy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    intercept = y_mean - slope * x_mean

    syy = sum((y - y_mean) ** 2 for y in ys)
    if syy == 0:
        # Y 全相同：与 X 无关，r 记 0（不伪造 ±1）
        r, r2 = 0.0, 0.0
    else:
        r = sxy / math.sqrt(sxx * syy)
        r = max(-1.0, min(1.0, r))
        r2 = r * r
    return Regression(
        n=n,
        slope=slope,
        intercept=intercept,
        r=r,
        r2=r2,
        x_mean=x_mean,
        y_mean=y_mean,
    )


def _relation_text(g: Regression | None, n: int) -> str:
    """文字结论：只描述「是否存在可识别的线性关系」，**不判定通过与否**（需求 7.3）。

    判定用语刻意保守：``r²`` 只用来区分「关系清晰」与「关系微弱」，
    这不是验收阈值，而是描述性分档。
    """
    if g is None:
        return "无法拟合（样本过少或项目负荷无变化）"
    if n < MIN_SAMPLES_REPORT:
        return f"关系不明确（仅 {n} 个样本，不足以判断）"
    if g.r2 >= 0.5:
        return f"存在可识别的线性关系（{'正' if g.r > 0 else '负'}相关，R²={g.r2:.2f}）"
    if g.r2 >= 0.16:
        return f"存在较弱线性关系（{'正' if g.r > 0 else '负'}相关，R²={g.r2:.2f}）"
    return f"未见可识别的线性关系（R²={g.r2:.2f}）"


def evaluate_metric(
    metric: str,
    sport: str | None,
    samples: list[Sample],
) -> MetricResult:
    """对**单一指标、单一分组**做验证。绝不跨指标合并样本（需求 7.2）。"""
    if metric in FORBIDDEN_METRICS:
        return MetricResult(
            metric=metric,
            sport=sport,
            n=len(samples),
            status="forbidden",
            relation="该指标已被需求 7.1 明确禁止作为验证指标",
        )
    if metric not in ALLOWED_METRICS:
        return MetricResult(
            metric=metric,
            sport=sport,
            n=len(samples),
            status="forbidden",
            relation="不在允许指标范围内（需求 7.1）",
        )

    # 需求 7.2：先按「指标归属」隔离，再按「运动类型适用性」过滤。
    # 不按 ``metric`` 字段过滤就会把四个指标混进同一组回归（这正是需求禁止的）。
    usable = [
        s
        for s in samples
        if s.metric == metric and metric_allowed(metric, s.sport)
    ]
    n = len(usable)
    if n < MIN_SAMPLES_EFFECT:
        return MetricResult(
            metric=metric,
            sport=sport,
            n=n,
            status="insufficient",
            relation="证据不足",
            note=f"有效配对样本 {n} 个，少于 {MIN_SAMPLES_EFFECT} 个，不下结论",
        )

    g = fit(usable)
    if g is None:
        return MetricResult(
            metric=metric,
            sport=sport,
            n=n,
            status="insufficient",
            relation="无法拟合",
            note="项目负荷在该组内无变化，斜率无定义",
        )

    note = ""
    if n < MIN_SAMPLES_REPORT:
        note = f"样本偏少（{n} < {MIN_SAMPLES_REPORT}），结果不稳定，仅供参考"
    return MetricResult(
        metric=metric,
        sport=sport,
        n=n,
        status="ok",
        regression=g,
        relation=_relation_text(g, n),
        note=note,
    )


# ------------------------------------------------------------
# 整份验证报告
# ------------------------------------------------------------
@dataclass(slots=True)
class ValidationReport:
    """一次验证运行的完整结果（需求 7.3 / 6.4）。

    - ``by_metric``：每个指标一组（跨类型整体）；
    - ``by_sport``：每个指标 × 每个运动类型；
    - 刻意**不提供**「总体通过/不通过」字段 —— 需求明确禁止统一阈值。
    """

    metrics: list[MetricResult] = field(default_factory=list)
    sports: list[MetricResult] = field(default_factory=list)
    samples_total: int = 0
    date_range: tuple[str, str] | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "date_range": list(self.date_range) if self.date_range else None,
            "samples_total": self.samples_total,
            "metrics": [m.to_dict() for m in self.metrics],
            "by_sport": [m.to_dict() for m in self.sports],
            "warnings": self.warnings,
            "disclaimer": (
                "Tredict 指标仅用于验证与比较，不视为生理负荷真值；"
                "不设统一通过阈值，各指标分别报告，不做优劣排名（需求 7.3 / 7.4）。"
            ),
        }


def build_report(
    samples: list[Sample],
    *,
    date_range: tuple[str, str] | None = None,
) -> ValidationReport:
    """按指标 × 作用域生成完整报告。

    分组规则：
    - 指标维度：四个允许指标各一条；
    - 运动类型维度：只对**该指标实际有样本**的运动类型出结果（不凑数）。
    """
    report = ValidationReport(samples_total=len(samples), date_range=date_range)
    sports_present = sorted({s.sport for s in samples})

    for metric in ALLOWED_METRICS:
        metric_samples = [
            s for s in samples if s.metric == metric and metric_allowed(metric, s.sport)
        ]
        report.metrics.append(evaluate_metric(metric, None, metric_samples))

        for sport in sports_present:
            pool = [s for s in metric_samples if s.sport == sport]
            if not pool:
                continue
            report.sports.append(evaluate_metric(metric, sport, pool))
            if sport not in METRIC_SPORTS.get(metric, frozenset()) and METRIC_SPORTS.get(metric):
                report.warnings.append(
                    f"指标 {metric} 不适用于运动 {sport}，相关样本已排除（需求 7.1）"
                )

    # 去重警告
    report.warnings = list(dict.fromkeys(report.warnings))
    return report
