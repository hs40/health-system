/** 任务状态徽标（运维台）：统一走 Chip 的 token 配色，不再写死 Tailwind 色阶。 */
import { Chip } from './ui/badges'

const TASK_TONE: Record<string, 'p4' | 'p2' | 'p1' | 'p3' | 'neutral'> = {
  success: 'p4',
  partial: 'p2',
  failed: 'p1',
  retry: 'p3',
  unknown: 'neutral',
}

const TASK_LABEL: Record<string, string> = {
  success: '成功',
  partial: '部分成功',
  failed: '失败',
  retry: '待重试',
  unknown: '结果未知',
}

export function StatusPill({ status }: { status: string }) {
  return <Chip tone={TASK_TONE[status] ?? 'neutral'}>{TASK_LABEL[status] ?? status}</Chip>
}
