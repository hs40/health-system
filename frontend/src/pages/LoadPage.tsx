/**
 * 训练负荷与 ACWR（2026-10-24 视觉重构）。
 *
 * 编码规则（单色阶分层）：
 *   负荷柱 = 一个蓝灰色相的三档明度（跑步最深 → 力量 → 室内有氧最浅），
 *   ACWR   = 唯一强调色细线；
 *   四个参考区间只做极淡背景分区，最佳区 0.8–1.3 加重并带描边。
 * 区间标签在带内垂直居中（修复此前标签压在上边界、看起来标错区间的问题）。
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import { api, ApiError } from '../api/client'
import { Chart } from '../components/ui/Chart'
import type { LoadSeriesResponse } from '../api/types'
import { Metric, Page, Section } from '../components/ui/layout'
import { addDays, todayISO } from '../lib/date'
import { sportColor, sportLabel, sportRank } from '../lib/sports'
import { ACWR_LINE_COLOR, ACWR_ZONES } from '../lib/acwr'

/** 验证状态文案（需求 6.3）：未验证类型照常计分，仅披露状态。 */
const VERIFY_LABELS: Record<string, string> = {
  insufficient: '可比性未验证（样本不足）',
  unregistered: '无专属公式，可比性待验证',
}

function stateLabel(state: string): string {
  return ACWR_ZONES.find((z) => z.key === state)?.short ?? state
}

