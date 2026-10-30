import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import { api, ApiError } from '../api/client'
import { Chart } from '../components/ui/Chart'
import type { Baseline, SeriesResponse } from '../api/types'
import { Metric, Page, Section } from '../components/ui/layout'
import { addDays, todayISO } from '../lib/date'

interface MetricDef {
  series: string // /api/data/series 的指标名
  baseline?: string // /api/data/baseline 的指标名（若有）
  label: string
  unit: string
  scale?: number // 展示缩放（如睡眠秒 → 小时）
  digits?: number
  garminHrvBand?: boolean // 用 Garmin 自带 HRV 基线带代替自算阈值（E24）
}

const METRICS: MetricDef[] = [
  { series: 'restingHeartRate', baseline: 'resting_heart_rate', label: '静息心率', unit: 'bpm' },
  {
    series: 'hrvLastNightAvg',
    baseline: 'hrv_last_night_avg',
    label: 'HRV（夜均）',
    unit: 'ms',
    garminHrvBand: true,
  },
  {
    series: 'sleepDurationSec',
    baseline: 'sleep_duration_sec',
    label: '睡眠时长',
    unit: 'h',
    scale: 1 / 3600,
    digits: 1,
  },
  // 晨起电量 = bodyBatteryAtWakeTime（醒时值）。
  // 注意：bodyBatteryWake 是「同步时刻残值」（傍晚任务读到当天已耗尽），
  // 近 60 天中位数仅 5，绝不能当晨起值展示（2026-10-22 修正）。
  { series: 'bodyBatteryMorning', label: '身体电量（晨起）', unit: '' },
  { series: 'sleepScore', label: '睡眠得分', unit: '分' },
  { series: 'bodyBatteryWake', label: '身体电量（同步时刻）', unit: '' },
  { series: 'steps', label: '步数', unit: '步' },
  { series: 'activeCalories', label: '活动消耗', unit: 'kcal' },
  { series: 'weightKg', label: '体重', unit: 'kg', digits: 1 },
  { series: 'spo2Avg', label: '血氧饱和度', unit: '%' },
  { series: 'respirationAvg', label: '呼吸率', unit: '次/分' },
]

/** 基线口径文案（E23/E24）：窗口 0 = 全历史；阈值类型由 metric 决定。 */
function baselineCaption(b: Baseline): string {
  const window = b.window_days === 0 ? '全历史' : `${b.window_days} 天`
  const kind =
    b.threshold_high !== null && b.threshold_low !== null
      ? '区间'
      : b.threshold_high !== null
        ? '均值 + 1σ'
        : b.threshold_low !== null
          ? '均值 − 1σ'
          : '—'
  return `${window} · ${kind}`
}

