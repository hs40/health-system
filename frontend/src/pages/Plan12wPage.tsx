/**
 * 12 周计划页（需求 §11.7）。
 *
 * - W1–W12 纵向排列，当前周高亮（「突出近期 12 周」）。
 * - **每周可展开查看该周 7 天详细计划**（点击行 → 读取该周周计划；未生成时如实说明）。
 * - 展示生成时关联的策略版本（§11.1）。
 * - 新旧计划比较依赖后端保留 12 周计划版本历史，当前库仅保留最新一版，
 *   故本页**不做假对比**；该限制登记在验收清单 T-5。
 */
import { Fragment, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import type { Plan12Week, WeeklyPlan } from '../api/types'
import { Chip } from '../components/ui/badges'
import { Page, Section } from '../components/ui/layout'
import { WEEKDAYS, addDays, mondayOf, todayISO } from '../lib/date'
import { sportLabel } from '../lib/sports'

const PHASE: Record<string, { label: string; tone: 'neutral' | 'p3' | 'p2' | 'p4' }> = {
  base: { label: '基础期', tone: 'neutral' },
  build: { label: '进展期', tone: 'p3' },
  peak: { label: '峰值期', tone: 'p2' },
  deload: { label: '减量期', tone: 'p4' },
}

const GOAL_LABEL: Record<string, string> = {
  cardio: '心肺',
  fatLoss: '减脂',
  muscleEndurance: '肌肉耐力',
  maxStrength: '最大力量',
  endurance: '耐力',
  strength: '力量',
  recovery: '恢复',
}

export default function Plan12wPage() {
  const qc = useQueryClient()
  const [openWeek, setOpenWeek] = useState<string | null>(null)

  const plan = useQuery<Plan12Week, ApiError>({
    queryKey: ['plan12w'],
    queryFn: () => api.get<Plan12Week>('/api/reports/plan12w'),
    retry: false,
  })

  const roll = useMutation<Plan12Week, ApiError>({
    mutationFn: () => api.post<Plan12Week>('/api/reports/plan12w/generate', {}),
    onSuccess: () => {
      setOpenWeek(null)
      void qc.invalidateQueries({ queryKey: ['plan12w'] })
    },
  })

  const d = plan.data
  const currentMonday = mondayOf(todayISO())
  const version = (d?.strategy?.['strategy_version_number'] as string | undefined) ?? null

  return (
    <Page className="space-y-[var(--hs-gap)] py-[var(--hs-gap)]">
      <div className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-[var(--hs-fs-h1)] font-bold" style={{ color: 'var(--hs-ink)' }}>
          12 周计划
        </h1>
        {d && (
          <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
            起点 {d.start_date}（周一） · 共 {(d.weeks ?? []).length} 周 · 每 4 周评估
            {version ? ` · 策略版本 ${version}` : ''}
          </span>
        )}
        <button
          className="hs-btn hs-btn-primary ml-auto"
          onClick={() => roll.mutate()}
          disabled={roll.isPending}
        >
          {roll.isPending ? '滚动更新中…' : '滚动更新'}
        </button>
      </div>

      {plan.isLoading && <div className="hs-card hs-skeleton h-32" />}

      {plan.error && (
        <div className="hs-card">
          <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-p1)' }}>
            读取失败：[{plan.error.code}] {plan.error.message}
          </p>
        </div>
      )}

      {d && (
        <>
          <Section title="目标权重" hint="训练资源分配权重；四项长期目标优先级固定为 心肺 → 减脂 → 肌肉耐力 → 最大力量">
            <div className="flex flex-wrap gap-x-6 gap-y-2">
              {Object.entries(d.goal_weights ?? {}).map(([k, v]) => (
                <div key={k} className="flex items-baseline gap-1.5">
                  <span className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
                    {GOAL_LABEL[k] ?? k}
                  </span>
                  <span
                    className="text-[var(--hs-fs-h3)] font-bold"
                    style={{ color: 'var(--hs-ink)', fontFamily: 'var(--hs-font-num)' }}
                  >
                    {(v * 100).toFixed(0)}%
                  </span>
                </div>
              ))}
            </div>
          </Section>

          <Section
            title="12 周周期化安排"
            hint="点击任一周展开该周 7 天计划；当前周高亮"
          >
            <div className="overflow-x-auto">
              <table className="w-full text-[var(--hs-fs-small)]">
                <thead>
                  <tr
                    className="text-left text-[var(--hs-fs-micro)]"
                    style={{ color: 'var(--hs-ink-4)', borderBottom: '1px solid var(--hs-border)' }}
                  >
                    <th className="py-2 pr-3 font-semibold">周次</th>
                    <th className="py-2 pr-3 font-semibold">起止</th>
                    <th className="py-2 pr-3 font-semibold">阶段</th>
                    <th className="py-2 pr-3 font-semibold">重点</th>
                    <th className="py-2 pr-3 text-right font-semibold">目标负荷</th>
                    <th className="py-2 pr-3 font-semibold">评估</th>
                    <th className="py-2 font-semibold" />
                  </tr>
                </thead>
                <tbody>
                  {(d.weeks ?? []).map((w) => {
                    const isCurrent = w.week_start === currentMonday
                    const open = openWeek === w.week_start
                    const phase = PHASE[w.phase] ?? { label: w.phase, tone: 'neutral' as const }
                    return (
                      <Fragment key={w.week_index}>
                        <tr
                          className="cursor-pointer transition"
                          style={{
                            borderBottom: '1px solid var(--hs-border)',
                            background: isCurrent
                              ? 'var(--hs-accent-soft)'
                              : open
                                ? 'var(--hs-surface-2)'
                                : undefined,
                          }}
                          onClick={() => setOpenWeek(open ? null : w.week_start)}
                          aria-expanded={open}
                        >
                          <td className="py-2.5 pr-3 font-semibold" style={{ color: 'var(--hs-ink)' }}>
                            W{w.week_index}
                            {isCurrent && (
                              <span
                                className="ml-1.5 text-[var(--hs-fs-micro)]"
                                style={{ color: 'var(--hs-accent-ink)' }}
                              >
                                本周
                              </span>
                            )}
                          </td>
                          <td
                            className="py-2.5 pr-3 whitespace-nowrap text-[var(--hs-fs-micro)]"
                            style={{ color: 'var(--hs-ink-3)' }}
                          >
                            {w.week_start.slice(5)} ~ {w.week_end.slice(5)}
                          </td>
                          <td className="py-2.5 pr-3">
                            <Chip tone={phase.tone}>{phase.label}</Chip>
                          </td>
                          <td className="py-2.5 pr-3" style={{ color: 'var(--hs-ink-2)' }}>
                            {w.focus}
                          </td>
                          <td
                            className="py-2.5 pr-3 text-right font-semibold"
                            style={{ color: 'var(--hs-ink)', fontFamily: 'var(--hs-font-num)' }}
                          >
                            {w.target_load_factor.toFixed(2)}×
                          </td>
                          <td className="py-2.5 pr-3">
                            {w.evaluation_due ? (
                              <Chip tone="p2">评估周</Chip>
                            ) : (
                              <span style={{ color: 'var(--hs-ink-4)' }}>—</span>
                            )}
                          </td>
                          <td
                            className="py-2.5 text-right text-[var(--hs-fs-micro)]"
                            style={{ color: 'var(--hs-ink-4)' }}
                          >
                            {open ? '收起 ▲' : '展开 7 天 ▼'}
                          </td>
                        </tr>
                        {open && (
                          <tr>
                            <td colSpan={7} className="pb-3 pt-1">
                              <WeekDays weekStart={w.week_start} />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <p className="mt-3 text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
              负荷为相对系数（1.00× 为基线）；第 4/8/12 周为评估与减量周。下一次滚动更新起点：
              {addDays(currentMonday, 7)}。
            </p>
          </Section>
        </>
      )}
    </Page>
  )
}

/** 展开区：读取该周周计划并渲染 7 天（只读，不触发生成） */
function WeekDays({ weekStart }: { weekStart: string }) {
  const q = useQuery<WeeklyPlan, ApiError>({
    queryKey: ['plan12w', 'week', weekStart],
    queryFn: () => api.get<WeeklyPlan>(`/api/plan/weekly/${weekStart}`),
    retry: false,
  })

  if (q.isLoading) return <div className="hs-skeleton h-20" />

  if (q.error) {
    const notFound = q.error instanceof ApiError && q.error.status === 404
    return (
      <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
        {notFound
          ? '该周尚未生成周计划（12 周计划只描述阶段与负荷，具体安排由周计划生成）。'
          : `读取失败：[${q.error.code}] ${q.error.message}`}
      </p>
    )
  }

  const days = q.data?.days ?? []
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
      {days.map((day) => {
        const s = day.session
        return (
          <div
            key={day.date}
            className="rounded-[var(--hs-r-sm)] border p-2"
            style={{
              borderColor: s ? 'var(--hs-border)' : 'var(--hs-border)',
              background: s ? 'var(--hs-surface)' : 'var(--hs-surface-2)',
            }}
          >
            <div className="flex items-baseline justify-between gap-1">
              <span className="text-[var(--hs-fs-micro)] font-bold" style={{ color: 'var(--hs-ink-2)' }}>
                {WEEKDAYS[day.weekday] ?? ''}
              </span>
              <span className="text-[10px]" style={{ color: 'var(--hs-ink-4)' }}>
                {day.date.slice(5)}
              </span>
            </div>
            {s ? (
              <>
                <div
                  className="mt-1 text-[var(--hs-fs-small)] font-semibold leading-snug"
                  style={{ color: 'var(--hs-ink)' }}
                >
                  {s.name}
                </div>
                <div className="mt-1 text-[10px] leading-relaxed" style={{ color: 'var(--hs-ink-3)' }}>
                  {sportLabel(s.sport)}
                  {s.target_hr_zone ? ` · ${s.target_hr_zone}` : ''}
                  {s.duration_min ? ` · ${s.duration_min}min` : ''}
                </div>
              </>
            ) : (
              <div className="mt-1 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
                休息
              </div>
            )}
            {day.is_night_shift && (
              <div className="mt-1">
                <Chip tone="p2">夜班</Chip>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
