/**
 * 单张训练日卡片（可拖动互换）。
 *
 * 卡片必备五项：名称、类型、目标、强度、时长（名称与时长醒目）。
 * 拖动状态由父组件持有（需要跨卡片判断 from/over），这里只做展示与事件上报。
 */
import type { WeekPlanCard, WeekPlanDay } from '../../../api/types'
import { Chip } from '../../../components/ui/badges'
import { CONFLICT_KIND_LABEL } from '../../../lib/reports'
import { sportLabel } from '../../../lib/sports'
import { durationText, goalText, type FullSession } from './format'

export interface ConflictMark {
  hard: boolean
  kinds: Set<string>
}

interface Props {
  day: WeekPlanDay
  card?: WeekPlanCard
  conflict?: ConflictMark
  isOpen: boolean
  isDragging: boolean
  isDragOver: boolean
  /** 是否已有某张卡正在被拖动——决定本卡是否可作为放置目标 */
  hasDragSource: boolean
  onDragStart: (date: string) => void
  onDragEnd: () => void
  onDragOver: (date: string) => void
  onDragLeave: (date: string) => void
  onDrop: (from: string, to: string) => void
  onClick: (date: string) => void
}

export function DayCard({
  day,
  card,
  conflict,
  isOpen,
  isDragging,
  isDragOver,
  hasDragSource,
  onDragStart,
  onDragEnd,
  onDragOver,
  onDragLeave,
  onDrop,
  onClick,
}: Props) {
  const session = day.session as FullSession | null | undefined

  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div
        draggable={Boolean(session)}
        onDragStart={(e) => {
          onDragStart(day.date)
          e.dataTransfer.effectAllowed = 'move'
          e.dataTransfer.setData('text/plain', day.date)
        }}
        onDragEnd={onDragEnd}
        onDragOver={(e) => {
          e.preventDefault()
          if (hasDragSource) onDragOver(day.date)
        }}
        onDragLeave={() => onDragLeave(day.date)}
        onDrop={(e) => {
          e.preventDefault()
          const from = e.dataTransfer.getData('text/plain')
          if (from) onDrop(from, day.date)
        }}
        onClick={() => onClick(day.date)}
        className={[
          'select-none overflow-hidden rounded-[var(--hs-r)] border p-2.5 transition-all duration-200',
          isDragging ? 'hs-dragging' : '',
          isDragOver ? 'hs-drag-over' : '',
          session ? 'cursor-grab active:cursor-grabbing' : 'cursor-pointer',
        ].join(' ')}
        style={{
          borderColor: conflict?.hard
            ? 'var(--hs-p1)'
            : conflict
              ? 'var(--hs-p2)'
              : 'var(--hs-border)',
          background: session ? 'var(--hs-surface)' : 'var(--hs-surface-2)',
          boxShadow: isOpen ? '0 2px 12px -6px rgba(31,94,255,.5)' : undefined,
          minHeight: 132,
        }}
        title={session ? '拖动可与其他日期互换；点击展开处方' : '休息日'}
      >
        {session ? (
          <>
            <div
              className="break-words text-[var(--hs-fs-small)] font-bold leading-snug"
              style={{ color: 'var(--hs-ink)' }}
            >
              {session.name}
            </div>
            <div className="mt-1 flex flex-wrap gap-1 [&>span]:min-w-0">
              <Chip tone="accent">{sportLabel(session.sport) ?? session.sport}</Chip>
              {session.target_hr_zone && <Chip tone="neutral">{session.target_hr_zone}</Chip>}
            </div>
            <div
              className="mt-1 break-words text-[var(--hs-fs-micro)] leading-snug"
              style={{ color: 'var(--hs-ink-3)' }}
            >
              目标：{goalText(session)}
            </div>
            <div
              className="mt-1.5 text-[var(--hs-fs-h2)] font-bold tabular-nums"
              style={{ color: 'var(--hs-accent-ink)' }}
            >
              {durationText(session)}
            </div>
            {conflict && (
              <div className="mt-1 flex flex-wrap gap-1 [&>span]:min-w-0">
                {[...conflict.kinds].map((k) => (
                  <Chip key={k} tone={conflict.hard ? 'p1' : 'p2'}>
                    {CONFLICT_KIND_LABEL[k] ?? k}
                  </Chip>
                ))}
              </div>
            )}
            {card?.is_adjusted && (
              <div className="mt-1">
                <Chip tone="p2">{card.badge ?? '日报已调整'}</Chip>
              </div>
            )}
          </>
        ) : (
          <div
            className="flex h-full items-center justify-center text-[var(--hs-fs-small)]"
            style={{ color: 'var(--hs-ink-4)' }}
          >
            休息日
          </div>
        )}
      </div>
    </div>
  )
}
