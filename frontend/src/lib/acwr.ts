/**
 * ACWR 参考区间（需求 9.3）与图表语义色。
 *
 * 视觉规则（2026-10-24）：负荷用单色阶，ACWR 用唯一强调色；
 * 四个区间只做**极淡背景分区**，最佳区 0.8–1.3 略强并带描边。
 * 标签在图内按区间**垂直居中**（此前 markArea 名称渲染在上边界，
 * 导致「最佳区」压在 1.3 线上、看起来在标注「加量慎行」）。
 */

export interface AcwrZone {
  key: string
  /** 图内标签（含范围，便于脱离图例也能读懂） */
  label: string
  /** 图例/悬停用的短名 */
  short: string
  from: number
  to: number
  /** 背景填充（极淡） */
  fill: string
  /** 语义前景色（图例色块描边、短标签） */
  fg: string
  /** 最佳区：加描边并加重标签 */
  emphasize?: boolean
}

export const ACWR_ZONES: AcwrZone[] = [
  {
    key: 'undertraining',
    label: '训练不足 <0.8',
    short: '训练不足',
    from: 0,
    to: 0.8,
    fill: 'rgba(31,94,255,0.035)',
    fg: '#1f5eff',
  },
  {
    key: 'sweet_spot',
    label: '最佳区 0.8–1.3',
    short: '最佳区',
    from: 0.8,
    to: 1.3,
    fill: 'rgba(23,121,74,0.10)',
    fg: '#17794a',
    emphasize: true,
  },
  {
    key: 'elevated',
    label: '加量慎行 1.3–1.5',
    short: '加量慎行',
    from: 1.3,
    to: 1.5,
    fill: 'rgba(183,89,10,0.05)',
    fg: '#b7590a',
  },
  {
    key: 'danger',
    label: '高风险 >1.5',
    short: '高风险',
    from: 1.5,
    to: 2.4,
    fill: 'rgba(192,39,31,0.05)',
    fg: '#c0271f',
  },
]

export function acwrZone(state?: string | null): AcwrZone | undefined {
  return ACWR_ZONES.find((z) => z.key === state)
}

/** ACWR 折线/圆点：唯一强调色（状态由背景区间与 tooltip 表达） */
export const ACWR_LINE_COLOR = '#b45309'
