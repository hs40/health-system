/**
 * 运动训练月历页（三期 §7.2 / §7.3 / §7.7）。
 *
 * 状态全部由 URL query 驱动：ym（月份）+ type（筛选）。从详情返回时带上
 * 这两个参数即可恢复原月份与筛选；重新从导航进入（无 query）默认全部类型。
 */
import { useMemo } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { WEEKDAYS, todayISO } from '../../lib/date'
import { useTrainingCalendar } from '../../hooks/trainingQueries'
import type { CalendarDay } from '../../api/training'

const WEEK_HEAD = WEEKDAYS.map((w) => w.replace('周', ''))

function currentYm(): string {
  return todayISO().slice(0, 7)
}

function shiftYm(ym: string, delta: number): string {
  const [y, m] = ym.split('-').map(Number)
  const d = new Date(y, (m ?? 1) - 1 + delta, 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

export default function TrainingPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const ym = params.get('ym') ?? currentYm()
  const type = params.get('type') ?? 'all'
  const { data, isLoading, isError } = useTrainingCalendar(ym, type)
  const today = todayISO()

  const setParam = (next: Record<string, string>) => {
    const p = new URLSearchParams(params)
    Object.entries(next).forEach(([k, v]) => p.set(k, v))
    setParams(p)
  }

  /** 进入详情时把日历状态（ym/type）一并带过去，供 §7.7 返回恢复。 */
  const goDetail = (date: string, seq: number) => {
    const p = new URLSearchParams({ date, seq: String(seq), ym, type })
    navigate(`/training/detail?${p.toString()}`)
  }

  const cells = useMemo<(CalendarDay | null)[]>(() => {
    if (!data) return []
    const [y, m] = ym.split('-').map(Number)
    const lead = (new Date(y, (m ?? 1) - 1, 1).getDay() + 6) % 7
    return [...Array.from({ length: lead }, () => null), ...(data.days ?? [])]
  }, [data, ym])

  const [year, month] = ym.split('-').map(Number)

  return (
    <div className="mx-auto px-4 sm:px-6" style={{ maxWidth: 'var(--hs-content-max)' }}>
      <div className="mb-4">
        <h1 className="hs-section-title">运动训练</h1>
        <p className="mt-1 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
          按 Garmin 实际训练日期查看历史训练。点日期或运动标识进入单次详情。
        </p>
      </div>

      <div className="hs-card space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="hs-btn"
            onClick={() => setParam({ ym: data?.prev_ym ?? shiftYm(ym, -1) })}
            aria-label="上一个月"
          >
            ← 上月
          </button>
          <button
            type="button"
            className="hs-btn"
            onClick={() => setParam({ ym: data?.next_ym ?? shiftYm(ym, 1) })}
            aria-label="下一个月"
          >
            下月 →
          </button>
          <button type="button" className="hs-btn" onClick={() => setParam({ ym: currentYm() })}>
            今天
          </button>
          <input
            type="month"
            className="hs-input"
            value={ym}
            onChange={(e) => e.target.value && setParam({ ym: e.target.value })}
            aria-label="选择年月"
          />
          <span className="text-[var(--hs-fs-small)] font-semibold" style={{ color: 'var(--hs-ink)' }}>
            {year} 年 {month} 月
          </span>
          <div className="ml-auto flex items-center gap-2">
            <label className="hs-label" htmlFor="training-type-filter">
              运动类型
            </label>
            <select
              id="training-type-filter"
              className="hs-input"
              value={type}
              onChange={(e) => setParam({ type: e.target.value })}
            >
              <option value="all">全部运动类型</option>
              {data?.types?.map((t) => (
                <option key={t.key} value={t.key}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {isError && (
          <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-p1)' }}>
            日历数据不可用，请稍后重试。
          </p>
        )}
        {isLoading && <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>加载中…</p>}

        <div className="grid grid-cols-7 gap-1.5">
          {WEEK_HEAD.map((w) => (
            <div key={w} className="hs-label py-1 text-center">
              {w}
            </div>
          ))}
          {cells.map((day, i) => {
            if (!day) return <div key={`e${i}`} />
            const items = day.items ?? []
            const future = day.date > today
            const clickable = !future && items.length > 0
            return (
              <div
                key={day.date}
                className="min-h-[78px] rounded-[var(--hs-r-sm)] border p-1.5"
                style={{
                  borderColor: day.date === today ? 'var(--hs-accent)' : 'var(--hs-border)',
                  background: clickable ? 'var(--hs-surface)' : 'var(--hs-surface-2)',
                  cursor: clickable ? 'pointer' : 'default',
                }}
                onClick={clickable ? () => goDetail(day.date, 1) : undefined}
              >
                <button
                  type="button"
                  disabled={!clickable}
                  className="text-[var(--hs-fs-micro)] font-semibold"
                  style={{ color: future ? 'var(--hs-ink-4)' : 'var(--hs-ink)' }}
                  aria-label={`${day.date}，${items.length} 次训练`}
                  onClick={(e) => {
                    e.stopPropagation()
                    if (clickable) goDetail(day.date, 1)
                  }}
                >
                  {Number(day.date.slice(8))}
                </button>
                <div className="mt-1 flex flex-wrap gap-1">
                  {items.map((item) => (
                    <button
                      key={item.activity_id}
                      type="button"
                      className="rounded-full border px-1 text-[13px] leading-5 transition hover:brightness-95"
                      style={{ borderColor: item.color, background: `${item.color}14` }}
                      title={item.label}
                      aria-label={`${item.label}：查看第 ${item.seq} 次训练`}
                      onClick={(e) => {
                        e.stopPropagation()
                        goDetail(day.date, item.seq)
                      }}
                    >
                      {item.emoji}
                    </button>
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
