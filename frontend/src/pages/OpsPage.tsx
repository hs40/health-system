import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import type { AuditEvent, HealthResponse, TaskRun } from '../api/types'
import { Page, Section } from '../components/ui/layout'
import { DateNav } from '../components/ui/DateNav'
import { TierNav } from '../components/ui/TierNav'
import { StatusPill } from '../components/Badges'

function fmt(ts?: string | null): string {
  if (!ts) return '—'
  return new Date(ts).toLocaleString('zh-CN', { hour12: false })
}

/** 数据同步状态（§8 运维：本地覆盖 + 最近校验）。 */
interface SyncStatus {
  generated_at: string
  datasets: Record<string, { files: number; latest: string }>
  tredict_activity_mirror: number
  sync_ledger: { rows: number; failed: number }
  last_verification: { report: string; generated_at?: string; diff?: number } | null
}

interface SyncResponse {
  accepted: boolean
  date: string
  sync: { status: string; written: string[]; skipped: string[]; missing: string[]; errors: Record<string, string> }
  report?: {
    priority: string | null
    data_state: string | null
    triggered_rules: string[]
    quality: string
  }
}

export default function OpsPage({ embedded = false }: { embedded?: boolean }) {
  const qc = useQueryClient()
  const [day, setDay] = useState<string>(new Date().toISOString().slice(0, 10))
  const [message, setMessage] = useState<string | null>(null)

  const health = useQuery<HealthResponse, ApiError>({
    queryKey: ['health'],
    queryFn: () => api.get<HealthResponse>('/api/ops/health'),
    refetchInterval: 15000,
  })
  const tasks = useQuery<{ tasks: TaskRun[] }, ApiError>({
    queryKey: ['tasks'],
    queryFn: () => api.get<{ tasks: TaskRun[] }>('/api/ops/tasks?limit=50'),
  })
  const audit = useQuery<{ events: AuditEvent[] }, ApiError>({
    queryKey: ['audit'],
    queryFn: () => api.get<{ events: AuditEvent[] }>('/api/ops/audit?limit=50'),
  })
  const syncStatus = useQuery<SyncStatus, ApiError>({
    queryKey: ['syncStatus'],
    queryFn: () => api.get<SyncStatus>('/api/ops/sync-status'),
    refetchInterval: 30000,
  })

  const sync = useMutation<SyncResponse, ApiError, string>({
    mutationFn: (d: string) => api.post<SyncResponse>('/api/ops/sync', { mode: 'incremental', date: d }),
    onSuccess: (data) => {
      const errs = Object.keys(data.sync.errors || {}).length
      setMessage(
        `同步完成（${data.date}）：${data.sync.status}，新写入 ${data.sync.written.length}，` +
          `跳过 ${data.sync.skipped.length}，缺失 ${data.sync.missing.length}，错误 ${errs}` +
          (data.report
            ? ` · 优先级 ${data.report.priority ?? '—'}（${data.report.data_state ?? '—'} 态）/ 质量 ${data.report.quality}`
            : ''),
      )
      void qc.invalidateQueries({ queryKey: ['tasks'] })
      void qc.invalidateQueries({ queryKey: ['audit'] })
      void qc.invalidateQueries({ queryKey: ['health'] })
      void qc.invalidateQueries({ queryKey: ['daily'] })
    },
    onError: (e) => setMessage(`同步失败：[${e.code}] ${e.message}`),
  })

  return (
    <Page className="space-y-[var(--hs-gap)] py-[var(--hs-gap)]">
      <header className="flex flex-wrap items-end justify-between gap-3">
        {embedded ? (
          <div />
        ) : (
          <div>
            <h1 className="text-[var(--hs-fs-h1)] font-bold" style={{ color: 'var(--hs-ink)' }}>
              运维台
            </h1>
            <p className="mt-1 text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
              系统健康 · 数据同步 · 任务与审计
            </p>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <DateNav mode="daily" value={day} onChange={setDay} />
          <button
            onClick={() => sync.mutate(day)}
            disabled={sync.isPending}
            className="hs-btn hs-btn-primary"
          >
            {sync.isPending ? '同步中…' : '同步并生成日报'}
          </button>
        </div>
      </header>

      {message && (
        <div className="rounded-lg border hs-bd hs-bg-2 px-3 py-2 text-sm hs-text-2">
          {message}
        </div>
      )}

      <div className="grid grid-cols-1 gap-[var(--hs-gap)] lg:grid-cols-[1fr_120px]">
        <div className="space-y-[var(--hs-gap)]">
          <Section id="ops-health" title="系统健康">
            {health.isLoading && <p className="text-sm hs-text-3">检测中…</p>}
            {health.data && (
              <>
                <div className="flex items-center gap-3">
                  <span
                    className={`inline-flex h-2.5 w-2.5 rounded-full ${
                      health.data.status === 'ok' ? 'hs-dot-p4' : 'hs-dot-p2'
                    }`}
                  />
                  <span className="text-sm font-medium hs-text-2">
                    {health.data.status} · v{health.data.version} · 规则 {health.data.risk_rule_version}
                  </span>
                </div>
                <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  {(health.data.deps ?? []).map((d) => (
                    <div
                      key={d.name}
                      className="flex items-center justify-between gap-2 rounded-lg border hs-bd hs-bg-2 px-3 py-2"
                    >
                      <span className="text-sm hs-text-2">{d.name}</span>
                      <span className="flex-1 truncate text-[11px] hs-text-3">{d.detail}</span>
                      <span className={`text-xs font-medium ${d.ok ? 'hs-text-p4' : 'hs-text-p2'}`}>
                        {d.ok ? '正常' : '未就绪'}
                      </span>
                    </div>
                  ))}
                </div>
              </>
            )}
          </Section>

          <Section id="ops-sync" title="数据同步状态">
            {syncStatus.isLoading && <p className="text-sm hs-text-3">加载中…</p>}
            {syncStatus.error && (
              <p className="text-sm hs-text-p1">[{syncStatus.error.code}] {syncStatus.error.message}</p>
            )}
            {syncStatus.data && (
              <div className="space-y-3 text-sm">
                <div className="flex flex-wrap gap-3 text-xs hs-text-3">
                  <span>
                    最近校验：
                    {syncStatus.data.last_verification
                      ? `${syncStatus.data.last_verification.report}（差异 ${
                          syncStatus.data.last_verification.diff ?? '—'
                        }）`
                      : '从未运行'}
                  </span>
                  <span>Tredict 活动镜像 {syncStatus.data.tredict_activity_mirror} 条</span>
                  <span>
                    同步账本 {syncStatus.data.sync_ledger.rows} 行 · 失败{' '}
                    <span className={syncStatus.data.sync_ledger.failed > 0 ? 'text-risk-high' : ''}>
                      {syncStatus.data.sync_ledger.failed}
                    </span>
                  </span>
                </div>
                <div className="grid grid-cols-2 gap-x-6 gap-y-1 md:grid-cols-3">
                  {Object.entries(syncStatus.data.datasets).map(([name, v]) => (
                    <div key={name} className="flex justify-between text-xs hs-text-2">
                      <span>{name}</span>
                      <span className="hs-text-3">
                        {v.files} 天 · 最新 {v.latest}
                      </span>
                    </div>
                  ))}
                </div>
                <p className="text-xs hs-text-3">
                  校验口径：只有「同一路径的值不同」才算错误；Garmin 新增字段记作 schema 漂移。有差异时消息中心会推送。
                </p>
              </div>
            )}
          </Section>

          <Section id="ops-tasks" title="任务状态">
            {tasks.data && tasks.data.tasks.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs hs-text-3">
                    <tr>
                      <th className="py-2 pr-3 whitespace-nowrap">类型</th>
                      <th className="py-2 pr-3 whitespace-nowrap">状态</th>
                      <th className="py-2 pr-3 whitespace-nowrap">开始</th>
                      <th className="py-2 pr-3 whitespace-nowrap">尝试</th>
                      <th className="py-2 pr-3 whitespace-nowrap">错误</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tasks.data.tasks.map((t) => (
                      <tr key={t.id} className="border-t hs-bd">
                        <td className="py-2 pr-3 hs-text-2">{t.task_type}</td>
                        <td className="py-2 pr-3">
                          <StatusPill status={t.status} />
                        </td>
                        <td className="py-2 pr-3 hs-text-3">{fmt(t.started_at)}</td>
                        <td className="py-2 pr-3 hs-text-3">{t.attempts}</td>
                        <td className="py-2 pr-3 text-xs hs-text-p1">
                          {t.error ? `[${t.error.code}] ${t.error.message}` : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-sm hs-text-3">暂无任务记录。点击上方「同步并生成日报」可产生记录。</p>
            )}
          </Section>

          <Section id="ops-audit" title="审计日志">
            {audit.data && audit.data.events.length > 0 ? (
              <ul className="space-y-2 text-sm">
                {audit.data.events.map((e) => (
                  <li key={e.id} className="flex items-start gap-3 border-b hs-bd pb-2">
                    <span className="rounded hs-bg-2 px-1.5 py-0.5 text-[11px] hs-text-3">
                      {e.category}
                    </span>
                    <span className="shrink-0 hs-text-3">{fmt(e.at)}</span>
                    <span className="truncate hs-text-2">{e.ref}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm hs-text-3">暂无审计记录。</p>
            )}
          </Section>
        </div>
        <TierNav
          items={[
            { id: 'ops-health', label: '健康' },
            { id: 'ops-sync', label: '同步' },
            { id: 'ops-tasks', label: '任务' },
            { id: 'ops-audit', label: '审计' },
          ]}
        />
      </div>
    </Page>
  )
}
