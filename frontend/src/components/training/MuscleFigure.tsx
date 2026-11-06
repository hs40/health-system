/**
 * Garmin 人体肌肉图（三期 §8.4，Q10/Q11）。
 *
 * 正/背面并排；primary 用主强调色、secondary 浅一档、未涉及灰显 —— 配色
 * 全部走 tokens.css 的语义变量，不在组件里写死色值。中文名为主、英文小字，
 * 与肌群表格口径一致。
 */
import { useMemo } from 'react'
import type { MuscleRegion } from '../../api/training'

type Shape = { x: number; y: number; w: number; h: number }

/** 各肌群在正/背面示意图上的分区（viewBox 0 0 180 400，坐标取整）。 */
const SHAPES: Record<string, { front: Shape[]; back: Shape[] }> = {
  SHOULDERS: {
    front: [{ x: 40, y: 70, w: 26, h: 20 }, { x: 114, y: 70, w: 26, h: 20 }],
    back: [{ x: 40, y: 70, w: 26, h: 20 }, { x: 114, y: 70, w: 26, h: 20 }],
  },
  CHEST: { front: [{ x: 66, y: 92, w: 48, h: 28 }], back: [] },
  BICEPS: {
    front: [{ x: 32, y: 98, w: 16, h: 32 }, { x: 132, y: 98, w: 16, h: 32 }],
    back: [],
  },
  TRICEPS: {
    front: [],
    back: [{ x: 32, y: 98, w: 16, h: 32 }, { x: 132, y: 98, w: 16, h: 32 }],
  },
  FOREARM: {
    front: [{ x: 30, y: 138, w: 14, h: 44 }, { x: 136, y: 138, w: 14, h: 44 }],
    back: [{ x: 30, y: 138, w: 14, h: 44 }, { x: 136, y: 138, w: 14, h: 44 }],
  },
  ABS: { front: [{ x: 78, y: 132, w: 24, h: 52 }], back: [] },
  OBLIQUES: {
    front: [{ x: 62, y: 138, w: 14, h: 44 }, { x: 104, y: 138, w: 14, h: 44 }],
    back: [],
  },
  HIPS: { front: [{ x: 76, y: 188, w: 28, h: 20 }], back: [] },
  QUADS: {
    front: [{ x: 66, y: 222, w: 22, h: 68 }, { x: 92, y: 222, w: 22, h: 68 }],
    back: [],
  },
  ABDUCTORS: {
    front: [],
    back: [{ x: 60, y: 202, w: 16, h: 30 }, { x: 104, y: 202, w: 16, h: 30 }],
  },
  ADDUCTORS: {
    front: [{ x: 82, y: 224, w: 7, h: 58 }, { x: 91, y: 224, w: 7, h: 58 }],
    back: [],
  },
  HAMSTRINGS: {
    front: [],
    back: [{ x: 66, y: 222, w: 22, h: 68 }, { x: 92, y: 222, w: 22, h: 68 }],
  },
  CALVES: {
    front: [{ x: 68, y: 302, w: 20, h: 50 }, { x: 92, y: 302, w: 20, h: 50 }],
    back: [{ x: 68, y: 302, w: 20, h: 50 }, { x: 92, y: 302, w: 20, h: 50 }],
  },
  TRAPS: { front: [], back: [{ x: 70, y: 66, w: 40, h: 24 }] },
  LATS: {
    front: [],
    back: [{ x: 58, y: 94, w: 26, h: 46 }, { x: 96, y: 94, w: 26, h: 46 }],
  },
  LOWER_BACK: { front: [], back: [{ x: 76, y: 144, w: 28, h: 40 }] },
  GLUTES: {
    front: [],
    back: [{ x: 64, y: 190, w: 24, h: 30 }, { x: 92, y: 190, w: 24, h: 30 }],
  },
}

/** 人体轮廓：静态 JSX 提到组件外，避免每次渲染重建。 */
const BODY_OUTLINE = (
  <g fill="var(--hs-surface-2)" stroke="var(--hs-border-strong)" strokeWidth={1.2}>
    <ellipse cx={90} cy={30} rx={17} ry={21} />
    <rect x={83} y={49} width={14} height={13} rx={3} />
    <path d="M52 66 L128 66 L116 210 L64 210 Z" />
    <path d="M52 66 L36 70 L26 194 L42 198 L58 92 Z" />
    <path d="M128 66 L144 70 L154 194 L138 198 L122 92 Z" />
    <path d="M64 210 L88 210 L84 382 L66 382 Z" />
    <path d="M92 210 L116 210 L114 382 L96 382 Z" />
  </g>
)

function fillOf(level: MuscleRegion['level']): string {
  if (level === 'primary') return 'var(--hs-accent)'
  if (level === 'secondary') return 'var(--hs-accent-soft)'
  return 'var(--hs-border)'
}

function BodyView({
  side,
  regions,
}: {
  side: 'front' | 'back'
  regions: Map<string, MuscleRegion>
}) {
  return (
    <svg
      viewBox="0 0 180 400"
      className="w-full"
      style={{ maxWidth: 168 }}
      role="img"
      aria-label={`人体肌肉图（${side === 'front' ? '正面' : '背面'}）`}
    >
      {BODY_OUTLINE}
      {Object.entries(SHAPES).map(([code, sides]) => {
        const region = regions.get(code)
        const shapes = side === 'front' ? sides.front : sides.back
        if (!region || shapes.length === 0) return null
        return (
          <g key={code}>
            {shapes.map((s, i) => (
              <rect
                key={i}
                x={s.x}
                y={s.y}
                width={s.w}
                height={s.h}
                rx={6}
                fill={fillOf(region.level)}
                stroke={region.level === 'secondary' ? 'var(--hs-accent)' : 'none'}
                strokeWidth={1}
              >
                <title>{`${region.zh}（${region.en}）· ${
                  region.level === 'primary' ? '主要' : region.level === 'secondary' ? '次要' : '未涉及'
                }`}</title>
              </rect>
            ))}
          </g>
        )
      })}
    </svg>
  )
}

export function MuscleFigure({ regions }: { regions: MuscleRegion[] }) {
  const regionMap = useMemo(() => new Map(regions.map((r) => [r.code, r])), [regions])
  const involved = regions.filter((r) => r.level !== 'none')

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap justify-center gap-8">
        {(['front', 'back'] as const).map((side) => (
          <div key={side} className="flex flex-col items-center gap-1">
            <span className="hs-label">{side === 'front' ? '正面' : '背面'}</span>
            <BodyView side={side} regions={regionMap} />
          </div>
        ))}
      </div>

      {involved.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {involved.map((r) => (
            <span
              key={r.code}
              className="inline-flex items-baseline gap-1 rounded-[var(--hs-r-sm)] border px-2 py-1 text-[var(--hs-fs-small)] font-semibold"
              style={{
                borderColor: r.level === 'primary' ? 'var(--hs-accent)' : 'var(--hs-border-strong)',
                background: r.level === 'primary' ? 'var(--hs-accent)' : 'var(--hs-accent-soft)',
                color: r.level === 'primary' ? '#fff' : 'var(--hs-accent-ink)',
              }}
            >
              {r.zh}
              <span
                className="text-[var(--hs-fs-micro)] font-normal"
                style={{ color: r.level === 'primary' ? 'rgba(255,255,255,.75)' : 'var(--hs-ink-4)' }}
              >
                {r.en}
              </span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
