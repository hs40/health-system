/**
 * 日报「顶部 · 当日结论」区块（纯展示）。
 *
 * 从 `DailyPage` 抽出：结论由规则化即时生成（`buildHeadline`），此处只负责渲染
 * 与把「重新分析」动作回调出去，不持有任何请求或状态。
 */
import type { DailyReport } from '../../../api/types'
import { Section } from '../../../components/ui/layout'
import { DataQualityBadge, QualityAlertBar, RiskBadge } from '../../../components/ui/badges'

export interface Headline {
  verdict: string
  tone: 'risk' | 'warn' | 'good' | 'default'
  reasons: string[]
}

const TONE_COLOR: Record<Headline['tone'], string> = {
  risk: 'var(--hs-p1)',
  warn: 'var(--hs-p2)',
  good: 'var(--hs-p4)',
  default: 'var(--hs-ink)',
}

interface Props {
  report: DailyReport
  headline: Headline
  onReanalyze: () => void
  busy: boolean
}

export function DailyHeadlineSection({ report, headline, onReanalyze, busy }: Props) {
  return (
    <Section>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-[240px] flex-1">
          <div className="hs-label">当日结论</div>
          <div
            className="mt-1 text-[var(--hs-fs-hero)] font-bold leading-tight tracking-tight"
            style={{ color: TONE_COLOR[headline.tone], fontSize: 'var(--hs-fs-h1)' }}
          >
            {headline.verdict}
          </div>
          <ul className="mt-2 space-y-1">
            {headline.reasons.map((r) => (
              <li
                key={r}
                className="flex gap-1.5 text-[var(--hs-fs-small)] leading-snug"
                style={{ color: 'var(--hs-ink-2)' }}
              >
                <span style={{ color: 'var(--hs-ink-4)' }}>·</span>
                {r}
              </li>
            ))}
          </ul>
        </div>

        <div className="flex flex-col items-start gap-2">
          <RiskBadge priority={report.risk?.priority} />
          <DataQualityBadge
            state={report.risk?.data_state}
            limitationCount={report.limitations?.length ?? 0}
          />
          <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
            规则 {report.risk?.rule_version ?? '—'} · 报告 v{report.version}
          </span>
        </div>
      </div>

      <QualityAlertBar
        state={report.risk?.data_state}
        limitations={report.limitations ?? []}
      />

      <div className="hs-divider" />

      {/* 今日训练安排 */}
      <div>
        <div className="mb-2 flex items-center justify-between">
          <span className="hs-label">今日训练安排</span>
          {report.today_plan_id && (
            <button className="hs-btn" onClick={onReanalyze} disabled={busy}>
              重新分析
            </button>
          )}
        </div>
        {report.today_plan_id ? (
          <div
            className="rounded-[var(--hs-r-sm)] border p-3"
            style={{ borderColor: 'var(--hs-border)', background: 'var(--hs-surface-2)' }}
          >
            <div className="text-[var(--hs-fs-h3)] font-bold" style={{ color: 'var(--hs-ink)' }}>
              {String(report.today_plan_id)}
            </div>
            <p className="mt-1 text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
              完整处方见「周计划」页；此处展示当日计划编号。
            </p>
          </div>
        ) : (
          <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
            {report.adjustment_reason
              ? `今日不生成计划：${report.adjustment_reason}`
              : '今日暂无训练计划。'}
          </p>
        )}
      </div>
    </Section>
  )
}
