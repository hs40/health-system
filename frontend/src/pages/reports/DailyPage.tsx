/**
 * 日报页（三层分层；全部内容直接展开，靠排版降权分层）。
 *
 * 顶部：当日结论（规则化即时摘要，不依赖 LLM）+ 风险等级 + 今日训练安排
 * 中部：前一日训练回顾（内容表 + 负荷/ACWR）→ 睡眠 → 恢复 → 四项长期目标
 * 底部：AI 详细分析（按需生成）/ 数据来源 / 异常情况 / 计划调整原因 / 数据限制
 * 交互：图表点选看明细、历史日报查询（日历打点 + 前后翻页）、右侧层级导航
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import type { DailyReport } from '../../api/types'
import { addDays as AddDays, todayISO as TodayISO } from '../../lib/date'
import { findLoadDay, toLoadDays } from '../../lib/loadSeries'
import { goalsOf, markedSet, numOrNull } from '../../lib/reports'
import { invalidateReport, useDaily, useLoadSeries, usePushEnabled, useReportIndex } from '../../hooks/reportQueries'
import { Appendix, Metric, Page, Section, TierLabel } from '../../components/ui/layout'
import {
  DataQualityBadge,
  QualityAlertBar,
  RiskBadge,
  StatusDot,
} from '../../components/ui/badges'
import { DateNav } from '../../components/ui/DateNav'
import { TierNav } from '../../components/ui/TierNav'
import { PointPopover, type PointDetail } from '../../components/ui/PointPopover'
import {
  ACWR_STATE_LABEL,
  GoalProgressGrid,
  LoadAcwrChart,
  TrendLine,
} from '../../components/reports/shared'
import { StandardSourceModal } from '../../components/StandardSourceModal'
import type { ChartPoint } from '../../components/ui/Chart'
import { sportLabel } from '../../lib/sports'



function hhmm(sec: unknown): string {
  const s = numOrNull(sec)
  if (s === null) return '—'
  const h = Math.floor(s / 3600)
  const m = Math.round((s % 3600) / 60)
  return `${h}h${String(m).padStart(2, '0')}m`
}

/**
 * 顶部结论：**规则化即时生成**（不调 LLM，秒开）。
 * 依据：风险优先级 + ACWR 状态 + 恢复指标缺口 + 今日计划是否生成。
 */
function buildHeadline(d: DailyReport): { verdict: string; tone: 'risk' | 'warn' | 'good' | 'default'; reasons: string[] } {
  const p = d.risk?.priority
  const acwr = d.acwr?.ratio
  const state = d.risk?.data_state
  const reasons: string[] = []

  if (p === 'P1') {
    reasons.push('风险规则判定 P1：今日按恢复日模板执行，不生成常规训练')
    // 触发原因（「哪条规则 + 实测值 vs 阈值」）：阈值观察期要看得到「为什么」
    if (d.risk?.reasons?.length) reasons.push(...d.risk.reasons)
    if (d.advisory?.length) reasons.push(...d.advisory)
    return { verdict: '今日强制恢复', tone: 'risk', reasons }
  }
  if (p === 'P2') {
    reasons.push('风险规则判定 P2：正常训练降量执行')
    if (d.risk?.reasons?.length) reasons.push(...d.risk.reasons)
    if (d.risk?.volume_halved) reasons.push('训练容量已减半')
    if (d.risk?.intensity_reduced) reasons.push('训练强度已下调')
    return { verdict: '今日减量训练', tone: 'warn', reasons }
  }
  if (acwr !== null && acwr !== undefined) {
    if (acwr >= 1.5) {
      reasons.push(`ACWR ${acwr.toFixed(2)} 超过 1.5 上限，负荷快速累积`)
      return { verdict: '负荷偏高，建议降量', tone: 'warn', reasons }
    }
    if (acwr < 0.8) {
      reasons.push(`ACWR ${acwr.toFixed(2)} 低于 0.8，训练刺激不足`)
      return { verdict: '训练不足，可适度加量', tone: 'default', reasons }
    }
    reasons.push(`ACWR ${acwr.toFixed(2)} 位于 0.8–1.3 适宜区间`)
  } else {
    reasons.push('ACWR 不可计算（慢性负荷为 0 或历史不足）')
  }
  const rhr = numOrNull((d.recovery ?? {})['resting_heart_rate'])
  const hrv = numOrNull((d.recovery ?? {})['hrv_last_night_avg'])
  if (rhr !== null) reasons.push(`静息心率 ${rhr} bpm`)
  if (hrv !== null) reasons.push(`HRV 夜均 ${hrv} ms`)
  if (state === 'C') reasons.push('数据兜底模式，以上结论仅作参考')
  if (p === 'P3') return { verdict: '状态良好，按进阶加量执行', tone: 'good', reasons }
  return { verdict: '按计划维持训练', tone: 'good', reasons }
}