export default function LoadPage() {
  const [from, setFrom] = useState<string>(addDays(todayISO(), -27))
  const [to, setTo] = useState<string>(todayISO())

  const series = useQuery<LoadSeriesResponse, ApiError>({
    queryKey: ['load-series', from, to],
    queryFn: () => api.get<LoadSeriesResponse>(`/api/data/load-series?from=${from}&to=${to}`),
    retry: false,
  })

  const days = series.data?.days ?? []
  const dates = useMemo(() => days.map((d) => d.date), [days])

  // 堆叠柱：按训练优先级排序（跑步最深、垫底），颜色深浅才有稳定含义
  const sports = useMemo(() => {
    const set = new Set<string>()
    for (const d of days) Object.keys(d.by_sport).forEach((s) => set.add(s))
    return [...set].sort((a, b) => sportRank(a) - sportRank(b) || a.localeCompare(b))
  }, [days])

  const barSeries: EChartsOption['series'] = sports.map((sport) => ({
    name: sportLabel(sport),
    type: 'bar',
    stack: 'load',
    barMaxWidth: 22,
    itemStyle: {
      color: sportColor(sport),
      borderColor: 'rgba(255,255,255,0.85)',
      borderWidth: 1,
    },
    emphasis: { focus: 'series' },
    data: days.map((d) => d.by_sport[sport] ?? 0),
  }))

  // ACWR 折线：不可计算日留 null —— 断线，不连线、不补 0（需求 8.4）
  const acwrSeries: EChartsOption['series'] = [
    ...barSeries,
    {
      name: 'ACWR',
      type: 'line',
      yAxisIndex: 1,
      smooth: false,
      symbolSize: 5,
      connectNulls: false,
      data: days.map((d) => d.acwr),
      lineStyle: { width: 2, color: ACWR_LINE_COLOR },
      itemStyle: { color: ACWR_LINE_COLOR },
      markArea: {
        silent: true,
        // 标签落在区间垂直中心；此前默认 insideTop 导致压在上边界、读起来标错区间
        label: {
          show: true,
          position: 'inside',
          color: '#5b6673',
          fontSize: 9,
          // 给标签一层极淡底色，避免被 ACWR 折线穿过导致读不清
          backgroundColor: 'rgba(255,255,255,0.72)',
          padding: [2, 4],
          borderRadius: 3,
          formatter: (p: { name?: string }) => p.name ?? '',
        },
        data: ACWR_ZONES.map((z) => [
          {
            yAxis: z.from,
            name: z.label,
            itemStyle: {
              color: z.fill,
              borderColor: z.emphasize ? 'rgba(23,121,74,0.30)' : 'transparent',
              borderWidth: z.emphasize ? 1 : 0,
            },
            label: z.emphasize
              ? {
                  show: true,
                  position: 'inside',
                  fontSize: 10,
                  fontWeight: 700,
                  color: '#17794a',
                  backgroundColor: 'rgba(255,255,255,0.85)',
                  padding: [2, 5],
                  borderRadius: 3,
                  formatter: (p: { name?: string }) => p.name ?? '',
                }
              : undefined,
          },
          { yAxis: z.to },
        ]),
      } as never,
    },
  ]

  const option: EChartsOption = {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const list = (Array.isArray(params) ? params : [params]) as {
          axisValue?: string
          seriesName?: string
          value?: number | null
          marker?: string
        }[]
        if (list.length === 0) return ''
        const date = list[0].axisValue ?? ''
        const day = days.find((d) => d.date === date)
        const lines = [`<b>${date}</b>`]
        for (const p of list) {
          if (p.seriesName === 'ACWR') {
            lines.push(
              p.value === null || p.value === undefined
                ? 'ACWR：不可计算'
                : `ACWR：${Number(p.value).toFixed(3)}（${stateLabel(day?.acwr_state ?? '')}）`,
            )
          } else if (p.value) {
            lines.push(`${p.marker ?? ''}${p.seriesName}：${Number(p.value).toFixed(1)}`)
          }
        }
        if (day && day.total > 0) lines.push(`<b>合计：${day.total.toFixed(1)}</b>`)
        // 需求 4.5：不可计算活动逐日披露原因，不静默丢弃
        for (const nc of day?.not_computable ?? []) {
          lines.push(
            `<span style="color:#b91c1c">不可计算：${sportLabel(nc.sport ?? '?')}${
              nc.reason ? `（${nc.reason}）` : ''
            }</span>`,
          )
        }
        // 需求 6.3：跨运动类型可比性未验证的类型逐日披露（分值照常计入）
        for (const [sport, status] of Object.entries(day?.unverified_sports ?? {})) {
          lines.push(
            `<span style="color:#92400e">${sportLabel(sport)}：${
              VERIFY_LABELS[status] ?? status
            }</span>`,
          )
        }
        if (day?.acwr_note) lines.push(`<span style="color:#92400e">${day.acwr_note}</span>`)
        return lines.join('<br/>')
      },
    },
    legend: {
      top: 0,
      left: 'center',
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { fontSize: 11, color: '#6b7684' },
    },
    grid: { left: 46, right: 52, top: 40, bottom: 44 },
    xAxis: {
      type: 'category',
      data: dates,
      axisLine: { lineStyle: { color: '#e3e7ec' } },
      axisTick: { show: false },
      axisLabel: { fontSize: 10, rotate: 30, color: '#6b7684' },
    },
    yAxis: [
      {
        type: 'value',
        name: '负荷',
        nameTextStyle: { fontSize: 10, color: '#6b7684' },
        scale: true,
        axisLabel: { fontSize: 10, color: '#6b7684' },
        splitLine: { lineStyle: { color: '#eef1f5' } },
      },
      {
        type: 'value',
        name: 'ACWR',
        min: 0,
        max: 2.2,
        nameTextStyle: { fontSize: 10, color: '#6b7684' },
        axisLabel: { fontSize: 10, color: '#6b7684' },
        splitLine: { show: false },
      },
    ],
    series: acwrSeries,
  }

  const latest = days.length > 0 ? days[days.length - 1] : null
  const trainedDays = days.filter((d) => d.total > 0).length
  const weekly = useMemo(() => {
    if (days.length === 0) return 0
    return days.slice(-7).reduce((a, d) => a + d.total, 0)
  }, [days])

  const hr = series.data?.hr_params

  return (
    <Page className="space-y-[var(--hs-gap)] py-[var(--hs-gap)]">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[var(--hs-fs-h1)] font-bold" style={{ color: 'var(--hs-ink)' }}>
            训练负荷与 ACWR
          </h1>
          <p className="mt-1 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
            左轴 = 负荷标准分 · 右轴 = ACWR（EWMA τ=7/28）
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="hs-label" htmlFor="load-from">
            从
          </label>
          <input
            id="load-from"
            type="date"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            className="hs-input"
          />
          <label className="hs-label" htmlFor="load-to">
            到
          </label>
          <input
            id="load-to"
            type="date"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            className="hs-input"
          />
          <div className="flex gap-1">
            {[
              { label: '近 7 天', days: 6 },
              { label: '近 28 天', days: 27 },
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

      {latest && (
        <div className="grid gap-[var(--hs-gap-sm)] sm:grid-cols-2 lg:grid-cols-4">
          <Metric
            label="今日 ACWR"
            value={latest.acwr === null ? '—' : latest.acwr.toFixed(2)}
            unit={latest.acwr === null ? '不可计算' : stateLabel(latest.acwr_state)}
            size="md"
          />
          <Metric label="今日负荷" value={latest.total.toFixed(1)} unit="分" size="md" />
          <Metric label="近 7 天负荷" value={weekly.toFixed(0)} unit="分" size="md" />
          <Metric label="有训练天数" value={`${trainedDays}/${days.length}`} unit="天" size="md" />
        </div>
      )}

      <Section title="逐日负荷与 ACWR" hint={`${from} ~ ${to} · 点击柱子查看当日明细`}>
        <Chart option={option} height={380} />
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5">
          {ACWR_ZONES.map((z) => (
            <span
              key={z.key}
              className="inline-flex items-center gap-1.5 text-[var(--hs-fs-micro)]"
              style={{ color: 'var(--hs-ink-3)' }}
            >
              <span
                className="h-2.5 w-2.5 rounded-[2px]"
                style={{ background: z.fill, border: `1px solid ${z.fg}66` }}
              />
              {z.label}
            </span>
          ))}
        </div>
        <p className="mt-2 text-[var(--hs-fs-micro)] leading-relaxed" style={{ color: 'var(--hs-ink-3)' }}>
          无训练日照常计 0（EWMA 按 0 参与）；慢性 EWMA 为 0 时 ACWR 断线不补值。悬停可查看当日分类型负荷与不可计算原因。
        </p>
      </Section>

      {hr && (
        <Section
          title="心率参数（Karvonen 五区）"
          hint={`最大 ${hr.max_hr} · 静息 ${hr.rest_hr} · HRR ${hr.hrr} bpm`}
        >
          <div className="flex flex-wrap gap-2">
            {hr.zones.map((z) => (
              <div
                key={z.name}
                className="rounded-[var(--hs-r-sm)] border px-3 py-2"
                style={{ borderColor: 'var(--hs-border)', background: 'var(--hs-surface-2)' }}
              >
                <div className="text-[var(--hs-fs-small)] font-semibold" style={{ color: 'var(--hs-ink)' }}>
                  {z.name}
                </div>
                <div className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-2)' }}>
                  {z.low_bpm}–{z.high_bpm} bpm
                </div>
                <div className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-3)' }}>
                  {Math.round(z.low_pct * 100)}–{Math.round(z.high_pct * 100)}% HRR
                </div>
              </div>
            ))}
          </div>
          <p className="mt-2 text-[var(--hs-fs-micro)] leading-relaxed" style={{ color: 'var(--hs-ink-3)' }}>
            参考负荷 R：有氧类 130 / 力量 60,000（原始负荷 ÷ R × 100 得标准分）。Tredict 指标仅用于验证比较，不并入负荷（需求 7.4）。
          </p>
        </Section>
      )}
    </Page>
  )
}
