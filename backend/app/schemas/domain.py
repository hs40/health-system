"""领域模型（Pydantic）：Plan §3.1 的落地，即 API 契约的取值结构。

命名约定：领域字段用英文小写下划线；Garmin 原始字段保持原名以利追溯。
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.enums import (
    AuditCategory,
    ConflictResolution,
    ConflictSeverity,
    DataState,
    FaultSeverity,
    MessageCategory,
    MessageImportance,
    PriorityLevel,
    QualityGrade,
    QualityLevel,
    SessionStatus,
    Sport,
    StrategyChangeType,
    StrategyStatus,
    TaskStatus,
)


# ============================================================
# 数据层
# ============================================================
class RawDailyRef(BaseModel):
    """原始数据只读引用（不复制内容，保证可回溯）。"""

    source: str  # 'garmin' | 'tredict'
    dataset: str  # 'daily_summary' / 'sleep' / 'hrv' ...
    date: date
    path: str
    fetched_at: datetime


class StandardDaily(BaseModel):
    """标准化层：统一单位 / 时区 / 口径（REQ-DATA-003/004）。"""

    date: date
    resting_heart_rate: int | None = None
    min_heart_rate: int | None = None
    max_heart_rate: int | None = None
    sleep_duration_sec: int | None = None
    sleep_start_ts: datetime | None = None  # 跨日睡眠用原始时间戳
    sleep_end_ts: datetime | None = None
    hrv_last_night_avg: float | None = None  # rmssd
    # —— Garmin 自带 HRV 基线区间（E24，2026-10-16 用户决策）——
    # 设备按用户个人历史给出基线带，取代自算 28 天 p10 分位：
    #   lowUpper       低区上沿 → 低于即「恢复不足」（R-001 判定线）
    #   balancedLow    平衡区下限
    #   balancedUpper  平衡区上限 → 高于即「有余力」（加量建议，非风险）
    #   weeklyAvg      7 天均值（Garmin status 的输入）
    #   status         BALANCED / UNBALANCED / LOW / HIGH / NONE
    hrv_low_upper: float | None = None
    hrv_balanced_low: float | None = None
    hrv_balanced_high: float | None = None
    hrv_weekly_avg: float | None = None
    hrv_status: str | None = None
    body_battery_wake: int | None = None
    # —— 风险引擎专用口径（2026-10-22 Q11）——
    # sleep_score：Garmin 睡眠得分（dailySleepDTO.sleepScores.overall.value，0–100）
    # body_battery_morning：**醒时**身体电量（dailySummary.bodyBatteryAtWakeTime）
    #   与 body_battery_wake 是不同口径：后者是【当日最后一次同步读到的值】
    #   （傍晚任务运行时已接近耗尽，实测近 30 天中位数仅 5），不得混用。
    sleep_score: int | None = None
    body_battery_morning: int | None = None
    weight_kg: float | None = None  # 0.1kg 精度
    steps: int | None = None
    active_calories: int | None = None
    intensity_moderate_min: int | None = None
    intensity_vigorous_min: int | None = None
    spo2_avg: float | None = None
    respiration_avg: float | None = None
    source_refs: list[RawDailyRef] = Field(default_factory=list)


class DataConflict(BaseModel):
    metric: str
    values: list[dict] = Field(default_factory=list)  # [{source, value}]
    severity: ConflictSeverity
    resolution: ConflictResolution


class DataQualityReport(BaseModel):
    date: date
    level: QualityLevel
    missing: list[str] = Field(default_factory=list)
    conflicts: list[DataConflict] = Field(default_factory=list)
    grade: QualityGrade
    notes: list[str] = Field(default_factory=list)


# ============================================================
# 计算层
# ============================================================
class Baseline(BaseModel):
    metric: str
    version: int
    window_days: int  # 0 = 全历史（截至当日）；>0 = 滚动窗口天数（E23）
    mean: float
    std: float
    p10: float
    p90: float
    mature: bool  # 未成熟 → 保守判断
    computed_at: datetime
    # 判定阈值（由 domain/baseline 按口径派生；供展示"真实判定线"用，不落库）
    threshold_low: float | None = None   # 低于即异常（HRV p10 / 睡眠 mean−1σ）
    threshold_high: float | None = None  # 高于即异常（静息心率 mean+1σ）


class TrainingLoad(BaseModel):
    date: date
    by_sport: dict[str, float] = Field(default_factory=dict)  # 分运动日累计负荷（0–100 分值之和）
    combined: float = 0.0  # 每日统一总负荷（各类型日累计之和，可超 100）
    method: str = ""
    # 不可计算的活动（必需字段缺失），按需求 4.5 保留为不可计算，不填 0
    not_computable: list[dict] = Field(default_factory=list)
    # 计入负荷但跨运动类型可比性未验证的类型 → 状态（需求 6.3）
    unverified_sports: dict[str, str] = Field(default_factory=dict)


class AcwrSnapshot(BaseModel):
    date: date
    acute: float          # 急性 EWMA（τ=7）
    chronic: float        # 慢性 EWMA（τ=28）
    ratio: float | None = None   # 慢性为 0 时不可计算
    sufficient: bool = True
    note: str = ""


class RiskAssessment(BaseModel):
    """风险评估结果（2026-10-22 重设计：四级优先级 + 三态数据关卡）。

    取代旧三级 ``level`` 体系。``priority`` 与 ``data_state`` 是**直接契约**
    （Q8），前端、日报、周报、月报一律以此为准。
    """

    date: date
    priority: PriorityLevel
    data_state: DataState
    triggered_rules: list[str] = Field(default_factory=list)  # 规则ID（可复现）
    # 人话原因（「哪条规则 + 实测值 vs 阈值」），供前端直接展示（L-1 附带需求）。
    # 旧 payload 无该字段 → 空列表，前端回退到通用文案。
    reasons: list[str] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)
    # 缺失的输入项（Q22：日报需展示「HRV 状态缺失」这类清单）
    missing_inputs: list[str] = Field(default_factory=list)
    # —— P2 输出动作（Q19/Q20）——
    volume_halved: bool = False       # 容量减半
    intensity_reduced: bool = False   # 强度减量
    floored_at_recovery_template: bool = False  # 叠加后触及恢复日下限
    # —— P3 加量幅度（Q4/Q18）——
    progress_duration_pct: list[int] = Field(default_factory=list)
    progress_strength_pct: list[float] = Field(default_factory=list)
    progress_strength_reps: list[int] = Field(default_factory=list)
    rule_version: str
    decided_at: datetime


# ============================================================
# 应用层
# ============================================================
class EnvironmentDaily(BaseModel):
    """环境（天气）日数据（阶段② T2.2；REQ §13.7/§13.8）。

    ``available=False`` 表示数据缺失 —— 下游**不得**据此放宽安全阈值、不得编造。
    """

    date: date
    location_id: str = ""
    location_name: str = ""
    lat: str | None = None
    lon: str | None = None
    available: bool = True
    temp_c: float | None = None
    humidity: float | None = None
    precip_mm: float | None = None
    weather_text: str = ""
    is_rain: bool = False
    is_snow: bool = False
    wind_scale: str | None = None
    source: str = "qweather"
    fetched_at: datetime | None = None


# ============================================================
# 策略版本（阶段③ T3.1/T3.2；Plan §4.1.1/4.1.5）
# ============================================================
class SeasonPolicyContent(BaseModel):
    """季节策略内容（``strategy_version.payload``，Plan §4.5）。"""

    season: str  # spring|summer|autumn|winter|transitional
    focus: str = ""
    endurance_ratio: float | None = None  # None = 动态判断（不预设固定比例）
    strength_ratio: float | None = None
    recovery_ratio: float | None = None
    weather_rules: dict = Field(default_factory=dict)  # 硬性环境约束参数
    long_term_priority: list[str] = Field(
        default_factory=lambda: ["cardio", "fatLoss", "muscleEndurance", "maxStrength"]
    )
    limitations: list[str] = Field(default_factory=list)


class StrategyVersion(BaseModel):
    """策略版本（API 契约）。"""

    strategy_version_id: str
    strategy_type: str = "season"
    version_number: str
    status: StrategyStatus
    change_type: StrategyChangeType
    parent_version_id: str | None = None
    created_at: datetime
    effective_at: datetime | None = None
    created_by: str = ""
    change_reason: str = ""
    evidence_summary: str = ""
    expected_effect: str = ""
    actual_effect: str | None = None
    payload: dict = Field(default_factory=dict)
    applicability_conditions: dict = Field(default_factory=dict)
    safety_constraints: dict = Field(default_factory=dict)
    validation_status: str = "pending"
    validation_result: dict = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    immutable: bool = True


class StrategyEvent(BaseModel):
    event_id: str
    event_type: str
    triggered_at: datetime
    current_version_id: str | None = None
    target_version_id: str | None = None
    task_id: str | None = None
    trigger_reason: str = ""
    steps: list[dict] = Field(default_factory=list)
    validation_result: dict = Field(default_factory=dict)
    failure_reason: str | None = None
    risk_state: str = ""
    notify_status: str = "pending"
    related_report_ref: str | None = None
    follow_up_status: str = "open"


class StrategyDiff(BaseModel):
    """三层版本差异（§9.6）。"""

    summary: dict = Field(default_factory=dict)  # 第一层：版本摘要
    key_changes: dict = Field(default_factory=dict)  # 第二层：关键差异
    full_diff: dict = Field(default_factory=dict)  # 第三层：完整差异（含未变化项）


# ============================================================
# 故障恢复 + 消息（阶段③ T3.9/T3.10）
# ============================================================
class FaultRecoveryTask(BaseModel):
    fault_id: str
    severity: FaultSeverity
    impact_scope: str = ""
    started_at: datetime
    fix_measures: str = ""
    fixed_at: datetime | None = None
    precheck_status: str = "pending"
    precheck_report: dict = Field(default_factory=dict)
    residual_risk: list[str] = Field(default_factory=list)
    needs_user_confirm: bool = False
    user_confirmed: bool = False
    user_confirmed_at: datetime | None = None
    recovery_executed_at: datetime | None = None
    final_result: str = ""


class Message(BaseModel):
    message_id: str
    at: datetime
    category: MessageCategory
    importance: MessageImportance = MessageImportance.NORMAL
    title: str
    body: str = ""
    ref_type: str = ""
    ref_id: str = ""
    link: str = ""
    is_read: bool = False
    is_resolved: bool = False
    send_status: str = "sent"


class StrengthSet(BaseModel):
    exercise: str
    sets: int
    reps: int
    load_kg: float | None = None
    rest_sec: int


class TrainingSession(BaseModel):
    """完整训练处方（REQ-PLAN-002）。"""

    id: str
    date: date
    sport: Sport
    name: str
    warmup: list[dict] = Field(default_factory=list)
    main: list[dict] = Field(default_factory=list)
    cooldown: list[dict] = Field(default_factory=list)
    target_type: str | None = None  # pace | heartRate | open
    # 目标心率区间（Z1–Z5，Karvonen 口径；2026-10-20 决策：周计划携带强度数据，
    # 完成度判定的强度基准。历史计划无此字段 → 判定降级为仅时长，并披露）。
    # 可空：未登记区间不阻塞计划生成/展示。
    target_hr_zone: str | None = None
    duration_min: int | None = None
    distance_m: int | None = None
    strength: list[StrengthSet] | None = None
    status: SessionStatus = SessionStatus.DRAFT
    garmin_workout_id: int | None = None
    garmin_scheduled_id: int | None = None
    # 处方依据（E25：恢复日等由固定模板生成时需要说明来源）
    rationale: str | None = None
    # 是否允许上传 Garmin（E25：恢复日计划仅本地可见，禁止上传）
    upload_allowed: bool = True


class WeekPlanDay(BaseModel):
    """本周计划中的单日条目（D17）。"""

    date: date
    weekday: int  # 0=周一 ... 6=周日
    session: TrainingSession | None = None
    is_night_shift: bool = False  # 夜班标记（D18）
    note: str | None = None


class DailyReport(BaseModel):
    """日报（REQ-REPORT-001 + D17 本周整体安排）。"""

    date: date
    yesterday_training: dict | None = None
    # 前一日训练完成度（§12.4 判定；2026-10-20 决策 Q7：判定只在日报/周报发生，
    # 周计划卡片只展示事实）。值 = DayJudgment.to_dict() + reason；
    # 周报从一周 7 份日报提取聚合，免重算。未判定日（无计划无记录等）为 None。
    yesterday_completion: dict | None = None
    load: TrainingLoad | None = None
    acwr: AcwrSnapshot | None = None
    # 需求 5.4：当日生效的心率参数（max/rest HR、Karvonen 方法、五区整数边界）。
    # 与负荷计算用的是同一组常量（single source of truth），不是另一套展示值。
    hr_params: dict | None = None
    sleep: dict | None = None
    recovery: dict | None = None
    capability: dict | None = None
    over_under_analysis: str | None = None
    today_plan_id: str | None = None  # 高风险时为 None
    adjustment_reason: str | None = None
    week_start: date | None = None  # 本周周一（D17）
    week_plan: list[WeekPlanDay] | None = None  # 本周整体计划安排（D17）
    # 四项长期目标进展（需求 §11.2「中部可展示关键图表」）：近 7 天窗口，
    # 基线取前一个 7 天区间；无数据时对应 goal 标 insufficient，不编造。
    # 旧版本报告无该字段 → None，前端显式说明而非留空。
    goal_progress: dict | None = None
    quality: DataQualityReport
    # 风险判定结果的**快照**；权威数据在 ``risk_assessment`` 表（每天一条，含 P4）。
    # 2026-10-22 起历史 payload 不再携带该字段（Q29/Q30），故为可选；
    # 新生成的日报仍会写入，便于单份报告自洽展示。
    risk: RiskAssessment | None = None
    limitations: list[str] = Field(default_factory=list)
    # 面向用户的建议：P1 时给出恢复日提示，P3 时给出加量幅度（Q6 / Q22）
    advisory: list[str] = Field(default_factory=list)
    strategy: dict | None = None  # 报告与策略版本关联（阶段④ T4.1，§11.1）
    # G-16（2026-10-24）：本报告使用的口径版本（acwr/quality/aggregation）。
    # 旧 payload 无该键 → 空 dict，读作 legacy，不回溯补齐。
    algo_versions: dict[str, str] = Field(default_factory=dict)
    version: int = 1


# ============================================================
# 运维层
# ============================================================
class TaskError(BaseModel):
    code: str
    message: str


class TaskRun(BaseModel):
    id: str
    task_type: str
    idempotency_key: str
    status: TaskStatus
    started_at: datetime
    finished_at: datetime | None = None
    attempts: int = 0
    checkpoint: str | None = None
    error: TaskError | None = None


class AuditEvent(BaseModel):
    id: str
    at: datetime
    category: AuditCategory
    ref: str
    detail: dict = Field(default_factory=dict)