export default function DailyPage() {
  const qc = useQueryClient()
  const [day, setDay] = useState<string>(TodayISO())
  const [showSource, setShowSource] = useState(false)
  const [detail, setDetail] = useState<PointDetail | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [pushMsg, setPushMsg] = useState<string | null>(null)

  const index = useReportIndex('daily')
  const pushEnabled = usePushEnabled()
  const { data, isLoading, error } = useDaily(day)
  const err = error as ApiError | null
  const notFound = err instanceof ApiError && err.status === 404

  // 负荷/ACWR 时序：日报前后各取 21 天（报告页自带图表，不跳转数据页）
  const from = AddDays(day, -21)
  const loadSeries = useLoadSeries(from, day)

  const sleepSeries = useQuery<{ metric: string; points: { date: string; value: number }[] }>({
    queryKey: ['series', 'sleepDurationSec', from, day],
    queryFn: () => api.get(`/api/data/series?metric=sleepDurationSec&from=${from}&to=${day}`),
    retry: false,
  })

  const rhrSeries = useQuery<{ metric: string; points: { date: string; value: number }[] }>({
    queryKey: ['series', 'restingHeartRate', from, day],
    queryFn: () => api.get(`/api/data/series?metric=restingHeartRate&from=${from}&to=${day}`),
    retry: false,
  })

  const invalidate = () => invalidateReport(qc, 'daily', day)

  const runSync = useMutation({
    mutationFn: () => api.post('/api/ops/sync', { mode: 'incremental', date: day }),
    onSettled: () => setBusy(null),
    onSuccess: invalidate,
  })
  const runAnalyze = useMutation({
    mutationFn: () => api.post(`/api/reports/daily/analyze?date=${day}`, {}),
    onSettled: () => setBusy(null),
    onSuccess: invalidate,
  })
  const runGenerate = useMutation({
    mutationFn: () => api.post(`/api/reports/daily/generate?date=${day}`, {}),
    onSettled: () => setBusy(null),
    onSuccess: invalidate,
  })
  const pushDing = useMutation<{ ok: boolean; error?: string | null }, ApiError>({
    mutationFn: () => api.post(`/api/reports/daily/push?date=${day}`, {}),
    onSettled: () => setBusy(null),
    onSuccess: (res) =>
      setPushMsg(res.ok ? '已推送至钉钉群' : `推送失败：${res.error ?? '未知错误'}`),
    onError: (e) => setPushMsg(`推送失败：${e.message}`),
  })

  const headline = useMemo(() => (data ? buildHeadline(data) : null), [data])
  const goals = goalsOf(data)
  const marked = markedSet(index.data?.items)

  const loadDays = useMemo(() => toLoadDays(loadSeries.data), [loadSeries.data])

  // 报告日：ACWR 取「当天」实时值（与下方图表同源，避免日报快照过期导致不一致）；
  // 前一日：综合负荷 / 可计算活动取「前一天」，而不是当天。
  const reportDay = useMemo(
    () => (data ? findLoadDay(loadSeries.data?.days, data.date.slice(5)) : undefined),
    [loadSeries.data, data],
  )
  const prevDay = useMemo(
    () => (data ? findLoadDay(loadSeries.data?.days, AddDays(data.date, -1).slice(5)) : undefined),
    [loadSeries.data, data],
  )
  const acwrValue = reportDay?.acwr != null ? Number(reportDay.acwr) : (data?.acwr?.ratio ?? null)
  const acwrState = reportDay?.acwr_state ?? null
  const prevLoad = useMemo(() => {
    if (prevDay?.total != null) return Number(prevDay.total)
    // 图表数据不可用时，回退到前一日活动表逐条 load 之和
    const yt = (data?.yesterday_training ?? {}) as Record<string, unknown>
    const acts = (yt['activities'] ?? yt['items'] ?? []) as Record<string, unknown>[]
    const vals = acts.map((a) => Number(a['load'])).filter((v) => Number.isFinite(v))
    return vals.length ? vals.reduce((s, v) => s + v, 0) : null
  }, [prevDay, data])

  const sleepPoints = useMemo(
    () =>
      (sleepSeries.data?.points ?? []).map((p) => ({
        date: p.date,
        value: p.value !== null ? Math.round((p.value / 3600) * 10) / 10 : null,
      })),
    [sleepSeries.data],
  )
  const rhrPoints = useMemo(
    () => (rhrSeries.data?.points ?? []).map((p) => ({ date: p.date, value: p.value })),
    [rhrSeries.data],
  )

  const onLoadPoint = (p: ChartPoint) => {
    const hit = findLoadDay(loadSeries.data?.days, p.date)
    if (!hit) return
    const sports = Object.entries(hit.by_sport ?? {})
    setDetail({
      title: `${hit.date} 训练负荷`,
      subtitle: `ACWR ${hit.acwr != null ? Number(hit.acwr).toFixed(2) : '不可计算'}${
        hit.acwr_state ? ` · ${ACWR_STATE_LABEL[hit.acwr_state] ?? hit.acwr_state}` : ''
      }`,
      rows: [
        {
          label: '当日总负荷',
          value: hit.total != null ? Number(hit.total).toFixed(2) : '0.00',
        },
        ...sports.map(([k, v]) => ({ label: k === 'rest' ? '休息' : sportLabel(k), value: String(v) })),
        ...(hit.not_computable?.length
          ? [{ label: '不可计算活动', value: `${hit.not_computable.length} 项` }]
          : []),
      ],
      note:
        hit.acwr_note ??
        '负荷为标准化分值之和，ACWR 用 EWMA（急 7 / 慢 28）回算；未验证可比性的运动类型会单独标注。',
    })
  }

  const onSleepPoint = (p: ChartPoint) => {
    setDetail({
      title: `${p.date} 睡眠`,
      subtitle: p.series,
      rows: [{ label: '睡眠时长', value: `${p.value} 小时` }],
      note: '本系统仅接入睡眠时长与起止时间，无深浅睡分期，因此不做分期质量判断。',
    })
  }

  const recovery = (data?.recovery ?? {}) as Record<string, unknown>
  const sleep = (data?.sleep ?? {}) as Record<string, unknown>

  const tiers = [
    { id: 'tier-top', label: '结论' },
    { id: 'sec-training', label: '训练' },
    { id: 'sec-sleep', label: '睡眠' },
    { id: 'sec-recovery', label: '恢复' },
    { id: 'sec-goals', label: '目标' },
    { id: 'tier-bottom', label: '详情' },
  ]

  return (
    <Page>
      <div className="grid grid-cols-1 gap-[var(--hs-gap)] lg:grid-cols-[1fr_120px]">
        <div className="min-w-0 space-y-[var(--hs-gap)]">
          {/* —— 页头：标题 + 日期导航 —— */}
          <header className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-baseline gap-2">
              <h1 className="text-[var(--hs-fs-h1)] font-bold tracking-tight" style={{ color: 'var(--hs-ink)' }}>
                日报
              </h1>
              <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                分层阅读 · 快速决策 → 深入追溯
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <DateNav mode="daily" value={day} onChange={setDay} marked={marked} />
              <button
                className="hs-btn"
                onClick={() => {
                  setBusy('sync')
                  runSync.mutate()
                }}
                disabled={busy !== null}
              >
                {busy === 'sync' ? '同步中…' : '同步数据'}
              </button>
              <button
                className="hs-btn"
                onClick={() => {
                  setPushMsg(null)
                  setBusy('dingtalk')
                  pushDing.mutate()
                }}
                disabled={busy !== null || !pushEnabled.data?.enabled}
                title={
                  pushEnabled.data?.enabled
                    ? '把当前日报推送到钉钉群'
                    : '未配置 DINGTALK_WEBHOOK'
                }
              >
                {busy === 'dingtalk' ? '推送中…' : '推送至钉钉'}
              </button>
            </div>
          </header>

          {pushMsg && (
            <p className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-3)' }}>
              {pushMsg}
            </p>
          )}

          {isLoading && <SkeletonBlock />}

          {notFound && (
            <Section title="这一天还没有日报">
              <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
                {day} 的报告尚未生成。可先「同步数据」从 Garmin 拉取，或直接生成。
              </p>
              <div className="flex gap-2">
                <button
                  className="hs-btn hs-btn-primary"
                  onClick={() => {
                    setBusy('generate')
                    runGenerate.mutate()
                  }}
                  disabled={busy !== null}
                >
                  {busy === 'generate' ? '生成中…' : '生成该日报告'}
                </button>
              </div>
            </Section>
          )}

          {error && !notFound && (
            <Section title="读取失败">
              <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-p1)' }}>
                [{err?.code}] {err?.message}
              </p>
            </Section>
          )}

          {data && headline && (
            <>
              {/* ============ 顶部：结论 / 风险 / 今日安排 ============ */}
              <TierLabel id="tier-top">顶部 · 当日结论</TierLabel>

              <Section>
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="min-w-[240px] flex-1">
                    <div className="hs-label">当日结论</div>
                    <div
                      className="mt-1 text-[var(--hs-fs-hero)] font-bold leading-tight tracking-tight"
                      style={{
                        color:
                          headline.tone === 'risk'
                            ? 'var(--hs-p1)'
                            : headline.tone === 'warn'
                              ? 'var(--hs-p2)'
                              : headline.tone === 'good'
                                ? 'var(--hs-p4)'
                                : 'var(--hs-ink)',
                        fontSize: 'var(--hs-fs-h1)',
                      }}
                    >
                      {headline.verdict}
                    </div>
                    <ul className="mt-2 space-y-1">
                      {headline.reasons.map((r) => (
                        <li
                          key={r}
                          className="flex gap-1.5 text-[var(--hs-fs-small)] leading-snug"
                          style={{ color: 'var(--hs-ink-2)' }}
                        >
                          <span style={{ color: 'var(--hs-ink-4)' }}>·</span>
                          {r}
                        </li>
                      ))}
                    </ul>
                  </div>

                  <div className="flex flex-col items-start gap-2">
                    <RiskBadge priority={data.risk?.priority} />
                    <DataQualityBadge
                      state={data.risk?.data_state}
                      limitationCount={data.limitations?.length ?? 0}
                    />
                    <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                      规则 {data.risk?.rule_version ?? '—'} · 报告 v{data.version}
                    </span>
                  </div>
                </div>

                <QualityAlertBar state={data.risk?.data_state} limitations={data.limitations ?? []} />

                <div className="hs-divider" />

                {/* 今日训练安排 */}
                <div>
                  <div className="mb-2 flex items-center justify-between">
                    <span className="hs-label">今日训练安排</span>
                    {data.today_plan_id && (
                      <button
                        className="hs-btn"
                        onClick={() => {
                          setBusy('plan')
                          runAnalyze.mutate()
                        }}
                        disabled={busy !== null}
                      >
                        重新分析
                      </button>
                    )}
                  </div>
                  {data.today_plan_id ? (
                    <div
                      className="rounded-[var(--hs-r-sm)] border p-3"
                      style={{ borderColor: 'var(--hs-border)', background: 'var(--hs-surface-2)' }}
                    >
                      <div className="text-[var(--hs-fs-h3)] font-bold" style={{ color: 'var(--hs-ink)' }}>
                        {String((data.today_plan_id as string))}
                      </div>
                      <p className="mt-1 text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                        完整处方见「周计划」页；此处展示当日计划编号。
                      </p>
                    </div>
                  ) : (
                    <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
                      {data.adjustment_reason
                        ? `今日不生成计划：${data.adjustment_reason}`
                        : '今日暂无训练计划。'}
                    </p>
                  )}
                </div>
              </Section>

              {/* ============ 中部：训练 / 睡眠 / 恢复 / 目标 ============ */}
              <TierLabel>中部 · 训练与恢复回顾</TierLabel>

              <Section
                id="sec-training"
                title="前一日运动训练回顾"
                hint="训练内容、分类型负荷与 ACWR（点击图上任意点看当日明细）"
              >
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Metric
                    label="ACWR"
                    value={acwrValue != null ? acwrValue.toFixed(2) : '—'}
                    hint={
                      acwrValue != null
                        ? acwrState
                          ? (ACWR_STATE_LABEL[acwrState] ?? acwrState)
                          : '实时值'
                        : (data.acwr?.note ?? '不可计算')
                    }
                    tone={
                      acwrValue != null && acwrValue >= 1.5
                        ? 'risk'
                        : acwrValue != null && acwrValue >= 1.3
                          ? 'warn'
                          : 'default'
                    }
                    size="sm"
                  />
                  <Metric
                    label="综合负荷"
                    value={prevLoad != null ? prevLoad.toFixed(2) : '—'}
                    unit="分"
                    hint="前一日各运动类型累计之和"
                    size="sm"
                  />
                  <Metric
                    label="可计算活动"
                    value={prevDay ? Object.keys(prevDay.by_sport ?? {}).length : '—'}
                    unit="类"
                    hint={
                      prevDay?.not_computable?.length
                        ? `${prevDay.not_computable.length} 项不可计算`
                        : '无缺失'
                    }
                    size="sm"
                  />
                  <Metric
                    label="睡眠时长"
                    value={hhmm(sleep['duration_sec'])}
                    hint="前一夜"
                    size="sm"
                  />
                </div>

                <LoadAcwrChart
                  days={loadDays}
                  height={undefined}
                  onPointClick={onLoadPoint}
                />

                {/* 运动内容表 */}
                <TrainingTable report={data} />

                {data.acwr?.note && (
                  <p className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-p2)' }}>
                    {data.acwr.note}
                  </p>
                )}
              </Section>

              <Section id="sec-sleep" title="睡眠报告" hint="时长与规律性；系统无睡眠分期数据">
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Metric label="睡眠时长" value={hhmm(sleep['duration_sec'])} size="sm" />
                  <Metric
                    label="入睡"
                    value={sleep['start_ts'] ? String(sleep['start_ts']).slice(11, 16) : '—'}
                    size="sm"
                  />
                  <Metric
                    label="起床"
                    value={sleep['end_ts'] ? String(sleep['end_ts']).slice(11, 16) : '—'}
                    size="sm"
                  />
                  <Metric
                    label="睡眠评分"
                    value={numOrNull(recovery['sleep_score']) ?? '—'}
                    hint="来自设备；与分期无关"
                    size="sm"
                  />
                </div>
                <TrendLine
                  points={sleepPoints}
                  unit="h"
                  label="睡眠时长"
                  onPointClick={onSleepPoint}
                />
              </Section>

              <Section id="sec-recovery" title="恢复指标" hint="静息心率 / HRV / 晨起身体电量（近三周趋势）">
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Metric label="静息心率" value={numOrNull(recovery['resting_heart_rate']) ?? '—'} unit="bpm" size="sm" />
                  <Metric label="HRV 夜均" value={numOrNull(recovery['hrv_last_night_avg']) ?? '—'} unit="ms" size="sm" />
                  <Metric label="晨起电量" value={numOrNull(recovery['body_battery_morning']) ?? '—'} size="sm" />
                  <Metric label="睡眠评分" value={numOrNull(recovery['sleep_score']) ?? '—'} size="sm" />
                </div>
                <TrendLine points={rhrPoints} unit="bpm" label="静息心率" color="#c0271f" />
              </Section>

              <Section id="sec-goals" title="长期目标进展" hint="近 7 天窗口；基线取前一个 7 天区间；无数据标注为数据不足">
                {goals ? (
                  <GoalProgressGrid goals={goals} compact />
                ) : (
                  <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
                    该日报未携带目标进展（旧版本报告）。重新生成该日报即可补齐；周报/月报中已提供四项长期目标。
                  </p>
                )}
                <p className="text-[var(--hs-fs-micro)] leading-relaxed" style={{ color: 'var(--hs-ink-4)' }}>
                  口径：心肺=区间平均静息心率（越低越好）；减脂=平均体重；肌耐力=Z2–Z3 总时长；
                  最大力量=力量训练容量（组×次×kg）。体重与静息心率为实测，其余为训练侧代理指标。
                </p>
              </Section>

              {/* ============ 底部：详细分析 / 来源 / 异常 / 调整原因 / 限制 ============ */}
              <TierLabel id="tier-bottom">底部 · 详细追溯</TierLabel>

              <div className="space-y-[var(--hs-gap-sm)]">
                <Appendix title="AI 详细分析">
                  {data.over_under_analysis ? (
                    <p className="leading-relaxed">{data.over_under_analysis}</p>
                  ) : (
                    <div className="flex flex-wrap items-center gap-2">
                      <span>尚未生成 AI 分析。</span>
                      <button
                        className="hs-btn"
                        onClick={() => {
                          setBusy('analyze')
                          runAnalyze.mutate()
                        }}
                        disabled={busy !== null}
                      >
                        {busy === 'analyze' ? '分析中…' : '生成 AI 结论'}
                      </button>
                    </div>
                  )}
                </Appendix>

                <Appendix title="数据来源">
                  <div className="flex flex-wrap items-center gap-3">
                    <span>Garmin 原始快照 → 标准层 → 本报告（每个取值可回溯到原始文件）。</span>
                    <button className="hs-btn" onClick={() => setShowSource(true)}>
                      查看原始出处
                    </button>
                  </div>
                  <div style={{ color: 'var(--hs-ink-4)' }}>
                    数据状态 {data.risk?.data_state ?? '—'} · 质量等级 {data.quality.grade} · 规则版本{' '}
                    {data.risk?.rule_version ?? '—'}
                  </div>
                </Appendix>

                <Appendix title="异常情况">
                  {data.risk?.missing_inputs?.length || data.quality.missing?.length ? (
                    <ul className="space-y-1">
                      {data.risk?.missing_inputs?.map((m) => (
                        <li key={`r-${m}`}>· 缺失输入：{m}</li>
                      ))}
                      {data.quality.missing?.map((m) => (
                        <li key={`q-${m}`}>· 质量缺失：{m}</li>
                      ))}
                    </ul>
                  ) : (
                    <p>无缺失维度；本日未检出数据异常。</p>
                  )}
                  {data.risk?.triggered_rules?.length ? (
                    <p style={{ color: 'var(--hs-ink-3)' }}>
                      命中规则：{data.risk.triggered_rules.join('、')}
                    </p>
                  ) : null}
                  {(data.quality.conflicts as unknown[])?.length ? (
                    <p>检出 {data.quality.conflicts.length} 项跨源冲突，已按来源优先级选源（不做平均）。</p>
                  ) : null}
                </Appendix>

                <Appendix title="计划调整原因">
                  {data.adjustment_reason ? (
                    <p>{data.adjustment_reason}</p>
                  ) : (
                    <p>本期计划未发生调整。</p>
                  )}
                  {data.risk?.volume_halved || data.risk?.intensity_reduced ? (
                    <p>
                      调整动作：{data.risk.volume_halved ? '容量减半 ' : ''}
                      {data.risk.intensity_reduced ? '强度下调' : ''}
                    </p>
                  ) : null}
                </Appendix>

                <Appendix title="数据限制">
                  {data.limitations?.length ? (
                    <ul className="space-y-1">
                      {data.limitations.map((l) => (
                        <li key={l}>· {l}</li>
                      ))}
                    </ul>
                  ) : (
                    <p>无额外限制说明。</p>
                  )}
                  <p style={{ color: 'var(--hs-ink-4)' }}>
                    本系统不提供医学诊断；所有阈值均为训练管理用途的工程口径。
                  </p>
                </Appendix>
              </div>
            </>
          )}
        </div>

        {data && <TierNav items={tiers} />}
      </div>

      {showSource && <StandardSourceModal day={day} onClose={() => setShowSource(false)} />}
      <PointPopover detail={detail} onClose={() => setDetail(null)} />
    </Page>
  )
}

