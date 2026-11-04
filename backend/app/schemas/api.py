"""API 层契约：请求 / 响应模型（Plan §4）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

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


class Plan12Week(BaseModel):
    """滚动 12 周计划（阶段 C）。"""

    start_date: date
    weeks: list[dict] = Field(default_factory=list)
    goal_weights: dict[str, float] = Field(default_factory=dict)
    strategy: dict | None = None  # 生成时有效策略版本关联（T4.1，§11.1）


# ============================================================
# 计划
# ============================================================
class WeeklyPlan(BaseModel):
    week_start: date  # 周一
    days: list[WeekPlanDay] = Field(default_factory=list)
    status: str = "draft"  # draft | evaluated | confirmed
    version: int = 1
    strategy: dict | None = None  # 生成时有效策略版本关联（T4.1，§11.1）
    # 周计划卡片标识（§12.3；2026-10-20 决策 Q10）：**读取时现算、不落库**。
    # key = 日期 ISO；value = card_view() 输出（badge/is_adjusted/reason/
    # report_ref/actual）。旧 payload 无此字段 → 空 dict，由接口现算填充。
    cards: dict[str, dict] = Field(default_factory=dict)
    # 最近一次 AI 评估结果（含结构化冲突与硬阻断标记）。读取时带出，
    # 前端刷新后仍能看到"评估过/被阻断"的状态，不会误以为可确认。
    evaluation: dict | None = None


class WeeklyPlanGenerateRequest(BaseModel):
    """生成周计划的入参（D18：夜班日由用户手动告知）。"""

    night_shift_days: list[date] = Field(default_factory=list)


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


__all__ = [
    "ApiError",
    "ApiErrorDetail",
    "AuditListResponse",
    "Baseline",
    "DailyReport",
    "DataQualityReport",
    "DepStatus",
    "EnvironmentDaily",
    "EnvironmentResponse",
    "FaultListResponse",
    "FaultReportRequest",
    "FaultRecoveryTask",
    "HealthResponse",
    "LocationsResponse",
    "LocationsUpdateRequest",
    "Message",
    "MessageDeleteRequest",
    "MessageDeleteResponse",
    "MessageListResponse",
    "MonthlyReport",
    "Plan12Week",
    "PlanEvaluation",
    "PrecheckResponse",
    "RiskLevel",
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
    "SyncRequest",
    "TaskListResponse",
    "TrainingSession",
    "UploadResult",
    "WeeklyPlan",
    "WeeklyPlanGenerateRequest",
    "WeeklyReport",
]
