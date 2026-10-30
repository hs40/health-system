/**
 * 周计划页（2026-10-22 重构）。
 *
 * 布局：第一行固定「周一…周日 + 日期」（不随拖动改动）；第二行七张训练卡片可拖动互换。
 * 卡片必备五项：训练计划名称、训练类型、训练目标、强度、时长（名称与时长醒目）。
 * 交互：
 *  - 拖动交换 → 立即保存草稿（不覆盖最终计划）→ 提示「AI 评估」按钮
 *  - 点击卡片在卡片下方展开完整处方，再次点击收起（日期不动）
 *  - 命名：周计划内的拖动/换位**不加任何前缀**。「调整」前缀只用于日报对当天计划的调整
 *    （需求 §12.2：只有日报调整后的训练名称前加「调整」，不得扩展为所有周计划版本变化）
 *  - 撤销：逐步撤销（操作栈）+ 撤销全部
 *  - 硬性安全阻断（后端 hard_block）→ 禁止保存为最终计划，且不可绕过
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import type {
  PlanEvaluation,
  TrainingSessionBrief,
  WeekPlanCard,
  WeekPlanDay,
  WeeklyPlan,
} from '../../api/types'
import { mondayOf, parseISO, todayISO, WEEKDAYS } from '../../lib/date'
import { sportLabel } from '../../lib/sports'
import { CONFLICT_KIND_LABEL } from '../../lib/reports'
import { Page } from '../../components/ui/layout'
import { Chip } from '../../components/ui/badges'
import { DateNav } from '../../components/ui/DateNav'
import { markedSet } from '../../lib/reports'
import { useReportIndex } from '../../hooks/reportQueries'

const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  evaluated: '已评估',
  confirmed: '已确认',
}

/** 需要完整处方的会话形态（后端 TrainingSession） */
interface FullSession extends TrainingSessionBrief {
  warmup?: Record<string, unknown>[]
  main?: Record<string, unknown>[]
  cooldown?: Record<string, unknown>[]
  strength?: { exercise: string; sets: number; reps: number; load_kg?: number | null; rest_sec: number }[]
  rationale?: string | null
  target_type?: string | null
  distance_m?: number | null
}

interface SwapStep {
  from: string
  to: string
}

function durationText(s?: TrainingSessionBrief | null): string {
  if (!s?.duration_min) return '—'
  return `${s.duration_min} min`
}

/** 卡片「训练目标」文案：力量日不套用心率区间说法（否则出现「力量 · 目标 Z2 区间有氧」） */
function goalText(s: TrainingSessionBrief): string {
  if (s.sport === 'strength' || s.sport === 'strength_training') return '力量与肌耐力（按组次）'
  return s.target_hr_zone ? `${s.target_hr_zone} 区间有氧` : '按处方执行'
}

/** target_type 是 pace | heartRate | open，直接展示会泄漏英文枚举 */
const TARGET_TYPE_LABEL: Record<string, string> = {
  pace: '配速',
  heartRate: '心率',
  open: '未指定',
}
function targetTypeLabel(t?: string | null): string {
  if (!t) return '未指定'
  return TARGET_TYPE_LABEL[t] ?? t
}

