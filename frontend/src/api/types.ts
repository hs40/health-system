/**
 * 前端类型：**由后端 OpenAPI 契约派生**（ARCHITECTURE_REVIEW A.9）。
 *
 * 怎么用
 * ------
 * 1. 绝大多数类型直接取自生成文件：
 *    ``components['schemas']['DailyReport']``
 * 2. 生成器表达不了、或前端独有（非后端返回）的类型，在本文件手写。
 *
 * 为什么不再手抄一遍
 * ------------------
 * 原先这些 interface 是手工维护的。实测比对（v3.4.2）发现手写字段**没有多余**，
 * 但**漏了 3 处后端已有字段**（`MonthlyReport.algo_versions` /
 * `.strategy` / `.strategy_changes`、`WeeklyPlan.strategy`、`WeeklyReport.algo_versions`）。
 * 漏字段不会让 tsc 报错——它只会让 `obj.that_field` 变成类型错误，
 * 于是开发者顺手加个 `as any` 绕过去，漂移就固化下来了。
 * 派生后这类漂移在 `npm run gen:api` 之后立刻消失。
 *
 * 构建顺序很重要
 * --------------
 * `schema.d.ts` **不入库**（`.gitignore`），必须先 `npm run gen:api` 再 `tsc`。
 * CI 里顺序反了会因「找不到模块」直接失败——这是刻意的，
 * 让「忘了生成」在构建期暴露，而不是在运行时拿到 undefined。
 */

import type { components } from './schema'

type S = components['schemas']

// ============================================================
// 枚举：后端用 Literal 定义，直接派生保证与后端同源
// ============================================================

/** 四级优先级（2026-10-22 取代三级 RiskLevel） */
export type PriorityLevel = S['PriorityLevel']
/** 三态数据质量关卡：A 全景判定 / B 纯负荷降级 / C 安全兜底 */
export type DataState = S['DataState']
export type QualityGrade = S['QualityGrade']

/**
 * 任务状态。
 *
 * 后端 schema 里是 ``str`` 而非枚举（历史哨兵值 ``unknown`` 只在读取旧数据时出现，
 * 不写进接口契约），所以这里保留手写联合类型。
 */
export type TaskStatus = 'success' | 'partial' | 'failed' | 'retry' | 'unknown'

// ============================================================
// 后端 Schema 的直接映射（随接口契约自动更新）
// ============================================================

export type DepStatus = S['DepStatus']
export type HealthResponse = S['HealthResponse']
export type TaskRun = S['TaskRun']
export type AuditEvent = S['AuditEvent']
export type RiskAssessment = S['RiskAssessment']
export type DataQualityReport = S['DataQualityReport']
export type DailyReport = S['DailyReport']
export type WeeklyReport = S['WeeklyReport']
export type WeeklyPlan = S['WeeklyPlan']
export type PlanConflict = S['PlanConflict']
export type PlanEvaluation = S['PlanEvaluation']
export type UploadResult = S['UploadResult']
export type RawDailyRef = S['RawDailyRef']
export type StandardDetail = S['StandardDetail']
export type Baseline = S['Baseline']
export type MonthlyReport = S['MonthlyReport']
export type Plan12Week = S['Plan12Week']
export type StrategyVersion = S['StrategyVersion']
export type StrategyEvent = S['StrategyEvent']
export type StrategyDiff = S['StrategyDiff']
export type FaultRecoveryTask = S['FaultRecoveryTask']
export type Message = S['Message']
export type MessageListResponse = S['MessageListResponse']
export type StrategyStatus = S['StrategyStatus']
export type FaultSeverity = S['FaultSeverity']
export type MessageCategory = S['MessageCategory']
export type MessageImportance = S['MessageImportance']
export type SeriesResponse = S['SeriesResponse']
export type LoadSeriesResponse = S['LoadSeriesResponse']
export type LoadSeriesDay = S['LoadSeriesDay']
/** 心率参数（Karvonen 五区），日报与负荷图共用同一份口径（需求 5.4）。 */
export type HrParamsView = S['HrParamsView']
export type EnvironmentResponse = S['EnvironmentResponse']
export type Plan12WeekItem = S['Plan12WeekItem']
export type WeekPlanDay = S['WeekPlanDay']
export type WeekPlanCard = S['WeekPlanCard']
export type TrainingSession = S['TrainingSession']

/**
 * 策略版本变化。
 *
 * 后端 `StrategyChange` 未注册为独立 schema——周报/月报里内联成
 * ``Record<string, unknown>[]``。这里的字段取自策略事件的实际产出，
 * 只保留前端展示所需的子集；未声明的字段请用 ``Record`` 的索引签名读取。
 */
export interface StrategyChange {
  event_id: string
  event_type: string
  changed_at: string
  version_number?: string | null
  change_type?: string | null
  reason?: string | null
  effective_at?: string | null
  [k: string]: unknown
}

// ============================================================
// 手写类型：后端把它内联在响应里（未注册为独立 component），或前端独有
// ============================================================

/** 策略字段级差异（§11.4）。后端内联在 StrategyDiff 里。 */
export interface FieldDiff {
  field: string
  before: unknown
  after: unknown
}

/** 前后策略版本之间的引用。后端内联。 */
export interface StrategyLink {
  version_number: string
  created_at: string
}
