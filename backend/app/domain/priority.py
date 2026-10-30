"""风险引擎核心：数据质量三态关卡 + 四级优先级判定（2026-10-22 重设计）。

纯函数、无副作用、无 IO。所有阈值来自 ``domain/rule_registry.py``，
本模块不得硬编码任何判定数值。

判断顺序（Q23）：
1. ``judge_data_state``  先确定「今天能判什么」（A 全景 / B 纯负荷 / C 兜底）；
2. ``judge_priority``    在可用维度内按 P1 → P2 → P3 → P4 取最高优先级。

**2026-10-25 ③（注册表驱动执行）**：本模块只保留「三态关卡」与「判定顺序」，
不再知道任何具体规则——条件、阈值、输出标志、人话原因全部来自
``rule_registry.RULES``（``ordered_specs()``）。改规则只需改注册表。

三态口径（Q7 / Q11b / Q24）：
- 核心 2 项 = HRV 状态 + 睡眠得分，任一缺失 → B；
- ``body_battery_morning`` 缺失 → B（P1 主路径依赖它）；
- ``hrv_status == "NONE"`` 算缺失（Garmin 用它表示无法判定）；
- 缺失 = 无记录 或 字段为空；**异常值按真实值参与判定**，不做主观过滤；
- ``acwr_ratio`` 缺失 → C（**负荷类**规则不成立，恢复类照判）；
  沿用值（``acwr_stale=True``）仍算「有值」，符合 Q27。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.domain import rule_registry as rr
from app.domain.rule_registry import hrv_available
from app.schemas.enums import DataState, PriorityLevel

# ============================================================
# 输入
# ============================================================


@dataclass(slots=True)
class DataStateInput:
    """判定所需的全部维度（扁平、无嵌套，便于穷举测试）。"""

    on_date: date
    # —— 负荷 ——
    acwr_ratio: float | None = None
    acwr_stale: bool = False
    acwr_source_date: date | None = None
    # —— HRV ——
    hrv_status: str | None = None  # Garmin: BALANCED / UNBALANCED / LOW / HIGH / NONE
    hrv_last_night_avg: float | None = None
    hrv_baseline: float | None = None  # 近 28 天 mean/median（Q5）
    # —— RHR ——
    rhr_today: int | None = None
    rhr_baseline: float | None = None  # 近 28 天均值（Q25）
    # —— 睡眠 ——
    sleep_score: int | None = None  # 0–100，Garmin overall
    # —— 身体电量 ——
    body_battery_morning: int | None = None  # 醒时值，0–100
    # —— HRV 连续平衡天数（含当天，Q17）——
    hrv_balanced_streak: int = 0


# ============================================================
# 三态关卡
# ============================================================

# 使判定降为 B 的缺失项（核心恢复维度）
_CORE_RECOVERY = ("hrv_status", "sleep_score", "body_battery_morning")

def judge_data_state(inp: DataStateInput) -> tuple[DataState, list[str]]:
    """返回 (数据状态, 缺失项清单)。

    缺失项清单供日报展示（Q22：「今日模式：纯负荷降级（HRV 状态缺失）」）。
    """
    missing: list[str] = []

    if inp.acwr_ratio is None:
        missing.append("acwr_ratio")

    if not hrv_available(inp.hrv_status):
        missing.append("hrv_status")

    if inp.sleep_score is None:
        missing.append("sleep_score")

    if inp.body_battery_morning is None:
        missing.append("body_battery_morning")

    if inp.rhr_today is None:
        missing.append("rhr")

    # C：负荷不可用 → 任何负荷判定都不成立，按原计划执行
    if inp.acwr_ratio is None:
        return DataState.FALLBACK, missing

    # B：任一核心恢复维度缺失 → 关加量通道，仅在可用维度内判定
    if any(m in missing for m in _CORE_RECOVERY):
        return DataState.LOAD_ONLY, missing

    return DataState.FULL, missing


# ============================================================
# 判定结果
# ============================================================

# 判定顺序：数字越小越先判（同优先级内保持注册表书写顺序）
_PRIORITY_RANK = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}


@dataclass(slots=True)
class PriorityResult:
    """四级优先级判定输出。"""

    priority: PriorityLevel
    triggered_rules: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # —— 输出动作 ——
    blocks_generate: bool = False   # P1：不生成计划
    blocks_upload: bool = False     # P1 / P2：不上传
    volume_halved: bool = False     # P2：容量减半
    intensity_reduced: bool = False  # P2：强度减量
    floored_at_recovery_template: bool = False  # P2 叠加后触及下限
    progress_duration_pct: tuple[int, int] | None = None
    progress_strength_pct: tuple[float, float] | None = None
    progress_strength_reps: tuple[int, int] | None = None
    # —— 数据说明 ——
    acwr_stale: bool = False
    acwr_source_date: date | None = None


def judge_priority(inp: DataStateInput, state: DataState) -> PriorityResult:
    """按注册表判定：收集命中规则 → 取最高优先级组 → 由该组规则导出动作。

    本函数**不知道任何具体规则**：条件、输出标志、人话原因、规则参数全部来自
    ``rule_registry.RULES``。新增或调整规则只需改注册表，判定器不变。

    语义（与 2026-10-25 之前逐条硬编码的实现完全一致，由特征测试锁定）：
    - 取「有规则命中的最高优先级」那一组，只记录该组的命中规则；
    - 拦截/减量动作由该组规则的输出标志**并集**导出；
    - 容量与强度同时命中 → 叠加执行且不低于恢复日模板下限；
    - 无人命中 → P4 兜底（P4 本身无条件，由「无人命中」表达）。
    """
    base = PriorityResult(
        priority=PriorityLevel.MAINTAIN,
        acwr_stale=inp.acwr_stale,
        acwr_source_date=inp.acwr_source_date,
    )

    fired = [
        spec
        for spec in rr.ordered_specs()
        if spec.condition is not None and spec.condition(inp, state, spec.thresholds)
    ]
    if not fired:
        return base

    top_rank = min(_PRIORITY_RANK[spec.priority] for spec in fired)
    group = [spec for spec in fired if _PRIORITY_RANK[spec.priority] == top_rank]
    head = group[0]

    volume = any(spec.adjust_volume for spec in group)
    intensity = any(spec.adjust_intensity for spec in group)
    result = PriorityResult(
        priority=PriorityLevel(head.priority),
        triggered_rules=[spec.rule_id for spec in group],
        reasons=[
            spec.reason(inp, spec.thresholds) for spec in group if spec.reason is not None
        ],
        blocks_generate=any(spec.hard_block_generate for spec in group),
        blocks_upload=any(spec.hard_block_upload for spec in group),
        volume_halved=volume,
        intensity_reduced=intensity,
        # Q20：两类同时命中 → 叠加执行，但下限不低于 P1 恢复日模板
        floored_at_recovery_template=volume and intensity,
        acwr_stale=inp.acwr_stale,
        acwr_source_date=inp.acwr_source_date,
    )

    # 规则专属参数（如 P3 加量幅度）由注册表承载
    for key in ("progress_duration_pct", "progress_strength_pct", "progress_strength_reps"):
        if key in head.params:
            setattr(result, key, head.params[key])
    return result
