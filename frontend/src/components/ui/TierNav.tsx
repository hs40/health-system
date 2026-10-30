/** 长页右侧层级导航：报告全部内容展开，靠它保持「不迷路」。 */
import { useEffect, useState } from 'react'

export interface TierItem {
  id: string
  label: string
}

export function TierNav({ items }: { items: TierItem[] }) {
  const [active, setActive] = useState(items[0]?.id ?? '')

  useEffect(() => {
    const els = items
      .map((i) => document.getElementById(i.id))
      .filter((e): e is HTMLElement => Boolean(e))
    if (els.length === 0) return
    const io = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
        if (visible[0]) setActive(visible[0].target.id)
      },
      { rootMargin: '-88px 0px -60% 0px', threshold: [0, 1] },
    )
    els.forEach((e) => io.observe(e))
    return () => io.disconnect()
  }, [items])

  if (items.length < 3) return null

  return (
    <nav className="hs-tiernav" aria-label="页面层级导航">
      {items.map((i) => {
        const on = i.id === active
        return (
          <a
            key={i.id}
            href={`#${i.id}`}
            onClick={(e) => {
              e.preventDefault()
              document.getElementById(i.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
            }}
            className="py-1 transition-colors"
            style={{
              color: on ? 'var(--hs-accent)' : 'var(--hs-ink-4)',
              fontWeight: on ? 700 : 500,
            }}
          >
            {i.label}
          </a>
        )
      })}
    </nav>
  )
}
