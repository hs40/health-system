/**
 * 前端类型：与后端 Pydantic 契约对齐（阶段 A 手工维护，后续由 OpenAPI 自动生成）。
 */

/** 四级优先级（2026-10-22 取代三级 RiskLevel，旧 R-001~R-008 已全量退役） */
export type PriorityLevel = 'P1' | 'P2' | 'P3' | 'P4'

/** 三态数据质量关卡：A 全景判定 / B 纯负荷降级 / C 安全兜底 */
export type DataState = 'A' | 'B' | 'C'
export type QualityGrade = 'ok' | 'partial' | 'insufficient'
export type TaskStatus = 'success' | 'partial' | 'failed' | 'retry' | 'unknown'

export interface DepStatus {
  name: string
  ok: boolean
  detail?: string | null
}

export interface HealthResponse {
  status: string
  version: string
  risk_rule_version: string
  deps: DepStatus[]
}

export interface TaskRun {
  id: string
  task_type: string
  idempotency_key: string
  status: TaskStatus
  started_at: string
  finished_at?: string | null
  attempts: number
  checkpoint?: string | null
  error?: { code: string; message: string } | null
}

export interface AuditEvent {
  id: string
  at: string
  category: string
  ref: string
  detail: Record<string, unknown>
}

export interface WeekPlanDay {
  date: string
  weekday: number
  session?: TrainingSessionBrief | null
  is_night_shift: boolean
  note?: string | null
}

export interface TrainingSessionBrief {
  id: string
  date: string
  sport: string
  name: string
  duration_min?: number | null
  /** 主阶段目标心率区间（Z1–Z5，Karvonen；需求 §12.4 强度基准） */
  target_hr_zone?: string | null
  status: string
  garmin_workout_id?: number | null
  garmin_scheduled_id?: number | null
}

export interface RiskAssessment {
  date: string
  priority: PriorityLevel
  data_state: DataState
  triggered_rules: string[]
  /** 人话原因（「哪条规则 + 实测值 vs 阈值」）；旧报告无此字段 */
  reasons?: string[]
  evidence: Record<string, unknown>
  missing_inputs: string[]
  volume_halved?: boolean
  intensity_reduced?: boolean
  floored_at_recovery_template?: boolean
  progress_duration_pct?: number[]
  progress_strength_pct?: number[]
  progress_strength_reps?: number[]
  rule_version: string
  decided_at: string
}

export interface DataQualityReport {
  date: string
  level: string
  grade: QualityGrade
  missing: string[]
  conflicts: unknown[]
  notes: string[]
}

/** 日报中的当日生效心率参数（需求 5.4）：Karvonen 方法 + 五区整数边界。 */
export interface DailyHrParams {
  max_hr: number
  rest_hr: number
  method: string
  hrr: number
  zones: { name: string; low_pct: number; high_pct: number; low_bpm: number; high_bpm: number }[]
}

export interface DailyReport {
  date: string
  yesterday_training?: Record<string, unknown> | null
  /** 前一日训练完成度（§12.4 判定；周报据此提取聚合） */
  yesterday_completion?: Record<string, unknown> | null
  load?: Record<string, unknown> | null
  acwr?: {
    date: string
    acute: number
    chronic: number
    ratio: number | null
    sufficient?: boolean
    note?: string | null
  } | null
  hr_params?: DailyHrParams | null
  sleep?: Record<string, unknown> | null
  recovery?: Record<string, unknown> | null
  capability?: Record<string, unknown> | null
  over_under_analysis?: string | null
  today_plan_id?: string | null
  adjustment_reason?: string | null
  week_start?: string | null
  week_plan?: WeekPlanDay[] | null
  /** 四项长期目标进展：近 7 天窗口，基线为前一个 7 天区间（旧报告为 null） */
  goal_progress?: { range?: { start: string; end: string }; goals?: Record<string, unknown> } | null
  quality: DataQualityReport
  /** 风险快照。历史 payload 已清理该字段（Q29/Q30），权威数据在 risk_assessment 表 */
  risk?: RiskAssessment | null
  limitations: string[]
  /** 面向用户的建议：P1 恢复日提示 / P3 加量幅度（Q6 / Q22） */
  advisory?: string[]
  version: number
}

/** 窗口内策略版本变化（§11.3/§11.4；只读策略事件，不触发重算） */
export interface StrategyChange {
  event_id: string
  event_type: string
  changed_at: string
  version_number?: string | null
  change_type?: string | null
  reason?: string | null
  effective_at?: string | null
}

