/** 日报区块共用的格式化工具。 */
import { numOrNull } from '../../../lib/reports'

/** 秒 → `8h05m`；无法解析时返回 `—`。 */
export function hhmm(sec: unknown): string {
  const s = numOrNull(sec)
  if (s === null) return '—'
  const h = Math.floor(s / 3600)
  const m = Math.round((s % 3600) / 60)
  return `${h}h${String(m).padStart(2, '0')}m`
}
