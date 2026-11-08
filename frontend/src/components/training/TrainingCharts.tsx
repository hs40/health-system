/** 运动训练详情图表（三期 §8.1 / §8.4，Q8 / Q12）。 */
import { useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import { Chart, CHART_COLORS } from '../ui/Chart'
import type { HrDataState, HrPoint, HrZoneStats, StrengthTimeline } from '../../api/training'

/**
 * 三态占位文案：把「FIT 明细未同步」与「本次确实无逐秒心率」分开。
 * NAS 单容器只挂 data/raw 却没搬 fit_jsonl 时，两图以前一律显示
 * 「本次无逐秒心率数据」，让用户误以为是软件坏了；现在给出可操作的提示。
 */
function hrEmptyText(state: HrDataState): string {
  if (state === 'fit_missing') {
    return '逐秒心率明细未同步：缺少该活动的 FIT 数据文件，请在 NAS 上补齐 data/raw/garmin/fit_jsonl/ 后重试'
  }
  if (state === 'no_hr_samples') return '本次运动未记录逐秒心率'
  return '本次无逐秒心率数据'
}

/** §8.1 心率区间横向柱状图：只展示实际各区间分布，不展示计划目标区间。 */
export function HrZoneChart({ stats, state }: { stats: HrZoneStats; state: HrDataState }) {
  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 8, right: 28, top: 10, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      xAxis: {
        type: 'value',
        name: '秒',
        nameTextStyle: { color: CHART_COLORS.ink3, fontSize: 10 },
        axisLabel: { color: CHART_COLORS.ink3, fontSize: 10 },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      yAxis: {
        type: 'category',
        data: (stats.zones ?? []).map((z) => `${z.name} ${z.low_bpm}–${z.high_bpm}`),
        axisLabel: { color: CHART_COLORS.ink3, fontSize: 11 },
        axisTick: { show: false },
      },
      series: [
        {
          type: 'bar',
          data: (stats.zones ?? []).map((z) => z.seconds),
          barWidth: '58%',
          itemStyle: { color: CHART_COLORS.accent, borderRadius: [0, 4, 4, 0] },
          label: { show: true, position: 'right', color: CHART_COLORS.ink3, fontSize: 10, formatter: '{c}s' },
        },
      ],
    }),
    [stats],
  )
  return (
    <Chart
      option={option}
      height={200}
      empty={state !== 'ok'}
      emptyText={hrEmptyText(state)}
    />
  )
}

/** §8.1 时间—心率折线图：缺失心率断线（connectNulls=false），不补 0（Q8）。 */
export function HrSeriesChart({ points, state }: { points: HrPoint[]; state: HrDataState }) {
  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 8, right: 16, top: 12, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: points.map((_, i) => i),
        name: '秒',
        nameTextStyle: { color: CHART_COLORS.ink3, fontSize: 10 },
        axisLabel: {
          color: CHART_COLORS.ink3,
          fontSize: 10,
          interval: Math.max(0, Math.floor(points.length / 6)),
        },
        boundaryGap: false,
      },
      yAxis: {
        type: 'value',
        name: 'bpm',
        nameTextStyle: { color: CHART_COLORS.ink3, fontSize: 10 },
        axisLabel: { color: CHART_COLORS.ink3, fontSize: 10 },
        splitLine: { lineStyle: { color: CHART_COLORS.grid } },
      },
      series: [
        {
          type: 'line',
          data: points.map((p) => p.hr),
          // 跑步/游泳逐秒心率可达 3000 点，LTTB 降采样保持折线形状并控制渲染成本
          sampling: 'lttb' as const,
          connectNulls: false,
          showSymbol: false,
          lineStyle: { color: CHART_COLORS.accent, width: 2 },
          areaStyle: { color: CHART_COLORS.accentSoft },
        },
      ],
    }),
    [points],
  )
  return (
    <Chart
      option={option}
      height={220}
      empty={state !== 'ok'}
      emptyText={hrEmptyText(state)}
    />
  )
}

/** §8.4 / Q12 力量时间轴：动作显示在对应时间段上，休息统一用秒，不写起止时刻。 */
export function StrengthTimelineView({ timeline }: { timeline: StrengthTimeline }) {
  const { active_seconds, rest_seconds } = timeline
  const segments = timeline.segments ?? []
  return (
    <div className="space-y-3">
      <div
        className="flex h-9 w-full overflow-hidden rounded-[var(--hs-r-sm)] border"
        style={{ borderColor: 'var(--hs-border)' }}
      >
        {segments.map((seg, i) => (
          <div
            key={i}
            className="min-w-[2px]"
            style={{
              flex: Math.max(1, seg.duration_sec),
              background: seg.kind === 'rest' ? 'var(--hs-border)' : 'var(--hs-accent-soft)',
              borderRight: '1px solid var(--hs-surface)',
            }}
            title={
              seg.kind === 'rest'
                ? `休息 ${seg.duration_sec} 秒`
                : `${seg.name_zh ?? ''} ${seg.duration_sec} 秒`
            }
          />
        ))}
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
        <span>总运动时间 <b style={{ color: 'var(--hs-ink)' }}>{active_seconds}</b> 秒</span>
        <span>总休息时间 <b style={{ color: 'var(--hs-ink)' }}>{rest_seconds}</b> 秒</span>
        <span>正式训练动作 <b style={{ color: 'var(--hs-ink)' }}>{timeline.formal_exercise_count}</b> 个</span>
      </div>

      <ul className="space-y-1.5">
        {segments.map((seg, i) => (
          <li key={i} className="flex items-baseline gap-2 text-[var(--hs-fs-small)]">
            <span
              className="inline-block h-2 w-2 shrink-0 rounded-sm"
              style={{
                background: seg.kind === 'rest' ? 'var(--hs-border-strong)' : 'var(--hs-accent)',
              }}
              aria-hidden
            />
            {seg.kind === 'rest' ? (
              <span style={{ color: 'var(--hs-ink-4)' }}>休息 {seg.duration_sec} 秒</span>
            ) : (
              <span style={{ color: 'var(--hs-ink)' }}>
                <b>{seg.name_zh}</b>
                {seg.reps != null && ` ${seg.reps} 次`}
                {seg.weight_kg != null && ` · ${seg.weight_kg} kg`}
                <span className="ml-1.5" style={{ color: 'var(--hs-ink-4)' }}>
                  {seg.duration_sec} 秒
                </span>
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
