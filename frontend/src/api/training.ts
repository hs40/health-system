/** 运动训练页面 API 契约（三期 §7/§8）。与后端 `api/activities.py` 对齐。 */
import { api } from './client'

export interface CalendarItem {
  seq: number
  activity_id: string
  type: string
  label: string
  emoji: string
  color: string
  template: string
}

export interface CalendarDay {
  date: string
  items: CalendarItem[]
}

export interface CalendarType {
  key: string
  label: string
  emoji: string
  color: string
  template: string
}

export interface CalendarResponse {
  ym: string
  filter: string
  prev_ym: string
  next_ym: string
  types: CalendarType[]
  days: CalendarDay[]
}

export interface MetricRow {
  label: string
  value: string
}

export interface HrZoneRow {
  name: string
  low_bpm: number
  high_bpm: number
  seconds: number
}

/** Q8：逐秒 record + 项目 Karvonen 五区重算；缺失秒数单独披露，不补 0。 */
export interface HrZoneStats {
  zones: HrZoneRow[]
  counted_seconds: number
  below_zone_seconds: number
  missing_seconds: number
  method: string
  max_hr: number
  rest_hr: number
}

export interface HrPoint {
  t: string | null
  hr: number | null
}

/** 跑步/室内有氧的 lap 分段与 HIIT 的动作段共用（字段按模板取用）。 */
export interface SegmentRow {
  index?: number
  kind?: 'exercise' | 'rest'
  label?: string
  key?: string
  name_zh?: string
  intensity?: string | null
  duration_sec: number
  distance_m?: number
  avg_hr?: number | null
  max_hr?: number | null
  reps?: number
  weight_kg?: number
}

export interface TrainingEffect {
  label: string
  label_raw: string | null
  aerobic: string | null
  anaerobic: string | null
}

export interface StrengthTimeline {
  segments: SegmentRow[]
  active_seconds: number
  rest_seconds: number
  formal_exercise_count: number
  formal_exercises: string[]
}

/** Q11：肌群 → 本次涉及动作（表格行）。 */
export interface MuscleRow {
  code: string
  zh: string
  en: string
  primary_moves: string[]
  secondary_moves: string[]
}

/** Q10：肌肉图分区高亮级别。 */
export interface MuscleRegion {
  code: string
  zh: string
  en: string
  level: 'primary' | 'secondary' | 'none'
}

export interface SwimmingData {
  distance_m: number | null
  pool_length_m: number | null
  strokes: number | null
  active_lengths: number | null
  average_swolf: number | null
  stroke_types: string[]
  lengths: {
    stroke: string | null
    duration_sec: number
    strokes: number | null
    avg_speed: number | null
    length_type: string | null
  }[]
}

export type DetailTemplate =
  | 'running'
  | 'indoor_cardio'
  | 'hiit'
  | 'strength'
  | 'swimming'
  | 'other'

export interface ActivityDetail {
  activity_id: string
  type: string
  label: string
  template: DetailTemplate
  name: string
  duration_sec: number
  metrics: MetricRow[]
  hr_zones: HrZoneStats
  hr_series: HrPoint[]
  training_effect: TrainingEffect
  segments: SegmentRow[]
  timeline?: StrengthTimeline
  muscle_table?: MuscleRow[]
  muscles?: MuscleRegion[]
  swimming?: SwimmingData
}

export interface DetailResponse {
  date: string
  seq: number
  total: number
  has_training: boolean
  prev_date: string
  next_date: string
  activity: ActivityDetail | null
}

export function fetchCalendar(ym: string, type: string): Promise<CalendarResponse> {
  const q = new URLSearchParams({ ym, type })
  return api.get<CalendarResponse>(`/api/activities/calendar?${q.toString()}`)
}

export function fetchDetail(date: string, seq: number): Promise<DetailResponse> {
  const q = new URLSearchParams({ date, seq: String(seq) })
  return api.get<DetailResponse>(`/api/activities/detail?${q.toString()}`)
}
