/** AI 评估结果卡片（冲突列表 + 发现/建议/风险提示 + 硬性阻断提示）。 */
import type { PlanEvaluation } from '../../../api/types'
import { Chip } from '../../../components/ui/badges'
import { CONFLICT_KIND_LABEL } from '../../../lib/reports'

function EvalList({
  title,
  items,
  tone,
}: {
  title: string
  items: string[]
  tone?: 'p1'
}) {
  if (!items?.length) return null
  return (
    <div>
      <div className="hs-label" style={tone === 'p1' ? { color: 'var(--hs-p1)' } : undefined}>
        {title}
      </div>
      <ul className="mt-1 space-y-0.5 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-2)' }}>
        {items.map((t, i) => (
          <li key={i} className="flex gap-1">
            <span style={{ color: 'var(--hs-ink-4)' }}>·</span>
            {t}
          </li>
        ))}
      </ul>
    </div>
  )
}

export function EvaluationPanel({ evaluation }: { evaluation: PlanEvaluation }) {
  const hardBlock = Boolean(evaluation.hard_block)

  return (
    <div className="hs-card">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="hs-section-title">AI 评估结果</div>
        <div className="flex items-center gap-2">
          {hardBlock ? (
            <Chip tone="p1" solid>
              存在硬性安全问题
            </Chip>
          ) : (
            <Chip tone="p4">未发现硬性阻断</Chip>
          )}
          <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
            {evaluation.evaluated_at?.slice(0, 16).replace('T', ' ')}
          </span>
        </div>
      </div>

      {(evaluation.structured_conflicts ?? []).length > 0 && (
        <ul className="mb-3 space-y-1.5">
          {(evaluation.structured_conflicts ?? []).map((c, i) => (
            <li
              key={i}
              className="rounded-[var(--hs-r-sm)] border px-2.5 py-2 text-[var(--hs-fs-small)]"
              style={{
                borderColor: c.hard_block
                  ? 'color-mix(in srgb, var(--hs-p1) 30%, transparent)'
                  : c.severity === 'warn'
                    ? 'color-mix(in srgb, var(--hs-p2) 30%, transparent)'
                    : 'var(--hs-border)',
                background: c.hard_block
                  ? 'var(--hs-p1-soft)'
                  : c.severity === 'warn'
                    ? 'var(--hs-p2-soft)'
                    : 'var(--hs-surface-2)',
              }}
            >
              <div className="mb-0.5 flex flex-wrap items-center gap-1.5">
                <Chip tone={c.hard_block ? 'p1' : c.severity === 'warn' ? 'p2' : 'p3'}>
                  {CONFLICT_KIND_LABEL[c.kind] ?? c.kind}
                </Chip>
                {c.hard_block && <Chip tone="p1">硬性阻断</Chip>}
                {(c.days ?? []).length > 0 && (
                  <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                    {(c.days ?? []).map((d) => `${d.slice(5)}`).join(' / ')}
                  </span>
                )}
                {c.source === 'ai' && (
                  <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                    AI 提示
                  </span>
                )}
              </div>
              <div style={{ color: 'var(--hs-ink-2)' }}>{c.message}</div>
            </li>
          ))}
        </ul>
      )}

      <div className="grid gap-3 sm:grid-cols-3">
        <EvalList title="发现" items={evaluation.findings ?? []} />
        <EvalList title="建议" items={evaluation.suggestions ?? []} />
        <EvalList title="风险提示" items={evaluation.risk_notes ?? []} tone="p1" />
      </div>

      {hardBlock && (
        <p
          className="mt-3 rounded-[var(--hs-r-sm)] px-2.5 py-2 text-[var(--hs-fs-small)] font-semibold"
          style={{ background: 'var(--hs-p1-soft)', color: 'var(--hs-p1)' }}
        >
          该计划存在硬性安全问题，系统不会因为你的确认而生效。请先按上面的建议调整（如换日、降强度或插入恢复日），再重新评估。
        </p>
      )}
    </div>
  )
}
