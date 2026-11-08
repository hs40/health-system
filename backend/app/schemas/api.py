"""API 层契约：请求 / 响应模型（Plan §4）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.domain import (
    AuditEvent,
    Baseline,
    DailyReport,
    DataQualityReport,
    EnvironmentDaily,
    FaultRecoveryTask,
    Message,
    RawDailyRef,
    StrategyEvent,
    StrategyVersion,
    TaskRun,
    TrainingSession,
    WeekPlanDay,
)
from app.schemas.enums import ErrorCode, RiskLevel


# ============================================================
# 统一错误
# ============================================================
class ApiErrorDetail(BaseModel):
    code: ErrorCode
    message: str
    detail: dict = Field(default_factory=dict)


class ApiError(BaseModel):
    error: ApiErrorDetail


# ============================================================
# 报告
# ============================================================
class WeeklyReport(BaseModel):
    """周报（阶段 B）。"""

    week_start: date  # 周一
    review: dict = Field(default_factory=dict)
    plan_vs_actual: dict = Field(default_factory=dict)
    # 周计划执行块（§12.4；2026-10-20 决策 Q7）：六态计数 + 计划/实际量分列 +
    # 调整日 + 逐日明细与差异原因 + 未判定日披露。数据从一周 7 份日报的
    # yesterday_completion 提取聚合（日报判定一次，周报不重算）。
    execution: dict = Field(default_factory=dict)
    load: dict = Field(default_factory=dict)
    recovery: dict = Field(default_factory=dict)
    goal_progress: dict = Field(default_factory=dict)
    deviation_reason: str | None = None
    strategy: dict | None = None  # 生成时有效策略版本关联（T4.1，§11.3）
    strategy_changes: list[dict] = Field(default_factory=list)  # 本周策略变化（§11.3）
    # G-16：本报告使用的口径版本；旧 payload 无该键 → 空 dict（legacy）
    algo_versions: dict[str, str] = Field(default_factory=dict)


class MonthlyReport(BaseModel):
    """月报（阶段 C）。"""

    month: str  # YYYY-MM
    review: dict = Field(default_factory=dict)
    adherence: float | None = None
    goal_progress: dict = Field(default_factory=dict)
    long_term_adjustment: str | None = None
    strategy: dict | None = None  # 生成时有效策略版本关联（T4.1，§11.4）
    strategy_changes: list[dict] = Field(default_factory=list)  # 本月策略变化（§11.4）
    # G-16：本报告使用的口径版本；旧 payload 无该键 → 空 dict（legacy）
    algo_versions: dict[str, str] = Field(default_factory=dict)


class Plan12WeekItem(BaseModel):
    """12 周计划中的一周。

    字段与 ``plan12w_service`` 的产出逐一对齐；此前它在 ``Plan12Week.weeks`` 里
    是裸 ``dict``，前端拿到的全是 ``unknown``——契约没有描述，就只能靠猜。
    """

    week_index: int
    week_start: date
    week_end: date
    phase: str  # base | build | peak | deload
    focus: str
    season: str
    season_focus: str
    # 长期优先级排序（cardio / fatLoss / muscleEndurance / maxStrength）
    long_term_priority: list[str] = Field(default_factory=list)
    target_load_factor: float
    evaluation_due: bool = False


class Plan12Week(BaseModel):
    """滚动 12 周计划（阶段 C）。"""

    start_date: date
    weeks: list[Plan12WeekItem] = Field(default_factory=list)
    goal_weights: dict[str, float] = Field(default_factory=dict)
    strategy: dict | None = None  # 生成时有效策略版本关联（T4.1，§11.1）


# ============================================================
# 计划
# ============================================================
class PlanConflict(BaseModel):
    """结构化冲突（2026-10-22）：可直接挂到日期卡片上，并区分能否阻断。

    ``hard_block=True`` 表示硬性安全问题——**不得因用户确认而绕过**，
    确认接口会拒绝该计划（见 ``plan_service.confirm_weekly_plan``）。
    """

    days: list[str] = Field(default_factory=list)  # 涉及的日期（ISO）；空 = 周级问题
    kind: str = ""  # recovery_gap | night_shift | load_spike | sport_conflict | goal_mismatch
    severity: str = "info"  # info | warn | block
    hard_block: bool = False
    message: str = ""
    source: str = "rule"  # rule = 确定性规则 / ai = LLM 输出


class PlanEvaluation(BaseModel):
    week_start: date
    findings: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    # 兼容旧客户端：纯文本冲突视图（= structured_conflicts 的 message 列表）
    conflicts: list[str] = Field(default_factory=list)
    structured_conflicts: list[PlanConflict] = Field(default_factory=list)
    hard_block: bool = False
    risk_notes: list[str] = Field(default_factory=list)
    evaluated_at: datetime


class WeekPlanCard(BaseModel):
    """周计划卡片视图（§12.3）：原安排 / 调整后 / 原因 / 日报引用 / 实际。

    结构对应 ``domain.adjustment.card_view()``；此前是裸 ``dict``，
    前端只能拿到 ``{[k: string]: unknown}``。
    """

    date: date
    original: str | None = None
    adjusted: str | None = None
    is_adjusted: bool = False
    badge: str | None = None
    reason: str | None = None
    report_ref: str | None = None
    actual: str | None = None


class WeeklyPlan(BaseModel):
    week_start: date  # 周一
    days: list[WeekPlanDay] = Field(default_factory=list)
    status: str = "draft"  # draft | evaluated | confirmed
    version: int = 1
    strategy: dict | None = None  # 生成时有效策略版本关联（T4.1，§11.1）
    # 周计划卡片标识（§12.3；2026-10-20 决策 Q10）：**读取时现算、不落库**。
    # key = 日期 ISO；value = card_view() 输出（badge/is_adjusted/reason/
    # report_ref/actual）。旧 payload 无此字段 → 空 dict，由接口现算填充。
    cards: dict[str, WeekPlanCard] = Field(default_factory=dict)
    # 最近一次 AI 评估结果（含结构化冲突与硬阻断标记）。读取时带出，
    # 前端刷新后仍能看到"评估过/被阻断"的状态，不会误以为可确认。
    evaluation: PlanEvaluation | None = None


class WeeklyPlanGenerateRequest(BaseModel):
    """生成周计划的入参（D18：夜班日由用户手动告知）。"""

    night_shift_days: list[date] = Field(default_factory=list)


class UploadResult(BaseModel):
    session_id: str
    # uploaded | scheduled | unknown | failed | skipped | blocked（blocked=E25 恢复日禁止上传）
    status: str
    garmin_workout_id: int | None = None
    garmin_scheduled_id: int | None = None
    message: str | None = None


# ============================================================
# 数据
# ============================================================
class SeriesPoint(BaseModel):
    date: date
    value: float


class SeriesResponse(BaseModel):
    metric: str
    points: list[SeriesPoint] = Field(default_factory=list)


class HrZone(BaseModel):
    name: str
    low_pct: float
    high_pct: float
    low_bpm: int
    high_bpm: int


class HrParamsView(BaseModel):
    """当日生效的心率参数（Karvonen 五区），图表图例与日报共用同一份口径。"""

    max_hr: int
    rest_hr: int
    method: str
    hrr: int
    zones: list[HrZone] = Field(default_factory=list)


class NotComputableItem(BaseModel):
    """不可计算的活动（必需字段缺失）：保留了原因，不填 0、不静默丢弃（需求 4.5）。"""

    activity_id: str
    sport: str
    raw: float = 0.0
    score: float = 0.0
    display: int | None = None
    reference_load: float = 0.0
    computed: bool = False
    reason: str = ""
    verification_status: str = ""
    verification_note: str = ""


class LoadSeriesDay(BaseModel):
    """负荷序列的逐日点（需求 4.5 / 8.1）：堆叠柱 + ACWR 折线的最小单元。

    无训练日也保留（``total=0``），保证 EWMA 不断轴；
    ``acwr=None`` + ``acwr_state="not_available"`` 表示慢性负荷为 0，不伪造数值。
    """

    date: date
    total: float
    by_sport: dict[str, float] = Field(default_factory=dict)
    acwr: float | None = None
    acwr_state: str
    acwr_note: str | None = None
    not_computable: list[NotComputableItem] = Field(default_factory=list)
    unverified_sports: dict[str, str] = Field(default_factory=dict)


class LoadSeriesResponse(BaseModel):
    start: date
    end: date
    days: list[LoadSeriesDay] = Field(default_factory=list)
    hr_params: HrParamsView
    reference_loads: dict[str, float] = Field(default_factory=dict)
    algo_versions: dict[str, str] = Field(default_factory=dict)


class StandardDetail(BaseModel):
    """标准层详情（REQ-UI-003 数据浏览 / REQ-DATA-004 可回溯）。

    某一天的**标准化取值** + 这些取值**来自哪些原始文件**（只读引用，不复制内容）。
    """

    date: date
    metrics: dict[str, Any] = Field(default_factory=dict)  # 键为 Garmin 原始字段名
    source_refs: list[RawDailyRef] = Field(default_factory=list)


# ============================================================
# 运维
# ============================================================
class DepStatus(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    risk_rule_version: str
    deps: list[DepStatus] = Field(default_factory=list)


class SyncRequest(BaseModel):
    mode: str = "incremental"  # incremental | full
    task: str | None = None  # 可选：限定单个任务类型
    date: str | None = None  # 可选：指定同步日期（YYYY-MM-DD），缺省为今天
    refresh: bool = False  # 可选：窗口内覆盖 raw（手动「同步数据」用它修复占位数据）


class TaskListResponse(BaseModel):
    tasks: list[TaskRun] = Field(default_factory=list)


class DataChangeRequest(BaseModel):
    """数据补充/修正的登记入参（需求 10.1）。

    只描述「哪一天的数据变了」，重算范围与时机由服务端自动决定——
    调用方不能指定重算区间，系统也不提供手动重算入口。
    """

    earliest_affected_date: date
    kind: str = "correction"  # correction | supplement
    detail: dict = Field(default_factory=dict)


class AuditListResponse(BaseModel):
    events: list[AuditEvent] = Field(default_factory=list)


# ============================================================
# 环境（阶段② T2.2）
# ============================================================
class EnvironmentResponse(EnvironmentDaily):
    """环境日数据响应（``available=False`` 表示缺失，不编造）。"""


class LocationsResponse(BaseModel):
    locations: list[str] = Field(default_factory=list)


class LocationsUpdateRequest(BaseModel):
    locations: list[str] = Field(default_factory=list)


# ============================================================
# 策略版本（阶段③ T3.2）
# ============================================================
class StrategyResponse(StrategyVersion):
    """策略版本响应（同领域模型）。"""


class StrategyListResponse(BaseModel):
    versions: list[StrategyVersion] = Field(default_factory=list)


class StrategyEventListResponse(BaseModel):
    events: list[StrategyEvent] = Field(default_factory=list)


class StrategyOptimizeRequest(BaseModel):
    payload: dict | None = None
    change_reason: str = "AI 优化生成候选"
    evidence_summary: str = ""
    expected_effect: str = ""


class StrategyRestoreRequest(BaseModel):
    reason: str = "用户手动恢复"


# ============================================================
# 故障恢复（阶段③ T3.9）
# ============================================================
class FaultListResponse(BaseModel):
    faults: list[FaultRecoveryTask] = Field(default_factory=list)


class PrecheckResponse(BaseModel):
    fault: FaultRecoveryTask


class FaultReportRequest(BaseModel):
    kind: str
    impact_scope: str = ""
    fix_measures: str = ""


# ============================================================
# 消息（阶段③ T3.10）
# ============================================================
class MessageListResponse(BaseModel):
    items: list[Message] = Field(default_factory=list)
    unread_count: int = 0


class MessageDeleteRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


class MessageDeleteResponse(BaseModel):
    deleted: int = 0


# ============================================================
# 运动训练页面（三期 §7/§8）
# ============================================================
# 2026-11-08：此前 ``/api/activities/*`` 返回裸 ``dict``，OpenAPI 里是
# ``{[key: string]: unknown}``，前端只能手抄一套 interface。补上契约后
# ``frontend/src/api/training.ts`` 改为从 ``schema.d.ts`` 派生。
# 关键字段 ``hr_data_state`` 让前端能区分「FIT 明细未同步」与「本次确实
# 无逐秒心率」——NAS 单容器缺 fit_jsonl 时两张 HR 图一起空白的根因。
class CalendarItem(BaseModel):
    seq: int
    activity_id: str
    type: str
    label: str
    emoji: str
    color: str
    template: str


class CalendarDay(BaseModel):
    date: str
    items: list[CalendarItem] = Field(default_factory=list)


class CalendarType(BaseModel):
    key: str
    label: str
    emoji: str
    color: str
    template: str


class CalendarResponse(BaseModel):
    ym: str
    filter: str
    prev_ym: str
    next_ym: str
    types: list[CalendarType] = Field(default_factory=list)
    days: list[CalendarDay] = Field(default_factory=list)


class MetricRow(BaseModel):
    label: str
    value: str


class HrZoneRow(BaseModel):
    name: str
    low_bpm: int
    high_bpm: int
    seconds: int


class HrZoneStats(BaseModel):
    """Q8：逐秒 record + 项目 Karvonen 五区；缺失秒数单独披露，不补 0。"""

    zones: list[HrZoneRow] = Field(default_factory=list)
    counted_seconds: int
    below_zone_seconds: int
    missing_seconds: int
    method: str
    max_hr: int
    rest_hr: int


class HrPoint(BaseModel):
    t: str | None = None
    hr: float | None = None


class TrainingEffect(BaseModel):
    label: str
    label_raw: str | None = None
    aerobic: str | None = None
    anaerobic: str | None = None


class SegmentRow(BaseModel):
    """跑步 Lap 分段与 HIIT/力量动作段共用，字段按模板取用（可缺省）。"""

    index: int | None = None
    kind: Literal["exercise", "rest"] | None = None
    label: str | None = None
    key: str | None = None
    name_zh: str | None = None
    intensity: str | None = None
    duration_sec: float = 0
    distance_m: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    reps: int | None = None
    weight_kg: float | None = None


class StrengthTimeline(BaseModel):
    segments: list[SegmentRow] = Field(default_factory=list)
    active_seconds: float
    rest_seconds: float
    formal_exercise_count: int
    formal_exercises: list[str] = Field(default_factory=list)


class MuscleRow(BaseModel):
    code: str
    zh: str
    en: str
    primary_moves: list[str] = Field(default_factory=list)
    secondary_moves: list[str] = Field(default_factory=list)


class MuscleRegion(BaseModel):
    code: str
    zh: str
    en: str
    level: Literal["primary", "secondary", "none"]


class SwimmingLength(BaseModel):
    stroke: str | None = None
    duration_sec: float
    strokes: float | None = None
    avg_speed: float | None = None
    length_type: str | None = None


class SwimmingData(BaseModel):
    distance_m: float | None = None
    pool_length_m: float | None = None
    strokes: float | None = None
    active_lengths: float | None = None
    average_swolf: float | None = None
    stroke_types: list[str] = Field(default_factory=list)
    lengths: list[SwimmingLength] = Field(default_factory=list)


DetailTemplate = Literal[
    "running", "indoor_cardio", "hiit", "strength", "swimming", "other"
]
HrDataState = Literal["ok", "no_hr_samples", "fit_missing"]


class ActivityDetail(BaseModel):
    activity_id: str
    type: str
    label: str
    template: DetailTemplate
    name: str
    duration_sec: int
    metrics: list[MetricRow] = Field(default_factory=list)
    hr_zones: HrZoneStats
    hr_series: list[HrPoint] = Field(default_factory=list)
    hr_data_state: HrDataState
    training_effect: TrainingEffect
    segments: list[SegmentRow] = Field(default_factory=list)
    # 以下四组仅对应模板的活动才带出，其余为 None
    timeline: StrengthTimeline | None = None
    muscle_table: list[MuscleRow] | None = None
    muscles: list[MuscleRegion] | None = None
    swimming: SwimmingData | None = None


class DetailResponse(BaseModel):
    date: str
    seq: int
    total: int
    has_training: bool
    prev_date: str
    next_date: str
    activity: ActivityDetail | None = None


__all__ = [
    "ActivityDetail",
    "ApiError",
    "ApiErrorDetail",
    "AuditListResponse",
    "Baseline",
    "CalendarDay",
    "CalendarItem",
    "CalendarResponse",
    "CalendarType",
    "DailyReport",
    "DataQualityReport",
    "DepStatus",
    "DetailResponse",
    "DetailTemplate",
    "EnvironmentDaily",
    "EnvironmentResponse",
    "FaultListResponse",
    "FaultReportRequest",
    "FaultRecoveryTask",
    "HealthResponse",
    "HrDataState",
    "HrPoint",
    "HrZoneRow",
    "HrZoneStats",
    "LocationsResponse",
    "LocationsUpdateRequest",
    "Message",
    "MessageDeleteRequest",
    "MessageDeleteResponse",
    "MessageListResponse",
    "MetricRow",
    "MonthlyReport",
    "MuscleRegion",
    "MuscleRow",
    "Plan12Week",
    "PlanEvaluation",
    "PrecheckResponse",
    "RiskLevel",
    "SegmentRow",
    "SeriesPoint",
    "SeriesResponse",
    "StandardDetail",
    "StrategyEvent",
    "StrategyEventListResponse",
    "StrategyListResponse",
    "StrategyOptimizeRequest",
    "StrategyResponse",
    "StrategyRestoreRequest",
    "StrategyVersion",
    "StrengthTimeline",
    "SwimmingData",
    "SwimmingLength",
    "SyncRequest",
    "TaskListResponse",
    "TrainingEffect",
    "TrainingSession",
    "UploadResult",
    "WeeklyPlan",
    "WeeklyPlanGenerateRequest",
    "WeeklyReport",
]
