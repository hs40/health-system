"""基线引擎（TA.6）：分指标个人基线，纯函数、可复现。

设计要点（2026-10-22 风险引擎重设计 Q5 后的口径）：
- **统一口径**：HRV / 静息心率 / 睡眠时长**全部使用近 28 天滚动窗口**。
  此前（E23）的分指标口径（HRV 28 天 p10、RHR/睡眠全历史 mean±σ）已废弃：
    - 全历史会被用户长期的生活方式漂移绑死，近期变化反映不出来；
    - p10 分位在 HRV 上对样本量极敏感，样本少时下界不稳。
  28 天是运动科学通用窗口，且三个指标口径一致，判定依据更易解释。
- **无前视偏差**：窗口为 ``(as_of - 28, as_of]``，**绝不**使用 ``as_of`` 之后的数据。
- 样本不足 → ``mature=False``，上层据此走**保守分支**（不做相对阈值判定）。
- 阈值口径集中在 ``domain/rule_registry.py``（T1.4），本模块只负责计算与派生。
- 每个 (metric, version) 组合可复现，写入 ``baseline`` 表供审计回溯。

注意：σ 倍数（1.0）、窗口长度（28 天）、最小样本数（14）均为**候选值**，
需随数据积累校准（与风险规则同批验证）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

from app.domain.rule_registry import (
    BASELINE_MIN_SAMPLES,
    BASELINE_SIGMA_MULTIPLIER,
    BASELINE_WINDOW_DAYS,
)
from app.schemas.domain import Baseline, StandardDaily

BASELINE_VERSION = 2  # Q5 口径变更（全历史/p10 → 统一 28 天 mean±σ），版本号 +1
DEFAULT_WINDOW_DAYS = BASELINE_WINDOW_DAYS
# 全历史口径的哨兵值：0 表示「截至 as_of 的全部历史」（不设左边界）。
# Q5 后**不再有指标使用**，保留常量供历史库读取与回放兼容。
FULL_HISTORY_WINDOW_DAYS = 0
# 最小有效样本数：低于此值视为"未成熟"，不用于相对阈值判定。
MIN_SAMPLES = BASELINE_MIN_SAMPLES

# 参与基线计算的指标 → StandardDaily 字段名
SUPPORTED_METRICS: dict[str, str] = {
    "hrv_last_night_avg": "hrv_last_night_avg",
    "resting_heart_rate": "resting_heart_rate",
    "sleep_duration_sec": "sleep_duration_sec",
}


@dataclass(frozen=True, slots=True)
class MetricPolicy:
    """某指标的基线口径：窗口 + 判定阈值类型（E23）。"""

    window_days: int   # FULL_HISTORY_WINDOW_DAYS(0) = 全历史
    threshold: str     # "p10" | "p90" | "meanPlusSigma" | "meanMinusSigma"

    @property
    def full_history(self) -> bool:
        return self.window_days == FULL_HISTORY_WINDOW_DAYS


# 分指标口径（Q5）：三个指标统一 28 天滚动 + 均值 ± kσ
# 阈值类型保留字段，便于将来个别指标改用不同派生方式时只改此处。
_THRESHOLD_KINDS: dict[str, str] = {
    "hrv_last_night_avg": "meanMinusSigma",
    "resting_heart_rate": "meanPlusSigma",
    "sleep_duration_sec": "meanMinusSigma",
}

METRIC_POLICIES: dict[str, MetricPolicy] = {
    metric: MetricPolicy(window_days=DEFAULT_WINDOW_DAYS, threshold=kind)
    for metric, kind in _THRESHOLD_KINDS.items()
}


def policy_for(metric: str) -> MetricPolicy:
    """取某指标的口径；不支持的指标抛 ``ValueError``。"""
    if metric not in METRIC_POLICIES:
        raise ValueError(f"不支持的基线指标：{metric}")
    return METRIC_POLICIES[metric]


def compute(
    metric: str,
    values: list[float],
    *,
    window_days: int | None = None,
    version: int = BASELINE_VERSION,
    now: dt.datetime | None = None,
) -> Baseline | None:
    """由一组数值计算基线；无有效样本时返回 ``None``。"""
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    wd = policy_for(metric).window_days if window_days is None else window_days
    arr = np.asarray(clean, dtype=float)
    return Baseline(
        metric=metric,
        version=version,
        window_days=wd,
        mean=float(np.mean(arr)),
        std=float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0,
        p10=float(np.percentile(arr, 10)),
        p90=float(np.percentile(arr, 90)),
        mature=arr.size >= MIN_SAMPLES,
        computed_at=now or dt.datetime.now(),
    )


def window_values(
    dailies: list[StandardDaily],
    metric: str,
    as_of: dt.date,
    window_days: int | None = None,
) -> list[float]:
    """取该指标在窗口内的有效值（升序）。

    - ``window_days`` 为 ``None`` → 按该指标的口径（E23）；
    - 口径为 ``FULL_HISTORY_WINDOW_DAYS``(0) → **全部历史**（截至 ``as_of``，无前视）；
    - 否则区间为 ``(as_of - window_days, as_of]``（左开右闭）。
    """
    attr = SUPPORTED_METRICS.get(metric)
    if attr is None:
        raise ValueError(f"不支持的基线指标：{metric}")
    wd = policy_for(metric).window_days if window_days is None else window_days
    start = None if wd == FULL_HISTORY_WINDOW_DAYS else as_of - dt.timedelta(days=wd)
    out: list[float] = []
    for d in sorted(dailies, key=lambda x: x.date):
        if d.date > as_of:  # 无前视：绝不用 as_of 之后的数据
            break
        if start is not None and not (start < d.date):
            continue
        v = getattr(d, attr)
        if v is not None:
            out.append(float(v))
    return out


def from_dailies(
    dailies: list[StandardDaily],
    metric: str,
    as_of: dt.date,
    *,
    window_days: int | None = None,
    version: int = BASELINE_VERSION,
    now: dt.datetime | None = None,
) -> Baseline | None:
    """从标准化日数据计算某指标截至 ``as_of`` 的基线。"""
    values = window_values(dailies, metric, as_of, window_days)
    return compute(metric, values, window_days=window_days, version=version, now=now)


def compute_all(
    dailies: list[StandardDaily],
    as_of: dt.date,
    *,
    window_days: int | None = None,
    version: int = BASELINE_VERSION,
    now: dt.datetime | None = None,
) -> dict[str, Baseline]:
    """计算全部受支持指标的基线，返回 {metric: Baseline}（无样本的指标被跳过）。

    ``window_days`` 为 ``None`` 时**各指标按自身口径**（E23）；显式传入则统一强制该窗口
    （仅供测试/回溯使用）。
    """
    out: dict[str, Baseline] = {}
    for metric in SUPPORTED_METRICS:
        b = from_dailies(
            dailies,
            metric,
            as_of,
            window_days=window_days,
            version=version,
            now=now,
        )
        if b is not None:
            out[metric] = b
    return out


# ---------------------------- 判定阈值派生（E23） ----------------------------
# 单一来源：规则消费的阈值一律由本模块按口径派生，禁止在调用方各自计算。
def threshold_low(metric: str, b: Baseline) -> float | None:
    """该指标的下界判定阈值（低于即异常）；无下界 → ``None``。"""
    kind = policy_for(metric).threshold
    if kind == "p10":
        # 已无指标使用（Q5 弃用 p10）；保留分支仅为兼容历史基线行
        return b.p10
    if kind == "meanMinusSigma":
        return b.mean - BASELINE_SIGMA_MULTIPLIER * b.std
    return None


def threshold_high(metric: str, b: Baseline) -> float | None:
    """该指标的上界判定阈值（高于即异常）；无上界 → ``None``。"""
    kind = policy_for(metric).threshold
    if kind == "p90":
        return b.p90
    if kind == "meanPlusSigma":
        return b.mean + BASELINE_SIGMA_MULTIPLIER * b.std
    return None


def with_thresholds(metric: str, b: Baseline) -> Baseline:
    """回填阈值字段（供 API / 前端展示**真实判定阈值**）。"""
    return b.model_copy(
        update={
            "threshold_low": threshold_low(metric, b),
            "threshold_high": threshold_high(metric, b),
        }
    )
