import { useEffect, type ReactNode } from 'react'

/** 轻量弹层：遮罩点击 / Esc 关闭；内容区阻止冒泡。 */
export function Modal({
  title,
  onClose,
  children,
}: {
  title: string
  onClose: () => void
  children: ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-auto p-4 sm:p-8"
      style={{ background: 'rgba(16,21,28,0.45)' }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="hs-card my-auto w-full max-w-3xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-[var(--hs-fs-h3)] font-semibold hs-text-1">{title}</h2>
          <button onClick={onClose} className="hs-btn">
            关闭
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}
