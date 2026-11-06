/** 运动训练页面 React Query hooks（queryKey 含全部参数，天然去重与缓存）。 */
import { useQuery } from '@tanstack/react-query'
import {
  fetchCalendar,
  fetchDetail,
  type CalendarResponse,
  type DetailResponse,
} from '../api/training'

export function useTrainingCalendar(ym: string, type: string) {
  return useQuery<CalendarResponse>({
    queryKey: ['training', 'calendar', ym, type],
    queryFn: () => fetchCalendar(ym, type),
    retry: false,
    staleTime: 60_000,
  })
}

export function useTrainingDetail(date: string, seq: number) {
  return useQuery<DetailResponse>({
    queryKey: ['training', 'detail', date, seq],
    queryFn: () => fetchDetail(date, seq),
    retry: false,
    staleTime: 60_000,
  })
}
