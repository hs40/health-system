/** 报告页公共**纯**数据层：类型、常量、索引/格式化（React hooks 见 hooks/reportQueries.ts）。 */
import type { DailyReport, MonthlyReport, WeeklyReport } from '../api/types'

/* ---------- 后端新增字段（与 backend/app/schemas 对齐） ---------- */

export interface GoalItem {
  label: string
  unit: string
  direction: 'up' | 'down'
  hint: string
  value: number | null
  baseline: number | null
  delta: number | null
  source: 'measured' | 'derived' | 'insufficient'
  baseline_start: string
  baseline_end: string
  range_start: string
  range_end: string
  limitations: string[]
}

export type GoalKey = 'cardio' | 'fatLoss' | 'muscleEndurance' | 'maxStrength'
export type GoalProgress = Record<GoalKey, GoalItem>

export const GOAL_ORDER: GoalKey[] = ['cardio', 'fatLoss', 'muscleEndurance', 'maxStrength']

export const GOAL_NAMES: Record<GoalKey, string> = {
  cardio: '心肺功能',
  fatLoss: '减脂减重',
  muscleEndurance: '肌肉耐力',
  maxStrength: '最大力量',
}

export interface PlanConflict {
  days: string[]
  kind: string
  severity: 'info' | 'warn' | 'block'
  hard_block: boolean
  message: string
  source: 'rule' | 'ai'
}

export const CONFLICT_KIND_LABEL: Record<string, string> = {
  recovery_gap: '恢复间隔',
  night_shift: '夜班影响',
  load_spike: '负荷尖峰',
  sport_conflict: '类型冲突',
  goal_mismatch: '目标偏差',
}

/* ---------- 索引 ---------- */

export interface IndexItem {
  key: string
  date: string
  week_end?: string
  version: number
}

export function markedSet(items: IndexItem[] | undefined): Set<string> {
  return new Set((items ?? []).map((i) => i.key))
}

export function goalsOf(
  report: DailyReport | WeeklyReport | MonthlyReport | undefined,
): GoalProgress | null {
  const raw = (report as { goal_progress?: unknown } | undefined)?.goal_progress
  const g = (raw as Record<string, unknown> | undefined)?.['goals']
  if (!g || typeof g !== 'object') return null
  return g as GoalProgress
}

/** 安全取出 Record 里的数值/文本，供 Metric 使用 */
export function num(v: unknown, digits = 1, fallback = '—'): string | number {
  if (v === null || v === undefined || v === '') return fallback
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return fallback
  return Number.isInteger(n) ? n : Number(n.toFixed(digits))
}

/** 取有限数值，否则 null（图表/判空用；区别于 `num()` 的格式化回退）。 */
export function numOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

export function txt(v: unknown, fallback = '—'): string {
  if (v === null || v === undefined || v === '') return fallback
  return String(v)
}

/* ---------- 生成时点 ---------- */

/** 周报：每周一 08:00 生成上一周。返回 { due: 是否已到生成时点, text } */
export function weeklyGenState(weekStart: string, now = new Date()): { due: boolean; text: string } {
  const end = new Date(`${weekStart}T00:00:00`)
  end.setDate(end.getDate() + 7) // 下周一
  end.setHours(8, 0, 0, 0)
  const due = now >= end
  const pad = (n: number) => String(n).padStart(2, '0')
  const text = `${end.getMonth() + 1}/${pad(end.getDate())} 周一 08:00`
  return { due, text }
}

/** 月报：每月 1 日 08:00 生成上一月。 */
export function monthlyGenState(month: string, now = new Date()): { due: boolean; text: string } {
  const [y, m] = month.split('-').map(Number)
  const dueDate = new Date(y, m, 1, 8, 0, 0) // 下月 1 日 08:00
  const due = now >= dueDate
  const pad = (n: number) => String(n).padStart(2, '0')
  return { due, text: `${dueDate.getFullYear()}/${pad(dueDate.getMonth() + 1)}/01 08:00` }
}