export interface WeeklyReport {
  week_start: string
  review: Record<string, unknown>
  plan_vs_actual: Record<string, unknown>
  /** 周计划执行块（§12.4）：六态计数/双量/调整日/逐日明细/未判定披露 */
  execution?: Record<string, unknown>
  load: Record<string, unknown>
  recovery: Record<string, unknown>
  goal_progress: Record<string, unknown>
  deviation_reason?: string | null
  /** 生成时有效策略版本关联（§11.3） */
  strategy?: Record<string, unknown> | null
  /** 本周发生的策略版本变化（§11.3） */
  strategy_changes?: StrategyChange[]
}

/** 周计划卡片（§12.3）：日报已调整标识 + 五项详情。 */
export interface WeekPlanCard {
  date: string
  original: string | null
  adjusted: string | null
  is_adjusted: boolean
  badge: string | null
  reason: string | null
  report_ref: string | null
  actual: string | null
}

export interface WeeklyPlan {
  week_start: string
  days: WeekPlanDay[]
  status: 'draft' | 'evaluated' | 'confirmed' | string
  version: number
  /** 逐日卡片（读取时现算；key = 日期 ISO） */
  cards?: Record<string, WeekPlanCard>
  /** 最近一次评估（含结构化冲突与硬阻断标记），随计划落库 */
  evaluation?: PlanEvaluation | null
}

/** 结构化冲突（2026-10-22）：可挂到日期卡片，hard_block 不可绕过 */
export interface PlanConflict {
  days: string[]
  kind: string
  severity: 'info' | 'warn' | 'block'
  hard_block: boolean
  message: string
  source: 'rule' | 'ai'
}

export interface PlanEvaluation {
  week_start: string
  findings: string[]
  suggestions: string[]
  /** 兼容旧字段：纯文本冲突（= structured_conflicts 的 message） */
  conflicts: string[]
  structured_conflicts: PlanConflict[]
  hard_block: boolean
  risk_notes: string[]
  evaluated_at: string
}

export interface UploadResult {
  session_id: string
  status: string
  garmin_workout_id?: number | null
  garmin_scheduled_id?: number | null
  message?: string | null
}

export interface SeriesResponse {
  metric: string
  points: { date: string; value: number }[]
}

/** 原始数据只读引用（REQ-DATA-004）：指向 raw 层的相对路径，不复制内容。 */
export interface RawDailyRef {
  source: string
  dataset: string
  date: string
  path: string
  fetched_at: string
}

/** 标准层详情（REQ-UI-003）：某日标准值 + 其原始出处。 */
export interface StandardDetail {
  date: string
  metrics: Record<string, number | string | null>
  source_refs: RawDailyRef[]
}

export interface Baseline {
  metric: string
  version: number
  window_days: number // 0 = 全历史（截至当日）；>0 = 滚动窗口天数（E23）
  mean: number
  std: number
  p10: number
  p90: number
  mature: boolean
  computed_at: string
  // 真实判定阈值（E23/E24）：低于/高于即触发；无该侧阈值时为 null
  threshold_low: number | null
  threshold_high: number | null
}

export interface MonthlyReport {
  month: string
  review: Record<string, unknown>
  adherence: number | null
  goal_progress: Record<string, unknown>
  long_term_adjustment?: string | null
}

export interface Plan12WeekItem {
  week_index: number
  week_start: string
  week_end: string
  phase: string
  focus: string
  target_load_factor: number
  evaluation_due: boolean
}

export interface Plan12Week {
  start_date: string
  weeks: Plan12WeekItem[]
  goal_weights: Record<string, number>
  /** 生成时有效策略版本关联（§11.1） */
  strategy?: Record<string, unknown> | null
}

/* ============================================================
 * 阶段③④：策略版本 / 消息 / 故障恢复 / 环境 / 报告关联
 * ============================================================ */

export type StrategyStatus = 'candidate' | 'verified' | 'active' | 'superseded' | 'invalid'
export type StrategyChangeType = 'aiOptimize' | 'manualRestore'
export type FaultSeverity = 'major' | 'general'
export type MessageCategory = 'strategyVersion' | 'risk' | 'taskFail' | 'needAction'
export type MessageImportance = 'normal' | 'high'

