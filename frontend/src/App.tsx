/**
 * 全局外壳：7 项主导航 + 右上角三个工具入口（环境数据 / 消息中心 / 运维台）。
 * 策略版本、策略比较页已取消（后端接口保留）。
 * 主题固定为 C1 舒展仪表盘（token 见 design/tokens.css）。
 */
import { Suspense, lazy, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api/client'
import { Drawer } from './components/ui/Drawer'
import { Chip, StatusDot } from './components/ui/badges'

const DailyPage = lazy(() => import('./pages/reports/DailyPage'))
const WeeklyPage = lazy(() => import('./pages/reports/WeeklyPage'))
const MonthlyPage = lazy(() => import('./pages/reports/MonthlyPage'))
const WeeklyPlanPage = lazy(() => import('./pages/plan/WeeklyPlanPage'))
const Plan12wPage = lazy(() => import('./pages/Plan12wPage'))
const OpsPage = lazy(() => import('./pages/OpsPage'))
const DataPage = lazy(() => import('./pages/DataPage'))
const LoadPage = lazy(() => import('./pages/LoadPage'))
const TrainingPage = lazy(() => import('./pages/training/TrainingPage'))
const TrainingDetailPage = lazy(() => import('./pages/training/TrainingDetailPage'))

const NAV = [
  { to: '/daily', label: '日报' },
  { to: '/plan', label: '周计划' },
  { to: '/weekly', label: '周报' },
  { to: '/monthly', label: '月报' },
  { to: '/plan12w', label: '12 周计划' },
  { to: '/data', label: '数据趋势' },
  { to: '/load', label: '训练负荷' },
  { to: '/training', label: '运动训练' },
]

type DrawerKind = 'env' | 'messages' | 'ops'

export default function App() {
  const [drawer, setDrawer] = useState<DrawerKind | null>(null)
  const navigate = useNavigate()

  /** 消息里的 link：/ops 指向运维抽屉（旧路由已取消），其余走前端路由 */
  const openLink = (link: string) => {
    if (!link) return
    if (link.startsWith('/ops')) {
      setDrawer('ops')
      return
    }
    setDrawer(null)
    navigate(link)
  }

  return (
    <div className="min-h-screen">
      <header
        className="sticky top-0 z-30 border-b backdrop-blur"
        style={{ borderColor: 'var(--hs-border)', background: 'rgba(255,255,255,.9)' }}
      >
        <div
          className="mx-auto flex items-center gap-4 px-4 sm:px-6"
          style={{ maxWidth: 'var(--hs-content-max)', height: 56 }}
        >
          <div className="flex shrink-0 items-baseline gap-2">
            <span className="text-[15px] font-bold tracking-tight" style={{ color: 'var(--hs-ink)' }}>
              健康运动
            </span>
          </div>

          <nav className="flex flex-1 items-center gap-0.5 overflow-x-auto whitespace-nowrap [scrollbar-width:none]">
            {NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                className={({ isActive }) =>
                  `shrink-0 whitespace-nowrap rounded-[var(--hs-r-sm)] px-2 py-1.5 text-[var(--hs-fs-small)] font-semibold transition ${
                    isActive ? '' : 'hover:bg-[var(--hs-surface-2)]'
                  }`
                }
                style={({ isActive }) => ({
                  color: isActive ? '#fff' : 'var(--hs-ink-3)',
                  background: isActive ? 'var(--hs-ink)' : undefined,
                })}
              >
                {n.label}
              </NavLink>
            ))}
          </nav>

          <div className="flex shrink-0 items-center gap-1">
            <ToolButton label="环境" icon="◎" onClick={() => setDrawer('env')} />
            <ToolButton label="消息" icon="✉" onClick={() => setDrawer('messages')} badge />
            <ToolButton label="运维" icon="⚙" onClick={() => setDrawer('ops')} />
          </div>
        </div>
      </header>

      <main className="py-[var(--hs-gap)]">
        <Suspense fallback={<div className="hs-card mx-auto mt-6 max-w-md">加载中…</div>}>
          <Routes>
            <Route path="/" element={<Navigate to="/daily" replace />} />
            <Route path="/daily" element={<DailyPage />} />
            <Route path="/plan" element={<WeeklyPlanPage />} />
            <Route path="/weekly" element={<WeeklyPage />} />
            <Route path="/monthly" element={<MonthlyPage />} />
            <Route path="/plan12w" element={<Plan12wPage />} />
            <Route path="/data" element={<DataPage />} />
            <Route path="/load" element={<LoadPage />} />
            <Route path="/training" element={<TrainingPage />} />
            <Route path="/training/detail" element={<TrainingDetailPage />} />
            {/* 已取消的页面：旧链接回到日报 */}
            <Route path="/strategy" element={<Navigate to="/daily" replace />} />
            <Route path="/strategy-compare" element={<Navigate to="/daily" replace />} />
            <Route path="/messages" element={<Navigate to="/daily" replace />} />
            <Route path="/environment" element={<Navigate to="/daily" replace />} />
            <Route path="/ops" element={<Navigate to="/daily" replace />} />
            <Route path="*" element={<Navigate to="/daily" replace />} />
          </Routes>
        </Suspense>
      </main>

      <Drawer
        open={drawer === 'env'}
        title="环境数据"
        subtitle="雨雪或低于 5℃ 时强制室内（硬性约束，不可被策略覆盖）"
        onClose={() => setDrawer(null)}
      >
        <EnvDrawerBody />
      </Drawer>
      <Drawer
        open={drawer === 'messages'}
        title="消息中心"
        subtitle="已读 ≠ 已解决；标记已读不会改变任何计划状态"
        onClose={() => setDrawer(null)}
      >
        <MessagesDrawerBody onOpenLink={openLink} />
      </Drawer>
      <Drawer
        open={drawer === 'ops'}
        title="运维台"
        subtitle="任务、审计与数据完整性"
        onClose={() => setDrawer(null)}
        width={760}
      >
        <Suspense fallback={<div className="text-[var(--hs-fs-small)]">加载中…</div>}>
          <OpsPage embedded />
        </Suspense>
      </Drawer>
    </div>
  )
}

