/** 右上角抽屉：环境数据 / 消息中心 / 运维台共用。 */
import { useEffect, type ReactNode } from 'react'

export function Drawer({
  open,
  title,
  subtitle,
  onClose,
  children,
  footer,
  width = 420,
}: {
  open: boolean
  title: string
  subtitle?: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  width?: number
}) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  return (
    <>
      <div
        className={`fixed inset-0 z-40 bg-black/20 transition-opacity ${
          open ? 'opacity-100' : 'pointer-events-none opacity-0'
        }`}
        onClick={onClose}
        aria-hidden={!open}
      />
      <aside
        className={`fixed right-0 top-0 z-50 flex h-full flex-col border-l transition-transform duration-200 ${
          open ? 'translate-x-0' : 'translate-x-full'
        }`}
        style={{
          width: `min(${width}px, 92vw)`,
          background: 'var(--hs-surface)',
          borderColor: 'var(--hs-border)',
          boxShadow: '-12px 0 40px -24px rgba(16,21,28,.35)',
        }}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <header
          className="flex items-start justify-between gap-3 border-b px-4 py-3"
          style={{ borderColor: 'var(--hs-border)' }}
        >
          <div>
            <div className="text-[var(--hs-fs-h3)] font-bold" style={{ color: 'var(--hs-ink)' }}>
              {title}
            </div>
            {subtitle && (
              <div className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                {subtitle}
              </div>
            )}
          </div>
          <button className="hs-btn px-2 py-0.5" onClick={onClose} aria-label="关闭面板">
            ✕
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-4 py-3">{children}</div>
        {footer && (
          <footer className="border-t px-4 py-2" style={{ borderColor: 'var(--hs-border)' }}>
            {footer}
          </footer>
        )}
      </aside>
    </>
  )
}
