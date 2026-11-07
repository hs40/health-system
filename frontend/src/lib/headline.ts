/**
 * 日报顶部结论：**规则化即时生成**（不调 LLM，秒开）。
 *
 * 依据：风险优先级 + ACWR 状态 + 恢复指标缺口 + 今日计划是否生成。
 * 从 `DailyPage` 抽出——这是纯函数，与渲染无关，放 lib 便于单独验证。
 */
import type { DailyReport } from '../api/types'
import { numOrNull } from './reports'

export interface Headline {
  verdict: string
  tone: 'risk' | 'warn' | 'good' | 'default'
  reasons: string[]
}

export function buildHeadline(d: DailyReport): Headline {
  const p = d.risk?.priority
  const acwr = d.acwr?.ratio
  const state = d.risk?.data_state
  const reasons: string[] = []

  if (p === 'P1') {
    reasons.push('风险规则判定 P1：今日按恢复日模板执行，不生成常规训练')
    // 触发原因（「哪条规则 + 实测值 vs 阈值」）：阈值观察期要看得到「为什么」
    if (d.risk?.reasons?.length) reasons.push(...d.risk.reasons)
    if (d.advisory?.length) reasons.push(...d.advisory)
    return { verdict: '今日强制恢复', tone: 'risk', reasons }
  }
  if (p === 'P2') {
    reasons.push('风险规则判定 P2：正常训练降量执行')
    if (d.risk?.reasons?.length) reasons.push(...d.risk.reasons)
    if (d.risk?.volume_halved) reasons.push('训练容量已减半')
    if (d.risk?.intensity_reduced) reasons.push('训练强度已下调')
    return { verdict: '今日减量训练', tone: 'warn', reasons }
  }
  if (acwr !== null && acwr !== undefined) {
    if (acwr >= 1.5) {
      reasons.push(`ACWR ${acwr.toFixed(2)} 超过 1.5 上限，负荷快速累积`)
      return { verdict: '负荷偏高，建议降量', tone: 'warn', reasons }
    }
    if (acwr < 0.8) {
      reasons.push(`ACWR ${acwr.toFixed(2)} 低于 0.8，训练刺激不足`)
      return { verdict: '训练不足，可适度加量', tone: 'default', reasons }
    }
    reasons.push(`ACWR ${acwr.toFixed(2)} 位于 0.8–1.3 适宜区间`)
  } else {
    reasons.push('ACWR 不可计算（慢性负荷为 0 或历史不足）')
  }
  const rhr = numOrNull((d.recovery ?? {})['resting_heart_rate'])
  const hrv = numOrNull((d.recovery ?? {})['hrv_last_night_avg'])
  if (rhr !== null) reasons.push(`静息心率 ${rhr} bpm`)
  if (hrv !== null) reasons.push(`HRV 夜均 ${hrv} ms`)
  if (state === 'C') reasons.push('数据兜底模式，以上结论仅作参考')
  if (p === 'P3') return { verdict: '状态良好，按进阶加量执行', tone: 'good', reasons }
  return { verdict: '按计划维持训练', tone: 'good', reasons }
}
