/** ECharts 统一封装：主题化配色 + 点击数据点回调（需求：交互查看数据点）。 */
import { useMemo } from 'react'
import ReactEChartsCore from 'echarts-for-react/lib/core'
import type { EChartsOption } from 'echarts'
import { echarts } from './echarts'

export const CHART_COLORS = {
  ink: '#10151c',
  ink3: '#5b6673',
  grid: '#e8ecf1',
  accent: '#1f5eff',
  accentSoft: 'rgba(31,94,255,0.14)',
  p1: '#c0271f',
  p2: '#b7590a',
  p3: '#0f7490',
  p4: '#17794a',
  run: '#1f5eff',
  indoor: '#0f7490',
  strength: '#b7590a',
  sleep: '#4b5fa8',
}

const BASE: EChartsOption = {
  textStyle: {
    fontFamily: "system-ui, -apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
    color: CHART_COLORS.ink3,
  },
  grid: { left: 8, right: 12, top: 28, bottom: 8, containLabel: true },
}

export interface ChartPoint {
  /** x 轴类目（日期） */
  date: string
  /** 系列名 */
  series: string
  value: number
}

export function Chart({
  option,
  height,
  onPointClick,
  empty,
  emptyText = '暂无数据',
}: {
  option: EChartsOption
  height?: number
  onPointClick?: (p: ChartPoint) => void
  empty?: boolean
  emptyText?: string
}) {
  const merged = useMemo<EChartsOption>(
    () => ({
      ...BASE,
      ...option,
      textStyle: { ...BASE.textStyle, ...(option.textStyle ?? {}) },
      grid: { ...BASE.grid, ...(option.grid ?? {}) },
    }),
    [option],
  )

  if (empty) {
    return (
      <div
        className="flex items-center justify-center rounded-[var(--hs-r-sm)] border border-dashed text-[var(--hs-fs-small)]"
        style={{
          height: height ?? 200,
          borderColor: 'var(--hs-border)',
          color: 'var(--hs-ink-4)',
          background: 'var(--hs-surface-2)',
        }}
      >
        {emptyText}
      </div>
    )
  }

  return (
    <ReactEChartsCore
      echarts={echarts}
      option={merged}
      style={{ height: height ?? 200, width: '100%' }}
      opts={{ renderer: 'canvas' }}
      notMerge
      lazyUpdate
      onEvents={
        onPointClick
          ? {
              click: (params: { componentType?: string; seriesName?: string; name?: string; value?: unknown }) => {
                if (params.componentType !== 'series') return
                const raw = Array.isArray(params.value)
                  ? params.value[params.value.length - 1]
                  : params.value
                const v = typeof raw === 'number' ? raw : Number(raw)
                if (!Number.isFinite(v)) return
                onPointClick({ date: String(params.name ?? ''), series: String(params.seriesName ?? ''), value: v })
              },
            }
          : undefined
      }
    />
  )
}