/** 运动内容表：前一日分运动类型的训练内容 */
function TrainingTable({ report }: { report: DailyReport }) {
  // 只读「前一日」实际训练（yesterday_training）；不再回退到 report.load（那是**今天**的负荷，
  // 放进「前一日回顾」会张冠李戴——2026-10-26 修复）。
  const yt = (report.yesterday_training ?? {}) as Record<string, unknown>
  const activities = (yt['activities'] ?? yt['items'] ?? []) as Record<string, unknown>[]

  if (activities.length === 0) {
    return (
      <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
        前一日无训练记录。
      </p>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[var(--hs-fs-small)]">
        <thead>
          <tr style={{ color: 'var(--hs-ink-4)' }}>
            {['运动类型', '训练内容', '时长', '负荷', '完成度'].map((h) => (
              <th
                key={h}
                className="border-b px-2 py-1.5 text-left font-semibold"
                style={{ borderColor: 'var(--hs-border)' }}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {activities.map((a: Record<string, unknown>, i: number) => {
              const sport = String(a['sport'] ?? a['type'] ?? '—')
              return (
                <tr key={i} style={{ color: 'var(--hs-ink-2)' }}>
                  <td className="border-b px-2 py-1.5" style={{ borderColor: 'var(--hs-border)' }}>
                    <span className="inline-flex items-center gap-1.5">
                      <StatusDot tone="p3" />
                      {sport === 'rest' ? '休息' : sportLabel(sport)}
                    </span>
                  </td>
                  <td className="border-b px-2 py-1.5" style={{ borderColor: 'var(--hs-border)' }}>
                    {String(a['name'] ?? a['title'] ?? '训练记录')}
                  </td>
                  <td className="border-b px-2 py-1.5" style={{ borderColor: 'var(--hs-border)' }}>
                    {numOrNull(a['duration_sec']) ? hhmm(a['duration_sec']) : numOrNull(a['duration_min']) ? `${a['duration_min']} 分钟` : '—'}
                  </td>
                  <td className="border-b px-2 py-1.5 font-semibold" style={{ borderColor: 'var(--hs-border)' }}>
                    {a['load'] !== undefined ? String(Math.round(Number(a['load']))) : '—'}
                  </td>
                  <td className="border-b px-2 py-1.5" style={{ borderColor: 'var(--hs-border)' }}>
                    {a['completion'] !== undefined ? String(a['completion']) : String(a['status'] ?? '—')}
                  </td>
                </tr>
              )
            },
          )}
        </tbody>
      </table>
    </div>
  )
}

function SkeletonBlock() {
  return (
    <div className="space-y-[var(--hs-gap)]">
      <div className="hs-card">
        <div className="hs-skeleton h-3 w-24" />
        <div className="hs-skeleton mt-2 h-8 w-64" />
        <div className="hs-skeleton mt-2 h-3 w-full" />
        <div className="hs-skeleton mt-1 h-3 w-3/4" />
      </div>
      <div className="hs-card">
        <div className="hs-skeleton h-4 w-40" />
        <div className="grid grid-cols-4 gap-2 py-3">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="hs-skeleton h-14" />
          ))}
        </div>
        <div className="hs-skeleton h-40 w-full" />
      </div>
    </div>
  )
}
