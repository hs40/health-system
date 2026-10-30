"""SQLModel 表定义（Plan §3.2）。

约定：
- 原始数据**不入库**（以只读文件挂载），此处仅存标准化 / 派生 / 应用 / 审计数据。
- 复杂结构统一用 JSON 列存储，避免过度规范化。
- 时间类型统一通过 `dt` 模块引用，避免字段名（date/datetime）与类型名冲突。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Column, DateTime, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

# 说明：SQLModel 会把 `datetime` 注解映射为要求 tz-aware 的 UTCDateTime。
# 本系统为单时区（Asia/Shanghai）部署，且睡眠起止刻意保存"本地墙钟"朴素时间，
# 故统一用 sa_type=DateTime 声明为朴素时间列，避免绑定报错。


def _json_col() -> Column:  # type: ignore[type-arg]
    return Column(JSON, nullable=True)


# ============================================================
# 标准化层
# ============================================================
class StandardDailyRow(SQLModel, table=True):
    __tablename__ = "standard_daily"
    __table_args__ = (UniqueConstraint("date", name="uq_standard_daily_date"),)

    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True)
    resting_heart_rate: int | None = None
    min_heart_rate: int | None = None
    max_heart_rate: int | None = None
    sleep_duration_sec: int | None = None
    sleep_start_ts: dt.datetime | None = Field(default=None, sa_type=DateTime)
    sleep_end_ts: dt.datetime | None = Field(default=None, sa_type=DateTime)
    hrv_last_night_avg: float | None = None
    # Garmin 自带 HRV 基线区间（E24）：lowUpper=低区上沿（R-001 判定线）、
    # balancedLow/balancedUpper=平衡区下限/上限（上限用于加量建议）、
    # weeklyAvg=7 天均值、status=BALANCED/UNBALANCED/LOW/HIGH/NONE
    hrv_low_upper: float | None = None
    hrv_balanced_low: float | None = None
    hrv_balanced_high: float | None = None
    hrv_weekly_avg: float | None = None
    hrv_status: str | None = None
    body_battery_wake: int | None = None
    # 风险引擎口径（Q11）：睡眠得分（0–100）与醒时电量
    sleep_score: int | None = None
    body_battery_morning: int | None = None
    weight_kg: float | None = None
    steps: int | None = None
    active_calories: int | None = None
    intensity_moderate_min: int | None = None
    intensity_vigorous_min: int | None = None
    spo2_avg: float | None = None
    respiration_avg: float | None = None
    source_refs: dict = Field(default_factory=dict, sa_column=_json_col())
    created_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)


# ============================================================
# 计算层
# ============================================================
class BaselineRow(SQLModel, table=True):
    __tablename__ = "baseline"
    __table_args__ = (UniqueConstraint("metric", "version", name="uq_baseline_metric_version"),)

    id: int | None = Field(default=None, primary_key=True)
    metric: str = Field(index=True)
    version: int
    window_days: int
    mean: float
    std: float
    p10: float
    p90: float
    mature: bool
    computed_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)


class TrainingLoadRow(SQLModel, table=True):
    __tablename__ = "training_load"
    __table_args__ = (UniqueConstraint("date", name="uq_load_date"),)

    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True)
    by_sport: dict = Field(default_factory=dict, sa_column=_json_col())
    combined: float = 0.0
    method: str = ""


class AcwrRow(SQLModel, table=True):
    __tablename__ = "acwr"
    __table_args__ = (UniqueConstraint("date", name="uq_acwr_date"),)

    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True)
    acute: float
    chronic: float
    # 需求 8.4：慢性 EWMA 为 0 → 不可计算，存 NULL（不得填 0 伪造比值）。
    # 迁移 d5f3a1c9e7b2 把该列从 NOT NULL 改为可空。
    ratio: float | None = None
    # G-16（2026-10-24）：口径版本号。**历史行保持 NULL（= legacy），不回填**——
    # 重算会抹掉「当时用旧口径」的事实；只有新写入/显式重算才盖当前版本。
    acwr_version: str | None = None


class RiskAssessmentRow(SQLModel, table=True):
    """每日风险评估（2026-10-22 重建：四级优先级 + 三态关卡）。

    每天必写一条，含 P4（Q15）—— 使「无记录」只代表当天任务未运行。
    """

    __tablename__ = "risk_assessment"
    __table_args__ = (UniqueConstraint("date", name="uq_risk_date"),)

    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True)
    priority: str = Field(index=True)      # P1 / P2 / P3 / P4
    data_state: str = Field(index=True)    # A / B / C
    triggered_rules: dict = Field(default_factory=dict, sa_column=_json_col())
    evidence: dict = Field(default_factory=dict, sa_column=_json_col())
    missing_inputs: dict = Field(default_factory=dict, sa_column=_json_col())
    volume_halved: bool = False
    intensity_reduced: bool = False
    floored_at_recovery_template: bool = False
    rule_version: str
    decided_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)


# ============================================================
# 应用层
# ============================================================
class DailyReportRow(SQLModel, table=True):
    __tablename__ = "daily_report"
    __table_args__ = (UniqueConstraint("date", name="uq_report_date"),)

    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True)
    payload: dict = Field(default_factory=dict, sa_column=_json_col())
    version: int = 1
    generated_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)


class TrainingSessionRow(SQLModel, table=True):
    __tablename__ = "training_session"

    id: str = Field(primary_key=True)
    date: dt.date = Field(index=True)
    sport: str = Field(index=True)
    name: str
    payload: dict = Field(default_factory=dict, sa_column=_json_col())
    status: str = Field(default="draft", index=True)
    garmin_workout_id: int | None = None
    garmin_scheduled_id: int | None = None


class WeeklyReportRow(SQLModel, table=True):
    __tablename__ = "weekly_report"
    __table_args__ = (UniqueConstraint("week_start", name="uq_weekly_report_start"),)

    id: int | None = Field(default=None, primary_key=True)
    week_start: dt.date = Field(index=True)  # 周一
    payload: dict = Field(default_factory=dict, sa_column=_json_col())
    version: int = 1
    generated_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)


class WeeklyPlanRow(SQLModel, table=True):
    __tablename__ = "weekly_plan"
    __table_args__ = (UniqueConstraint("week_start", "version", name="uq_weekly_start_version"),)

    id: int | None = Field(default=None, primary_key=True)
    week_start: dt.date = Field(index=True)  # 周一
    payload: dict = Field(default_factory=dict, sa_column=_json_col())
    status: str = Field(default="draft", index=True)
    version: int = 1
    updated_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)


class MonthlyReportRow(SQLModel, table=True):
    __tablename__ = "monthly_report"
    __table_args__ = (UniqueConstraint("month", name="uq_monthly_month"),)

    id: int | None = Field(default=None, primary_key=True)
    month: str = Field(index=True)  # YYYY-MM
    payload: dict = Field(default_factory=dict, sa_column=_json_col())
    version: int = 1
    generated_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)


class Plan12WeekRow(SQLModel, table=True):
    __tablename__ = "plan_12w"

    id: int | None = Field(default=None, primary_key=True)
    start_date: dt.date = Field(index=True)
    payload: dict = Field(default_factory=dict, sa_column=_json_col())
    version: int = 1
    updated_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)


# ============================================================
# 运维层
# ============================================================
class TaskRunRow(SQLModel, table=True):
    __tablename__ = "task_run"

    id: str = Field(primary_key=True)
    task_type: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    status: str = Field(index=True)  # success|partial|failed|retry|unknown
    started_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)
    finished_at: dt.datetime | None = Field(default=None, sa_type=DateTime)
    attempts: int = 0
    checkpoint: str | None = None
    error: dict = Field(default_factory=dict, sa_column=_json_col())


class AuditEventRow(SQLModel, table=True):
    __tablename__ = "audit_event"

    id: str = Field(primary_key=True)
    at: dt.datetime = Field(default_factory=dt.datetime.now, index=True, sa_type=DateTime)
    category: str = Field(index=True)
    ref: str = Field(default="")
    detail: dict = Field(default_factory=dict, sa_column=_json_col())


class ExternalCallLogRow(SQLModel, table=True):
    """外部调用记录（REQ-PRIV-001：记录传输目的/范围/模型/结果）。"""

    __tablename__ = "external_call_log"

    id: int | None = Field(default=None, primary_key=True)
    at: dt.datetime = Field(default_factory=dt.datetime.now, index=True, sa_type=DateTime)
    system: str = Field(index=True)  # garmin | tredict | llm
    purpose: str = ""
    scope: str = ""  # 传输的数据范围描述
    request_summary: dict = Field(default_factory=dict, sa_column=_json_col())
    result: str = ""  # ok | error | unknown
    latency_ms: int | None = None
# ============================================================
# 二期新增：环境数据 + 冲突检测（阶段② T2.2 / T2.6）
# ============================================================
class EnvironmentDailyRow(SQLModel, table=True):
    """环境（天气）日数据（Plan §5 T2.2；REQ-AIR/环境 §13.7/§13.8）。

    按 (date, location_id) 去重；天气不可用时 ``available=False`` 且各字段为 ``None``
    （**不编造**）。
    """

    __tablename__ = "environment_daily"
    __table_args__ = (
        UniqueConstraint("date", "location_id", name="uq_environment_date_location"),
    )

    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True)
    location_id: str = Field(default="", index=True)
    location_name: str = ""
    lat: str | None = None
    lon: str | None = None
    # 可用性：False → 数据缺失，下游不得据此放宽安全阈值
    available: bool = True
    # 观测/预报值
    temp_c: float | None = None
    humidity: float | None = None
    precip_mm: float | None = None
    weather_text: str = ""
    is_rain: bool = False
    is_snow: bool = False
    wind_scale: str | None = None
    # 追溯与时效
    source: str = "qweather"
    fetched_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)
    payload: dict = Field(default_factory=dict, sa_column=_json_col())


class TrainingConflictRow(SQLModel, table=True):
    """训练/数据冲突记录（Plan §5 T2.6；REQ §3.5/§13.7）。

    记录跨源冲突的采用值、来源、选择依据与差异，供审计与前端展示。
    """

    __tablename__ = "training_conflict"

    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True)
    metric: str = Field(index=True)
    severity: str = Field(index=True)  # irrelevant|affectsConclusion|affectsSafety
    resolution: str = ""  # keptSource|flagged|blocked
    kept_source: str = ""
    kept_value: dict = Field(default_factory=dict, sa_column=_json_col())
    detail: dict = Field(default_factory=dict, sa_column=_json_col())
    created_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)


# ============================================================
# 二期新增：策略版本 + 事件 + 故障恢复 + 消息（阶段③ T3.1/T3.9/T3.10）
# ============================================================
class StrategyVersionRow(SQLModel, table=True):
    """季节策略版本（Plan §4.1.1；REQ §17.1）。

    **不可变性**：正式版本禁止 UPDATE/DELETE；修改策略 → **新建版本**。
    状态流转通过 ``strategy_event`` 记录，本表仅保留当前快照字段。
    """

    __tablename__ = "strategy_version"
    __table_args__ = (
        UniqueConstraint("strategy_type", "version_number", name="uq_strategy_type_ver"),
    )

    strategy_version_id: str = Field(primary_key=True)
    strategy_type: str = Field(index=True)  # season（二期仅季节策略）
    version_number: str = Field(index=True)  # 可读版本号 e.g. "season-1.0.0"
    status: str = Field(index=True)  # candidate|verified|active|superseded|invalid
    change_type: str  # aiOptimize|autoRollback|manualRestore
    parent_version_id: str | None = Field(default=None, index=True)  # 变更前版本
    created_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)
    effective_at: dt.datetime | None = Field(default=None, sa_type=DateTime)
    created_by: str = ""  # ai|system|user
    change_reason: str = ""
    evidence_summary: str = ""  # 依据摘要
    expected_effect: str = ""
    actual_effect: str | None = None  # 后续评估写回
    payload: dict = Field(default_factory=dict, sa_column=_json_col())  # 策略内容
    applicability_conditions: dict = Field(default_factory=dict, sa_column=_json_col())
    safety_constraints: dict = Field(default_factory=dict, sa_column=_json_col())
    validation_status: str = "pending"  # pending|passed|failed
    validation_result: dict = Field(default_factory=dict, sa_column=_json_col())
    limitations: list[str] = Field(default_factory=list, sa_column=_json_col())
    immutable: bool = True  # 正式版本不可改


class StrategyEventRow(SQLModel, table=True):
    """策略事件（Plan §4.1.2）：版本状态流转与审计。"""

    __tablename__ = "strategy_event"

    event_id: str = Field(primary_key=True)
    event_type: str = Field(index=True)  # optimize|rollback|restore|validate|activate|fail
    triggered_at: dt.datetime = Field(default_factory=dt.datetime.now, index=True, sa_type=DateTime)
    current_version_id: str | None = None  # 当前版本
    target_version_id: str | None = None  # 目标版本
    task_id: str | None = Field(default=None, index=True)
    trigger_reason: str = ""
    steps: list[dict] = Field(default_factory=list, sa_column=_json_col())  # 执行步骤
    validation_result: dict = Field(default_factory=dict, sa_column=_json_col())
    failure_reason: str | None = None
    risk_state: str = ""
    notify_status: str = "pending"  # pending|sent|failed
    related_report_ref: str | None = None
    follow_up_status: str = "open"  # open|resolved|blocked


class FaultRecoveryTaskRow(SQLModel, table=True):
    """故障恢复任务（Plan §4.1.3；REQ §8/§17.3）。"""

    __tablename__ = "fault_recovery_task"

    fault_id: str = Field(primary_key=True)
    severity: str = Field(index=True)  # general|major
    impact_scope: str = ""
    started_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)
    fix_measures: str = ""
    fixed_at: dt.datetime | None = Field(default=None, sa_type=DateTime)
    precheck_status: str = "pending"  # pending|passed|failed
    precheck_report: dict = Field(default_factory=dict, sa_column=_json_col())
    residual_risk: list[str] = Field(default_factory=list, sa_column=_json_col())
    needs_user_confirm: bool = False
    user_confirmed: bool = False
    user_confirmed_at: dt.datetime | None = Field(default=None, sa_type=DateTime)
    recovery_executed_at: dt.datetime | None = Field(default=None, sa_type=DateTime)
    final_result: str = ""


class MessageRow(SQLModel, table=True):
    """系统消息（Plan §4.1.4；REQ §10）。"""

    __tablename__ = "message"

    message_id: str = Field(primary_key=True)
    at: dt.datetime = Field(default_factory=dt.datetime.now, index=True, sa_type=DateTime)
    category: str = Field(index=True)  # strategyVersion|risk|taskFail|needAction
    importance: str = Field(default="normal")  # normal|high
    title: str
    body: str = ""
    ref_type: str = ""  # strategy_version|fault|report
    ref_id: str = ""
    link: str = ""  # 前端跳转路径
    is_read: bool = False
    is_resolved: bool = False  # 已读 ≠ 已解决（§10.3）
    send_status: str = "sent"  # pending|sent|failed


# ============================================================
# 同步层（P1，2026-10-17）：Garmin ↔ 本地 ↔ Tredict
# ------------------------------------------------------------
# 设计约束（见 docs/tredict_api_structure.md 与决策台账 Q6/Q17/Q18/Q28/Q29）：
# - 本轮**只建数据链路**，不改任何计算逻辑；这 4 张表**不参与** baseline / risk / report。
# - 全部**不加外键**：`matched_garmin_id` 可空（匹配可能失败），
#   `sync_ledger.entity_id` 是多态外键（指向三张不同表），SQLite 无法表达。
# - 迁移只建结构，数据回填走独立幂等脚本（Q29）。
# ============================================================
class GarminActivityRecordRow(SQLModel, table=True):
    """FIT 逐秒数据（P4，Q34=A 的 SQLite 一侧）。

    只存能算的 7 个通道；全量归档在
    ``data/raw/garmin/fit_jsonl/<activity_id>.jsonl.gz``。

    主键用 ``(activity_id, seq)`` 而非 ``(activity_id, t)``：
    同一条活动内可能有同一秒的两条 record，用 t 做键会静默丢行。
    """

    __tablename__ = "garmin_activity_record"
    __table_args__ = (
        Index("ix_garmin_activity_record_activity_t", "activity_id", "t"),
    )

    activity_id: str = Field(primary_key=True)
    seq: int = Field(primary_key=True)  # 文件内原始顺序
    t: int  # UTC epoch 秒（口径 A：活动时间戳是真 UTC）
    hr: int | None = None
    speed: float | None = None
    power: float | None = None
    cadence: float | None = None  # cadence + fractional_cadence
    altitude: float | None = None
    distance: float | None = None


class TredictActivityRow(SQLModel, table=True):
    """Tredict 活动精简版（Q17）。

    仅用于**上传校验与追溯**，不参与任何计算。
    不落 ``seriesSampled``（时间序列），只留可匹配的元数据。
    """

    __tablename__ = "tredict_activity"

    tredict_id: str = Field(primary_key=True)
    # Tredict 的 date 是**真 UTC**（Tredict 解析 FIT 得到），
    # local_date 是 +8h 还原后的本地日期，用于与 Garmin startTimeLocal 对齐。
    utc_datetime: dt.datetime = Field(sa_type=DateTime)
    local_date: dt.date = Field(index=True)
    sport_type: str = Field(index=True)  # running|cycling|swimming|misc
    sub_sport_type: str = ""
    title: str = ""
    # Tredict 有 durationTotal 与 duration 两个时长，二者差值实测最大 188s，
    # 匹配用哪个留待 P5 实测决定，故两个都存。
    duration_total_sec: int | None = None
    duration_sec: int | None = None
    distance_m: float | None = None
    has_notes: bool = False
    training_type: str = "file"  # file=设备上传 | planned=Tredict 内手工创建
    matched_garmin_id: str | None = Field(default=None, index=True)
    match_confidence: str = "none"  # high|medium|ambiguous|none
    payload_sha256: str = ""
    fetched_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)


class TredictGeneratedRow(SQLModel, table=True):
    """Tredict 自有 / 自算数据（Q23=A：**只存档 + 可查，不进计算层**）。

    覆盖注册表中的 ``capacity`` / ``zones`` / ``training_effort`` /
    ``planned_workout`` / ``equipment`` / ``activity_metrics`` 等类别。
    新增类别只需在注册表加一行，无需改表结构。

    ``content_hash`` 用于内容去重：内容未变则不写新行。
    """

    __tablename__ = "tredict_generated"
    __table_args__ = (
        UniqueConstraint("entity", "entity_key", "version", name="uq_tredict_generated"),
    )

    id: int | None = Field(default=None, primary_key=True)
    entity: str = Field(index=True)
    entity_key: str = Field(index=True)  # 复合键字符串化（如 "running|2026-09-01T13:41:46Z"）
    version: int = 1  # 同一 (entity, entity_key) 的内容版本，内容变则 +1
    content_hash: str = Field(index=True)
    remote_updated_at: dt.datetime | None = Field(default=None, sa_type=DateTime)
    payload: dict = Field(default_factory=dict, sa_column=_json_col())
    fetched_at: dt.datetime = Field(default_factory=dt.datetime.now, sa_type=DateTime)


class SyncLedgerRow(SQLModel, table=True):
    """逐条同步状态（Q20 的 ``verify_run`` 已被本表取代）。

    设计要点：状态挂在**记录**上而非「批次」上，因此不需要 run 历史表；
    ``first_failed_at`` 一个字段即可回答「这个问题从哪天开始的」。
    """

    __tablename__ = "sync_ledger"
    __table_args__ = (
        UniqueConstraint("entity", "entity_id", "direction", name="uq_sync_ledger"),
        Index("ix_sync_ledger_state_direction", "state", "direction"),
    )

    id: int | None = Field(default=None, primary_key=True)
    entity: str = Field(index=True)  # garmin_activity|tredict_activity|tredict_generated|sleep...
    entity_id: str = Field(index=True)
    direction: str = Field(index=True)  # garmin2local|local2tredict|tredict2local
    local_hash: str = ""
    remote_hash: str = ""
    # pending|ok|missing_remote|missing_local|mismatch|ambiguous
    state: str = Field(default="pending", index=True)
    first_failed_at: dt.datetime | None = Field(default=None, sa_type=DateTime)
    last_checked_at: dt.datetime | None = Field(default=None, sa_type=DateTime)
    attempts: int = 0
    detail: dict = Field(default_factory=dict, sa_column=_json_col())
