/** 周计划卡片与处方共用的类型与文案格式化。 */
import type { TrainingSessionBrief } from '../../../api/types'

/** 计划状态中文名（后端 status 枚举） */
export const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  evaluated: '已评估',
  confirmed: '已确认',
}

/** 需要完整处方的会话形态（后端 TrainingSession） */
export interface FullSession extends TrainingSessionBrief {
  warmup?: Record<string, unknown>[]
  main?: Record<string, unknown>[]
  cooldown?: Record<string, unknown>[]
  strength?: {
    exercise: string
    sets: number
    reps: number
    load_kg?: number | null
    rest_sec: number
  }[]
  rationale?: string | null
  target_type?: string | null
  distance_m?: number | null
}

/** 卡片「时长」文案 */
export function durationText(s?: TrainingSessionBrief | null): string {
  if (!s?.duration_min) return '—'
  return `${s.duration_min} min`
}

/** 卡片「训练目标」文案：力量日不套用心率区间说法（否则出现「力量 · 目标 Z2 区间有氧」） */
export function goalText(s: TrainingSessionBrief): string {
  if (s.sport === 'strength' || s.sport === 'strength_training') return '力量与肌耐力（按组次）'
  return s.target_hr_zone ? `${s.target_hr_zone} 区间有氧` : '按处方执行'
}

/** target_type 是 pace | heartRate | open，直接展示会泄漏英文枚举 */
const TARGET_TYPE_LABEL: Record<string, string> = {
  pace: '配速',
  heartRate: '心率',
  open: '未指定',
}

export function targetTypeLabel(t?: string | null): string {
  if (!t) return '未指定'
  return TARGET_TYPE_LABEL[t] ?? t
}
