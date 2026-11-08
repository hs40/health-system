/** 周计划卡片与处方共用的类型与文案格式化。 */
import type { TrainingSession } from '../../../api/types'

/**
 * 完整处方会话。
 *
 * 后端已把 ``TrainingSession`` 注册为独立 schema（v3.4.2），字段涵盖
 * warmup/main/cooldown/strength/rationale/target_type/distance_m 等全部处方内容；
 * 此前手写的 ``FullSession`` 是它的子集镜像，已删除以免再漂移。
 */
export type FullSession = TrainingSession

/** 计划状态中文名（后端 status 枚举） */
export const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  evaluated: '已评估',
  confirmed: '已确认',
}

/** 卡片「时长」文案 */
export function durationText(s?: TrainingSession | null): string {
  if (!s?.duration_min) return '—'
  return `${s.duration_min} min`
}

/** 卡片「训练目标」文案：力量日不套用心率区间说法（否则出现「力量 · 目标 Z2 区间有氧」） */
export function goalText(s: TrainingSession): string {
  if (s.sport === 'strength') return '力量与肌耐力（按组次）'
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
