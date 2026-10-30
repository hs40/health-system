/** 报告页公共展示块：趋势图、目标进展、数据来源/限制附录。 */
import type { EChartsOption } from 'echarts'
import { Chart, CHART_COLORS, type ChartPoint } from '../ui/Chart'
import { Delta, Metric } from '../ui/layout'
import { GOAL_NAMES, GOAL_ORDER, type GoalKey, type GoalProgress } from '../../lib/reports'
import { sportColor, sportLabel, sportRank } from '../../lib/sports'
import { ACWR_LINE_COLOR } from '../../lib/acwr'

export interface SeriesDay {
  date: string
  total?: number | null
  acwr?: number | null
  acwr_state?: string | null
  by_sport?: Record<string, number>
  [k: string]: unknown
}

/** 把 #rgb / #rrggbb 转成带透明度的 rgba（面积填充跟随线色，避免红线上盖蓝底） */
function withAlpha(hex: string, a: number): string {
  const h = hex.replace('#', '')
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
  const n = Number.parseInt(full, 16)
  if (!Number.isFinite(n)) return `rgba(31,94,255,${a})`
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`
}

export const ACWR_STATE_LABEL: Record<string, string> = {
  undertraining: '偏低（不足）',
  sweet_spot: '适宜区间',
  elevated: '偏高',
  danger: '过高',
  not_available: '不可计算',
}

/**
 * 训练负荷 + ACWR 组合图：下层堆叠柱（分运动类型），上层折线（ACWR）+ 参考带。
 * 点击任一数据点 → 回调明细。
 */
export function LoadAcwrChart({
  days,
  height,
  onPointClick,
}: {
  days: SeriesDay[]
  height?: number
  onPointClick?: (p: ChartPoint) => void
}) {
  const dates = days.map((d) => d.date.slice(5))
  const sports = Array.from(new Set(days.flatMap((d) => Object.keys(d.by_sport ?? {})))).sort(
    (a, b) => sportRank(a) - sportRank(b) || a.localeCompare(b),
  )

  const option: EChartsOption = {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      valueFormatter: (v) => (typeof v === 'number' ? v.toFixed(1) : String(v ?? '—')),
    },
    legend: {
      top: 0,
      left: 'center',
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { fontSize: 11, color: CHART_COLORS.ink3 },
    },
    xAxis: {
      type: 'category',
      data: dates,
      axisLine: { lineStyle: { color: CHART_COLORS.grid } },
      axisLabel: { fontSize: 10 },
      axisTick: { show: false },
    },
    yAxis: [
      {
        type: 'value',
        name: '负荷',
        nameTextStyle: { fontSize: 10, color: CHART_COLORS.ink3 },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
        axisLabel: { fontSize: 10 },
      },
      {
        type: 'value' as const,
        name: 'ACWR',
        min: 0,
        max: 2,
        nameTextStyle: { fontSize: 10, color: CHART_COLORS.ink3 },
        splitLine: { show: false },
        axisLabel: { fontSize: 10 },
      },
    ],
    series: [
      ...sports.map((sp) => ({
        name: sportLabel(sp),
        type: 'bar' as const,
        stack: 'load',
        data: days.map((d) => d.by_sport?.[sp] ?? 0),
        // 单色阶分层：同色相不同明度，段间 1px 白线让层级可见
        itemStyle: {
          color: sportColor(sp),
          borderColor: 'rgba(255,255,255,0.85)',
          borderWidth: 1,
        },
        barMaxWidth: 18,
      })),
      {
        name: 'ACWR',
        type: 'line' as const,
        yAxisIndex: 1,
        data: days.map((d) => d.acwr ?? null),
        connectNulls: false,
              symbol: 'circle' as const,
              symbolSize: 6,
              // 唯一强调色：状态由背景区间与 tooltip 表达，圆点不再分色
              lineStyle: { color: ACWR_LINE_COLOR, width: 2 },
              itemStyle: { color: ACWR_LINE_COLOR },
              markArea: {
                silent: true,
                itemStyle: {
                  color: 'rgba(23,121,74,0.10)',
                  borderColor: 'rgba(23,121,74,0.28)',
                  borderWidth: 1,
                },
                data: [[{ yAxis: 0.8 }, { yAxis: 1.3 }]] as never,
              },
              markLine: {
                silent: true,
                symbol: 'none',
                label: {
                  fontSize: 9,
                  color: CHART_COLORS.ink3,
                  position: 'insideEndTop' as const,
                },
                lineStyle: { type: 'dashed' as const, color: '#cdd4dc' },
              data: [{ yAxis: 1.5, label: { formatter: '1.5 上限' } }],
            },
      },
    ],
  }

  return (
    <Chart
      option={option}
      height={height}
      onPointClick={onPointClick}
      empty={days.length === 0}
      emptyText="该区间没有训练负荷数据"
    />
  )
}

/** 单指标趋势线（睡眠 / HRV / 静息心率 / 体重） */
export function TrendLine({
  points,
  unit,
  color = CHART_COLORS.accent,
  height,
  band,
  onPointClick,
  label,
}: {
  points: { date: string; value: number | null }[]
  unit: string
  color?: string
  height?: number
  /** 参考带（如基线 mean±σ） */
  band?: { low: number; high: number } | null
  onPointClick?: (p: ChartPoint) => void
  label?: string
}) {
  const option: EChartsOption = {
    tooltip: { trigger: 'axis', valueFormatter: (v) => (typeof v === 'number' ? `${v}${unit}` : '—') },
    grid: { left: 4, right: 10, top: 16, bottom: 4, containLabel: true },
    xAxis: {
      type: 'category',
      data: points.map((p) => p.date.slice(5)),
      axisLine: { lineStyle: { color: CHART_COLORS.grid } },
      axisLabel: { fontSize: 10 },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      scale: true,
      splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      axisLabel: { fontSize: 10 },
    },
    series: [
      {
        name: label ?? unit,
        type: 'line',
        data: points.map((p) => p.value),
        connectNulls: false,
        smooth: 0.2,
        symbol: 'circle',
        symbolSize: 5,
        lineStyle: { color, width: 2 },
        itemStyle: { color },
        areaStyle: { color: withAlpha(color, 0.07) },
        ...(band
          ? {
              markArea: {
                silent: true,
                itemStyle: { color: 'rgba(23,121,74,0.08)' },
                data: [[{ yAxis: band.low }, { yAxis: band.high }]] as never,
              },
            }
          : {}),
      },
    ],
  }
  return (
    <Chart
      option={option}
      height={height}
      onPointClick={onPointClick}
      empty={points.every((p) => p.value === null)}
      emptyText="该区间没有该项数据"
    />
  )
}

/** 四项长期目标进展：紧凑网格 / 叙事单栏自动适配 */
export function GoalProgressGrid({
  goals,
  compact,
}: {
  goals: GoalProgress
  /** 网格布局用 2×2，叙事布局用单列 */
  compact: boolean
}) {
  return (
    <div className={compact ? 'grid grid-cols-2 gap-2' : 'space-y-2'}>
      {GOAL_ORDER.map((k: GoalKey) => {
        const g = goals[k]
        if (!g) return null
        const insufficient = g.source === 'insufficient' || g.value === null
        return (
          <Metric
            key={k}
            label={GOAL_NAMES[k]}
            value={insufficient ? '数据不足' : g.value}
            unit={insufficient ? undefined : g.unit}
            delta={g.delta}
            deltaUnit={g.unit}
            goodDirection={g.direction}
            hint={
              insufficient
                ? '本期无实测数据'
                : `基线 ${g.baseline ?? '—'}${g.unit} · ${g.hint}`
            }
            size="md"
            tone={insufficient ? 'warn' : 'default'}
          />
        )
      })}
    </div>
  )
}