function ToolButton({
  label,
  icon,
  onClick,
  badge,
}: {
  label: string
  icon: string
  onClick: () => void
  badge?: boolean
}) {
  const messages = useQuery<{ items: unknown[]; unread_count: number }>({
    queryKey: ['messages', 'unread'],
    queryFn: () => api.get('/api/messages'),
    retry: false,
    enabled: Boolean(badge),
    staleTime: 60_000,
  })
  const unread = messages.data?.unread_count ?? 0
  return (
    <button
      onClick={onClick}
      className="relative flex items-center gap-1 rounded-[var(--hs-r-sm)] px-2 py-1.5 text-[var(--hs-fs-small)] font-semibold transition hover:bg-[var(--hs-surface-2)]"
      style={{ color: 'var(--hs-ink-3)' }}
      title={label}
      aria-label={label}
    >
      <span aria-hidden className="text-[13px]">
        {icon}
      </span>
      <span className="hidden sm:inline">{label}</span>
      {badge && unread > 0 && (
        <span
          className="absolute -right-0.5 top-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full px-1 text-[9px] font-bold text-white"
          style={{ background: 'var(--hs-p1)' }}
        >
          {unread > 99 ? '99+' : unread}
        </span>
      )}
    </button>
  )
}

function EnvDrawerBody() {
  const env = useQuery<import('./api/types').EnvironmentDaily>({
    queryKey: ['environment', 'today'],
    queryFn: () => {
      const d = new Date()
      const pad = (n: number) => String(n).padStart(2, '0')
      const iso = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
      return api.get(`/api/environment/daily?date=${iso}`)
    },
    retry: false,
  })
  const d = env.data
  if (env.isLoading) return <div className="hs-skeleton h-24" />
  if (!d)
    return (
      <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-3)' }}>
        今日环境数据不可用。系统不会因数据缺失而放宽任何安全阈值。
      </p>
    )
  const severe = d.is_rain || d.is_snow || (d.temp_c != null && d.temp_c < 5)
  const rows: [string, string][] = [
    ['天气', d.weather_text || '—'],
    ['气温', d.temp_c != null ? `${d.temp_c} ℃` : '数据缺失'],
    ['降水', d.precip_mm != null ? `${d.precip_mm} mm` : '—'],
    ['湿度', d.humidity != null ? `${d.humidity}%` : '—'],
    ['风力', d.wind_scale ?? '—'],
    ['地点', d.location_name || '—'],
  ]
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {d.available ? (
          <Chip tone={severe ? 'p1' : 'p4'}>
            <StatusDot tone={severe ? 'p1' : 'p4'} />
            {severe ? '硬性约束：强制室内' : '可户外训练'}
          </Chip>
        ) : (
          <Chip tone="p2">数据不可用</Chip>
        )}
      </div>
      <dl className="grid grid-cols-[5rem_1fr] gap-y-1.5 text-[var(--hs-fs-small)]">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt style={{ color: 'var(--hs-ink-4)' }}>{k}</dt>
            <dd className="font-semibold" style={{ color: 'var(--hs-ink)' }}>
              {v}
            </dd>
          </div>
        ))}
      </dl>
      <p className="text-[var(--hs-fs-micro)] leading-relaxed" style={{ color: 'var(--hs-ink-4)' }}>
        来源：{d.source} · 采集 {d.fetched_at?.slice(0, 16).replace('T', ' ') ?? '—'}。雨雪或低于 5℃
        会强制改为室内训练，该约束优先级高于季节策略。
      </p>
    </div>
  )
}

