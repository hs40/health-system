"""训练负荷域 —— 心率参数与 Karvonen 五区（需求第五章）。

口径（需求 5.1–5.3，用户已确认）：

- 最大心率 **180 bpm**，**不因日常训练波动自动调整**，只有可靠测量或明确确认才改
- 静息心率 **52 bpm**，作为**固定参数**使用（见下）
- 区间计算方法 **Karvonen 心率储备法**：

      HRR    = HR_max - HR_rest
      HR_tgt = HR_rest + p × HRR

- 五区：Z1 50–60%、Z2 60–70%、Z3 70–80%、Z4 80–90%、Z5 90–100%
- 边界**四舍五入为整数 bpm**，且保证连续、不重叠

静息心率来源：**只使用 Garmin 睡眠数据中的静息心率**。某天没有即记缺失，
不填补、不视为 0。

静息心率参数化（2026-10-20 用户决策，取代需求报告 §5.3 的自动调整机制）
----------------------------------------------------------------------
需求报告 §5.3 原定「基线相对当前有效基线偏离 >1σ 且持续连续 30 天 → 以最近
28 天均值作新基线」。经核实该机制**无法成立**，故降档为固定参数：

1. ``rest_hr`` 在负荷计算与心率区间中**固定取 52**（``REST_HR_BASELINE``），
   不随静息心率数据自动调整；
2. 静息心率的**异常判定**不走本模块 —— 由风险规则 R-002 用
   「全历史（截至当日）均值 + 1σ」完成（决策 E23，见 ``domain/baseline.py``）；
3. 原实现（``rest_hr_baseline`` / ``should_adjust_baseline`` 等）已删除，
   原因留档：σ 由 28 天窗口自身算出，而「连续偏差 30 天」要求该窗口本身稳定，
   二者互斥 —— 稳定期 σ→0 使「偏离 >1σ」恒真，任何变化都会触发；
   若保留恒定值则 σ=0 直接短路，机制永不触发。两种极端下都不能得到有意义的
   触发点。

若要重新引入，须先给出不依赖窗口自身 σ 的比较基准（例如固定 bpm 下限或
MAD），并按 T1.4 用真实数据校准后再接线。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ------------------------------------------------------------
# 参数（需求 5.1）
# ------------------------------------------------------------
MAX_HR = 180
REST_HR_BASELINE = 52
HR_METHOD = "karvonen"

# 五区心率储备百分比（下界, 上界）
ZONE_BANDS: tuple[tuple[float, float], ...] = (
    (0.50, 0.60),
    (0.60, 0.70),
    (0.70, 0.80),
    (0.80, 0.90),
    (0.90, 1.00),
)
ZONE_NAMES: tuple[str, ...] = ("Z1", "Z2", "Z3", "Z4", "Z5")


@dataclass(frozen=True, slots=True)
class ZoneBand:
    name: str
    low_pct: float
    high_pct: float
    low_bpm: int
    high_bpm: int


@dataclass(frozen=True, slots=True)
class HrParams:
    """某日实际生效的心率参数（日报按需求 5.4 展示这组值）。"""

    max_hr: int = MAX_HR
    rest_hr: int = REST_HR_BASELINE
    method: str = HR_METHOD
    zones: tuple[ZoneBand, ...] = field(default_factory=tuple)

    @property
    def hrr(self) -> int:
        return self.max_hr - self.rest_hr

    def to_dict(self) -> dict:
        return {
            "max_hr": self.max_hr,
            "rest_hr": self.rest_hr,
            "method": self.method,
            "hrr": self.hrr,
            "zones": [
                {
                    "name": z.name,
                    "low_pct": z.low_pct,
                    "high_pct": z.high_pct,
                    "low_bpm": z.low_bpm,
                    "high_bpm": z.high_bpm,
                }
                for z in self.zones
            ],
        }


def _round_half_up(value: float) -> int:
    """四舍五入（Python 的 round 是银行家舍入，这里要的是数学四舍五入）。"""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def build_zones(max_hr: int = MAX_HR, rest_hr: int = REST_HR_BASELINE) -> tuple[ZoneBand, ...]:
    """按 Karvonen 生成五区，边界取整且**连续不重叠**。

    相邻区间共用同一个边界值：Z1 的上界 == Z2 的下界，避免出现空档或重叠。
    """
    hrr = max_hr - rest_hr
    # 6 个边界点：Z1 下界、Z1/Z2、Z2/Z3、Z3/Z4、Z4/Z5、Z5 上界
    pcts = [ZONE_BANDS[0][0]] + [b[1] for b in ZONE_BANDS]
    bounds = [_round_half_up(rest_hr + p * hrr) for p in pcts]
    out: list[ZoneBand] = []
    for i, (low_pct, high_pct) in enumerate(ZONE_BANDS):
        low, high = bounds[i], bounds[i + 1]
        if high < low:  # 极端参数下的保护：至少保持 1 bpm 宽度
            high = low
        out.append(
            ZoneBand(
                name=ZONE_NAMES[i],
                low_pct=low_pct,
                high_pct=high_pct,
                low_bpm=low,
                high_bpm=high,
            )
        )
    return tuple(out)


def params(max_hr: int = MAX_HR, rest_hr: int = REST_HR_BASELINE) -> HrParams:
    """当日生效的心率参数（含五区）。"""
    return HrParams(
        max_hr=max_hr, rest_hr=rest_hr, method=HR_METHOD, zones=build_zones(max_hr, rest_hr)
    )


def hrr_ratio(
    hr: float | None, max_hr: int = MAX_HR, rest_hr: int = REST_HR_BASELINE
) -> float | None:
    """心率储备比例 ``(HR - HR_rest) / HRR``，截断到 [0, 1]。

    心率缺失返回 ``None`` —— 调用方据此判定**不可计算**，不得当作 0。
    """
    if hr is None:
        return None
    span = max_hr - rest_hr
    if span <= 0:
        return None
    return max(0.0, min(1.0, (float(hr) - rest_hr) / span))


def zone_index(hr: float, zones: tuple[ZoneBand, ...]) -> int | None:
    """心率落在第几区（0 基）；低于 Z1 下界返回 ``None``。"""
    for i, z in enumerate(zones):
        if hr <= z.high_bpm:
            return i if hr >= z.low_bpm else None
    return len(zones) - 1 if hr > zones[-1].high_bpm else None