export interface StrategyVersion {
  strategy_version_id: string
  strategy_type: string
  version_number: string
  status: StrategyStatus
  change_type: StrategyChangeType
  parent_version_id?: string | null
  created_at: string
  effective_at?: string | null
  created_by: string
  change_reason: string
  evidence_summary: string
  expected_effect: string
  actual_effect?: string | null
  payload: Record<string, unknown>
  applicability_conditions: Record<string, unknown>
  safety_constraints: Record<string, unknown>
  validation_status: string
  validation_result: Record<string, unknown>
  limitations: string[]
  immutable: boolean
}

export interface StrategyEvent {
  event_id: string
  event_type: string
  triggered_at: string
  current_version_id?: string | null
  target_version_id?: string | null
  task_id?: string | null
  trigger_reason: string
  steps: Record<string, unknown>[]
  validation_result: Record<string, unknown>
  failure_reason?: string | null
  risk_state: string
  notify_status: string
  related_report_ref?: string | null
  follow_up_status: string
}

/** 三层版本差异（§9.6）：摘要 / 关键差异 / 完整差异 */
export interface FieldDiff {
  field: string
  from: unknown
  to: unknown
  changed: boolean
}

export interface StrategyDiff {
  summary: Record<string, unknown>
  key_changes: Record<string, unknown>
  full_diff: {
    scalar_fields: Record<string, FieldDiff>
    payload: Record<string, FieldDiff>
    meta_fields: Record<string, Record<string, FieldDiff>>
    limitations: FieldDiff
    validation_result: Record<string, FieldDiff>
    note: string
  }
}


export interface FaultRecoveryTask {
  fault_id: string
  severity: FaultSeverity
  impact_scope: string
  started_at: string
  fix_measures: string
  fixed_at?: string | null
  precheck_status: string
  precheck_report: Record<string, unknown>
  residual_risk: string[]
  needs_user_confirm: boolean
  user_confirmed: boolean
  user_confirmed_at?: string | null
  recovery_executed_at?: string | null
  final_result: string
}

export interface Message {
  message_id: string
  at: string
  category: MessageCategory
  importance: MessageImportance
  title: string
  body: string
  ref_type: string
  ref_id: string
  link: string
  is_read: boolean
  is_resolved: boolean
  send_status: string
}

export interface MessageListResponse {
  items: Message[]
  unread_count: number
}

/** 报告与策略版本关联块（§11.1） */
export interface StrategyLink {
  report_generated_at: string
  data_range: { start: string; end: string }
  rule_version: string
  model_version: string
  strategy_version_id?: string | null
  strategy_version_number?: string | null
  strategy_status?: string | null
  strategy_effective_at?: string | null
  risk_state: string
  is_recalculation: boolean
  recalc_of_generated_at?: string | null
}

/** 环境（天气）日数据（REQ §13.7/§13.8）。available=false 表示缺失，不得放宽阈值、不得编造。 */
export interface EnvironmentDaily {
  date: string
  location_id: string
  location_name: string
  lat?: string | null
  lon?: string | null
  available: boolean
  temp_c?: number | null
  humidity?: number | null
  precip_mm?: number | null
  weather_text: string
  is_rain: boolean
  is_snow: boolean
  wind_scale?: string | null
  source: string
  fetched_at?: string | null
}

/** 常用训练区域列表（REQ §13.7）；AI 建议需用户确认方可新增。 */
export interface LocationsResponse {
  locations: string[]
}

// —— 训练负荷 / ACWR 图表（GET /api/data/load-series，开发计划 4.11）——
export interface LoadSeriesDay {
  date: string
  total: number // 当日统一总负荷（标准分之和，可超 100）
  by_sport: Record<string, number> // 分运动类型日累计（堆叠柱各层）
  acwr: number | null // 慢性 EWMA 为 0 → null（不可计算，不伪造）
  acwr_state: string // undertraining | sweet_spot | elevated | danger | not_available
  acwr_note: string | null
  not_computable: { activity_id?: string; sport?: string; reason?: string }[]
  /** 需求 6.3：计入负荷但跨运动类型可比性未验证的类型 → verified/insufficient/unregistered */
  unverified_sports: Record<string, string>
}

export interface LoadSeriesHrZone {
  name: string
  low_pct: number
  high_pct: number
  low_bpm: number
  high_bpm: number
}

export interface LoadSeriesResponse {
  start: string
  end: string
  days: LoadSeriesDay[]
  hr_params: {
    max_hr: number
    rest_hr: number
    method: string
    hrr: number
    zones: LoadSeriesHrZone[]
  }
  reference_loads: Record<string, number>
}
