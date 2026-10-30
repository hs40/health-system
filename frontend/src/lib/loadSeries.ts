/**
 * 负荷时序（`/api/data/load-series`）的**共用查询键与纯映射**（④ 报告页去重，2026-10-26）。
 *
 * 此前三个报告页各写一份 `days` 映射与 `find`，且 queryKey 用 `'loadSeries'`，
 * 与 `LoadPage` 的 `'load-series'` 不一致 → 同一端点两份缓存、失效不同步（缓存撕裂）。
 * 这里收敛为唯一 `loadSeriesKey`，并把纯映射抽成函数（lib 只留纯函数）。
 */
import type { LoadSeriesDay, LoadSeriesResponse } from '../api/types'

/** 唯一 queryKey（三页 + LoadPage 共用）。 */
export function loadSeriesKey(from: string, to: string) {
  return ['load-series', from, to] as const
}

/** 图表消费的日视图（`LoadAcwrChart` 不读 `total`，保留以便未来复用）。 */
export interface LoadDayView {
  date: string
  total: number
  by_sport: Record<string, number>
  acwr: number | null
  acwr_state: string
  [k: string]: unknown
}

/** `LoadSeriesResponse` → 图表日数组（三页共用，替代各自的 `map`）。 */
export function toLoadDays(data: LoadSeriesResponse | undefined): LoadDayView[] {
  return (data?.days ?? []).map((d) => ({
    date: d.date,
    total: d.total,
    by_sport: d.by_sport,
    acwr: d.acwr,
    acwr_state: d.acwr_state,
  }))
}

/** 按 `MM-DD`（图表 x 轴）找当天**原始**记录（弹窗用 `total` / `not_computable` / `acwr_note`）。 */
export function findLoadDay(
  days: LoadSeriesDay[] | undefined,
  shortDate: string,
): LoadSeriesDay | undefined {
  return days?.find((d) => d.date.slice(5) === shortDate)
}
