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
 *
 * 本文件只负责**数据编排与交互状态**；展示区块拆到 `components/plan/weekly/`。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import type { PlanEvaluation, WeekPlanDay, WeeklyPlan } from '../../api/types'
import { mondayOf, todayISO } from '../../lib/date'
import { markedSet } from '../../lib/reports'
import { Page } from '../../components/ui/layout'
import { Chip } from '../../components/ui/badges'
import { DateNav } from '../../components/ui/DateNav'
import { useReportIndex } from '../../hooks/reportQueries'
import { WeekHeader } from '../../components/plan/weekly/WeekHeader'
import { DayCard, type ConflictMark } from '../../components/plan/weekly/DayCard'
import { SessionPrescription } from '../../components/plan/weekly/SessionPrescription'
import { EvaluationPanel } from '../../components/plan/weekly/EvaluationPanel'
import { PlanActionBar } from '../../components/plan/weekly/PlanActionBar'
import { STATUS_LABEL, type FullSession } from '../../components/plan/weekly/format'

interface SwapStep {
  from: string
  to: string
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
      baseline.current.days = (plan.data.days ?? []).map((d) => ({ ...d }))
    }
    setOriginal(baseline.current.days)

    if (plan.data) {
      setDays((plan.data.days ?? []).map((d) => ({ ...d })))
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
    const next = original.map((d) => ({ ...d }))
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
    const map = new Map<string, ConflictMark>()
    for (const c of evaluation?.structured_conflicts ?? []) {
      for (const d of c.days ?? []) {
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
    expandedIdx >= 0
      ? ((days?.[expandedIdx]?.session as FullSession | null | undefined) ?? null)
      : null

  const status = plan.data?.status ?? 'draft'
  const hardBlock = Boolean(evaluation?.hard_block)
  const isBusy = busy !== null
  const canConfirm =
    Boolean(days) && !hardBlock && !dirty && !isBusy && (status === 'evaluated' || status === 'draft')

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
                disabled={isBusy}
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
              <WeekHeader days={days} onToggleNight={toggleNight} />

              {/* —— 第二行：七张训练卡片（可拖动互换）—— */}
              <div
                className="mt-2 grid items-start"
                style={{
                  gridTemplateColumns: 'repeat(7, minmax(0, 1fr))',
                  gap: 'var(--hs-gap-sm)',
                }}
              >
                {days.map((d) => (
                  <DayCard
                    key={d.date}
                    day={d}
                    card={plan.data?.cards?.[d.date]}
                    conflict={conflictsByDate.get(d.date)}
                    isOpen={expanded === d.date}
                    isDragging={dragFrom === d.date}
                    isDragOver={dragOver === d.date}
                    hasDragSource={Boolean(dragFrom) && dragFrom !== d.date}
                    onDragStart={setDragFrom}
                    onDragEnd={() => {
                      setDragFrom(null)
                      setDragOver(null)
                    }}
                    onDragOver={setDragOver}
                    onDragLeave={(date) => setDragOver((cur) => (cur === date ? null : cur))}
                    onDrop={(from, to) => {
                      setDragOver(null)
                      setDragFrom(null)
                      if (from !== to) swap(from, to)
                    }}
                    onClick={(date) => setExpanded((cur) => (cur === date ? null : date))}
                  />
                ))}

                {/* 详情：卡片下方展开（从所点列横向跨到行尾，日期不动）。
                    七列各约 95px，处方文字在单列内会严重折行，故跨列展示。 */}
                {expandedIdx >= 0 && expandedSession && (
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

        {evaluation && <EvaluationPanel evaluation={evaluation} />}

        {days && (
          <PlanActionBar
            dirty={dirty}
            hardBlock={hardBlock}
            confirmed={status === 'confirmed'}
            canUndoStep={history.current.length > 0}
            canUndoAll={Boolean(original)}
            canConfirm={canConfirm}
            busy={busy}
            onUndoStep={undoStep}
            onUndoAll={undoAll}
            onEvaluate={() => {
              setBusy('eval')
              evaluate.mutate()
            }}
            onConfirm={() => {
              setBusy('confirm')
              confirm.mutate()
            }}
          />
        )}
      </div>
    </Page>
  )
}
