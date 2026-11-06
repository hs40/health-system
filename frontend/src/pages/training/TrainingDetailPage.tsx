/**
 * 单次运动训练详情页（三期 §7.4 / §7.5 / §7.6 / §8）。
 *
 * 六类模板：跑步 / 室内有氧 / HIIT / 力量 / 游泳 / 其他。
 * 缺失的专属指标不展示、不虚构（§7.4 / §8.6）；不显示开始时间、不重复
 * Emoji 与运动类型颜色、不提供 FIT 下载入口（§7.4 / §7.8）。
 */
import { useNavigate, useSearchParams } from 'react-router-dom'
import { todayISO } from '../../lib/date'
import { useTrainingDetail } from '../../hooks/trainingQueries'
import type {
  ActivityDetail,
  DetailTemplate,
  MetricRow,
  MuscleRegion,
  SegmentRow,
  SwimmingData,
  TrainingEffect,
} from '../../api/training'
import { HrSeriesChart, HrZoneChart, StrengthTimelineView } from '../../components/training/TrainingCharts'
import { MuscleFigure } from '../../components/training/MuscleFigure'

function MetricsTable({ rows }: { rows: MetricRow[] }) {
  return (
    <dl className="grid grid-cols-[8rem_1fr] gap-y-2 text-[var(--hs-fs-small)]">
      {rows.map((r) => (
        <div key={r.label} className="contents">
          <dt style={{ color: 'var(--hs-ink-4)' }}>{r.label}</dt>
          <dd className="font-semibold" style={{ color: 'var(--hs-ink)' }}>
            {r.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function EffectView({ effect }: { effect: TrainingEffect }) {
  return (
    <div className="space-y-1 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-2)' }}>
      <div>
        训练效果：<b style={{ color: 'var(--hs-ink)' }}>{effect.label}</b>
      </div>
      {effect.aerobic && <div>有氧：{effect.aerobic}</div>}
      {effect.anaerobic && <div>无氧：{effect.anaerobic}</div>}
    </div>
  )
}

/** Q8 脚注：口径来源 + 缺失秒数披露（不补 0）。 */
function HrZoneFootnote({ activity }: { activity: ActivityDetail }) {
  const z = activity.hr_zones
  return (
    <p className="mt-3 text-[var(--hs-fs-micro)] leading-relaxed" style={{ color: 'var(--hs-ink-4)' }}>
      按 Karvonen 五区（HRmax {z.max_hr} / HRrest {z.rest_hr}）由逐秒心率重算。
      {z.below_zone_seconds > 0 && ` 低于 Z1 下界 ${z.below_zone_seconds} 秒未计入。`}
      {z.missing_seconds > 0 && ` 心率缺失 ${z.missing_seconds} 秒未补 0，单独披露。`}
    </p>
  )
}

function LapSegmentTable({ segments }: { segments: SegmentRow[] }) {
  if (segments.length === 0) {
    return <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>本次无分段数据</p>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[var(--hs-fs-small)]">
        <thead>
          <tr style={{ color: 'var(--hs-ink-4)' }}>
            <th className="py-1.5 font-semibold">分段</th>
            <th className="py-1.5 font-semibold">内容</th>
            <th className="py-1.5 font-semibold">时长</th>
            <th className="py-1.5 font-semibold">距离</th>
            <th className="py-1.5 font-semibold">平均心率</th>
          </tr>
        </thead>
        <tbody>
          {segments.map((s, i) => (
            <tr key={i} className="border-t" style={{ borderColor: 'var(--hs-border)' }}>
              <td className="py-1.5" style={{ color: 'var(--hs-ink-3)' }}>{s.index ?? i + 1}</td>
              <td className="py-1.5 font-semibold" style={{ color: 'var(--hs-ink)' }}>{s.label ?? '分段'}</td>
              <td className="py-1.5">{s.duration_sec} 秒</td>
              <td className="py-1.5">{s.distance_m != null ? `${s.distance_m} m` : '—'}</td>
              <td className="py-1.5">{s.avg_hr != null ? `${s.avg_hr} bpm` : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** §8.3 HIIT 分段注明动作中文名（Q7 动作库）。 */
function HiitSegmentList({ segments }: { segments: SegmentRow[] }) {
  if (segments.length === 0) {
    return <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>本次无分段数据</p>
  }
  return (
    <ul className="space-y-1.5 text-[var(--hs-fs-small)]">
      {segments.map((s, i) => (
        <li key={i} style={{ color: s.kind === 'rest' ? 'var(--hs-ink-4)' : 'var(--hs-ink)' }}>
          {s.kind === 'rest' ? (
            <>休息 {s.duration_sec} 秒</>
          ) : (
            <>
              <b>{s.name_zh}</b>
              {s.reps != null && ` ${s.reps} 次`}
              {s.weight_kg != null && ` · ${s.weight_kg} kg`}
              <span className="ml-1.5" style={{ color: 'var(--hs-ink-4)' }}>{s.duration_sec} 秒</span>
            </>
          )}
        </li>
      ))}
    </ul>
  )
}

/** Q11 肌群表格：中文为主 + 英文小字，按「肌群 → 本次涉及动作」聚合。 */
function MuscleTable({ rows }: { rows: NonNullable<ActivityDetail['muscle_table']> }) {
  if (rows.length === 0) {
    return <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>本次无肌群数据</p>
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[var(--hs-fs-small)]">
        <thead>
          <tr style={{ color: 'var(--hs-ink-4)' }}>
            <th className="py-1.5 font-semibold">训练肌群</th>
            <th className="py-1.5 font-semibold">主要动作</th>
            <th className="py-1.5 font-semibold">次要动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.code} className="border-t" style={{ borderColor: 'var(--hs-border)' }}>
              <td className="py-1.5 font-semibold" style={{ color: 'var(--hs-ink)' }}>
                {r.zh}
                <span className="ml-1.5 text-[var(--hs-fs-micro)] font-normal" style={{ color: 'var(--hs-ink-4)' }}>
                  {r.en}
                </span>
              </td>
              <td className="py-1.5">{r.primary_moves.join('、') || '—'}</td>
              <td className="py-1.5">{r.secondary_moves.join('、') || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function StrengthSection({ activity }: { activity: ActivityDetail }) {
  return (
    <div className="space-y-4">
      <section className="hs-card">
        <h2 className="hs-section-title mb-3">训练时间轴</h2>
        {activity.timeline ? (
          <StrengthTimelineView timeline={activity.timeline} />
        ) : (
          <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>本次无逐组数据</p>
        )}
      </section>
      {/* §8.4 / Q11：先表格后人体图 */}
      <section className="hs-card">
        <h2 className="hs-section-title mb-3">训练肌群</h2>
        <MuscleTable rows={activity.muscle_table ?? []} />
      </section>
      <section className="hs-card">
        <h2 className="hs-section-title mb-3">肌肉热力图</h2>
        {activity.muscles && activity.muscles.length > 0 ? (
          <MuscleFigure regions={activity.muscles as MuscleRegion[]} />
        ) : (
          <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>本次无肌群数据</p>
        )}
      </section>
    </div>
  )
}

function SwimmingSection({ data }: { data: SwimmingData | undefined }) {
  if (!data) return null
  const rows: MetricRow[] = [
    { label: '游泳距离', value: data.distance_m != null ? `${data.distance_m} m` : '—' },
    { label: '泳池长度', value: data.pool_length_m != null ? `${data.pool_length_m} m` : '—' },
    { label: '划水次数', value: data.strokes != null ? String(data.strokes) : '—' },
    { label: '泳姿', value: data.stroke_types.length ? data.stroke_types.join('、') : '—' },
    { label: '平均 SWOLF', value: data.average_swolf != null ? String(data.average_swolf) : '—' },
  ]
  return (
    <section className="hs-card">
      <h2 className="hs-section-title mb-3">游泳专属指标</h2>
      <MetricsTable rows={rows} />
      {data.lengths.length > 0 && (
        <div className="mt-4">
          <h3 className="hs-label mb-2">分段时间</h3>
          <div className="flex flex-wrap gap-2">
            {data.lengths.slice(0, 40).map((l, i) => (
              <span
                key={i}
                className="rounded-[var(--hs-r-sm)] border px-2 py-1 text-[var(--hs-fs-micro)]"
                style={{ borderColor: 'var(--hs-border)', color: 'var(--hs-ink-3)' }}
              >
                {l.stroke ?? '分段'} · {l.duration_sec}s
                {l.strokes != null && ` · ${l.strokes} 次`}
              </span>
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="hs-card">
      <h2 className="hs-section-title mb-3">{title}</h2>
      {children}
    </section>
  )
}

/** 按六类模板组织详情内容；「其他」只保留基本信息，不补不存在的指标。 */
function DetailBody({ activity }: { activity: ActivityDetail }) {
  const t: DetailTemplate = activity.template
  const cardio = t === 'running' || t === 'indoor_cardio' || t === 'hiit'
  return (
    <div className="space-y-4">
      <Card title="训练指标">
        <MetricsTable rows={activity.metrics} />
      </Card>

      {cardio && (
        <>
          <Card title="心率区间">
            <HrZoneChart stats={activity.hr_zones} />
            <HrZoneFootnote activity={activity} />
          </Card>
          <Card title="训练效果">
            <EffectView effect={activity.training_effect} />
          </Card>
          <Card title="时间—心率">
            <HrSeriesChart points={activity.hr_series} />
          </Card>
          <Card title="训练分段">
            {t === 'hiit' ? (
              <HiitSegmentList segments={activity.segments} />
            ) : (
              <LapSegmentTable segments={activity.segments} />
            )}
          </Card>
        </>
      )}

      {t === 'strength' && <StrengthSection activity={activity} />}
      {t === 'swimming' && <SwimmingSection data={activity.swimming} />}
    </div>
  )
}

export default function TrainingDetailPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const date = params.get('date') ?? todayISO()
  const rawSeq = Number(params.get('seq') ?? 1)
  const seq = Number.isFinite(rawSeq) && rawSeq >= 1 ? Math.floor(rawSeq) : 1
  // ym/type 仅用于返回日历时恢复状态；详情内容本身不沿用筛选（§7.6）
  const ym = params.get('ym') ?? date.slice(0, 7)
  const type = params.get('type') ?? 'all'
  const { data, isLoading, isError } = useTrainingDetail(date, seq)

  const calendarHref = `/training?${new URLSearchParams({ ym, type }).toString()}`
  const goto = (nextDate: string, nextSeq: number) => {
    const p = new URLSearchParams({ date: nextDate, seq: String(nextSeq), ym, type })
    navigate(`/training/detail?${p.toString()}`)
  }

  return (
    <div className="mx-auto px-4 sm:px-6" style={{ maxWidth: 920 }}>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <button type="button" className="hs-btn" onClick={() => navigate(calendarHref)}>
          ← 返回日历
        </button>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="hs-btn"
            disabled={!data || seq <= 1}
            onClick={() => goto(date, seq - 1)}
          >
            ← 上一条
          </button>
          <button
            type="button"
            className="hs-btn"
            disabled={!data || seq >= data.total}
            onClick={() => goto(date, seq + 1)}
          >
            下一条 →
          </button>
        </div>
      </div>

      {isLoading && <div className="hs-card">加载中…</div>}
      {isError && (
        <div className="hs-card text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-p1)' }}>
          详情数据不可用，请返回日历重试。
        </div>
      )}

      {data && (
        <>
          <div className="hs-card mb-4">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h1 className="hs-section-title">{date}</h1>
              <span className="text-[var(--hs-fs-small)] font-semibold" style={{ color: 'var(--hs-ink-3)' }}>
                {data.activity?.label ?? '训练'} · {data.activity?.name ?? ''}
              </span>
            </div>
            {data.has_training && (
              <p className="mt-1.5 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
                第 {data.seq} 次训练 / 共 {data.total} 次
              </p>
            )}
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <button type="button" className="hs-btn" onClick={() => goto(data.prev_date, 1)}>
                ← 上一天
              </button>
              <button type="button" className="hs-btn" onClick={() => goto(data.next_date, 1)}>
                下一天 →
              </button>
            </div>
          </div>

          {data.activity ? (
            <DetailBody activity={data.activity} />
          ) : (
            <div className="hs-card text-center">
              <p className="text-[var(--hs-fs-body)] font-semibold" style={{ color: 'var(--hs-ink)' }}>
                这一天没有训练记录
              </p>
              <p className="mt-1 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
                可以用上一天 / 下一天继续浏览，或返回日历。
              </p>
              <button type="button" className="hs-btn mt-3" onClick={() => navigate(calendarHref)}>
                返回日历
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
