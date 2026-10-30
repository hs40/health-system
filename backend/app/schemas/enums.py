"""全局枚举：与 Plan §3.1 / §4.5 一一对应，是前后端契约的取值来源。"""

from __future__ import annotations

from enum import StrEnum


class QualityLevel(StrEnum):
    """数据质量要求对应的使用场景（REQ-DATA-006）。"""

    REPORT = "report"  # 日报分析
    CAPABILITY = "capability"  # 运动能力评估
    ADJUSTMENT = "adjustment"  # 普通训练调整
    HIGH_RISK = "highRisk"  # 高风险决策


class QualityGrade(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class ConflictSeverity(StrEnum):
    IRRELEVANT = "irrelevant"  # 无关差异
    AFFECTS_CONCLUSION = "affectsConclusion"  # 影响一般结论
    AFFECTS_SAFETY = "affectsSafety"  # 影响安全决策


class ConflictResolution(StrEnum):
    KEPT_SOURCE = "keptSource"
    FLAGGED = "flagged"
    BLOCKED = "blocked"


class RiskLevel(StrEnum):
    """三级风险（REQ-RISK-001）。

    .. deprecated::
       2026-10-22 风险引擎重设计（Q1/Q8/Q12）后**已退役**：判定结果改为
       ``PriorityLevel``（P1–P4）+ ``DataState``（A/B/C）。本枚举仅保留给
       历史代码引用过渡，新代码一律不得使用。
    """

    NORMAL = "normal"  # 正常波动
    NEEDS_ADJUSTMENT = "needsAdjustment"  # 需要调整
    HIGH = "high"  # 高风险


class DataState(StrEnum):
    """数据质量三态关卡（2026-10-22 风险引擎重设计，Q7/Q22/Q23）。

    - ``A`` 全景判定：四维（负荷 / HRV / 睡眠 / RHR）数据齐全；
    - ``B`` 纯负荷降级：核心恢复维度缺失，仅按负荷与 RHR 判定，关闭加量通道；
    - ``C`` 安全兜底：连负荷也不可用 → **只关负荷类规则**（P1-LOAD-HRV / P2-LOAD /
      P3-PROGRESS），恢复类规则（睡眠 / 电量 / HRV / RHR）照判；恢复极差仍会
      触发 P1/P2（2026-10-25 用户决策 L-7）。
    """

    FULL = "A"
    LOAD_ONLY = "B"
    FALLBACK = "C"


class PriorityLevel(StrEnum):
    """四级优先级（2026-10-22 风险引擎重设计，Q2/Q18/Q20/Q26）。

    - ``P1`` 强制休息：不生成计划、不上传，输出 15–20min Z1 恢复日模板；
    - ``P2`` 减量：按触发条件分流为「容量减半」或「强度减量」，可生成但不上传；
    - ``P3`` 进阶加量：时长/距离 +5~10%，力量 +2.5~5% 或 +1~2 reps；
    - ``P4`` 维持：照原计划执行。
    """

    REST = "P1"
    REDUCE = "P2"
    PROGRESS = "P3"
    MAINTAIN = "P4"


PRIORITY_ORDER: dict[PriorityLevel, int] = {
    PriorityLevel.REST: 3,
    PriorityLevel.REDUCE: 2,
    PriorityLevel.PROGRESS: 1,
    PriorityLevel.MAINTAIN: 0,
}
"""优先级排序（数值越大越紧急）。用于多规则命中时取最高优先级。"""


class DataSufficiency(StrEnum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class Sport(StrEnum):
    RUNNING = "running"
    INDOOR_CARDIO = "indoorCardio"
    STRENGTH = "strength"


class SessionStatus(StrEnum):
    DRAFT = "draft"
    EVALUATED = "evaluated"
    CONFIRMED = "confirmed"
    UPLOADED = "uploaded"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class TaskStatus(StrEnum):
    """任务状态（REQ-TASK-001）。"""

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    RETRY = "retry"
    UNKNOWN = "unknown"  # 执行结果未知


class TaskType(StrEnum):
    GARMIN_SYNC = "garminSync"
    TREDICT_UPLOAD = "tredictUpload"
    TREDICT_PULL = "tredictPull"
    QUALITY_CHECK = "qualityCheck"
    STANDARDIZE = "standardize"
    BASELINE = "baseline"
    LOAD_ACWR = "loadAcwr"
    RISK_ASSESS = "riskAssess"
    DAILY_REPORT = "dailyReport"
    PLAN_UPLOAD = "planUpload"
    WEEKLY_PLAN = "weeklyPlan"
    MONTHLY_REPORT = "monthlyReport"
    RECOMPUTE = "recompute"  # 负荷/ACWR 重算（需求 10.1–10.6）


class AuditCategory(StrEnum):
    """最低必要审计的六类（REQ-AUDIT-001）。"""

    DATA_SOURCE = "dataSource"
    QUALITY = "quality"
    CALCULATION = "calculation"
    RISK = "risk"
    PLAN_VERSION = "planVersion"
    UPLOAD = "upload"
    TASK = "task"
    MODEL_CHANGE = "modelChange"


class StrategyType(StrEnum):
    """策略类型（二期仅季节策略，Plan §4.1.1）。"""

    SEASON = "season"


class StrategyStatus(StrEnum):
    """策略版本状态（§17.1）。

    语义：
    - ``candidate``：候选，未参与决策；
    - ``verified``：通过完整运行校验，但尚未生效；
    - ``active``：当前有效（同一 strategy_type 同时最多一个）；
    - ``superseded``：被后续版本取代（历史）；
    - ``invalid``：标记为无效/已知缺陷，禁止参与决策。
    """

    CANDIDATE = "candidate"
    VERIFIED = "verified"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALID = "invalid"


class StrategyChangeType(StrEnum):
    """版本产生方式（§19.4：每次变更产生新版本）。"""

    AI_OPTIMIZE = "aiOptimize"
    MANUAL_RESTORE = "manualRestore"


class StrategyEventType(StrEnum):
    """策略事件类型（Plan §4.1.2）。"""

    OPTIMIZE = "optimize"
    RESTORE = "restore"
    VALIDATE = "validate"
    ACTIVATE = "activate"
    FAIL = "fail"


class ValidationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class NotifyStatus(StrEnum):
    """消息发送状态（§10.1：版本已创建 ≠ 消息已送达）。"""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class FaultSeverity(StrEnum):
    """故障分级（§8.1）。"""

    GENERAL = "general"  # 一般技术故障：修复后可按规则自动恢复
    MAJOR = "major"  # 重大故障：恢复优化必须等用户确认


class PrecheckStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class MessageCategory(StrEnum):
    """消息分类（Plan §4.1.4）。"""

    STRATEGY_VERSION = "strategyVersion"
    RISK = "risk"
    TASK_FAIL = "taskFail"
    NEED_ACTION = "needAction"


class MessageImportance(StrEnum):
    NORMAL = "normal"
    HIGH = "high"


class FollowUpStatus(StrEnum):
    """事件后续处理状态（§10.3：已读 ≠ 已解决）。"""

    OPEN = "open"
    RESOLVED = "resolved"
    BLOCKED = "blocked"


class ErrorCode(StrEnum):
    """统一错误码（Plan §4.5）。"""

    VALIDATION = "E_VALIDATION"
    NOT_FOUND = "E_NOT_FOUND"
    DATA_MISSING = "E_DATA_MISSING"
    DATA_CONFLICT = "E_DATA_CONFLICT"
    DATA_CORRUPT = "E_DATA_CORRUPT"
    RISK_HIGH_BLOCK = "E_RISK_HIGH_BLOCK"
    TASK_IN_PROGRESS = "E_TASK_IN_PROGRESS"
    EXTERNAL_TIMEOUT = "E_EXTERNAL_TIMEOUT"
    EXTERNAL_UNKNOWN = "E_EXTERNAL_UNKNOWN"
    LLM_INVALID_OUTPUT = "E_LLM_INVALID_OUTPUT"
    RULE_NOT_VERIFIED = "E_RULE_NOT_VERIFIED"
    # —— 阶段③ 策略版本 / 回退 / 恢复（Plan §4.4）——
    STRATEGY_NOT_VERIFIED = "E_STRATEGY_NOT_VERIFIED"
    STRATEGY_OPTIMIZE_PAUSED = "E_STRATEGY_OPTIMIZE_PAUSED"
    RECOVERY_NEED_CONFIRM = "E_RECOVERY_NEED_CONFIRM"
    WEATHER_UNAVAILABLE = "E_WEATHER_UNAVAILABLE"
    CONCURRENCY_CONFLICT = "E_CONCURRENCY_CONFLICT"


ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.DATA_MISSING: 409,
    ErrorCode.DATA_CONFLICT: 409,
    ErrorCode.DATA_CORRUPT: 500,
    ErrorCode.RISK_HIGH_BLOCK: 409,
    ErrorCode.TASK_IN_PROGRESS: 409,
    ErrorCode.EXTERNAL_TIMEOUT: 502,
    ErrorCode.EXTERNAL_UNKNOWN: 502,
    ErrorCode.LLM_INVALID_OUTPUT: 502,
    ErrorCode.RULE_NOT_VERIFIED: 503,
    ErrorCode.STRATEGY_NOT_VERIFIED: 409,
    ErrorCode.STRATEGY_OPTIMIZE_PAUSED: 409,
    ErrorCode.RECOVERY_NEED_CONFIRM: 409,
    ErrorCode.WEATHER_UNAVAILABLE: 502,
    ErrorCode.CONCURRENCY_CONFLICT: 409,
}
