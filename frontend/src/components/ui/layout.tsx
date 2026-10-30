/**
 * 基础排版与容器组件。
 *
 * 分层原则（2026-10-22）：报告页**全部内容直接展开、不折叠**，
 * 层级靠排版降权表达——顶部用大号结论数字，中部用卡片网格，
 * 底部用 `Appendix`（低对比背景 + 小字号 + 左侧竖线）降权。
 */
import type { CSSProperties, ReactNode } from 'react'
import clsx from 'clsx'

export function Page({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={clsx('mx-auto w-full px-4 sm:px-6', className)}
      style={{ maxWidth: 'var(--hs-content-max)' }}
    >
      {children}
    </div>
  )
}

/** 三层之间的分隔标题：TOP / MIDDLE / BOTTOM 的可见锚点 */
export function TierLabel({ children, id }: { children: ReactNode; id?: string }) {
  return (
    <div id={id} className="mb-3 mt-2 flex items-center gap-2 scroll-mt-24">
      <span
        className="text-[10px] font-bold tracking-[0.18em]"
        style={{ color: 'var(--hs-ink-4)' }}
      >
        {children}
      </span>
      <span className="h-px flex-1" style={{ background: 'var(--hs-border)' }} />
    </div>
  )
}

export function Section({
  title,
  hint,
  right,
  children,
  id,
  className,
  flush,
}: {
  title?: ReactNode
  hint?: ReactNode
  right?: ReactNode
  children: ReactNode
  id?: string
  className?: string
  flush?: boolean
}) {
  return (
    <section id={id} className={clsx('hs-card scroll-mt-24', className)}>
      {(title || right) && (
        <header className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <div className="flex min-w-0 flex-col">
            {title && <h2 className="hs-section-title truncate">{title}</h2>}
            {hint && (
              <p className="mt-0.5 text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                {hint}
              </p>
            )}
          </div>
          {right}
        </header>
      )}
      <div className={flush ? '' : 'space-y-3'}>{children}</div>
    </section>
  )
}

/** 底部附录块：内容全展开，仅做视觉降权 */
export function Appendix({
  title,
  children,
  id,
}: {
  title: ReactNode
  children: ReactNode
  id?: string
}) {
  return (
    <div id={id} className="hs-appendix scroll-mt-24">
      <div
        className="mb-2 flex items-center gap-1.5 text-[var(--hs-fs-micro)] font-bold uppercase tracking-[0.1em]"
        style={{ color: 'var(--hs-ink-3)' }}
      >
        {title}
      </div>
      <div className="space-y-1.5 leading-relaxed">{children}</div>
    </div>
  )
}

export function Divider({ className }: { className?: string }) {
  return <div className={clsx('hs-divider', className)} />
}

/** 指标块：大号数字 + 标签 + 增量 + 说明 */
export function Metric({
  label,
  value,
  unit,
  delta,
  deltaUnit,
  goodDirection = 'none',
  hint,
  size = 'md',
  tone,
  style,
}: {
  label: string
  value: ReactNode
  unit?: string
  delta?: number | null
  deltaUnit?: string
  /** 增量方向的好坏判定：up=越大越好，down=越小越好 */
  goodDirection?: 'up' | 'down' | 'none'
  hint?: ReactNode
  size?: 'sm' | 'md' | 'lg'
  tone?: 'default' | 'risk' | 'good' | 'warn'
  style?: CSSProperties
}) {
  const valueSize =
    size === 'lg' ? 'var(--hs-fs-hero)' : size === 'sm' ? '18px' : 'var(--hs-fs-h1)'
  const toneColor =
    tone === 'risk'
      ? 'var(--hs-p1)'
      : tone === 'warn'
        ? 'var(--hs-p2)'
        : tone === 'good'
          ? 'var(--hs-p4)'
          : 'var(--hs-ink)'
  return (
    <div
      className="rounded-[var(--hs-r-sm)] border px-3 py-2.5"
      style={{ borderColor: 'var(--hs-border)', background: 'var(--hs-surface-2)', ...style }}
    >
      <div className="hs-label">{label}</div>
      <div className="mt-1 flex items-baseline gap-1">
        <span
          style={{
            fontFamily: 'var(--hs-font-num)',
            fontSize: valueSize,
            fontWeight: 650,
            letterSpacing: '-0.02em',
            lineHeight: 1.05,
            color: toneColor,
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {value ?? '—'}
        </span>
        {unit && (
          <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
            {unit}
          </span>
        )}
      </div>
      {/* 只有明确传入 delta（含 null = 无上期对比）时才渲染，避免无对比语义的卡片出现噪音 */}
      {delta !== undefined && (
        <Delta value={delta} unit={deltaUnit} goodDirection={goodDirection} />
      )}
      {hint && (
        <div className="mt-1 text-[var(--hs-fs-micro)] leading-snug" style={{ color: 'var(--hs-ink-4)' }}>
          {hint}
        </div>
      )}
    </div>
  )
}

export function Delta({
  value,
  unit,
  goodDirection = 'none',
  suffix,
}: {
  value?: number | null
  unit?: string
  goodDirection?: 'up' | 'down' | 'none'
  suffix?: string
}) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return (
      <span className="ml-1 text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
        {suffix ?? '无上期对比'}
      </span>
    )
  }
  const up = value > 0
  const flat = value === 0
  const good =
    goodDirection === 'none' || flat ? null : goodDirection === 'up' ? up : !up
  const color = good === null ? 'var(--hs-ink-3)' : good ? 'var(--hs-p4)' : 'var(--hs-p1)'
  return (
    <span
      className="ml-1 text-[var(--hs-fs-micro)] font-semibold"
      style={{ color }}
      title="与上一等长周期对比"
    >
      {flat ? '持平' : `${up ? '↑' : '↓'}${Math.abs(value).toFixed(Math.abs(value) < 1 ? 2 : 1)}`}
      {!flat && unit ? ` ${unit}` : ''}
    </span>
  )
}
