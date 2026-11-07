/** 周计划表头：星期一…日 + 日期 + 夜班开关（固定行，不随拖动改变）。 */
import type { WeekPlanDay } from '../../../api/types'
import { parseISO, todayISO, WEEKDAYS } from '../../../lib/date'

interface Props {
  days: WeekPlanDay[]
  onToggleNight: (date: string) => void
}

export function WeekHeader({ days, onToggleNight }: Props) {
  const today = todayISO()

  return (
    <div
      className="grid border-b pb-2"
      style={{
        borderColor: 'var(--hs-border)',
        gridTemplateColumns: 'repeat(7, minmax(0, 1fr))',
        gap: 'var(--hs-gap-sm)',
      }}
    >
      {days.map((d, i) => {
        const isToday = d.date === today
        const parsed = parseISO(d.date)
        return (
          <div key={`h-${d.date}`} className="min-w-0 px-1">
            <div
              className="whitespace-nowrap text-[var(--hs-fs-h3)] font-bold leading-tight"
              style={{ color: isToday ? 'var(--hs-accent)' : 'var(--hs-ink)' }}
            >
              {WEEKDAYS[i] ?? d.date}
            </div>
            <div
              className="whitespace-nowrap text-[var(--hs-fs-micro)] font-semibold"
              style={{ color: 'var(--hs-ink-3)' }}
            >
              {parsed.getMonth() + 1}月{parsed.getDate()}日
            </div>
            <label
              className="mt-1 inline-flex cursor-pointer items-center gap-1 text-[var(--hs-fs-micro)]"
              style={{ color: d.is_night_shift ? 'var(--hs-accent-ink)' : 'var(--hs-ink-4)' }}
              title="夜班日：次日安排高强度会被规则阻断"
            >
              <input
                type="checkbox"
                checked={d.is_night_shift}
                onChange={() => onToggleNight(d.date)}
                className="h-3 w-3"
              />
              夜班
            </label>
          </div>
        )
      })}
    </div>
  )
}
