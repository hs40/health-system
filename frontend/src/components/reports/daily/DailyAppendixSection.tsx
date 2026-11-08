/**
 * 日报「底部 · 详细追溯」区块（纯展示）。
 *
 * 五个折叠面板：AI 详细分析 / 数据来源 / 异常情况 / 计划调整原因 / 数据限制。
 * 从 `DailyPage` 抽出，只把「生成 AI 结论」「查看原始出处」两个动作回调出去。
 */
import type { DailyReport } from '../../../api/types'
import { Appendix } from '../../../components/ui/layout'

interface Props {
  report: DailyReport
  onGenerateAnalysis: () => void
  onShowSource: () => void
  busy: boolean
}

export function DailyAppendixSection({
  report,
  onGenerateAnalysis,
  onShowSource,
  busy,
}: Props) {
  const hasAnomaly = Boolean(
    report.risk?.missing_inputs?.length || report.quality.missing?.length,
  )

  return (
    <div className="space-y-[var(--hs-gap-sm)]">
      <Appendix title="AI 详细分析">
        {report.over_under_analysis ? (
          <p className="leading-relaxed">{report.over_under_analysis}</p>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <span>尚未生成 AI 分析。</span>
            <button className="hs-btn" onClick={onGenerateAnalysis} disabled={busy}>
              生成 AI 结论
            </button>
          </div>
        )}
      </Appendix>

      <Appendix title="数据来源">
        <div className="flex flex-wrap items-center gap-3">
          <span>Garmin 原始快照 → 标准层 → 本报告（每个取值可回溯到原始文件）。</span>
          <button className="hs-btn" onClick={onShowSource}>
            查看原始出处
          </button>
        </div>
        <div style={{ color: 'var(--hs-ink-4)' }}>
          数据状态 {report.risk?.data_state ?? '—'} · 质量等级 {report.quality.grade} · 规则版本{' '}
          {report.risk?.rule_version ?? '—'}
        </div>
      </Appendix>

      <Appendix title="异常情况">
        {hasAnomaly ? (
          <ul className="space-y-1">
            {report.risk?.missing_inputs?.map((m) => (
              <li key={`r-${m}`}>· 缺失输入：{m}</li>
            ))}
            {report.quality.missing?.map((m) => (
              <li key={`q-${m}`}>· 质量缺失：{m}</li>
            ))}
          </ul>
        ) : (
          <p>无缺失维度；本日未检出数据异常。</p>
        )}
        {report.risk?.triggered_rules?.length ? (
          <p style={{ color: 'var(--hs-ink-3)' }}>
            命中规则：{report.risk.triggered_rules.join('、')}
          </p>
        ) : null}
        {(report.quality.conflicts ?? []).length ? (
          <p>
            检出 {(report.quality.conflicts ?? []).length} 项跨源冲突，已按来源优先级选源（不做平均）。
          </p>
        ) : null}
      </Appendix>

      <Appendix title="计划调整原因">
        {report.adjustment_reason ? (
          <p>{report.adjustment_reason}</p>
        ) : (
          <p>本期计划未发生调整。</p>
        )}
        {report.risk?.volume_halved || report.risk?.intensity_reduced ? (
          <p>
            调整动作：{report.risk.volume_halved ? '容量减半 ' : ''}
            {report.risk.intensity_reduced ? '强度下调' : ''}
          </p>
        ) : null}
      </Appendix>

      <Appendix title="数据限制">
        {report.limitations?.length ? (
          <ul className="space-y-1">
            {report.limitations.map((l) => (
              <li key={l}>· {l}</li>
            ))}
          </ul>
        ) : (
          <p>无额外限制说明。</p>
        )}
        <p style={{ color: 'var(--hs-ink-4)' }}>
          本系统不提供医学诊断；所有阈值均为训练管理用途的工程口径。
        </p>
      </Appendix>
    </div>
  )
}
