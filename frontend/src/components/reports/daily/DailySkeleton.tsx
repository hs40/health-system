/** 日报加载态骨架屏（纯展示）。 */
export function DailySkeletonBlock() {
  return (
    <div className="space-y-[var(--hs-gap)]">
      <div className="hs-card">
        <div className="hs-skeleton h-3 w-24" />
        <div className="hs-skeleton mt-2 h-8 w-64" />
        <div className="hs-skeleton mt-2 h-3 w-full" />
        <div className="hs-skeleton mt-1 h-3 w-3/4" />
      </div>
      <div className="hs-card">
        <div className="hs-skeleton h-4 w-40" />
        <div className="grid grid-cols-4 gap-2 py-3">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="hs-skeleton h-14" />
          ))}
        </div>
        <div className="hs-skeleton h-40 w-full" />
      </div>
    </div>
  )
}
