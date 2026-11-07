/** 完整训练处方：热身 / 主项 / 放松 / 力量组，直接铺开（卡片下方跨列展开）。 */
import { targetTypeLabel, type FullSession } from './format'

const labelStyle = { color: 'var(--hs-accent-ink)' }

function stepText(st: Record<string, unknown>): string {
  const parts: string[] = []
  if (st['name']) parts.push(String(st['name']))
  if (st['duration_sec']) parts.push(`${Math.round(Number(st['duration_sec']) / 60)} min`)
  if (st['distance_m']) parts.push(`${st['distance_m']} m`)
  if (st['hr_zone']) parts.push(`Z${st['hr_zone']}`)
  return parts.join(' · ') || '—'
}

export function SessionPrescription({ s }: { s: FullSession }) {
  const blocks: [string, Record<string, unknown>[]][] = [
    ['热身', s.warmup ?? []],
    ['主项', s.main ?? []],
    ['放松', s.cooldown ?? []],
  ]

  return (
    <div className="space-y-2">
      <div className="grid gap-2 sm:grid-cols-3">
        {blocks
          .filter(([, steps]) => steps.length > 0)
          .map(([label, steps]) => (
            <div key={label}>
              <div className="hs-label" style={labelStyle}>
                {label}
              </div>
              <ul className="mt-0.5 space-y-0.5">
                {steps.map((st, i) => (
                  <li key={i} style={{ color: 'var(--hs-ink-2)' }}>
                    {stepText(st)}
                  </li>
                ))}
              </ul>
            </div>
          ))}
      </div>

      {s.strength && s.strength.length > 0 && (
        <div>
          <div className="hs-label" style={labelStyle}>
            力量组
          </div>
          <ul className="mt-0.5 grid gap-x-3 gap-y-0.5 sm:grid-cols-2 lg:grid-cols-3">
            {s.strength.map((st, i) => (
              <li key={i} style={{ color: 'var(--hs-ink-2)' }}>
                {st.exercise} {st.sets}×{st.reps}
                {st.load_kg ? ` @${st.load_kg}kg` : '（负荷自选）'} 休息 {st.rest_sec}s
              </li>
            ))}
          </ul>
        </div>
      )}

      {s.rationale && (
        <p className="leading-relaxed" style={{ color: 'var(--hs-ink-3)' }}>
          <span className="font-bold">依据：</span>
          {s.rationale}
        </p>
      )}
      <div style={{ color: 'var(--hs-ink-4)' }}>
        目标 {targetTypeLabel(s.target_type)} · {s.target_hr_zone ?? '未设定强度区间'}
        {s.duration_min ? ` · 总时长 ${s.duration_min} min` : ''}
        {s.distance_m ? ` · 距离 ${s.distance_m} m` : ''}
      </div>
    </div>
  )
}