export default function WeeklyPlanPage() {
  const qc = useQueryClient()
  const [weekStart, setWeekStart] = useState<string>(mondayOf(todayISO()))
  const [days, setDays] = useState<WeekPlanDay[] | null>(null)
  const [original, setOriginal] = useState<WeekPlanDay[] | null>(null)
  const [evaluation, setEvaluation] = useState<PlanEvaluation | null>(null)
  const [dirty, setDirty] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [dragFrom, setDragFrom] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState<string | null>(null)
  const history = useRef<SwapStep[]>([])

  const index = useReportIndex('weekly')

  const plan = useQuery<WeeklyPlan, ApiError>({
    queryKey: ['weeklyPlan', weekStart],
    queryFn: () => api.get<WeeklyPlan>(`/api/plan/weekly/${weekStart}`),
    retry: false,
  })

  const notFound = plan.error instanceof ApiError && plan.error.status === 404

  /**
   * 「原始计划」基线：本周**首次加载**时的计划快照，仅用于「撤销全部」。
   * 周计划内的拖动/换位不加名称前缀（前缀只属于日报对当天计划的调整，§12.2）。
   * 必须是会话级快照——若每次 plan.data 刷新都重置，撤销全部会退回到已调整的数据。
   */
  const baseline = useRef<{ week: string; days: WeekPlanDay[] | null }>({ week: '', days: null })

  useEffect(() => {
    const weekChanged = baseline.current.week !== weekStart
    if (weekChanged) baseline.current = { week: weekStart, days: null }
    if (plan.data && !baseline.current.days) {
      baseline.current.days = structuredClone(plan.data.days)
    }
    setOriginal(baseline.current.days)

    if (plan.data) {
      setDays(structuredClone(plan.data.days))
      setEvaluation(plan.data.evaluation ?? null)
    } else {
      setDays(null)
      setEvaluation(null)
    }

    // 仅换周时重置编辑态；保存草稿后的刷新保留提示与历史
    if (weekChanged) {
      setDirty(false)
      setExpanded(null)
      history.current = []
      setMsg(null)
    }
  }, [plan.data, weekStart])

  const reload = () => void qc.invalidateQueries({ queryKey: ['weeklyPlan', weekStart] })

  const saveDraft = useMutation<WeeklyPlan, ApiError, WeekPlanDay[]>({
    mutationFn: (next) =>
      api.put<WeeklyPlan>(`/api/plan/weekly/${weekStart}`, {
        week_start: weekStart,
        days: next,
        status: 'draft',
        version: plan.data?.version ?? 1,
      }),
    onSettled: () => setBusy(null),
    onSuccess: () => {
      setDirty(false)
      setMsg('调整已保存为草稿（未覆盖最终计划）')
      reload()
    },
    onError: (e) => setMsg(`保存草稿失败：[${e.code}] ${e.message}`),
  })

  const evaluate = useMutation<PlanEvaluation, ApiError>({
    mutationFn: () => api.post<PlanEvaluation>(`/api/plan/weekly/${weekStart}/evaluate`, {}),
    onSettled: () => setBusy(null),
    onSuccess: (data) => {
      setEvaluation(data)
      setMsg(
        data.hard_block
          ? 'AI 评估完成：发现硬性安全问题，该计划不能保存为最终计划'
          : 'AI 评估完成：可确认保存为最终计划',
      )
      reload()
    },
    onError: (e) => setMsg(`评估失败：[${e.code}] ${e.message}`),
  })

  const confirm = useMutation<WeeklyPlan, ApiError>({
    mutationFn: () => api.post<WeeklyPlan>(`/api/plan/weekly/${weekStart}/confirm`, {}),
    onSettled: () => setBusy(null),
    onSuccess: () => {
      setMsg('已保存为最终周计划')
      setDirty(false)
      reload()
    },
    onError: (e) => setMsg(`确认失败：[${e.code}] ${e.message}`),
  })

  const generate = useMutation<WeeklyPlan, ApiError>({
    mutationFn: () =>
      api.post<WeeklyPlan>(`/api/plan/weekly/${weekStart}/generate`, {
        night_shift_days: (days ?? []).filter((d) => d.is_night_shift).map((d) => d.date),
      }),
    onSettled: () => setBusy(null),
    onSuccess: () => {
      setMsg('已生成本周计划草稿')
      reload()
    },
    onError: (e) => setMsg(`生成失败：[${e.code}] ${e.message}`),
  })

  /** 拖动交换：只互换 session（日期不动）；换完立即保存草稿 */
  const swap = (fromDate: string, toDate: string) => {
    if (!days || fromDate === toDate) return
    const next = days.map((d) => {
      if (d.date !== fromDate && d.date !== toDate) return d
      const other = days.find((x) => x.date === (d.date === fromDate ? toDate : fromDate))
      // session.date 必须与所在日一致（后端校验）；名称保持原样，不加「调整」前缀
      const sess = other?.session ? { ...other.session, date: d.date } : null
      const nightFrom = days.find((x) => x.date === fromDate)?.is_night_shift
      const nightTo = days.find((x) => x.date === toDate)?.is_night_shift
      const night = d.date === fromDate ? nightTo : nightFrom
      return { ...d, session: sess, is_night_shift: Boolean(night) }
    })
    setDays(next)
    history.current.push({ from: fromDate, to: toDate })
    setDirty(true)
    setEvaluation(null)
    setBusy('save')
    saveDraft.mutate(next)
  }

  const undoStep = () => {
    if (!days || history.current.length === 0) return
    const last = history.current.pop()
    if (!last) return
    const next = days.map((d) => {
      if (d.date !== last.from && d.date !== last.to) return d
      const other = days.find((x) => x.date === (d.date === last.from ? last.to : last.from))
      const sess = other?.session ? { ...other.session, date: d.date } : null
      return { ...d, session: sess }
    })
    setDays(next)
    setDirty(true)
    setEvaluation(null)
    setBusy('save')
    saveDraft.mutate(next)
  }

  const undoAll = () => {
    if (!original) return
    const next = structuredClone(original)
    setDays(next)
    history.current = []
    setDirty(true)
    setEvaluation(null)
    setBusy('save')
    saveDraft.mutate(next)
  }

  const toggleNight = (date: string) => {
    if (!days) return
    const next = days.map((d) => (d.date === date ? { ...d, is_night_shift: !d.is_night_shift } : d))
    setDays(next)
    setDirty(true)
    setEvaluation(null)
    setBusy('save')
    saveDraft.mutate(next)
  }

  const conflictsByDate = useMemo(() => {
    const map = new Map<string, { hard: boolean; kinds: Set<string> }>()
    for (const c of evaluation?.structured_conflicts ?? []) {
      for (const d of c.days) {
        const cur = map.get(d) ?? { hard: false, kinds: new Set<string>() }
        cur.hard = cur.hard || c.hard_block
        cur.kinds.add(c.kind)
        map.set(d, cur)
      }
    }
    return map
  }, [evaluation])

  // 展开中的处方：跨列展示需要列下标与 session
  const expandedIdx = days?.findIndex((d) => d.date === expanded) ?? -1
  const expandedSession =
    expandedIdx >= 0 ? ((days?.[expandedIdx]?.session as FullSession | null | undefined) ?? null) : null

  const status = plan.data?.status ?? 'draft'
  const hardBlock = Boolean(evaluation?.hard_block)
  const canConfirm =
    Boolean(days) && !hardBlock && !dirty && !busy && (status === 'evaluated' || status === 'draft')

  return (
    <Page>
      <div className="space-y-[var(--hs-gap)] pb-24">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-baseline gap-2">
            <h1 className="text-[var(--hs-fs-h1)] font-bold tracking-tight" style={{ color: 'var(--hs-ink)' }}>
              周计划
            </h1>
            <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
              七天横向 · 拖动换位 · AI 复核后确认
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <DateNav
              mode="weekly"
              value={weekStart}
              onChange={setWeekStart}
              marked={markedSet(index.data?.items)}
            />
            {plan.data && (
              <Chip tone={status === 'confirmed' ? 'p4' : status === 'evaluated' ? 'p3' : 'neutral'}>
                {STATUS_LABEL[status] ?? status} · v{plan.data.version}
              </Chip>
            )}
            {plan.data?.evaluation?.hard_block && (
              <Chip tone="p1" solid>
                上次评估：硬性阻断
              </Chip>
            )}
          </div>
        </header>

        {msg && (
          <div
            className="rounded-[var(--hs-r-sm)] border px-3 py-2 text-[var(--hs-fs-small)]"
            style={{
              borderColor: hardBlock ? 'var(--hs-p1)' : 'var(--hs-border)',
              background: hardBlock ? 'var(--hs-p1-soft)' : 'var(--hs-surface)',
              color: hardBlock ? 'var(--hs-p1)' : 'var(--hs-ink-2)',
            }}
          >
            {msg}
          </div>
        )}

        {notFound && !days && (
          <div className="hs-card">
            <div className="hs-section-title">本周尚无计划</div>
            <p className="mt-1 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
              可先生成草稿（依据上周实际训练、负荷/ACWR、睡眠恢复、训练能力、夜班与恢复间隔）。
            </p>
            <div className="mt-3">
              <button
                className="hs-btn hs-btn-primary"
                onClick={() => {
                  setBusy('gen')
                  generate.mutate()
                }}
                disabled={busy !== null}
              >
                生成草稿
              </button>
            </div>
          </div>
        )}

        {plan.isLoading && <div className="hs-card hs-skeleton h-40" />}

        {plan.error && !notFound && (
          <div className="hs-card">
            <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-p1)' }}>
              [{plan.error.code}] {plan.error.message}
            </p>
          </div>
        )}

        {days && (
          <div className="hs-card overflow-x-auto pb-2">
            <div className="min-w-[720px]">
              {/* —— 第一行：星期一至日 + 日期（固定，不随拖动改变）—— */}
              <div
                className="grid border-b pb-2"
                style={{
                  borderColor: 'var(--hs-border)',
                  gridTemplateColumns: 'repeat(7, minmax(0, 1fr))',
                  gap: 'var(--hs-gap-sm)',
                }}
              >
                {days.map((d, i) => {
                  const isToday = d.date === todayISO()
                  return (
                    <div key={`h-${d.date}`} className="min-w-0 px-1">
                      <div
                        className="whitespace-nowrap text-[var(--hs-fs-h3)] font-bold leading-tight"
                        style={{ color: isToday ? 'var(--hs-accent)' : 'var(--hs-ink)' }}
                      >
                        {WEEKDAYS[i] ?? d.date}
                      </div>
                      <div
                        className="whitespace-nowrap text-[var(--hs-fs-micro)] font-semibold"
                        style={{ color: 'var(--hs-ink-3)' }}
                      >
                        {parseISO(d.date).getMonth() + 1}月{parseISO(d.date).getDate()}日
                      </div>
                      <label
                        className="mt-1 inline-flex cursor-pointer items-center gap-1 text-[var(--hs-fs-micro)]"
                        style={{ color: d.is_night_shift ? 'var(--hs-accent-ink)' : 'var(--hs-ink-4)' }}
                        title="夜班日：次日安排高强度会被规则阻断"
                      >
                        <input
                          type="checkbox"
                          checked={d.is_night_shift}
                          onChange={() => toggleNight(d.date)}
                          className="h-3 w-3"
                        />
                        夜班
                      </label>
                    </div>
                  )
                })}
              </div>

              {/* —— 第二行：七张训练卡片（可拖动互换）—— */}
              <div
                className="mt-2 grid items-start"
                style={{
                  gridTemplateColumns: 'repeat(7, minmax(0, 1fr))',
                  gap: 'var(--hs-gap-sm)',
                }}
              >
                {days.map((d) => {
                  const s = d.session as FullSession | null | undefined
                  const card: WeekPlanCard | undefined = plan.data?.cards?.[d.date]
                  const conflict = conflictsByDate.get(d.date)
                  const isOpen = expanded === d.date
                  return (
                    <div key={d.date} className="flex min-w-0 flex-col gap-1">
                      <div
                        draggable={Boolean(s)}
                        onDragStart={(e) => {
                          setDragFrom(d.date)
                          e.dataTransfer.effectAllowed = 'move'
                          e.dataTransfer.setData('text/plain', d.date)
                        }}
                        onDragEnd={() => {
                          setDragFrom(null)
                          setDragOver(null)
                        }}
                        onDragOver={(e) => {
                          e.preventDefault()
                          if (dragFrom && dragFrom !== d.date) setDragOver(d.date)
                        }}
                        onDragLeave={() => setDragOver((cur) => (cur === d.date ? null : cur))}
                        onDrop={(e) => {
                          e.preventDefault()
                          const from = e.dataTransfer.getData('text/plain') || dragFrom
                          setDragOver(null)
                          setDragFrom(null)
                          if (from && from !== d.date) swap(from, d.date)
                        }}
                        onClick={() => setExpanded(isOpen ? null : d.date)}
                        className={[
                          'select-none overflow-hidden rounded-[var(--hs-r)] border p-2.5 transition-all duration-200',
                          dragFrom === d.date ? 'hs-dragging' : '',
                          dragOver === d.date ? 'hs-drag-over' : '',
                          s ? 'cursor-grab active:cursor-grabbing' : 'cursor-pointer',
                        ].join(' ')}
                        style={{
                          borderColor: conflict?.hard
                            ? 'var(--hs-p1)'
                            : conflict
                              ? 'var(--hs-p2)'
                              : 'var(--hs-border)',
                          background: s ? 'var(--hs-surface)' : 'var(--hs-surface-2)',
                          boxShadow: isOpen ? '0 2px 12px -6px rgba(31,94,255,.5)' : undefined,
                          minHeight: 132,
                        }}
                        title={s ? '拖动可与其他日期互换；点击展开处方' : '休息日'}
                      >
                        {s ? (
                          <>
                            <div
                              className="break-words text-[var(--hs-fs-small)] font-bold leading-snug"
                              style={{ color: 'var(--hs-ink)' }}
                            >
                              {s.name}
                            </div>
                            <div className="mt-1 flex flex-wrap gap-1 [&>span]:min-w-0">
                              <Chip tone="accent">{sportLabel(s.sport) ?? s.sport}</Chip>
                              {s.target_hr_zone && <Chip tone="neutral">{s.target_hr_zone}</Chip>}
                            </div>
                            <div
                              className="mt-1 break-words text-[var(--hs-fs-micro)] leading-snug"
                              style={{ color: 'var(--hs-ink-3)' }}
                            >
                              目标：{goalText(s)}
                            </div>
                            <div
                              className="mt-1.5 text-[var(--hs-fs-h2)] font-bold tabular-nums"
                              style={{ color: 'var(--hs-accent-ink)' }}
                            >
                              {durationText(s)}
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
                })}

                {/* 详情：卡片下方展开（从所点列横向跨到行尾，日期不动）。
                    七列各约 95px，处方文字在单列内会严重折行，故跨列展示。 */}
                {expandedIdx != null && expandedSession && (
                  <div
                    className="rounded-[var(--hs-r-sm)] border p-2.5 text-[var(--hs-fs-small)]"
                    style={{
                      gridColumn: `${expandedIdx + 1} / -1`,
                      borderColor: 'var(--hs-accent)',
                      background: 'var(--hs-accent-soft)',
                    }}
                  >
                    <div className="mb-1.5 flex items-center justify-between gap-2">
                      <span className="font-bold" style={{ color: 'var(--hs-accent-ink)' }}>
                        {expandedSession.name} · 完整处方
                      </span>
                      <button
                        className="rounded border px-2 py-0.5 text-[var(--hs-fs-micro)] font-semibold"
                        style={{
                          borderColor: 'var(--hs-border-strong)',
                          background: 'var(--hs-surface)',
                          color: 'var(--hs-ink-2)',
                        }}
                        onClick={() => setExpanded(null)}
                      >
                        收起
                      </button>
                    </div>
                    <SessionPrescription s={expandedSession} />
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {evaluation && (
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

            {evaluation.structured_conflicts.length > 0 && (
              <ul className="mb-3 space-y-1.5">
                {evaluation.structured_conflicts.map((c, i) => (
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
                      {c.days.length > 0 && (
                        <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
                          {c.days.map((d) => `${d.slice(5)}`).join(' / ')}
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
              <EvalList title="发现" items={evaluation.findings} />
              <EvalList title="建议" items={evaluation.suggestions} />
              <EvalList title="风险提示" items={evaluation.risk_notes} tone="p1" />
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
        )}

        {/* —— 吸底操作条 —— */}
        {days && (
          <div
            className="fixed inset-x-0 bottom-0 z-30 border-t backdrop-blur"
            style={{ borderColor: 'var(--hs-border)', background: 'rgba(255,255,255,.92)' }}
          >
            <div className="mx-auto flex max-w-[var(--hs-content-max)] flex-wrap items-center gap-2 px-4 py-2.5">
              <span className="text-[var(--hs-fs-small)] font-semibold" style={{ color: 'var(--hs-ink-2)' }}>
                {dirty
                  ? '有未评估的调整（已存为草稿）'
                  : hardBlock
                    ? '当前草稿存在硬性阻断'
                    : status === 'confirmed'
                      ? '已保存为最终周计划'
                      : '草稿已保存'}
              </span>
              <div className="ml-auto flex flex-wrap items-center gap-2">
                <button
                  className="hs-btn"
                  onClick={undoStep}
                  disabled={history.current.length === 0 || busy !== null}
                >
                  撤销上一步
                </button>
                <button className="hs-btn" onClick={undoAll} disabled={!original || busy !== null}>
                  撤销全部
                </button>
                <button
                  className="hs-btn"
                  onClick={() => {
                    setBusy('eval')
                    evaluate.mutate()
                  }}
                  disabled={busy !== null || !days}
                >
                  {busy === 'eval' ? '评估中…' : 'AI 评估'}
                </button>
                <button
                  className="hs-btn hs-btn-primary"
                  onClick={() => {
                    setBusy('confirm')
                    confirm.mutate()
                  }}
                  disabled={!canConfirm}
                  title={hardBlock ? '存在硬性安全问题，不可确认' : dirty ? '请先完成 AI 评估' : ''}
                >
                  {busy === 'confirm' ? '保存中…' : '保存为最终计划'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </Page>
  )
}

function EvalList({ title, items, tone }: { title: string; items: string[]; tone?: 'p1' }) {
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

/** 完整训练处方：热身 / 主项 / 放松 / 力量组，直接铺开（卡片下方跨列展开） */
function SessionPrescription({ s }: { s: FullSession }) {
  const stepText = (st: Record<string, unknown>) => {
    const parts: string[] = []
    if (st['name']) parts.push(String(st['name']))
    if (st['duration_sec']) parts.push(`${Math.round(Number(st['duration_sec']) / 60)} min`)
    if (st['distance_m']) parts.push(`${st['distance_m']} m`)
    if (st['hr_zone']) parts.push(`Z${st['hr_zone']}`)
    return parts.join(' · ') || '—'
  }
  const blocks: [string, Record<string, unknown>[]][] = [
    ['热身', s.warmup ?? []],
    ['主项', s.main ?? []],
    ['放松', s.cooldown ?? []],
  ]
  return (
    <div className="space-y-2">
      <div className="grid gap-2 sm:grid-cols-3">
        {blocks
          .filter(([, steps]) => steps.length > 0)
          .map(([label, steps]) => (
            <div key={label}>
              <div className="hs-label" style={{ color: 'var(--hs-accent-ink)' }}>
                {label}
              </div>
              <ul className="mt-0.5 space-y-0.5">
                {steps.map((st, i) => (
                  <li key={i} style={{ color: 'var(--hs-ink-2)' }}>
                    {stepText(st)}
                  </li>
                ))}
              </ul>
            </div>
          ))}
      </div>

      {s.strength && s.strength.length > 0 && (
        <div>
          <div className="hs-label" style={{ color: 'var(--hs-accent-ink)' }}>
            力量组
          </div>
          <ul className="mt-0.5 grid gap-x-3 gap-y-0.5 sm:grid-cols-2 lg:grid-cols-3">
            {s.strength.map((st, i) => (
              <li key={i} style={{ color: 'var(--hs-ink-2)' }}>
                {st.exercise} {st.sets}×{st.reps}
                {st.load_kg ? ` @${st.load_kg}kg` : '（负荷自选）'} 休息 {st.rest_sec}s
              </li>
            ))}
          </ul>
        </div>
      )}

      {s.rationale && (
        <p className="leading-relaxed" style={{ color: 'var(--hs-ink-3)' }}>
          <span className="font-bold">依据：</span>
          {s.rationale}
        </p>
      )}
      <div style={{ color: 'var(--hs-ink-4)' }}>
        目标 {targetTypeLabel(s.target_type)} · {s.target_hr_zone ?? '未设定强度区间'}
        {s.duration_min ? ` · 总时长 ${s.duration_min} min` : ''}
        {s.distance_m ? ` · 距离 ${s.distance_m} m` : ''}
      </div>
    </div>
  )
}

