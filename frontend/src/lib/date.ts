/** 本地日期工具（避免 UTC 偏移导致跨日错位）。 */

export function fmtDate(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

export function parseISO(iso: string): Date {
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(y, (m ?? 1) - 1, d ?? 1)
}

export function todayISO(): string {
  return fmtDate(new Date())
}

export function addDays(iso: string, n: number): string {
  const d = parseISO(iso)
  d.setDate(d.getDate() + n)
  return fmtDate(d)
}

/** 返回 iso 所在周的周一（周一为一周之始，D17）。 */
export function mondayOf(iso: string): string {
  const d = parseISO(iso)
  const back = (d.getDay() + 6) % 7 // 周一=0
  d.setDate(d.getDate() - back)
  return fmtDate(d)
}

export const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
