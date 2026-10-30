/**
 * 周报页（与日报沿用同一套分层与交互，不折叠）。
 * 顶部：本周总结 / 关键结论 / 风险提示
 * 中部：实际训练 / 专项负荷 / 综合负荷与 ACWR / 睡眠恢复 / 长期目标
 * 底部：详细分析 / 数据来源 / 计划偏差与调整原因 / 数据限制
 */
import { useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import type { WeeklyReport } from '../../api/types'
import { addDays, mondayOf, todayISO } from '../../lib/date'
import { findLoadDay, toLoadDays } from '../../lib/loadSeries'
import { sportLabel } from '../../lib/sports'
import { goalsOf, markedSet, num as numText, numOrNull, weeklyGenState } from '../../lib/reports'
import { invalidateReport, useLoadSeries, useReportIndex, useWeekly } from '../../hooks/reportQueries'
import { Appendix, Delta, Metric, Page, Section, TierLabel } from '../../components/ui/layout'
import { Chip } from '../../components/ui/badges'
import { DateNav } from '../../components/ui/DateNav'
import { TierNav } from '../../components/ui/TierNav'
import { PointPopover, type PointDetail } from '../../components/ui/PointPopover'
import type { ChartPoint } from '../../components/ui/Chart'
import {
  ACWR_STATE_LABEL,
  GoalProgressGrid,
  LoadAcwrChart,
  TrendLine,
} from '../../components/reports/shared'

/** 策略版本变化类型（§6.2 三类必须可区分） */
const CHANGE_TYPE_LABEL: Record<string, string> = {
  aiOptimize: 'AI 自动优化',
  manualRestore: '用户手动恢复',
}

export default function WeeklyPage() {
  const qc = useQueryClient()
  const [weekStart, setWeekStart] = useState<string>('')
  const [detail, setDetail] = useState<PointDetail | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  // 默认「上一周」（周报回顾上一周）
  const defaultWeek = useMemo(() => addDays(mondayOf(todayISO()), -7), [])

  const activeWeek = weekStart || defaultWeek
  const report = useWeekly(activeWeek)
  const index = useReportIndex('weekly')
  const gen = weeklyGenState(activeWeek)

  const series = useLoadSeries(activeWeek, addDays(activeWeek, 6))

  const generate = useMutation<WeeklyReport, ApiError>({
    mutationFn: () => api.post(`/api/reports/weekly/generate?week_start=${activeWeek}`, {}),
    onSettled: () => setBusy(null),
    onSuccess: () => invalidateReport(qc, 'weekly', activeWeek),
  })

  const r = report.data
  const notFound = report.error instanceof ApiError && report.error.status === 404
  const review = (r?.review ?? {}) as Record<string, unknown>
  const load = (r?.load ?? {}) as Record<string, unknown>
  const recovery = (r?.recovery ?? {}) as Record<string, unknown>
  const planVs = (r?.plan_vs_actual ?? {}) as Record<string, unknown>
  const execution = (r?.execution ?? {}) as Record<string, unknown>
  const goals = goalsOf(r)
  const changes = r?.strategy_changes ?? []
  const deltaPrev = (recovery['delta_vs_prev_week'] ?? {}) as Record<string, number | null>

  const days = useMemo(() => toLoadDays(series.data), [series.data])

  const onPoint = (p: ChartPoint) => {
    const hit = findLoadDay(series.data?.days, p.date)
    if (!hit) return
    setDetail({
      title: `${hit.date}`,
      subtitle: `总负荷 ${hit.total ?? 0}`,
      rows: [
        { label: 'ACWR', value: hit.acwr != null ? hit.acwr.toFixed(2) : '不可计算' },
        { label: '状态', value: ACWR_STATE_LABEL[hit.acwr_state] ?? hit.acwr_state },
        ...Object.entries(hit.by_sport ?? {}).map(([k, v]) => ({
          label: sportLabel(k) ?? k,
          value: String(v),
        })),
      ],
      note: hit.acwr_note ?? 'ACWR 由历史负荷 EWMA 回算，不可计算时不伪造数值。',
    })
  }

  const riskChips = useMemo(() => {
    const out: { label: string; tone: 'p1' | 'p2' | 'p3' | 'p4' }[] = []
    const rest = numOrNull(review['rest_days'])
    const reduce = numOrNull(review['reduce_days'])
    if (rest) out.push({ label: `P1 休息 ${rest} 天`, tone: 'p1' })
    if (reduce) out.push({ label: `P2 减量 ${reduce} 天`, tone: 'p2' })
    if (numOrNull(review['assessed_days'])) out.push({ label: `已评估 ${review['assessed_days']} 天`, tone: 'p4' })
    return out
  }, [review])

  return (
    <Page>
      <div className="grid grid-cols-1 gap-[var(--hs-gap)] lg:grid-cols-[1fr_120px]">
        <div className="min-w-0 space-y-[var(--hs-gap)]">
          <header className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-baseline gap-2">
              <h1 className="text-[var(--hs-fs-h1)] font-bold tracking-tight" style={{ color: 'var(--hs-ink)' }}>
                周报
              </h1>
              <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                每周一 08:00 回顾上一周
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <DateNav
                mode="weekly"
                value={activeWeek}
                onChange={setWeekStart}
                marked={markedSet(index.data?.items)}
                notice={gen.due ? undefined : `将于 ${gen.text} 生成`}
              />
            </div>
          </header>

          {report.isLoading && <div className="hs-card hs-skeleton h-40" />}

          {(notFound || (report.error && !r)) && (
            <div className="hs-card">
              <div className="hs-section-title">本期周报尚未生成</div>
              <p className="mt-1 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
                {gen.due
                  ? '已过生成时点，可以立即生成这一周的周报。'
                  : `该周的周报将于 ${gen.text} 自动生成；现在也可以手动生成。`}
              </p>
              <div className="mt-3 flex gap-2">
                <button
                  className="hs-btn hs-btn-primary"
                  onClick={() => {
                    setBusy('gen')
                    generate.mutate()
                  }}
                  disabled={busy !== null}
                >
                  {busy === 'gen' ? '生成中…' : '立即生成'}
                </button>
              </div>
            </div>
          )}

          {r && (
            <>
              <TierLabel>顶部 · 本周总结</TierLabel>

              <Section>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Metric
                    label="本周综合负荷"
                    value={numOrNull(load['total_combined']) ?? 0}
                    unit="分"
                    hint={`${numOrNull(load['days_with_load']) ?? 0} 天有负荷`}
                  />
                  <Metric
                    label="静息心率"
                    value={numText(recovery['resting_heart_rate_avg'])}
                    unit="bpm"
                    delta={numOrNull(deltaPrev['resting_heart_rate_avg'])}
                    goodDirection="down"
                  />
                  <Metric
                    label="HRV"
                    value={numText(recovery['hrv_avg'])}
                    unit="ms"
                    delta={numOrNull(deltaPrev['hrv_avg'])}
                    goodDirection="up"
                  />
                  <Metric
                    label="平均睡眠"
                    value={numText(recovery['sleep_avg_hours'], 2)}
                    unit="h"
                    delta={
                      numOrNull(deltaPrev['sleep_avg_sec']) != null
                        ? Math.round(((numOrNull(deltaPrev['sleep_avg_sec']) as number) / 3600) * 10) / 10
                        : null
                    }
                    deltaUnit="h"
                    goodDirection="up"
                  />
                </div>

                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="hs-label">风险提示</span>
                  {riskChips.length ? (
                    riskChips.map((c) => (
                      <Chip key={c.label} tone={c.tone}>
                        {c.label}
                      </Chip>
                    ))
                  ) : (
                    <span className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
                      本周无风险等级记录
                    </span>
                  )}
                </div>

                <p className="text-[var(--hs-fs-small)] leading-relaxed" style={{ color: 'var(--hs-ink-2)' }}>
                  本周共 {numOrNull(review['days_with_data']) ?? 0} 天有数据，完成计划训练{' '}
                  {numOrNull(planVs['created_sessions']) ?? 0} 节（计划 {numOrNull(planVs['planned_sessions']) ?? 0} 节）；
                  夜班 {((planVs['night_shift_days'] as string[]) ?? []).length} 天。
                </p>
              </Section>

              <TierLabel>中部 · 训练与恢复</TierLabel>

              <Section id="wk-training" title="实际训练与专项负荷" hint="点击数据点查看当日明细">
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {Object.entries((load['by_sport'] as Record<string, number>) ?? {}).map(([k, v]) => (
                    <Metric key={k} label={sportLabel(k) ?? k} value={v} unit="分" size="sm" />
                  ))}
                  {Object.keys((load['by_sport'] as Record<string, number>) ?? {}).length === 0 && (
                    <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
                      本周无分类型负荷记录
                    </p>
                  )}
                </div>
                <LoadAcwrChart days={days} onPointClick={onPoint} />
              </Section>

              <Section id="wk-recovery" title="睡眠与恢复趋势">
                <div className="grid grid-cols-3 gap-2">
                  <Metric label="静息心率" value={numText(recovery['resting_heart_rate_avg'])} unit="bpm" size="sm" />
                  <Metric label="HRV" value={numText(recovery['hrv_avg'])} unit="ms" size="sm" />
                  <Metric label="平均睡眠" value={numText(recovery['sleep_avg_hours'], 2)} unit="h" size="sm" />
                </div>
                <p className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                  周级均值；逐日趋势见日报与「数据趋势」页。睡眠无分期数据。
                </p>
              </Section>

              <Section id="wk-goals" title="四项长期目标进展" hint="基线取上一周">
                {goals ? (
                  <GoalProgressGrid goals={goals} compact />
                ) : (
                  <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
                    本期未包含目标进展数据。
                  </p>
                )}
              </Section>

              <Section id="wk-exec" title="计划执行">
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Metric label="计划训练" value={numOrNull(planVs['planned_sessions']) ?? 0} unit="节" size="sm" />
                  <Metric label="已建计划" value={numOrNull(planVs['created_sessions']) ?? 0} unit="节" size="sm" />
                  <Metric label="已上传" value={numOrNull(planVs['uploaded_sessions']) ?? 0} unit="节" size="sm" />
                  <Metric
                    label="执行判定"
                    value={numOrNull(execution['judged_days']) ?? '—'}
                    unit="天"
                    hint={
                      (execution['missing_days'] as string[] | undefined)?.length
                        ? `${(execution['missing_days'] as string[]).length} 天未判定`
                        : undefined
                    }
                    size="sm"
                  />
                </div>
              </Section>

              <TierLabel>底部 · 追溯</TierLabel>

              <div className="space-y-[var(--hs-gap-sm)]">
                <Appendix title="详细分析与建议">
                  <p>
                    本周负荷 {numOrNull(load['total_combined']) ?? 0} 分，静息心率{' '}
                    {numText(recovery['resting_heart_rate_avg'])} bpm（
                    <Delta value={numOrNull(deltaPrev['resting_heart_rate_avg'])} unit=" bpm" goodDirection="down" />
                    ），HRV {numText(recovery['hrv_avg'])} ms。
                  </p>
                  <p>
                    建议基于以上三项与 ACWR 状态调整下周强度；本系统不提供医学诊断，结论仅用于训练管理。
                  </p>
                </Appendix>

                <Appendix title="数据来源">
                  <p>Garmin 原始快照 → 标准层 → 周报聚合；规则版本见日报。</p>
                  <p style={{ color: 'var(--hs-ink-4)' }}>
                    生成时间 {r.week_start} 当周 · 覆盖 {numOrNull(review['days_with_data']) ?? 0}/7 天
                  </p>
                </Appendix>

                <Appendix title="本周策略版本变化">
                  {changes.length ? (
                    <ul className="space-y-1">
                      {changes.map((c) => (
                        <li key={c.event_id}>
                          · {c.changed_at?.slice(0, 16).replace('T', ' ') ?? '—'} ·{' '}
                          {CHANGE_TYPE_LABEL[c.change_type ?? ''] ?? c.change_type ?? '变更'}
                          {c.version_number ? ` ${c.version_number}` : ''} ——{' '}
                          {c.reason || '未记录变更原因'}
                          {c.effective_at
                            ? `（生效 ${c.effective_at.slice(0, 16).replace('T', ' ')}）`
                            : ''}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p>本周无策略版本变化。</p>
                  )}
                  <p style={{ color: 'var(--hs-ink-4)' }}>
                    策略版本变化不会自动重算历史报告；对本周训练无可确认实质影响时，不编造影响结论。
                  </p>
                </Appendix>

                <Appendix title="计划偏差与调整原因">
                  {r.deviation_reason ? (
                    <p>{r.deviation_reason}</p>
                  ) : (
                    <p>本期未记录计划偏差原因。</p>
                  )}
                </Appendix>

                <Appendix title="数据限制">
                  <p>· 睡眠仅有总时长与起止时间，无深浅睡分期。</p>
                  <p>· ACWR 需要足够历史负荷，早期周期可能标记为「不可计算」。</p>
                  <p>· 跨运动类型可比性未验证的类型会单独标注，不参与综合负荷结论。</p>
                </Appendix>
              </div>
            </>
          )}
        </div>

        {r && (
          <TierNav
            items={[
              { id: 'wk-training', label: '训练' },
              { id: 'wk-recovery', label: '恢复' },
              { id: 'wk-goals', label: '目标' },
              { id: 'wk-exec', label: '执行' },
            ]}
          />
        )}
      </div>

      <PointPopover detail={detail} onClose={() => setDetail(null)} />
    </Page>
  )
}
