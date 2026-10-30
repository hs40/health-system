/** 图表数据点浮层：点击图上某点 → 显示该日/该周的明细与计算依据。 */
import { useEffect } from 'react'

export interface PointDetail {
  title: string
  subtitle?: string
  rows: { label: string; value: string }[]
  note?: string
}

export function PointPopover({
  detail,
  onClose,
}: {
  detail: PointDetail | null
  onClose: () => void
}) {
  useEffect(() => {
    if (!detail) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [detail, onClose])

  if (!detail) return null

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/20 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="w-full max-w-md rounded-[var(--hs-r)] border p-4 shadow-xl"
        style={{ background: 'var(--hs-surface)', borderColor: 'var(--hs-border-strong)' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-2 flex items-start justify-between gap-3">
          <div>
            <div className="text-[var(--hs-fs-h3)] font-bold" style={{ color: 'var(--hs-ink)' }}>
              {detail.title}
            </div>
            {detail.subtitle && (
              <div className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                {detail.subtitle}
              </div>
            )}
          </div>
          <button className="hs-btn px-2 py-0.5" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-[var(--hs-fs-small)]">
          {detail.rows.map((r) => (
            <div key={r.label} className="contents">
              <dt style={{ color: 'var(--hs-ink-4)' }}>{r.label}</dt>
              <dd className="text-right font-semibold" style={{ color: 'var(--hs-ink)' }}>
                {r.value}
              </dd>
            </div>
          ))}
        </dl>
        {detail.note && (
          <p
            className="mt-3 rounded-[var(--hs-r-sm)] p-2 text-[var(--hs-fs-micro)] leading-relaxed"
            style={{ background: 'var(--hs-surface-2)', color: 'var(--hs-ink-3)' }}
          >
            {detail.note}
          </p>
        )}
      </div>
    </div>
  )
}
