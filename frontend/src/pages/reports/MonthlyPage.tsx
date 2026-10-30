/**
 * 月报页（分层趋势分析）：在周报基础上评估长期趋势与阶段效果。
 * 顶部：月度总结 / 四项长期目标进展 / 关键结论
 * 中部：训练与专项负荷、ACWR、睡眠恢复、运动表现的长期趋势
 * 底部：阶段评估、数据来源、计划执行偏差及原因、下月建议
 * 交互：数据点明细、历史月份对比（较上月增量 + 叠加基准线）、个人基线展示
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import type { MonthlyReport } from '../../api/types'
import { findLoadDay, toLoadDays } from '../../lib/loadSeries'
import { goalsOf, markedSet, monthlyGenState, num as numText, numOrNull } from '../../lib/reports'
import { invalidateReport, useLoadSeries, useMonthly, useReportIndex } from '../../hooks/reportQueries'
import { sportLabel } from '../../lib/sports'
import { Appendix, Delta, Metric, Page, Section, TierLabel } from '../../components/ui/layout'
import { Chip } from '../../components/ui/badges'
import { DateNav } from '../../components/ui/DateNav'
import { TierNav } from '../../components/ui/TierNav'
import { PointPopover, type PointDetail } from '../../components/ui/PointPopover'
import type { ChartPoint } from '../../components/ui/Chart'
import { ACWR_STATE_LABEL, GoalProgressGrid, LoadAcwrChart, TrendLine } from '../../components/reports/shared'

function monthBounds(month: string): { start: string; end: string } {
  const [y, m] = month.split('-').map(Number)
  const start = `${month}-01`
  const last = new Date(y, m, 0).getDate()
  return { start, end: `${month}-${String(last).padStart(2, '0')}` }
}

export default function MonthlyPage() {
  const qc = useQueryClient()
  const defaultMonth = useMemo(() => {
    const first = new Date()
    first.setDate(1)
    first.setMonth(first.getMonth() - 1)
    return `${first.getFullYear()}-${String(first.getMonth() + 1).padStart(2, '0')}`
  }, [])
  const [month, setMonth] = useState<string>(defaultMonth)
  const [detail, setDetail] = useState<PointDetail | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const report = useMonthly(month)
  const index = useReportIndex('monthly')
  const gen = monthlyGenState(month)
  const { start, end } = monthBounds(month)

  const series = useLoadSeries(start, end)

  const rhrSeries = useQuery<{ metric: string; points: { date: string; value: number }[] }>({
    queryKey: ['series', 'restingHeartRate', start, end],
    queryFn: () => api.get(`/api/data/series?metric=restingHeartRate&from=${start}&to=${end}`),
    retry: false,
  })

  const generate = useMutation<MonthlyReport, ApiError>({
    mutationFn: () => api.post(`/api/reports/monthly/generate?month=${month}`, {}),
    onSettled: () => setBusy(null),
    onSuccess: () => invalidateReport(qc, 'monthly', month),
  })

  const analyze = useMutation<MonthlyReport, ApiError>({
    mutationFn: () => api.post(`/api/reports/monthly/analyze?month=${month}`, {}),
    onSettled: () => setBusy(null),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['monthly', month] }),
  })

  const r = report.data
  const notFound = report.error instanceof ApiError && report.error.status === 404
  const review = (r?.review ?? {}) as Record<string, unknown>
  const gp = (r?.goal_progress ?? {}) as Record<string, unknown>
  const deltaMonth = (gp['delta_vs_prev_month'] ?? {}) as Record<string, number | null>
  const goals = goalsOf(r)

  const days = useMemo(() => toLoadDays(series.data), [series.data])

  const rhrPoints = useMemo(
    () => (rhrSeries.data?.points ?? []).map((p) => ({ date: p.date, value: p.value })),
    [rhrSeries.data],
  )

  const onPoint = (p: ChartPoint) => {
    const hit = findLoadDay(series.data?.days, p.date)
    if (!hit) return
    setDetail({
      title: `${hit.date}`,
      subtitle: `总负荷 ${hit.total ?? 0} · ACWR ${hit.acwr != null ? hit.acwr.toFixed(2) : '不可计算'}`,
      rows: Object.entries(hit.by_sport ?? {}).map(([k, v]) => ({
        label: sportLabel(k) ?? k,
        value: String(v),
      })),
      note: ACWR_STATE_LABEL[hit.acwr_state] ?? '',
    })
  }

  const bySport = (review['load_by_sport'] as Record<string, number>) ?? {}
  const avgRhr = numOrNull(rhrPoints.length ? rhrPoints.reduce((s, p) => s + p.value, 0) / rhrPoints.length : null)

  return (
    <Page>
      <div className="grid grid-cols-1 gap-[var(--hs-gap)] lg:grid-cols-[1fr_120px]">
        <div className="min-w-0 space-y-[var(--hs-gap)]">
          <header className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-baseline gap-2">
              <h1 className="text-[var(--hs-fs-h1)] font-bold tracking-tight" style={{ color: 'var(--hs-ink)' }}>
                月报
              </h1>
              <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                每月 1 日 08:00 回顾上一月
              </span>
            </div>
            <DateNav
              mode="monthly"
              value={month}
              onChange={setMonth}
              marked={markedSet(index.data?.items)}
              notice={gen.due ? undefined : `将于 ${gen.text} 生成`}
            />
          </header>

          {report.isLoading && <div className="hs-card hs-skeleton h-40" />}

          {notFound && (
            <div className="hs-card">
              <div className="hs-section-title">{month} 月报尚未生成</div>
              <p className="mt-1 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
                {gen.due ? '已过生成时点，可立即生成。' : `将于 ${gen.text} 自动生成；现在也可手动生成。`}
              </p>
              <div className="mt-3">
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
              <TierLabel>顶部 · 月度总结</TierLabel>

              <Section>
                <p className="text-[var(--hs-fs-h3)] leading-relaxed" style={{ color: 'var(--hs-ink)' }}>
                  {month} 月：综合负荷 {numOrNull(review['load_total']) ?? 0} 分，
                  {numOrNull(review['days_with_data']) ?? 0} 天有数据，
                  计划依从率 {r.adherence != null ? `${Math.round(r.adherence * 100)}%` : '—'}
                  （较上月{' '}
                  <Delta
                    value={
                      deltaMonth['adherence'] != null ? Math.round(deltaMonth['adherence'] * 100) : null
                    }
                    unit="pp"
                    goodDirection="up"
                  />
                  ）。
                </p>

                <div className="hs-divider" />

                <div className="hs-label mb-1">四项长期目标进展</div>
                {goals ? (
                  <GoalProgressGrid goals={goals} compact />
                ) : (
                  <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
                    本期未包含目标进展数据。
                  </p>
                )}

                <div className="hs-divider" />

                <div className="flex flex-wrap items-center gap-2">
                  <span className="hs-label">关键结论</span>
                  <Chip tone="p4">有数据 {numOrNull(review['days_with_data']) ?? 0} 天</Chip>
                  {numOrNull(review['rest_days']) ? <Chip tone="p1">P1 休息 {numText(review['rest_days'], 0)} 天</Chip> : null}
                  {numOrNull(review['reduce_days']) ? <Chip tone="p2">P2 减量 {numText(review['reduce_days'], 0)} 天</Chip> : null}
                  {avgRhr != null && (
                    <Chip tone="p3">
                      静息心率均值 {avgRhr.toFixed(0)} bpm
                      <Delta value={numOrNull(deltaMonth['resting_heart_rate_avg'])} unit=" bpm" goodDirection="down" />
                    </Chip>
                  )}
                </div>
              </Section>

              <TierLabel>中部 · 长期趋势</TierLabel>

              <Section id="mo-training" title="训练与专项负荷（月度趋势）" hint="点击数据点看当日明细">
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {Object.entries(bySport).map(([k, v]) => (
                    <Metric key={k} label={sportLabel(k) ?? k} value={v} unit="分" size="sm" />
                  ))}
                  <Metric label="月度总负荷" value={numOrNull(review['load_total']) ?? 0} unit="分" size="sm" />
                </div>
                <LoadAcwrChart days={days} onPointClick={onPoint} />
              </Section>

              <Section id="mo-recovery" title="睡眠与恢复趋势">
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Metric
                    label="平均体重"
                    value={numText(gp['weight_avg_kg'])}
                    unit="kg"
                    delta={numOrNull(deltaMonth['weight_avg_kg'])}
                    goodDirection="down"
                    size="sm"
                  />
                  <Metric label="步数合计" value={numText(gp['steps_total'], 0)} size="sm" />
                  <Metric label="活动热量" value={numText(gp['active_calories_total'], 0)} unit="kcal" size="sm" />
                  <Metric
                    label="依从率"
                    value={r.adherence != null ? `${Math.round(r.adherence * 100)}%` : '—'}
                    size="sm"
                  />
                </div>
                <TrendLine points={rhrPoints} unit="bpm" label="静息心率" color="#c0271f" />
              </Section>

              <Section id="mo-perf" title="运动表现与数据可比性">
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {Object.entries(bySport).map(([k, v]) => (
                    <Metric
                      key={`p-${k}`}
                      label={`${sportLabel(k) ?? k} 专项负荷`}
                      value={v}
                      unit="分"
                      hint="跨类型相加前需验证可比性"
                      size="sm"
                    />
                  ))}
                </div>
                <p className="text-[var(--hs-fs-micro)] leading-relaxed" style={{ color: 'var(--hs-ink-4)' }}>
                  运动表现仅在同一运动类型内部比较；跨类型综合负荷需该类型可比性验证通过，未验证的类型单独展示。
                </p>
              </Section>

              <Section id="mo-exec" title="计划执行">
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Metric label="P3 进阶日" value={numOrNull(review['progress_days']) ?? 0} unit="天" size="sm" />
                  <Metric label="P4 维持日" value={numOrNull(review['maintain_days']) ?? 0} unit="天" size="sm" />
                  <Metric label="已评估" value={numOrNull(review['assessed_days']) ?? 0} unit="天" size="sm" />
                  <Metric label="依从率" value={r.adherence != null ? `${Math.round(r.adherence * 100)}%` : '—'} size="sm" />
                </div>
              </Section>

              <TierLabel>底部 · 阶段评估</TierLabel>

              <div className="space-y-[var(--hs-gap-sm)]">
                <Appendix title="阶段评估与下月建议">
                  {r.long_term_adjustment ? (
                    <p>{r.long_term_adjustment}</p>
                  ) : (
                    <div className="flex flex-wrap items-center gap-2">
                      <span>尚未生成长期目标偏离分析。</span>
                      <button
                        className="hs-btn"
                        onClick={() => {
                          setBusy('analyze')
                          analyze.mutate()
                        }}
                        disabled={busy !== null}
                      >
                        {busy === 'analyze' ? '分析中…' : '生成阶段评估'}
                      </button>
                    </div>
                  )}
                  <p style={{ color: 'var(--hs-ink-4)' }}>
                    结论依据：四项长期目标较上月变化、ACWR 与恢复匹配度、计划依从率。阈值均为工程口径，非医学诊断。
                  </p>
                </Appendix>

                <Appendix title="数据来源">
                  <p>
                    {month} 全月 Garmin 原始快照 → 标准层 → 月报聚合；覆盖 {numOrNull(review['days_with_data']) ?? 0} 天。
                  </p>
                </Appendix>

                <Appendix title="计划执行偏差及原因">
                  {numOrNull(review['rest_days']) || numOrNull(review['reduce_days']) ? (
                    <p>
                      P1 休息 {numOrNull(review['rest_days']) ?? 0} 天、P2 减量 {numOrNull(review['reduce_days']) ?? 0} 天，属规则触发的主动调整。
                    </p>
                  ) : (
                    <p>本月无规则触发的计划调整。</p>
                  )}
                </Appendix>

                <Appendix title="数据限制">
                  <p>· 睡眠无分期；月内缺数据的天不参与均值，也不按 0 填充。</p>
                  <p>· 最大力量以训练容量（组×次×kg）代理，不等同于 1RM 实测。</p>
                  <p>· 体重与静息心率为实测，其余目标为训练侧代理指标。</p>
                </Appendix>
              </div>
            </>
          )}
        </div>

        {r && (
          <TierNav
            items={[
              { id: 'mo-training', label: '训练' },
              { id: 'mo-recovery', label: '恢复' },
              { id: 'mo-perf', label: '表现' },
              { id: 'mo-exec', label: '执行' },
            ]}
          />
        )}
      </div>

      <PointPopover detail={detail} onClose={() => setDetail(null)} />
    </Page>
  )
}
