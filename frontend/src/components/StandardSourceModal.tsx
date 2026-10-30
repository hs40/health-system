import { useQuery } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import type { StandardDetail } from '../api/types'
import { Modal } from './Modal'

/** 标准层字段 → 中文名（与后端 ``/api/data/standard`` 的 metrics 键一致）。 */
const METRIC_LABELS: Record<string, string> = {
  restingHeartRate: '静息心率',
  minHeartRate: '最低心率',
  maxHeartRate: '最高心率',
  sleepDurationSec: '睡眠时长',
  sleepStartTs: '入睡时间',
  sleepEndTs: '醒来时间',
  hrvLastNightAvg: 'HRV（夜均）',
  hrvLowUpper: 'HRV 低区上沿',
  hrvBalancedLow: 'HRV 平衡区下沿',
  hrvBalancedHigh: 'HRV 平衡区上沿',
  hrvWeeklyAvg: 'HRV 周均',
  hrvStatus: 'HRV 状态',
  sleepScore: '睡眠得分',
  bodyBatteryMorning: '身体电量（晨起）',
  bodyBatteryWake: '身体电量（同步时刻）',
  weightKg: '体重',
  steps: '步数',
  activeCalories: '活动消耗',
  intensityModerateMin: '中等强度',
  intensityVigorousMin: '高强度',
  spo2Avg: '血氧饱和度',
  respirationAvg: '呼吸率',
}

const METRIC_UNITS: Record<string, string> = {
  restingHeartRate: 'bpm',
  minHeartRate: 'bpm',
  maxHeartRate: 'bpm',
  hrvLastNightAvg: 'ms',
  weightKg: 'kg',
  steps: '步',
  activeCalories: 'kcal',
  intensityModerateMin: 'min',
  intensityVigorousMin: 'min',
  spo2Avg: '%',
  respirationAvg: '次/分',
}

/** 来源数据集 → 中文名（``_STANDARDIZE_SOURCES``）。 */
const DATASET_LABELS: Record<string, string> = {
  daily_summary: '日汇总',
  sleep: '睡眠',
  hrv: 'HRV',
  spo2: '血氧',
  respiration: '呼吸',
  weight: '体重',
}

function formatMetric(key: string, value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (key === 'sleepDurationSec') {
    const s = Number(value)
    if (!Number.isFinite(s)) return '—'
    return `${Math.floor(s / 3600)}h${String(Math.round((s % 3600) / 60)).padStart(2, '0')}m`
  }
  if (key === 'sleepStartTs' || key === 'sleepEndTs') {
    return String(value).replace('T', ' ').slice(0, 16)
  }
  return String(value)
}

export function StandardSourceModal({ day, onClose }: { day: string; onClose: () => void }) {
  const { data, isLoading, error } = useQuery<StandardDetail, ApiError>({
    queryKey: ['standard', day],
    queryFn: () => api.get<StandardDetail>(`/api/data/standard?date=${day}`),
    retry: false,
  })

  return (
    <Modal title={`数据溯源 · ${day}`} onClose={onClose}>
      {isLoading && <p className="text-sm hs-text-3">加载中…</p>}

      {error && (
        <p className="text-sm hs-text-p1">
          {error.status === 404
            ? `${day} 无标准化数据（尚未同步或该日缺数据）。`
            : `[${error.code}] ${error.message}`}
        </p>
      )}

      {data && (
        <div className="space-y-4">
          <div>
            <div className="hs-label mb-2">标准层取值</div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
              {Object.entries(data.metrics).map(([key, value]) => (
                <div key={key} className="flex items-baseline justify-between gap-2 text-sm">
                  <span className="hs-text-3">{METRIC_LABELS[key] ?? key}</span>
                  <span className="font-medium hs-text-1">
                    {formatMetric(key, value)}
                    {value !== null && value !== undefined && METRIC_UNITS[key] && (
                      <span className="ml-1 text-xs hs-text-3">{METRIC_UNITS[key]}</span>
                    )}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div>
            <div className="hs-label mb-2">
              原始出处（{data.source_refs.length} 个文件 · 相对 data/raw 的路径）
            </div>
            {data.source_refs.length === 0 ? (
              <p className="text-sm hs-text-3">该日未记录原始文件引用。</p>
            ) : (
              <ul className="space-y-1.5">
                {data.source_refs.map((r) => (
                  <li
                    key={r.path}
                    className="rounded-lg border hs-bd hs-bg-2 px-3 py-2 text-xs"
                  >
                    <div className="flex items-center gap-2">
                      <span className="rounded hs-bg-2 px-1.5 py-0.5 font-medium hs-text-2">
                        {DATASET_LABELS[r.dataset] ?? r.dataset}
                      </span>
                      <code className="hs-text-2">{r.path}</code>
                    </div>
                    <div className="mt-1 hs-text-3">
                      来源 {r.source} · 落盘时间 {String(r.fetched_at).replace('T', ' ').slice(0, 19)}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <p className="text-xs hs-text-3">
            原始快照为权威数据（REQ-DATA-001，只写一次、不覆盖）；此处仅引用，不复制内容。
          </p>
        </div>
      )}
    </Modal>
  )
}
