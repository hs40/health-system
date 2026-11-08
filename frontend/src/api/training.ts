/**
 * 运动训练页面 API 契约（三期 §7/§8）。
 *
 * 类型**由后端 OpenAPI 派生**（ARCHITECTURE_REVIEW A.9），不再手抄一整套
 * interface：2026-11-08 后端为修复两张心率图空白补了 ``hr_data_state``，
 * 手抄契约对此完全不知情，前端只能一直显示「本次无逐秒心率数据」。
 * 现在 ``npm run gen:api`` 之后后端契约的变化会立刻在 tsc 暴露。
 *
 * ``schema.d.ts`` 不入库（.gitignore），构建顺序必须是 gen:api → tsc → vite build。
 */
import { api } from './client'
import type { components } from './schema'

type S = components['schemas']

export type CalendarItem = S['CalendarItem']
export type CalendarDay = S['CalendarDay']
export type CalendarType = S['CalendarType']
export type CalendarResponse = S['CalendarResponse']
export type MetricRow = S['MetricRow']
export type HrZoneRow = S['HrZoneRow']
/** Q8：逐秒 record + 项目 Karvonen 五区重算；缺失秒数单独披露，不补 0。 */
export type HrZoneStats = S['HrZoneStats']
export type HrPoint = S['HrPoint']
/** 跑步/室内有氧的 lap 分段与 HIIT/力量动作段共用（字段按模板取用）。 */
export type SegmentRow = S['SegmentRow']
export type TrainingEffect = S['TrainingEffect']
export type StrengthTimeline = S['StrengthTimeline']
export type MuscleRow = S['MuscleRow']
export type MuscleRegion = S['MuscleRegion']
export type SwimmingData = S['SwimmingData']
/** 三态：ok / no_hr_samples / fit_missing（后端内联在 ActivityDetail 里）。 */
export type HrDataState = S['ActivityDetail']['hr_data_state']
export type DetailTemplate = S['ActivityDetail']['template']
export type ActivityDetail = S['ActivityDetail']
export type DetailResponse = S['DetailResponse']

export function fetchCalendar(ym: string, type: string): Promise<CalendarResponse> {
  const q = new URLSearchParams({ ym, type })
  return api.get<CalendarResponse>(`/api/activities/calendar?${q.toString()}`)
}

export function fetchDetail(date: string, seq: number): Promise<DetailResponse> {
  const q = new URLSearchParams({ date, seq: String(seq) })
  return api.get<DetailResponse>(`/api/activities/detail?${q.toString()}`)
}