function MessagesDrawerBody({ onOpenLink }: { onOpenLink: (link: string) => void }) {
  const qc = useQueryClient()
  const [err, setErr] = useState<string | null>(null)

  const list = useQuery<{
    items: {
      message_id: string
      at: string
      importance: string
      title: string
      body: string
      link: string
      is_read: boolean
      is_resolved: boolean
    }[]
    unread_count: number
  }>({
    queryKey: ['messages', 'drawer'],
    queryFn: () => api.get('/api/messages'),
    retry: false,
  })

  // 标记已读只改变消息状态，**不触发任何策略变化**（§10.3）；已读 ≠ 已解决。
  const refresh = () => qc.invalidateQueries({ queryKey: ['messages'] })

  const markOne = useMutation({
    mutationFn: (id: string) => api.post(`/api/messages/${id}/read`),
    onSuccess: refresh,
    onError: (e: Error) => setErr(e.message),
  })

  const markAll = useMutation({
    mutationFn: () => api.post<{ updated: number }>('/api/messages/read-all'),
    onSuccess: refresh,
    onError: (e: Error) => setErr(e.message),
  })

  const items = list.data?.items ?? []
  const unread = list.data?.unread_count ?? 0
  const busy = markOne.isPending || markAll.isPending

  if (list.isLoading) return <div className="hs-skeleton h-24" />
  if (items.length === 0)
    return (
      <p className="text-[var(--hs-fs-small)]" style={{ color: 'var(--hs-ink-4)' }}>
        暂无消息。
      </p>
    )
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-ink-4)' }}>
          共 {items.length} 条 · 未读 {unread} 条
        </span>
        <button
          className="hs-btn"
          aria-label="全部标记已读"
          onClick={() => {
            setErr(null)
            markAll.mutate()
          }}
          disabled={busy || unread === 0}
        >
          {markAll.isPending ? '处理中…' : '全部标记已读'}
        </button>
      </div>

      {err && (
        <p className="text-[var(--hs-fs-micro)]" style={{ color: 'var(--hs-p1)' }}>
          操作失败：{err}
        </p>
      )}

      <ul className="space-y-2">
        {items.slice(0, 30).map((m) => (
          <li
            key={m.message_id}
            className="rounded-[var(--hs-r-sm)] border p-2.5"
            style={{
              borderColor: 'var(--hs-border)',
              background: m.is_read ? 'var(--hs-surface)' : 'var(--hs-accent-soft)',
            }}
          >
            <div className="flex items-center gap-1.5">
              <Chip tone={m.importance === 'high' ? 'p1' : 'neutral'}>
                {m.importance === 'high' ? '重要' : '普通'}
              </Chip>
              <span className="text-[var(--hs-fs-small)] font-bold" style={{ color: 'var(--hs-ink)' }}>
                {m.title}
              </span>
              {!m.is_read && <Chip tone="accent">未读</Chip>}
              {m.is_resolved && <Chip tone="p4">已解决</Chip>}
            </div>
            <p className="mt-1 text-[var(--hs-fs-micro)] leading-snug" style={{ color: 'var(--hs-ink-3)' }}>
              {m.body}
            </p>
            <div className="mt-1 flex items-center justify-between gap-2">
              <span className="text-[10px]" style={{ color: 'var(--hs-ink-4)' }}>
                {m.at?.slice(0, 16).replace('T', ' ')}
              </span>
              <div className="flex items-center gap-3">
                {m.link && (
                  <button
                    className="text-[var(--hs-fs-micro)] font-semibold"
                    style={{ color: 'var(--hs-ink-3)' }}
                    aria-label={`查看详情：${m.title}`}
                    onClick={() => onOpenLink(m.link)}
                  >
                    查看详情
                  </button>
                )}
                {!m.is_read && (
                  <button
                    className="text-[var(--hs-fs-micro)] font-semibold"
                    style={{ color: 'var(--hs-accent-ink)' }}
                    aria-label={`标记已读：${m.title}`}
                    onClick={() => {
                      setErr(null)
                      markOne.mutate(m.message_id)
                    }}
                    disabled={busy}
                  >
                    {markOne.isPending && markOne.variables === m.message_id ? '处理中…' : '标记已读'}
                  </button>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
