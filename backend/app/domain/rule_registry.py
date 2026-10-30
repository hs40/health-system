"""安全规则登记表：**四级优先级规则族 P1–P4**（2026-10-22 风险引擎重设计）。

本模块把风险规则从「散落在 ``priority.py`` 的硬编码逻辑」变为**结构化、可查、可校准**
的数据描述。所有判定阈值**只**在此定义，``priority.py`` 只负责执行。

与旧版（R-001 ~ R-008）的关系：**全量替换**（Q1/Q12）。旧三级体系
（normal / needsAdjustment / high）已退役，编号不复用、不保留兼容层。
历史判定痕迹由 ``daily_report.payload`` 承载，规则版本号随评估一并落库。

设计约束（对齐 REQ-RISK-002 / REQ-RISK-007）：
- 每条规则的**依据等级**必须显式标注，不得冒充医学标准（T1.4）；
- 标注 ``inferred`` 的阈值均为**候选值**，需随数据积累校准；
- P4 为兜底，无条件命中，保证每天必有结论（Q15）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.schemas.enums import DataState


class Basis(StrEnum):
    """阈值/规则的依据等级（T1.4，不得冒充医学标准）。"""

    MEDICAL = "medical"          # 医学共识/指南
    SPORT_SCIENCE = "sport_science"  # 运动科学文献/通用实践
    DEVICE = "device"            # 设备厂商定义
    BUSINESS = "business"        # 业务约束
    INFERRED = "inferred"        # 工程推测/候选值（必须校准）


class AIAuthority(StrEnum):
    """AI/模型对规则的介入权限（REQ-RISK-002：程序逻辑不得被模型覆盖）。"""

    NONE = "none"            # 模型完全不得覆盖（硬性程序逻辑）
    ANNOTATE = "annotate"    # 模型可补充说明，但不得改变判定


@dataclass(slots=True, frozen=True)
class RuleSpec:
    """一条优先级规则的结构化定义。"""

    rule_id: str                  # P1-* / P2-* / P3-* / P4
    priority: str                 # P1 / P2 / P3 / P4
    title: str
    basis: Basis
    basis_note: str
    indicators: tuple[str, ...]
    trigger: str
    exceptions: str
    action: str
    ai_authority: AIAuthority
    version: str
    thresholds: dict = field(default_factory=dict)
    # —— 输出侧 ——
    hard_block_generate: bool = False  # 是否拦截「计划生成」
    hard_block_upload: bool = False    # 是否拦截「计划上传」
    adjust_intensity: bool = False     # 强度减量
    adjust_volume: bool = False        # 容量减半
    # —— 执行侧（2026-10-25 ③：注册表驱动执行）——
    # condition 与 trigger 文字一一对应；reason 产出人话原因（含实测值）；
    # params 承载规则专属参数（如 P3 加量幅度）。P4 无条件——兜底由判定器负责。
    condition: Callable[[Any, DataState, dict], bool] | None = None
    reason: Callable[[Any, dict], str] | None = None
    params: dict = field(default_factory=dict)


# ============================================================
# 规则版本
# ============================================================
RULE_VERSION = "0.3.0"  # 0.3.0（2026-10-25 L-7）：C 态放行恢复类规则
"""四级优先级规则族版本（2026-10-22 重设计，取代 candidate-0.1.0）。"""


# ============================================================
# 阈值集中定义（唯一事实来源）
# ============================================================

# —— 负荷（ACWR）——
# 依据：运动科学通用实践（急性/慢性负荷比 0.8–1.3 为适宜区间）。
# 注意：阈值为**用户给定**，与原「本地自算 load」链路同口径；换源须重新校准（Q16）。
ACWR_HIGH = 1.5          # > 此值 → 「负荷过高」（P1 判据 / P2 上限）
ACWR_ELEVATED = 1.3      # ≥ 此值 → 「负荷偏高」（P2 容量减半）
ACWR_PROGRESS_LOW = 0.8  # P3 加量的负荷下限
ACWR_PROGRESS_HIGH = 1.1  # P3 加量的负荷上限（超过则非「有余力」）
ACWR_STALE_WINDOW_DAYS = 3  # ACWR 缺失时允许沿用的最大回溯天数（Q3）

# —— HRV ——
HRV_BALANCED_STATUS = "BALANCED"  # Garmin 原生「平衡」状态（Q17）
HRV_LOW_DELTA_MS = 5.0            # 低于基线多少毫秒算「低」（P2 判据）
HRV_BALANCED_DAYS_FOR_PROGRESS = 3  # P3 要求连续平衡天数（含当天，Q17）

# —— 睡眠 ——
SLEEP_SCORE_CRITICAL = 50   # < 此值 → P1 判据之一
SLEEP_SCORE_LOW = 60        # < 此值 → P2 判据
SLEEP_SCORE_GOOD = 75       # ≥ 此值 → P3 判据

# —— 身体电量（醒时值）——
BATTERY_CRITICAL = 40       # < 此值 → P1 判据之一
BATTERY_GOOD = 75           # ≥ 此值 → P3 判据

# —— 静息心率 ——
RHR_ELEVATED_BPM = 3        # 高于 28 天均值多少 bpm 算「升高」（P2 判据）
RHR_FLAT_TOLERANCE_BPM = 1  # 「持平」容差：≤ 基线 + 1bpm（P3 判据，Q25）

# —— 基线窗口（Q5：全部改为近 28 天）——
BASELINE_WINDOW_DAYS = 28
BASELINE_MIN_SAMPLES = 14   # 成熟所需最少样本（沿用旧口径）

# 均值 ± kσ 的 k（Q5 后三个指标统一口径，HRV 也不再走 p10）。
# 1.0 为**候选值**（约对应正态下 16% 尾部），需按 T1.4 校准流程验证。
BASELINE_SIGMA_MULTIPLIER = 1.0

# —— P2 叠加下限（Q20）：叠加后不低于恢复日模板 ——
RECOVERY_TEMPLATE_MIN_SEC = 15 * 60   # 恢复日模板下限（15min Z1）
RECOVERY_TEMPLATE_MAX_SEC = 20 * 60   # 恢复日模板上限（20min Z1）

# —— P1 恢复日模板（Q6）——
RECOVERY_TEMPLATE_ZONE = "Z1"
RECOVERY_TEMPLATE_NOTE = "强制休息：15–20min Z1 主动恢复 + 拉伸，不进行任何强度训练。"

# —— P3 加量幅度（Q4/Q18）——
PROGRESS_DURATION_PCT = (5, 10)      # 时长/距离 +5~10%
PROGRESS_STRENGTH_PCT = (2.5, 5.0)   # 力量 +2.5~5%
PROGRESS_STRENGTH_REPS = (1, 2)      # 或 +1~2 reps

# —— 周计划处方步骤模板（REQ-PLAN-002：卡片展开需展示完整处方）——
# LLM 只返回 sport/name/duration/target_hr_zone 时，按下列比例合成
# 热身 / 主项 / 放松；LLM 已返回的块**原样保留**，只补缺的块。
PRESCRIPTION_WARMUP_RATIO = 0.10   # 热身占总时长比例
PRESCRIPTION_COOLDOWN_RATIO = 0.08  # 放松占总时长比例
PRESCRIPTION_WARMUP_MAX_SEC = 600   # 热身上限 10min
PRESCRIPTION_COOLDOWN_MAX_SEC = 480  # 放松上限 8min
PRESCRIPTION_WARMUP_MIN_SEC = 120    # 热身下限 2min
PRESCRIPTION_COOLDOWN_MIN_SEC = 120  # 放松下限 2min
# 各运动类型的兜底处方文案（强度目标由 target_hr_zone 决定，不在此硬编码区间）
PRESCRIPTION_WARMUP_NAMES = {
    "running": "慢跑与关节活动",
    "indoorCardio": "低阻力骑行/椭圆机热身",
    "strength": "关节活动与轻重量激活",
}
PRESCRIPTION_MAIN_NAMES = {
    "running": "主项有氧跑",
    "indoorCardio": "主项有氧训练",
    "strength": "主项力量训练",
}
PRESCRIPTION_COOLDOWN_NAME = "拉伸与呼吸放松"
# 力量日兜底组数：组×次固定，负荷留空（由用户在设备上按能力选择，系统不编造重量）
PRESCRIPTION_STRENGTH_SETS = 3
PRESCRIPTION_STRENGTH_REPS = 12
PRESCRIPTION_STRENGTH_REST_SEC = 60
PRESCRIPTION_STRENGTH_EXERCISES = (
    "深蹲",
    "硬拉",
    "卧推",
    "划船",
    "肩推",
    "引体向上/高位下拉",
)
# 处方依据文案：让用户知道步骤是规则合成还是 LLM 生成
PRESCRIPTION_FALLBACK_RATIONALE = (
    "处方步骤由系统按运动类型、时长与目标心率区间自动合成（热身/主项/放松），"
    "主项目标区间来自本周计划登记的强度；可在设备上按当日体感微调。"
)
PRESCRIPTION_LLM_RATIONALE = "处方步骤来自 AI 生成的本周计划。"

# ============================================================
# 指标判据（规则层词汇：priority 与注册表共用，避免两套「平衡」定义）
# ============================================================
_HRV_UNAVAILABLE = {"NONE", "none", ""}


def hrv_available(status: str | None) -> bool:
    """HRV 是否可判定（Garmin 用 ``NONE`` 表示无法判定，等同缺失）。"""
    return status is not None and status not in _HRV_UNAVAILABLE


def hrv_unbalanced(status: str | None) -> bool:
    """HRV 是否「非平衡」。缺失已在三态关卡处理，这里只判有值情形。"""
    return hrv_available(status) and status != HRV_BALANCED_STATUS


# ============================================================
# 触发条件（condition）与人话原因（reason）
# 每个函数与同名 RuleSpec 的 trigger / action 文字一一对应；
# 改条件必须同步改文字，parity 测试会抓住不一致。
# ============================================================
def _p1_load_hrv(inp: Any, state: DataState, th: dict) -> bool:
    return (
        inp.acwr_ratio is not None
        and inp.acwr_ratio > th["acwr_high"]
        and hrv_unbalanced(inp.hrv_status)
    )


def _p1_load_hrv_reason(inp: Any, th: dict) -> str:
    return (
        f"负荷过高（ACWR {inp.acwr_ratio:.2f} > {th['acwr_high']}）"
        f"且 HRV 未处于平衡态（{inp.hrv_status}）"
    )


def _p1_sleep_battery(inp: Any, state: DataState, th: dict) -> bool:
    return (
        inp.sleep_score is not None
        and inp.body_battery_morning is not None
        and inp.sleep_score < th["sleep_critical"]
        and inp.body_battery_morning < th["battery_critical"]
    )


def _p1_sleep_battery_reason(inp: Any, th: dict) -> str:
    return (
        f"睡眠得分极低（{inp.sleep_score} < {th['sleep_critical']}）"
        f"且晨起电量极低（{inp.body_battery_morning} < {th['battery_critical']}）"
    )


def _p2_load(inp: Any, state: DataState, th: dict) -> bool:
    return inp.acwr_ratio is not None and inp.acwr_ratio >= th["acwr_elevated"]


def _p2_load_reason(inp: Any, th: dict) -> str:
    return f"负荷偏高（ACWR {inp.acwr_ratio:.2f} ≥ {th['acwr_elevated']}）：容量减半"


def _p2_hrv(inp: Any, state: DataState, th: dict) -> bool:
    return (
        inp.hrv_last_night_avg is not None
        and inp.hrv_baseline is not None
        and inp.hrv_last_night_avg < inp.hrv_baseline - th["delta_ms"]
    )


def _p2_hrv_reason(inp: Any, th: dict) -> str:
    return (
        f"HRV 低于基线（{inp.hrv_last_night_avg:.1f} < "
        f"{inp.hrv_baseline:.1f} - {th['delta_ms']}）：强度减量"
    )


def _p2_sleep(inp: Any, state: DataState, th: dict) -> bool:
    return inp.sleep_score is not None and inp.sleep_score < th["sleep_low"]


def _p2_sleep_reason(inp: Any, th: dict) -> str:
    return f"睡眠得分偏低（{inp.sleep_score} < {th['sleep_low']}）：强度减量"


def _p2_rhr(inp: Any, state: DataState, th: dict) -> bool:
    return (
        inp.rhr_today is not None
        and inp.rhr_baseline is not None
        and inp.rhr_today >= inp.rhr_baseline + th["elevated_bpm"]
    )


def _p2_rhr_reason(inp: Any, th: dict) -> str:
    return (
        f"静息心率升高（{inp.rhr_today} ≥ {inp.rhr_baseline:.1f}"
        f" + {th['elevated_bpm']}）：强度减量"
    )


def _p3_progress(inp: Any, state: DataState, th: dict) -> bool:
    return (
        state is DataState.FULL
        and inp.acwr_ratio is not None
        and th["acwr_low"] <= inp.acwr_ratio <= th["acwr_high"]
        and inp.hrv_balanced_streak >= th["balanced_days"]
        and inp.sleep_score is not None
        and inp.sleep_score >= th["sleep_good"]
        and inp.body_battery_morning is not None
        and inp.body_battery_morning >= th["battery_good"]
        and inp.rhr_today is not None
        and inp.rhr_baseline is not None
        and inp.rhr_today <= inp.rhr_baseline + th["rhr_tolerance_bpm"]
    )


def _p3_progress_reason(inp: Any, th: dict) -> str:
    return (
        f"负荷适宜（ACWR {inp.acwr_ratio:.2f}，{th['acwr_low']}~{th['acwr_high']}）+ "
        f"HRV 连续 {inp.hrv_balanced_streak} 天平衡 + "
        f"睡眠 {inp.sleep_score} + 电量 {inp.body_battery_morning} + "
        f"RHR {inp.rhr_today} 不高于基线：可加量"
    )


RULES: dict[str, RuleSpec] = {
    # ========================================================
    # P1 强制休息
    # ========================================================
    "P1-LOAD-HRV": RuleSpec(
        rule_id="P1-LOAD-HRV",
        priority="P1",
        title="负荷过高 + HRV 不平衡",
        basis=Basis.SPORT_SCIENCE,
        basis_note="ACWR 超过 1.5 表示急性负荷相对慢性负荷显著偏高，叠加 HRV 非平衡态"
        "说明恢复不足，两者同向 → 强制休息。阈值为用户给定候选值。",
        indicators=("acwr_ratio", "hrv_status"),
        trigger=f"acwr_ratio > {ACWR_HIGH} 且 hrv_status != {HRV_BALANCED_STATUS}",
        exceptions="acwr_ratio 缺失 → 不判定（走 C 态）；沿用值（acwr_stale）仍可触发（Q28-i）",
        action="P1：不生成计划、不上传，输出 15–20min Z1 恢复日模板",
        ai_authority=AIAuthority.NONE,
        version=RULE_VERSION,
        thresholds={"acwr_high": ACWR_HIGH, "balanced_status": HRV_BALANCED_STATUS},
        hard_block_generate=True,
        hard_block_upload=True,
        condition=_p1_load_hrv,
        reason=_p1_load_hrv_reason,
    ),
    "P1-SLEEP-BATTERY": RuleSpec(
        rule_id="P1-SLEEP-BATTERY",
        priority="P1",
        title="睡眠极差 + 晨起电量极低",
        basis=Basis.SPORT_SCIENCE,
        basis_note="睡眠得分与晨起身体电量同时处于低位，表示夜间恢复严重不足；"
        "两项均为设备原生指标，口径稳定。阈值为用户给定候选值。",
        indicators=("sleep_score", "body_battery_morning"),
        trigger=(
            f"sleep_score < {SLEEP_SCORE_CRITICAL}"
            f" 且 body_battery_morning < {BATTERY_CRITICAL}"
        ),
        exceptions="任一项缺失 → 该路径不可判定（B 态按可用维度判定，Q23）",
        action="P1：不生成计划、不上传，输出 15–20min Z1 恢复日模板",
        ai_authority=AIAuthority.NONE,
        version=RULE_VERSION,
        thresholds={
            "sleep_critical": SLEEP_SCORE_CRITICAL,
            "battery_critical": BATTERY_CRITICAL,
        },
        hard_block_generate=True,
        hard_block_upload=True,
        condition=_p1_sleep_battery,
        reason=_p1_sleep_battery_reason,
    ),
    # ========================================================
    # P2 减量（按触发条件分流，Q19）
    # ========================================================
    "P2-LOAD": RuleSpec(
        rule_id="P2-LOAD",
        priority="P2",
        title="负荷偏高 → 容量减半",
        basis=Basis.SPORT_SCIENCE,
        basis_note="ACWR 进入 1.3–1.5 区间：总量偏高但未到强制休息程度，"
        "降低容量以抑制负荷继续累积（**不减强度**，保留训练刺激）。",
        indicators=("acwr_ratio",),
        trigger=f"{ACWR_ELEVATED} ≤ acwr_ratio ≤ {ACWR_HIGH}",
        exceptions="acwr_ratio > ACWR_HIGH 且 HRV 平衡 → 仍走本规则（P1 不成立，但负荷确实高）",
        action="P2：容量减半（时长/距离 × 0.5），可生成但不上传",
        ai_authority=AIAuthority.NONE,
        version=RULE_VERSION,
        thresholds={"acwr_elevated": ACWR_ELEVATED, "acwr_high": ACWR_HIGH},
        hard_block_upload=True,
        adjust_volume=True,
        condition=_p2_load,
        reason=_p2_load_reason,
    ),
    "P2-HRV": RuleSpec(
        rule_id="P2-HRV",
        priority="P2",
        title="HRV 显著低于基线 → 强度减量",
        basis=Basis.SPORT_SCIENCE,
        basis_note="HRV 低于近 28 天基线 5ms 以上，表示恢复能力下降；"
        "降低强度以减小生理压力（**不减容量**，维持训练频次）。",
        indicators=("hrv_last_night_avg", "hrv_baseline"),
        trigger=f"hrv_last_night_avg < hrv_baseline - {HRV_LOW_DELTA_MS}",
        exceptions="基线缺失或样本不足 → 不判定",
        action="P2：强度减量，可生成但不上传",
        ai_authority=AIAuthority.NONE,
        version=RULE_VERSION,
        thresholds={"delta_ms": HRV_LOW_DELTA_MS, "window_days": BASELINE_WINDOW_DAYS},
        hard_block_upload=True,
        adjust_intensity=True,
        condition=_p2_hrv,
        reason=_p2_hrv_reason,
    ),
    "P2-SLEEP": RuleSpec(
        rule_id="P2-SLEEP",
        priority="P2",
        title="睡眠得分偏低 → 强度减量",
        basis=Basis.DEVICE,
        basis_note="Garmin 睡眠得分为设备综合评分（时长/结构/静息心率/躁动），"
        "低于 60 分表示睡眠质量不足。阈值为用户给定候选值。",
        indicators=("sleep_score",),
        trigger=f"sleep_score < {SLEEP_SCORE_LOW}",
        exceptions="睡眠得分缺失 → B 态，本规则不可判定",
        action="P2：强度减量，可生成但不上传",
        ai_authority=AIAuthority.NONE,
        version=RULE_VERSION,
        thresholds={"sleep_low": SLEEP_SCORE_LOW},
        hard_block_upload=True,
        adjust_intensity=True,
        condition=_p2_sleep,
        reason=_p2_sleep_reason,
    ),
    "P2-RHR": RuleSpec(
        rule_id="P2-RHR",
        priority="P2",
        title="静息心率升高 → 强度减量",
        basis=Basis.SPORT_SCIENCE,
        basis_note="静息心率高于近 28 天均值 3bpm 以上，是恢复不足的经典信号"
        "（与 HRV 互为印证）。对比 28 天均值而非单日环比，避免日内噪声（Q25）。",
        indicators=("rhr_today", "rhr_baseline"),
        trigger=f"rhr_today ≥ rhr_baseline + {RHR_ELEVATED_BPM}",
        exceptions="RHR 或基线缺失 → 不判定",
        action="P2：强度减量，可生成但不上传",
        ai_authority=AIAuthority.NONE,
        version=RULE_VERSION,
        thresholds={
            "elevated_bpm": RHR_ELEVATED_BPM,
            "window_days": BASELINE_WINDOW_DAYS,
        },
        hard_block_upload=True,
        adjust_intensity=True,
        condition=_p2_rhr,
        reason=_p2_rhr_reason,
    ),
    # ========================================================
    # P3 进阶加量（五项全满足，Q18）
    # ========================================================
    "P3-PROGRESS": RuleSpec(
        rule_id="P3-PROGRESS",
        priority="P3",
        title="状态良好 → 进阶加量",
        basis=Basis.SPORT_SCIENCE,
        basis_note="五项同时满足才加量：负荷在适宜区间（0.8–1.1）、HRV 连续 3 天平衡、"
        "睡眠 ≥75、晨起电量 ≥75、RHR 不高于基线 +1bpm。"
        "条件苛刻是**刻意设计**，避免冒进加量（Q18）；各阈值为用户给定候选值，需校准。",
        indicators=(
            "acwr_ratio",
            "hrv_status",
            "sleep_score",
            "body_battery_morning",
            "rhr_today",
            "rhr_baseline",
        ),
        trigger=(
            f"{ACWR_PROGRESS_LOW} ≤ acwr_ratio ≤ {ACWR_PROGRESS_HIGH}"
            f" 且 连续 {HRV_BALANCED_DAYS_FOR_PROGRESS} 天 hrv_status == {HRV_BALANCED_STATUS}"
            f" 且 sleep_score ≥ {SLEEP_SCORE_GOOD}"
            f" 且 body_battery_morning ≥ {BATTERY_GOOD}"
            f" 且 rhr_today ≤ rhr_baseline + {RHR_FLAT_TOLERANCE_BPM}"
        ),
        exceptions="B 态（任一核心恢复维度缺失）→ 加量通道关闭（Q7）",
        action="P3：时长/距离 +5~10%，力量 +2.5~5% 或 +1~2 reps",
        ai_authority=AIAuthority.NONE,
        version=RULE_VERSION,
        thresholds={
            "acwr_low": ACWR_PROGRESS_LOW,
            "acwr_high": ACWR_PROGRESS_HIGH,
            "balanced_days": HRV_BALANCED_DAYS_FOR_PROGRESS,
            "sleep_good": SLEEP_SCORE_GOOD,
            "battery_good": BATTERY_GOOD,
            "rhr_tolerance_bpm": RHR_FLAT_TOLERANCE_BPM,
        },
        condition=_p3_progress,
        reason=_p3_progress_reason,
        params={
            "progress_duration_pct": PROGRESS_DURATION_PCT,
            "progress_strength_pct": PROGRESS_STRENGTH_PCT,
            "progress_strength_reps": PROGRESS_STRENGTH_REPS,
        },
    ),
    # ========================================================
    # P4 维持（兜底）
    # ========================================================
    "P4": RuleSpec(
        rule_id="P4",
        priority="P4",
        title="维持原计划",
        basis=Basis.BUSINESS,
        basis_note="兜底规则：无任何 P1/P2/P3 命中时维持原计划。"
        "**每天必写一条评估记录（含 P4）**，使「无记录」只代表任务未运行（Q15）。",
        indicators=(),
        trigger="以上规则均未命中",
        exceptions="—",
        action="P4：照原计划执行",
        ai_authority=AIAuthority.NONE,
        version=RULE_VERSION,
        thresholds={},
    ),
}


def ordered_specs() -> tuple[RuleSpec, ...]:
    """按**判定顺序**返回规则（P1 → P2 → P3 → P4，同优先级内保持书写顺序）。

    判定器只依赖这个顺序与每条规则的 condition / 输出标志，不再知道任何具体规则。
    """
    return tuple(RULES.values())


def active_rules() -> dict[str, RuleSpec]:
    """返回当前生效的规则集（四级优先级规则族全部生效）。"""
    return dict(RULES)


def get_rule(rule_id: str) -> RuleSpec:
    return RULES[rule_id]


def rules_for_priority(priority: str) -> list[RuleSpec]:
    """按优先级取规则（供文档与测试遍历）。"""
    return [r for r in RULES.values() if r.priority == priority]
