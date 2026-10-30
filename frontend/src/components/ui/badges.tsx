/** 徽标族：风险等级（P1–P4 交通灯）、数据完整度、状态、冲突级别。 */
import type { ReactNode } from 'react'
import type { DataState, PriorityLevel } from '../../api/types'

type Tone = 'p1' | 'p2' | 'p3' | 'p4' | 'neutral' | 'accent'

const TONE_VARS: Record<Tone, { fg: string; bg: string; bd: string }> = {
  p1: { fg: 'var(--hs-p1)', bg: 'var(--hs-p1-soft)', bd: 'color-mix(in srgb, var(--hs-p1) 28%, transparent)' },
  p2: { fg: 'var(--hs-p2)', bg: 'var(--hs-p2-soft)', bd: 'color-mix(in srgb, var(--hs-p2) 28%, transparent)' },
  p3: { fg: 'var(--hs-p3)', bg: 'var(--hs-p3-soft)', bd: 'color-mix(in srgb, var(--hs-p3) 28%, transparent)' },
  p4: { fg: 'var(--hs-p4)', bg: 'var(--hs-p4-soft)', bd: 'color-mix(in srgb, var(--hs-p4) 28%, transparent)' },
  neutral: { fg: 'var(--hs-ink-3)', bg: 'var(--hs-surface-2)', bd: 'var(--hs-border)' },
  accent: { fg: 'var(--hs-accent-ink)', bg: 'var(--hs-accent-soft)', bd: 'color-mix(in srgb, var(--hs-accent) 30%, transparent)' },
}

export function Chip({
  tone = 'neutral',
  children,
  title,
  solid,
  className,
}: {
  tone?: Tone
  children: ReactNode
  title?: string
  solid?: boolean
  className?: string
}) {
  const v = TONE_VARS[tone]
  return (
    <span
      title={title}
      className={`inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full border px-2 py-[3px] text-[var(--hs-fs-micro)] font-semibold leading-none ${className ?? ''}`}
      style={{
        color: solid ? '#fff' : v.fg,
        background: solid ? v.fg : v.bg,
        borderColor: v.bd,
      }}
    >
      {children}
    </span>
  )
}

const PRIORITY: Record<PriorityLevel, { tone: Tone; label: string; sub: string }> = {
  P1: { tone: 'p1', label: 'P1 强制休息', sub: '不生成常规计划、不上传' },
  P2: { tone: 'p2', label: 'P2 减量', sub: '可生成但不上传' },
  P3: { tone: 'p3', label: 'P3 进阶加量', sub: '按推荐幅度加量' },
  P4: { tone: 'p4', label: 'P4 维持', sub: '按原计划执行' },
}

/** 风险等级：交通灯 + 文字（避免仅靠颜色传达信息） */
export function RiskBadge({
  priority,
  size = 'md',
}: {
  priority?: PriorityLevel | null
  size?: 'sm' | 'md' | 'lg'
}) {
  if (!priority) return <Chip tone="neutral">无风险判定</Chip>
  const m = PRIORITY[priority] ?? PRIORITY.P4
  if (size === 'sm') return <Chip tone={m.tone} solid>{m.label}</Chip>
  return (
    <div className="flex items-center gap-2">
      <span
        className="inline-flex items-center rounded-[var(--hs-r-sm)] px-3 py-1.5 font-bold"
        style={{
          background: TONE_VARS[m.tone].bg,
          color: TONE_VARS[m.tone].fg,
          border: `1px solid ${TONE_VARS[m.tone].bd}`,
          fontSize: size === 'lg' ? 'var(--hs-fs-h2)' : 'var(--hs-fs-h3)',
        }}
      >
        {m.label}
      </span>
      <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
        {m.sub}
      </span>
    </div>
  )
}

const STATE: Record<DataState, { tone: Tone; label: string }> = {
  A: { tone: 'p4', label: 'A 全景判定' },
  B: { tone: 'p2', label: 'B 纯负荷降级' },
  C: { tone: 'p1', label: 'C 安全兜底' },
}

export function DataQualityBadge({
  state,
  limitationCount = 0,
  onClick,
}: {
  state?: DataState | null
  limitationCount?: number
  onClick?: () => void
}) {
  const m = state ? (STATE[state] ?? STATE.C) : null
  const label = m ? m.label : '数据完整度未知'
  const extra = limitationCount > 0 ? ` · ${limitationCount} 项限制` : ''
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[var(--hs-fs-micro)] font-semibold"
      style={{
        color: m ? TONE_VARS[m.tone].fg : 'var(--hs-ink-3)',
        background: m ? TONE_VARS[m.tone].bg : 'var(--hs-surface-2)',
        borderColor: m ? TONE_VARS[m.tone].bd : 'var(--hs-border)',
        cursor: onClick ? 'pointer' : 'default',
      }}
      title="数据完整度：点开查看缺失项与限制"
    >
      <span
        className="inline-block h-1.5 w-1.5 rounded-full"
        style={{ background: m ? TONE_VARS[m.tone].fg : 'var(--hs-ink-4)' }}
      />
      {label}
      {extra}
    </button>
  )
}

/** 数据状态 C 或严重限制时，顶部升级为醒目警示条 */
export function QualityAlertBar({ state, limitations }: { state?: DataState | null; limitations: string[] }) {
  if (state !== 'C' && limitations.length === 0) return null
  const severe = state === 'C'
  return (
    <div
      className="flex items-start gap-2 rounded-[var(--hs-r-sm)] border px-3 py-2 text-[var(--hs-fs-small)]"
      style={{
        borderColor: severe ? TONE_VARS.p1.bd : TONE_VARS.p2.bd,
        background: severe ? TONE_VARS.p1.bg : TONE_VARS.p2.bg,
        color: severe ? TONE_VARS.p1.fg : 'var(--hs-p2)',
      }}
    >
      <span className="mt-[2px] font-bold">!</span>
      <div>
        <span className="font-semibold">
          {severe ? '数据兜底模式：结论置信度下降' : `存在 ${limitations.length} 项数据限制`}
        </span>
        <span className="ml-1 opacity-80">
          {severe
            ? '关键维度缺失，系统已按安全回退执行，以下结论仅作参考。'
            : '部分结论受数据覆盖度影响，详见页面底部「数据限制」。'}
        </span>
      </div>
    </div>
  )
}

export function StatusDot({ tone }: { tone: Tone }) {
  return (
    <span
      className="inline-block h-2 w-2 rounded-full"
      style={{ background: TONE_VARS[tone].fg }}
    />
  )
}
