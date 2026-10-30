/**
 * 运动类型中文名与图表配色。
 *
 * 系统里存在两套运动类型写法，映射表必须同时覆盖：
 *   1. **活动/负荷数据**：后端 Sport 枚举值，snake_case
 *      （running / indoor_cardio / strength_training）
 *   2. **训练计划会话**：LLM 生成的周计划/处方使用 camelCase
 *      （running / indoorCardio / strength）
 * 只覆盖其中一套会导致页面直接展示原始枚举名（"STRENGTH_TRAINING"/"strength"）。
 */

/** 运动类型中文名（未列出的按原样展示，不静默吞掉未知类型）。 */
export const SPORT_LABELS: Record<string, string> = {
  running: '跑步',
  indoor_cardio: '室内有氧',
  indoorCardio: '室内有氧',
  strength_training: '力量',
  strength: '力量',
  lap_swimming: '游泳',
  lapSwimming: '游泳',
  hiit: 'HIIT',
  hiking: '徒步',
  walking: '步行',
  meditation: '冥想',
  breathwork: '呼吸训练',
  other: '其他',
}

/**
 * 图表系列配色：**单色阶分层**（2026-10-24 视觉方案）。
 *
 * 负荷图只用一个蓝灰色相，按训练优先级取三档明度：
 *   跑步（最深）→ 力量（中）→ 室内有氧（最浅）
 * 其余运动类型统一用最浅档，避免新增色相抢戏。
 * 深浅不是"数值大小"，而是稳定的类型身份——跨日报/周报/月报/负荷页一致。
 */
export const SPORT_COLORS: Record<string, string> = {
  running: '#22374d',
  strength_training: '#4e6b85',
  strength: '#4e6b85',
  indoor_cardio: '#8fa6bb',
  indoorCardio: '#8fa6bb',
  lap_swimming: '#c2ced9',
  lapSwimming: '#c2ced9',
  hiit: '#c2ced9',
  hiking: '#c2ced9',
  walking: '#c2ced9',
  meditation: '#c2ced9',
  breathwork: '#c2ced9',
  other: '#c2ced9',
}

export function sportLabel(sport: string): string {
  return SPORT_LABELS[sport] ?? sport
}

export function sportColor(sport: string): string {
  return SPORT_COLORS[sport] ?? '#c2ced9'
}

/**
 * 图例与堆叠顺序：跑步（最底、最深）→ 力量 → 室内有氧 → 其他。
 * 顺序固定后，颜色的深浅含义才稳定，不随当日出现的类型变化。
 */
const SPORT_RANK: Record<string, number> = {
  running: 0,
  strength_training: 1,
  strength: 1,
  indoor_cardio: 2,
  indoorCardio: 2,
}

export function sportRank(sport: string): number {
  return SPORT_RANK[sport] ?? 9
}
