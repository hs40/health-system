/** 周计划吸底操作条：撤销 / AI 评估 / 保存为最终计划。 */
interface Props {
  dirty: boolean
  hardBlock: boolean
  confirmed: boolean
  canUndoStep: boolean
  canUndoAll: boolean
  canConfirm: boolean
  busy: string | null
  onUndoStep: () => void
  onUndoAll: () => void
  onEvaluate: () => void
  onConfirm: () => void
}

function statusText({ dirty, hardBlock, confirmed }: Pick<Props, 'dirty' | 'hardBlock' | 'confirmed'>) {
  if (dirty) return '有未评估的调整（已存为草稿）'
  if (hardBlock) return '当前草稿存在硬性阻断'
  if (confirmed) return '已保存为最终周计划'
  return '草稿已保存'
}

export function PlanActionBar({
  dirty,
  hardBlock,
  confirmed,
  canUndoStep,
  canUndoAll,
  canConfirm,
  busy,
  onUndoStep,
  onUndoAll,
  onEvaluate,
  onConfirm,
}: Props) {
  const isBusy = busy !== null

  return (
    <div
      className="fixed inset-x-0 bottom-0 z-30 border-t backdrop-blur"
      style={{ borderColor: 'var(--hs-border)', background: 'rgba(255,255,255,.92)' }}
    >
      <div className="mx-auto flex max-w-[var(--hs-content-max)] flex-wrap items-center gap-2 px-4 py-2.5">
        <span className="text-[var(--hs-fs-small)] font-semibold" style={{ color: 'var(--hs-ink-2)' }}>
          {statusText({ dirty, hardBlock, confirmed })}
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <button className="hs-btn" onClick={onUndoStep} disabled={!canUndoStep || isBusy}>
            撤销上一步
          </button>
          <button className="hs-btn" onClick={onUndoAll} disabled={!canUndoAll || isBusy}>
            撤销全部
          </button>
          <button className="hs-btn" onClick={onEvaluate} disabled={isBusy}>
            {busy === 'eval' ? '评估中…' : 'AI 评估'}
          </button>
          <button
            className="hs-btn hs-btn-primary"
            onClick={onConfirm}
            disabled={!canConfirm}
            title={hardBlock ? '存在硬性安全问题，不可确认' : dirty ? '请先完成 AI 评估' : ''}
          >
            {busy === 'confirm' ? '保存中…' : '保存为最终计划'}
          </button>
        </div>
      </div>
    </div>
  )
}