export default function DataPage() {
  const [metricKey, setMetricKey] = useState<string>(METRICS[0].series)
  const [from, setFrom] = useState<string>(addDays(todayISO(), -29))
  const [to, setTo] = useState<string>(todayISO())

  const def = METRICS.find((m) => m.series === metricKey) ?? METRICS[0]

  const series = useQuery<SeriesResponse, ApiError>({
    queryKey: ['series', metricKey, from, to],
    queryFn: () =>
      api.get<SeriesResponse>(`/api/data/series?metric=${metricKey}&from=${from}&to=${to}`),
    retry: false,
  })

  const baseline = useQuery<Baseline, ApiError>({
    queryKey: ['baseline', def.baseline],
    queryFn: () => api.get<Baseline>(`/api/data/baseline?metric=${def.baseline}`),
    enabled: Boolean(def.baseline),
    retry: false,
  })

  // Garmin 自带 HRV 基线带（E24）：低区上沿 / 平衡区上沿，逐日取值（随设备基线漂移）
  const bandLow = useQuery<SeriesResponse, ApiError>({
    queryKey: ['series', 'hrvLowUpper', from, to],
    queryFn: () =>
      api.get<SeriesResponse>(`/api/data/series?metric=hrvLowUpper&from=${from}&to=${to}`),
    enabled: Boolean(def.garminHrvBand),
    retry: false,
  })

  const bandHigh = useQuery<SeriesResponse, ApiError>({
    queryKey: ['series', 'hrvBalancedHigh', from, to],
    queryFn: () =>
      api.get<SeriesResponse>(`/api/data/series?metric=hrvBalancedHigh&from=${from}&to=${to}`),
    enabled: Boolean(def.garminHrvBand),
    retry: false,
  })

  const points = series.data?.points ?? []
  const scaled = useMemo(
    () => points.map((p) => (def.scale ? p.value * def.scale : p.value)),
    [points, def.scale],
  )

  const stats = useMemo(() => {
    if (scaled.length === 0) return null
    const sum = scaled.reduce((a, b) => a + b, 0)
    const sorted = [...scaled].sort((a, b) => a - b)
    const mid = Math.floor(sorted.length / 2)
    const median =
      sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid]
    return {
      n: scaled.length,
      latest: scaled[scaled.length - 1],
      avg: sum / scaled.length,
      min: sorted[0],
      max: sorted[sorted.length - 1],
      median,
    }
  }, [scaled])

  const digits = def.digits ?? 0
  const b = baseline.data
  // 标线只用**真实判定阈值**（E23）：静息心率=全历史 mean+1σ、睡眠=全历史 mean−1σ。
  // 不再画 p10/p90 —— 那已不是判定依据，画出来会与实际拦截口径不一致。
  // HRV 走 Garmin 自带基线带（E24），由下方的基线带序列单独画。
  const markLines =
    b && !def.garminHrvBand
      ? [
          ...(b.threshold_low !== null
            ? [{ yAxis: def.scale ? b.threshold_low * def.scale : b.threshold_low, name: '阈值下' }]
            : []),
          ...(b.threshold_high !== null
            ? [{ yAxis: def.scale ? b.threshold_high * def.scale : b.threshold_high, name: '阈值上' }]
            : []),
        ]
      : []

  // Garmin HRV 基线带（E24）：按日期对齐到当前 x 轴，缺失日留 null（断线不连线）
  const dates = points.map((p) => p.date)
  const align = (resp?: SeriesResponse): (number | null)[] => {
    const m = new Map((resp?.points ?? []).map((p) => [p.date, p.value]))
    return dates.map((d) => m.get(d) ?? null)
  }
  const bandSeries: EChartsOption['series'] = def.garminHrvBand
    ? [
        {
          name: '低区上沿（R-001 判定线）',
          type: 'line',
          smooth: false,
          symbol: 'none',
          connectNulls: false,
          data: align(bandLow.data),
          lineStyle: { width: 1, type: 'dashed', color: '#b7590a' },
          itemStyle: { color: '#b7590a' },
        },
        {
          name: '平衡区上沿（加量参考）',
          type: 'line',
          smooth: false,
          symbol: 'none',
          connectNulls: false,
          data: align(bandHigh.data),
          lineStyle: { width: 1, type: 'dashed', color: '#0f7490' },
          itemStyle: { color: '#0f7490' },
        },
      ]
    : []

  const option: EChartsOption = {
    tooltip: { trigger: 'axis' },
    grid: { left: 52, right: 20, top: 28, bottom: 40 },
    xAxis: {
      type: 'category',
      data: points.map((p) => p.date),
      axisLabel: { fontSize: 10, rotate: 30, color: '#5b6673' },
    },
    yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
    series: [
      {
        name: def.label,
        type: 'line',
        smooth: true,
        showSymbol: points.length <= 40,
        symbolSize: 5,
        data: scaled,
        lineStyle: { width: 2, color: '#22374d' },
        itemStyle: { color: '#22374d' },
        areaStyle: { color: 'rgba(34,55,77,0.06)' },
        markLine: markLines.length
          ? {
              silent: true,
              symbol: 'none',
              lineStyle: { type: 'dashed', color: '#b7590a' },
              label: { formatter: '{b}', fontSize: 10, color: '#b7590a', position: 'insideEndTop' },
              data: markLines,
            }
          : undefined,
      },
      ...bandSeries,
    ],
  }

  return (
    <Page className="space-y-[var(--hs-gap)] py-[var(--hs-gap)]">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[var(--hs-fs-h1)] font-bold" style={{ color: 'var(--hs-ink)' }}>
            数据趋势
          </h1>
          <p className="mt-1 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
            单指标趋势 · 个人基线与判定阈值
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={metricKey}
            onChange={(e) => setMetricKey(e.target.value)}
            className="hs-input"
            aria-label="选择指标"
          >
            {METRICS.map((m) => (
              <option key={m.series} value={m.series}>
                {m.label}
              </option>
            ))}
          </select>
          <label className="hs-label" htmlFor="data-from">
            从
          </label>
          <input
            id="data-from"
            type="date"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            className="hs-input"
          />
          <label className="hs-label" htmlFor="data-to">
            到
          </label>
          <input
            id="data-to"
            type="date"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            className="hs-input"
          />
          <div className="flex gap-1">
            {[
              { label: '近 7 天', days: 6 },
              { label: '近 30 天', days: 29 },
              { label: '近 90 天', days: 89 },
            ].map((r) => (
              <button
                key={r.days}
                className="hs-btn"
                onClick={() => {
                  setFrom(addDays(todayISO(), -r.days))
                  setTo(todayISO())
                }}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
      </header>

      {series.isLoading && <div className="hs-card hs-skeleton h-32" />}

      {series.error && (
        <div className="hs-card">
          <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-p1)' }}>
            读取失败：[{series.error.code}] {series.error.message}
          </p>
          <p className="mt-1 text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-3)' }}>
            可先缩小日期范围后重试；若仍失败，请在运维台检查数据同步状态。
          </p>
        </div>
      )}

      {series.data && points.length === 0 && (
        <div className="hs-card">
          <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-2)' }}>
            所选区间没有「{def.label}」数据。
          </p>
          <p className="mt-1 text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-3)' }}>
            可调整时间范围，或先在运维台确认当天数据已同步。
          </p>
        </div>
      )}

      {stats && (
        <>
          <div className="grid gap-[var(--hs-gap-sm)] sm:grid-cols-3 lg:grid-cols-5">
            <Metric label="最新" value={`${stats.latest.toFixed(digits)}`} unit={def.unit} size="md" />
            <Metric label="均值" value={`${stats.avg.toFixed(digits)}`} unit={def.unit} size="md" />
            <Metric label="中位数" value={`${stats.median.toFixed(digits)}`} unit={def.unit} size="md" />
            <Metric label="最低" value={`${stats.min.toFixed(digits)}`} unit={def.unit} size="md" />
            <Metric label="最高" value={`${stats.max.toFixed(digits)}`} unit={def.unit} size="md" />
          </div>

          <Section
            title={`${def.label}（${from} ~ ${to}）`}
            hint={
              def.garminHrvBand
                ? 'Garmin 自带 HRV 基线带（低区上沿 / 平衡区上沿）'
                : b
                  ? `个人基线 v${b.version} · ${baselineCaption(b)}${b.mature ? '' : ' · 未成熟'}`
                  : '无个人基线'
            }
          >
            <Chart option={option} height={360} />
            <p className="mt-2 text-[var(--hs-fs-micro)] leading-relaxed" style={{ color: 'var(--hs-ink-3)' }}>
              共 {stats.n} 个数据点
              {def.garminHrvBand
                ? '（橙虚线 = Garmin 低区上沿，低于即「恢复不足」；青虚线 = 平衡区上沿，高于即有加量余力）'
                : markLines.length
                  ? '（虚线为实际判定阈值：低于/高于即触发对应规则）'
                  : ''}
            </p>
          </Section>
        </>
      )}
    </Page>
  )
}
