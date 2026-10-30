/**
 * 报告相关 React Query hooks（从 `lib/reports.ts` 拆出，lib 只留纯函数；④ 2026-10-26）。
 *
 * - 四个报告查询 hook 原在 lib/reports.ts，与类型/纯函数混装；
 * - 新增 `useLoadSeries` 统一负荷时序查询键（修复与 LoadPage 的缓存撕裂）；
 * - `invalidateReport` 收敛「生成/分析后失效报告 + 索引」的重复逻辑。
 */
import { useQuery, type QueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { DailyReport, LoadSeriesResponse, MonthlyReport, WeeklyReport } from '../api/types'
import { loadSeriesKey } from '../lib/loadSeries'
import type { IndexItem } from '../lib/reports'

export function useReportIndex(kind: 'daily' | 'weekly' | 'monthly') {
  return useQuery<{ kind: string; items: IndexItem[]; count: number }>({
    queryKey: ['reportIndex', kind],
    queryFn: () => api.get(`/api/reports/index?kind=${kind}`),
    staleTime: 60_000,
    retry: false,
  })
}

export function useDaily(date: string) {
  return useQuery<DailyReport>({
    queryKey: ['daily', date],
    queryFn: () => api.get(`/api/reports/daily?date=${date}`),
    retry: false,
  })
}

export function useWeekly(weekStart: string) {
  return useQuery<WeeklyReport>({
    queryKey: ['weekly', weekStart],
    queryFn: () => api.get(`/api/reports/weekly?week_start=${weekStart}`),
    retry: false,
  })
}

export function useMonthly(month: string) {
  return useQuery<MonthlyReport>({
    queryKey: ['monthly', month],
    queryFn: () => api.get(`/api/reports/monthly?month=${month}`),
    retry: false,
  })
}

/** 负荷时序（唯一 queryKey，三页 + LoadPage 共用）。 */
export function useLoadSeries(from: string, to: string) {
  return useQuery<LoadSeriesResponse>({
    queryKey: loadSeriesKey(from, to),
    queryFn: () => api.get(`/api/data/load-series?from=${from}&to=${to}`),
    retry: false,
  })
}

/** 钉钉推送是否已配置（前端据此把「推送至钉钉」按钮置灰）。 */
export function usePushEnabled() {
  return useQuery<{ enabled: boolean }>({
    queryKey: ['push-enabled'],
    queryFn: () => api.get('/api/reports/daily/push-enabled'),
    staleTime: 60_000,
    retry: false,
  })
}

/** 生成/分析成功后失效「报告 + 索引」两类缓存（三页共用）。 */
export function invalidateReport(
  qc: QueryClient,
  kind: 'daily' | 'weekly' | 'monthly',
  key: string,
): void {
  void qc.invalidateQueries({ queryKey: [kind, key] })
  void qc.invalidateQueries({ queryKey: ['reportIndex', kind] })
}
