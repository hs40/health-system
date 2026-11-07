/**
 * 日报页（三层分层；全部内容直接展开，靠排版降权分层）。
 *
 * 顶部：当日结论（规则化即时摘要，不依赖 LLM）+ 风险等级 + 今日训练安排
 * 中部：前一日训练回顾（内容表 + 负荷/ACWR）→ 睡眠 → 恢复 → 四项长期目标
 * 底部：AI 详细分析（按需生成）/ 数据来源 / 异常情况 / 计划调整原因 / 数据限制
 * 交互：图表点选看明细、历史日报查询（日历打点 + 前后翻页）、右侧层级导航
 *
 * 本文件只负责**数据编排与交互状态**；展示区块拆到 `components/reports/daily/`，
 * 结论规则拆到 `lib/headline.ts`。
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import type { DailyReport } from '../../api/types'
import { addDays as AddDays, todayISO as TodayISO } from '../../lib/date'
import { findLoadDay, toLoadDays } from '../../lib/loadSeries'
import { buildHeadline } from '../../lib/headline'
import { goalsOf, markedSet, numOrNull } from '../../lib/reports'
import { sportLabel } from '../../lib/sports'
import {
  invalidateReport,
  useDaily,
  useLoadSeries,
  usePushEnabled,
  useReportIndex,
} from '../../hooks/reportQueries'
import { Metric, Page, Section, TierLabel } from '../../components/ui/layout'
import { DateNav } from '../../components/ui/DateNav'
import { TierNav } from '../../components/ui/TierNav'
import { PointPopover, type PointDetail } from '../../components/ui/PointPopover'
import {
  ACWR_STATE_LABEL,
  GoalProgressGrid,
  LoadAcwrChart,
  TrendLine,
} from '../../components/reports/shared'
import { DailyHeadlineSection } from '../../components/reports/daily/DailyHeadlineSection'
import { DailyAppendixSection } from '../../components/reports/daily/DailyAppendixSection'
import { DailyTrainingTable } from '../../components/reports/daily/DailyTrainingTable'
import { DailySkeletonBlock } from '../../components/reports/daily/DailySkeleton'
import { hhmm } from '../../components/reports/daily/format'
import { StandardSourceModal } from '../../components/StandardSourceModal'
import type { ChartPoint } from '../../components/ui/Chart'

// ACWR 预热窗口：与后端 `/api/data/load-series` 的 SERIES_WARMUP_DAYS 保持一致。
// 后端对每一天都按「D−56」固定预热，前端请求窗口小于它就会算出不一致的 ACWR。
const LOAD_WINDOW_DAYS = 56

const TIERS = [
  { id: 'tier-top', label: '结论' },
  { id: 'sec-training', label: '训练' },
  { id: 'sec-sleep', label: '睡眠' },
  { id: 'sec-recovery', label: '恢复' },
  { id: 'sec-goals', label: '目标' },
  { id: 'tier-bottom', label: '详情' },
]

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

  // 负荷/ACWR 时序：报告页自带图表，不跳转数据页。
  //
  // 窗口必须覆盖 ACWR 的预热口径：后端 `/api/data/load-series` 对每一天都固定用
  // 「D−56」作 EWMA 预热起点（`api/data.py` SERIES_WARMUP_DAYS）。如果只取 21 天，
  // 查看历史日期时算出的 ACWR 会与日报不一致，或 `findLoadDay` 直接匹配不到而
  // 静默回退到快照。这里取 56 天，与后端同口径。
  const from = AddDays(day, -LOAD_WINDOW_DAYS)
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
    // refresh=true：窗口内覆盖 raw。07:30 的睡眠占位（全 null）是 append-only 写下的，
    // 不覆盖的话「同步数据」拉到完整睡眠也写不进去，日报仍为空。
    mutationFn: () =>
      api.post('/api/ops/sync', { mode: 'incremental', date: day, refresh: true }),
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
  const isBusy = busy !== null

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
                disabled={isBusy}
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
                disabled={isBusy || !pushEnabled.data?.enabled}
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

          {isLoading && <DailySkeletonBlock />}

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
                  disabled={isBusy}
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

              <DailyHeadlineSection
                report={data}
                headline={headline}
                busy={isBusy}
                onReanalyze={() => {
                  setBusy('plan')
                  runAnalyze.mutate()
                }}
              />

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

                <DailyTrainingTable report={data} />

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

              <DailyAppendixSection
                report={data}
                busy={isBusy}
                onGenerateAnalysis={() => {
                  setBusy('analyze')
                  runAnalyze.mutate()
                }}
                onShowSource={() => setShowSource(true)}
              />
            </>
          )}
        </div>

        {data && <TierNav items={TIERS} />}
      </div>

      {showSource && <StandardSourceModal day={day} onClose={() => setShowSource(false)} />}
      <PointPopover detail={detail} onClose={() => setDetail(null)} />
    </Page>
  )
}
