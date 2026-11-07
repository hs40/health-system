/**
 * 日报「前一日运动训练回顾」内容表（纯展示）。
 *
 * 只读 `yesterday_training`：不回退到 `report.load`——那是**今天**的负荷，
 * 放进「前一日回顾」会张冠李戴（2026-10-26 修复）。
 */
import type { DailyReport } from '../../../api/types'
import { StatusDot } from '../../../components/ui/badges'
import { numOrNull } from '../../../lib/reports'
import { sportLabel } from '../../../lib/sports'
import { hhmm } from './format'

const HEADERS = ['运动类型', '训练内容', '时长', '负荷', '完成度'] as const

const cell = 'border-b px-2 py-1.5'

function durationLabel(a: Record<string, unknown>): string {
  if (numOrNull(a['duration_sec'])) return hhmm(a['duration_sec'])
  if (numOrNull(a['duration_min'])) return `${a['duration_min']} 分钟`
  return '—'
}

export function DailyTrainingTable({ report }: { report: DailyReport }) {
  const yt = (report.yesterday_training ?? {}) as Record<string, unknown>
  const activities = (yt['activities'] ?? yt['items'] ?? []) as Record<string, unknown>[]

  if (activities.length === 0) {
    return (
      <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
        前一日无训练记录。
      </p>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[var(--hs-fs-small)]">
        <thead>
          <tr style={{ color: 'var(--hs-ink-4)' }}>
            {HEADERS.map((h) => (
              <th
                key={h}
                className="border-b px-2 py-1.5 text-left font-semibold"
                style={{ borderColor: 'var(--hs-border)' }}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {activities.map((a, i) => {
            const sport = String(a['sport'] ?? a['type'] ?? '—')
            return (
              <tr key={i} style={{ color: 'var(--hs-ink-2)' }}>
                <td className={cell} style={{ borderColor: 'var(--hs-border)' }}>
                  <span className="inline-flex items-center gap-1.5">
                    <StatusDot tone="p3" />
                    {sport === 'rest' ? '休息' : sportLabel(sport)}
                  </span>
                </td>
                <td className={cell} style={{ borderColor: 'var(--hs-border)' }}>
                  {String(a['name'] ?? a['title'] ?? '训练记录')}
                </td>
                <td className={cell} style={{ borderColor: 'var(--hs-border)' }}>
                  {durationLabel(a)}
                </td>
                <td
                  className={`${cell} font-semibold`}
                  style={{ borderColor: 'var(--hs-border)' }}
                >
                  {a['load'] !== undefined ? String(Math.round(Number(a['load']))) : '—'}
                </td>
                <td className={cell} style={{ borderColor: 'var(--hs-border)' }}>
                  {a['completion'] !== undefined
                    ? String(a['completion'])
                    : String(a['status'] ?? '—')}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
