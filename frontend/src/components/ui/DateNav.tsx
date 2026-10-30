/** 日期导航：前后翻页 + 「今天」+ 日历打点（哪些日期已有报告）。 */
import { useMemo, useState } from 'react'
import clsx from 'clsx'
import { addDays, fmtDate, mondayOf, parseISO, todayISO, WEEKDAYS } from '../../lib/date'

export interface DateNavProps {
  /** daily: YYYY-MM-DD；weekly: 周一；monthly: YYYY-MM */
  mode: 'daily' | 'weekly' | 'monthly'
  value: string
  onChange: (next: string) => void
  /** 已有报告的 key 集合（日历打点） */
  marked?: Set<string>
  /** 未生成时点的提示（如「将于 10/26 08:00 生成」） */
  notice?: string
}

function shift(mode: DateNavProps['mode'], value: string, dir: -1 | 1): string {
  if (mode === 'daily') return addDays(value, dir)
  if (mode === 'weekly') return addDays(value, dir * 7)
  const [y, m] = value.split('-').map(Number)
  const d = new Date(y, m - 1 + dir, 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function current(mode: DateNavProps['mode']): string {
  const t = todayISO()
  if (mode === 'daily') return t
  if (mode === 'weekly') return mondayOf(t)
  return t.slice(0, 7)
}

function labelOf(mode: DateNavProps['mode'], value: string): string {
  if (mode === 'daily') {
    const d = parseISO(value)
    return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${WEEKDAYS[(d.getDay() + 6) % 7]}`
  }
  if (mode === 'weekly') {
    const end = addDays(value, 6)
    return `${value.slice(5)} ~ ${end.slice(5)}（周一至周日）`
  }
  const [y, m] = value.split('-').map(Number)
  return `${y}年${m}月`
}

export function DateNav({ mode, value, onChange, marked, notice }: DateNavProps) {
  const [open, setOpen] = useState(false)
  const isCurrent = value === current(mode)

  return (
    <div className="flex flex-wrap items-center gap-2">
      <div
        className="flex items-stretch overflow-hidden rounded-[var(--hs-r-sm)] border"
        style={{ borderColor: 'var(--hs-border-strong)', background: 'var(--hs-surface)' }}
      >
        <button
          type="button"
          onClick={() => onChange(shift(mode, value, -1))}
          className="px-2.5 text-[var(--hs-fs-small)] font-bold transition hover:bg-[var(--hs-surface-2)]"
          style={{ color: 'var(--hs-ink-3)' }}
          title="上一期"
          aria-label="上一期"
        >
          ←
        </button>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="relative min-w-[168px] border-x px-3 py-1.5 text-[var(--hs-fs-small)] font-semibold"
          style={{ borderColor: 'var(--hs-border)', color: 'var(--hs-ink)' }}
        >
          {labelOf(mode, value)}
          {marked && marked.size > 0 && (
            <span
              className="ml-1.5 inline-block h-1.5 w-1.5 rounded-full align-middle"
              style={{ background: 'var(--hs-accent)' }}
              title={`已有 ${marked.size} 份报告`}
            />
          )}
        </button>
        <button
          type="button"
          onClick={() => onChange(shift(mode, value, 1))}
          className="px-2.5 text-[var(--hs-fs-small)] font-bold transition hover:bg-[var(--hs-surface-2)]"
          style={{ color: 'var(--hs-ink-3)' }}
          title="下一期"
          aria-label="下一期"
        >
          →
        </button>
      </div>

      <button
        type="button"
        onClick={() => {
          onChange(current(mode))
          setOpen(false)
        }}
        disabled={isCurrent}
        className="hs-btn"
        style={isCurrent ? { opacity: 0.4 } : undefined}
      >
        今天
      </button>

      {notice && (
        <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
          {notice}
        </span>
      )}

      {open && marked && (
        <MiniCalendar
          mode={mode}
          value={value}
          marked={marked}
          onPick={(v) => {
            onChange(v)
            setOpen(false)
          }}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  )
}

/** 日历弹层：有报告的日期带圆点（需要 GET /api/reports/index） */
function MiniCalendar({
  mode,
  value,
  marked,
  onPick,
  onClose,
}: {
  mode: DateNavProps['mode']
  value: string
  marked: Set<string>
  onPick: (v: string) => void
  onClose: () => void
}) {
  const cursor0 = mode === 'monthly' ? `${value}-01` : mode === 'weekly' ? addDays(value, 3) : value
  const [cursor, setCursor] = useState(cursor0)
  const d = parseISO(cursor)
  const first = new Date(d.getFullYear(), d.getMonth(), 1)
  const daysInMonth = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate()
  const lead = (first.getDay() + 6) % 7

  const cells = useMemo(() => {
    const out: (string | null)[] = Array.from({ length: lead }, () => null)
    for (let i = 1; i <= daysInMonth; i++) {
      out.push(fmtDate(new Date(d.getFullYear(), d.getMonth(), i)))
    }
    return out
  }, [d.getFullYear(), d.getMonth(), daysInMonth, lead])

  const pickFor = (iso: string) => (mode === 'weekly' ? mondayOf(iso) : mode === 'monthly' ? iso.slice(0, 7) : iso)
  const isMarked = (iso: string) => marked.has(pickFor(iso))
  const isActive = (iso: string) => pickFor(iso) === value

  return (
    <div className="relative w-full">
      <div
        className="absolute left-0 top-1 z-20 w-[268px] rounded-[var(--hs-r)] border p-3 shadow-lg"
        style={{ borderColor: 'var(--hs-border-strong)', background: 'var(--hs-surface)' }}
      >
        <div className="mb-2 flex items-center justify-between">
          <button
            className="hs-btn px-2 py-0.5"
            onClick={() => setCursor(fmtDate(new Date(d.getFullYear(), d.getMonth() - 1, 1)))}
          >
            ←
          </button>
          <span className="text-[var(--hs-fs-small)] font-bold" style={{ color: 'var(--hs-ink)' }}>
            {d.getFullYear()}年{d.getMonth() + 1}月
          </span>
          <button
            className="hs-btn px-2 py-0.5"
            onClick={() => setCursor(fmtDate(new Date(d.getFullYear(), d.getMonth() + 1, 1)))}
          >
            →
          </button>
        </div>
        <div className="grid grid-cols-7 gap-0.5 text-center text-[10px]" style={{ color: 'var(--hs-ink-4)' }}>
          {WEEKDAYS.map((w) => (
            <div key={w} className="py-0.5 font-semibold">
              {w.replace('周', '')}
            </div>
          ))}
        </div>
        <div className="grid grid-cols-7 gap-0.5">
          {cells.map((iso, i) => {
            if (!iso) return <div key={`e${i}`} />
            const active = isActive(iso)
            const mk = isMarked(iso)
            return (
              <button
                key={iso}
                onClick={() => onPick(pickFor(iso))}
                className={clsx(
                  'relative rounded py-1 text-[var(--hs-fs-micro)] transition',
                  active ? 'font-bold' : 'hover:bg-[var(--hs-surface-2)]',
                )}
                style={{
                  background: active ? 'var(--hs-accent)' : undefined,
                  color: active ? '#fff' : mk ? 'var(--hs-ink)' : 'var(--hs-ink-3)',
                }}
              >
                {Number(iso.slice(8))}
                {mk && (
                  <span
                    className="absolute bottom-0.5 left-1/2 h-1 w-1 -translate-x-1/2 rounded-full"
                    style={{ background: active ? '#fff' : 'var(--hs-accent)' }}
                  />
                )}
              </button>
            )
          })}
        </div>
        <div className="mt-2 flex items-center justify-between">
          <span className="text-[10px]" style={{ color: 'var(--hs-ink-4)' }}>
            ● 已有报告
          </span>
          <button className="hs-btn px-2 py-0.5" onClick={onClose}>
            收起
          </button>
        </div>
      </div>
    </div>
  )
}
