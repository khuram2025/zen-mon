// APM Usage Analytics — who uses what: traffic, pages, operations and users.

import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, Download, Users, FileSearch, Info } from 'lucide-react'
import {
  ComposedChart, Area, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { KpiTile, fmtMs, fmtPct } from '@/components/apm/shared'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { toCsv, downloadCsv } from '@/components/servers/tables'

/* ── Types (mirror /apm/usage/*) ────────────────────────────────────────── */

interface UsagePoint { t: string; requests: number; users: number; errors: number }
interface UsageSummary {
  requests: number; unique_users: number; pages: number; services: number
  errors: number; error_rate: number; avg_ms: number; p95_ms: number
  series: UsagePoint[]
}
interface PageRow {
  route: string; service: string; hits: number; users: number
  errors: number; error_rate: number; p95_ms: number; last_hit: string | null
}
interface OpRow {
  service: string; operation: string; hits: number; users: number
  errors: number; error_rate: number; p95_ms: number
}
interface UserRow {
  user_id: string; requests: number; errors: number; services: number; pages: number
  first_seen: string | null; last_seen: string | null
}
interface UsersResp { users: UserRow[]; attributed_requests: number; total_requests: number }

/* ── Constants & helpers ────────────────────────────────────────────────── */

// Data window is bounded to 7 days server-side.
const HOURS_OPTIONS = [
  { value: 3, label: 'Last 3 hours' },
  { value: 24, label: 'Last 24 hours' },
  { value: 72, label: 'Last 3 days' },
  { value: 168, label: 'Last 7 days' },
]

const CHART = {
  requests: '#3b82f6',
  users: '#22d3ee',
  errors: '#ef4444',
  grid: 'rgba(148,163,184,0.15)',
  tick: '#94a3b8',
  tooltipBg: '#0d121b',
  tooltipBorder: '#1e293b',
  tooltipText: '#e5e7eb',
}

function fmtCount(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${(n / 1000).toFixed(0)}K`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return `${Math.round(n)}`
}

function errTone(rate: number): string | undefined {
  return rate > 0.02 ? '#ef4444' : undefined
}

/* ── Small building blocks ──────────────────────────────────────────────── */

function QueryError({ label, error }: { label: string; error: unknown }) {
  return (
    <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
      Failed to load {label} — {apiErrorMessage(error)}
    </div>
  )
}

function LoadingRows({ colSpan }: { colSpan: number }) {
  return (
    <Tr>
      <Td colSpan={colSpan} className="py-10 text-center">
        <span className="inline-flex items-center gap-2 text-muted"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</span>
      </Td>
    </Tr>
  )
}

/** Proportional inline bar: value relative to the list max. */
function RankBar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.max(2, (value / max) * 100) : 0
  return (
    <div className="mt-1 h-1.5 w-full overflow-hidden rounded bg-surface2">
      <div className="h-full rounded bg-primary/60" style={{ width: `${pct}%` }} />
    </div>
  )
}

function CsvButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface2 px-2 py-1 text-xs text-text2 transition-colors hover:text-text hover:border-primary/40"
      title="Export as CSV"
    >
      <Download className="h-3 w-3" /> CSV
    </button>
  )
}

function TableCard({ title, hint, onCsv, children }: {
  title: string; hint?: string; onCsv: () => void; children: React.ReactNode
}) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 py-3 px-4 border-b border-border">
        <div className="flex items-baseline gap-2 min-w-0">
          <CardTitle className="text-sm">{title}</CardTitle>
          {hint && <span className="text-[10px] uppercase tracking-wider text-muted truncate">{hint}</span>}
        </div>
        <CsvButton onClick={onCsv} />
      </CardHeader>
      <CardContent className="p-0">{children}</CardContent>
    </Card>
  )
}

/* ── Page ───────────────────────────────────────────────────────────────── */

export function UsagePage() {
  const [params, setParams] = useSearchParams()
  const hours = Number(params.get('hours')) || 24
  const service = params.get('service') || ''

  const setParam = (k: string, v: string) => {
    const n = new URLSearchParams(params)
    if (v) n.set(k, v); else n.delete(k)
    setParams(n, { replace: true })
  }

  // Debounced service filter (applies to every usage endpoint).
  const [serviceInput, setServiceInput] = useState(service)
  useEffect(() => {
    const t = setTimeout(() => { if (serviceInput !== service) setParam('service', serviceInput.trim()) }, 400)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceInput])

  const qp = (extra?: Record<string, string>) => {
    const p = new URLSearchParams({ hours: String(hours), ...extra })
    if (service) p.set('service', service)
    return p.toString()
  }

  const summary = useQuery<UsageSummary>({
    queryKey: ['apm', 'usage', 'summary', { hours, service }],
    queryFn: async () => (await api.get(`/apm/usage/summary?${qp()}`)).data,
    refetchInterval: 30_000,
  })
  const pagesQ = useQuery<{ pages: PageRow[] }>({
    queryKey: ['apm', 'usage', 'pages', { hours, service }],
    queryFn: async () => (await api.get(`/apm/usage/pages?${qp({ limit: '50' })}`)).data,
  })
  const opsQ = useQuery<{ operations: OpRow[] }>({
    queryKey: ['apm', 'usage', 'operations', { hours, service }],
    queryFn: async () => (await api.get(`/apm/usage/operations?${qp({ limit: '50' })}`)).data,
  })
  const usersQ = useQuery<UsersResp>({
    queryKey: ['apm', 'usage', 'users', { hours, service }],
    queryFn: async () => (await api.get(`/apm/usage/users?${qp({ limit: '50' })}`)).data,
  })

  const s = summary.data
  const pages = pagesQ.data?.pages ?? []
  const ops = opsQ.data?.operations ?? []
  const users = usersQ.data?.users ?? []

  const maxPageHits = pages.length ? pages[0].hits : 0
  const maxOpHits = ops.length ? ops[0].hits : 0
  const maxUserReqs = users.length ? users[0].requests : 0

  const chartData = useMemo(
    () => (s?.series ?? []).map((p) => ({ ...p, ts: new Date(p.t).getTime() })),
    [s?.series],
  )
  const xTick = (ts: number) => {
    const d = new Date(ts)
    return hours >= 72
      ? d.toLocaleDateString([], { month: '2-digit', day: '2-digit' })
      : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }

  const attribution = usersQ.data
    ? usersQ.data.total_requests > 0 ? usersQ.data.attributed_requests / usersQ.data.total_requests : 0
    : null

  const isFetching = summary.isFetching || pagesQ.isFetching || opsQ.isFetching || usersQ.isFetching

  /* — CSV exports — */
  const exportPages = () => downloadCsv(`usage-pages-${hours}h.csv`, toCsv(pages, [
    { header: 'Route', value: (r) => r.route },
    { header: 'Service', value: (r) => r.service },
    { header: 'Hits', value: (r) => r.hits },
    { header: 'Users', value: (r) => r.users },
    { header: 'Errors', value: (r) => r.errors },
    { header: 'Error rate', value: (r) => r.error_rate },
    { header: 'p95 ms', value: (r) => r.p95_ms },
    { header: 'Last hit', value: (r) => r.last_hit ?? '' },
  ]))
  const exportOps = () => downloadCsv(`usage-operations-${hours}h.csv`, toCsv(ops, [
    { header: 'Operation', value: (r) => r.operation },
    { header: 'Service', value: (r) => r.service },
    { header: 'Hits', value: (r) => r.hits },
    { header: 'Users', value: (r) => r.users },
    { header: 'Errors', value: (r) => r.errors },
    { header: 'Error rate', value: (r) => r.error_rate },
    { header: 'p95 ms', value: (r) => r.p95_ms },
  ]))
  const exportUsers = () => downloadCsv(`usage-users-${hours}h.csv`, toCsv(users, [
    { header: 'User', value: (r) => r.user_id },
    { header: 'Requests', value: (r) => r.requests },
    { header: 'Errors', value: (r) => r.errors },
    { header: 'Services', value: (r) => r.services },
    { header: 'Pages', value: (r) => r.pages },
    { header: 'First seen', value: (r) => r.first_seen ?? '' },
    { header: 'Last seen', value: (r) => r.last_seen ?? '' },
  ]))

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="Usage Analytics"
        description="Who uses what — traffic, pages, functions and users across your instrumented services."
        article="usage"
        actions={
          <>
            {isFetching && <Loader2 className="w-4 h-4 animate-spin text-muted" />}
            <Input
              className="w-52"
              placeholder="Filter by service…"
              value={serviceInput}
              onChange={(e) => setServiceInput(e.target.value)}
            />
            <select
              value={hours}
              onChange={(e) => setParam('hours', e.target.value)}
              className="h-9 rounded-md bg-surface2 border border-border text-sm px-2 text-text"
              title="Usage data is retained for 7 days"
            >
              {HOURS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </>
        }
      />

      {summary.isError && <QueryError label="usage summary" error={summary.error} />}

      {/* KPI row */}
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-3">
        <KpiTile label="Requests" value={fmtCount(s?.requests)} />
        <KpiTile label="Unique users" value={fmtCount(s?.unique_users)} />
        <KpiTile label="Pages" value={fmtCount(s?.pages)} />
        <KpiTile label="Latency avg / p95" value={s ? `${fmtMs(s.avg_ms)} / ${fmtMs(s.p95_ms)}` : '—'} />
        <KpiTile label="Error rate" value={fmtPct(s?.error_rate)} accent={s ? errTone(s.error_rate) : undefined} />
      </div>

      {/* Traffic chart */}
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 py-3 px-4 border-b border-border">
          <CardTitle className="text-sm">Traffic</CardTitle>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            {([['Requests', CHART.requests], ['Users', CHART.users], ['Errors', CHART.errors]] as const).map(([label, color]) => (
              <span key={label} className="flex items-center gap-1.5 text-[11px] text-text2">
                <span className="h-2 w-2 rounded-sm" style={{ background: color }} /> {label}
              </span>
            ))}
          </div>
        </CardHeader>
        <CardContent className="pt-4">
          {summary.isLoading ? (
            <div className="flex h-[220px] items-center justify-center gap-2 text-muted">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading…
            </div>
          ) : chartData.length === 0 ? (
            <div className="flex h-[220px] items-center justify-center text-sm text-muted">No traffic in this window.</div>
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <ComposedChart data={chartData} margin={{ top: 4, right: 12, bottom: 0, left: -8 }}>
                <defs>
                  <linearGradient id="usage-req" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={CHART.requests} stopOpacity={0.35} />
                    <stop offset="100%" stopColor={CHART.requests} stopOpacity={0} />
                  </linearGradient>
                  <linearGradient id="usage-err" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={CHART.errors} stopOpacity={0.35} />
                    <stop offset="100%" stopColor={CHART.errors} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke={CHART.grid} vertical={false} />
                <XAxis dataKey="ts" type="number" scale="time" domain={['dataMin', 'dataMax']}
                  tick={{ fontSize: 10, fill: CHART.tick }} tickFormatter={xTick} minTickGap={40} />
                <YAxis tick={{ fontSize: 10, fill: CHART.tick }} width={48} allowDecimals={false} />
                <Tooltip
                  contentStyle={{ background: CHART.tooltipBg, border: `1px solid ${CHART.tooltipBorder}`, borderRadius: 8, fontSize: 12, color: CHART.tooltipText }}
                  labelFormatter={(ts) => new Date(Number(ts)).toLocaleString()}
                  formatter={(v: number | string, name: string) => [Number(v).toLocaleString(), name.charAt(0).toUpperCase() + name.slice(1)]}
                />
                <Area type="monotone" dataKey="requests" stroke={CHART.requests} strokeWidth={2} fill="url(#usage-req)" isAnimationActive={false} dot={false} />
                <Area type="monotone" dataKey="errors" stroke={CHART.errors} strokeWidth={1.5} fill="url(#usage-err)" isAnimationActive={false} dot={false} />
                <Line type="monotone" dataKey="users" stroke={CHART.users} strokeWidth={2} dot={false} isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
          )}
        </CardContent>
      </Card>

      {/* Top pages / Top operations */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <div className="space-y-3">
          {pagesQ.isError && <QueryError label="top pages" error={pagesQ.error} />}
          <TableCard title="Top pages" hint="by hits" onCsv={exportPages}>
            <Table>
              <THead>
                <Tr>
                  <Th className="w-8">#</Th><Th>Route</Th>
                  <Th className="text-right w-32">Hits</Th><Th className="text-right">Users</Th>
                  <Th className="text-right">Err rate</Th><Th className="text-right">p95</Th>
                </Tr>
              </THead>
              <TBody>
                {pagesQ.isLoading ? <LoadingRows colSpan={6} /> : pages.length === 0 ? (
                  <Tr><Td colSpan={6} className="py-8 text-center text-muted"><FileSearch className="w-5 h-5 mx-auto mb-1.5" />No page traffic in this window.</Td></Tr>
                ) : pages.map((p, i) => (
                  <Tr key={p.route}>
                    <Td className="text-xs text-muted">{i + 1}</Td>
                    <Td>
                      <div className="font-mono text-xs text-text truncate max-w-[16rem]" title={p.route}>{p.route}</div>
                      <div className="text-[10px] text-muted truncate">{p.service}</div>
                    </Td>
                    <Td className="text-right">
                      <span className="font-mono text-xs tabular-nums">{fmtCount(p.hits)}</span>
                      <RankBar value={p.hits} max={maxPageHits} />
                    </Td>
                    <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(p.users)}</Td>
                    <Td className="text-right font-mono text-xs" style={{ color: errTone(p.error_rate) }}>{fmtPct(p.error_rate)}</Td>
                    <Td className="text-right font-mono text-xs">{fmtMs(p.p95_ms)}</Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </TableCard>
        </div>

        <div className="space-y-3">
          {opsQ.isError && <QueryError label="top operations" error={opsQ.error} />}
          <TableCard title="Top functions" hint="operations by hits" onCsv={exportOps}>
            <Table>
              <THead>
                <Tr>
                  <Th className="w-8">#</Th><Th>Operation</Th>
                  <Th className="text-right w-32">Hits</Th><Th className="text-right">Users</Th>
                  <Th className="text-right">Err rate</Th><Th className="text-right">p95</Th>
                </Tr>
              </THead>
              <TBody>
                {opsQ.isLoading ? <LoadingRows colSpan={6} /> : ops.length === 0 ? (
                  <Tr><Td colSpan={6} className="py-8 text-center text-muted"><FileSearch className="w-5 h-5 mx-auto mb-1.5" />No operations in this window.</Td></Tr>
                ) : ops.map((o, i) => (
                  <Tr key={`${o.service}:${o.operation}`}>
                    <Td className="text-xs text-muted">{i + 1}</Td>
                    <Td>
                      <div className="font-mono text-xs text-text truncate max-w-[16rem]" title={o.operation}>{o.operation}</div>
                      <div className="text-[10px] text-muted truncate">{o.service}</div>
                    </Td>
                    <Td className="text-right">
                      <span className="font-mono text-xs tabular-nums">{fmtCount(o.hits)}</span>
                      <RankBar value={o.hits} max={maxOpHits} />
                    </Td>
                    <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(o.users)}</Td>
                    <Td className="text-right font-mono text-xs" style={{ color: errTone(o.error_rate) }}>{fmtPct(o.error_rate)}</Td>
                    <Td className="text-right font-mono text-xs">{fmtMs(o.p95_ms)}</Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </TableCard>
        </div>
      </div>

      {/* Top users */}
      {usersQ.isError && <QueryError label="top users" error={usersQ.error} />}
      {!usersQ.isError && users.length === 0 && !usersQ.isLoading && (s?.requests ?? 0) > 0 ? (
        <div className="rounded-lg border border-info/30 bg-info/10 px-4 py-4">
          <div className="flex items-start gap-3">
            <Info className="w-4 h-4 text-info mt-0.5 shrink-0" />
            <div className="text-sm">
              <div className="font-medium text-info">No user attribution yet</div>
              <p className="text-text2 mt-1">
                Set the <code className="font-mono text-xs bg-surface2 rounded px-1 py-0.5">enduser.id</code> attribute
                on your spans to unlock per-user analytics. For example (OTel):
              </p>
              <code className="mt-2 block w-fit rounded-md bg-surface2 border border-border px-2.5 py-1.5 font-mono text-xs text-text">
                span.set_attribute("enduser.id", current_user.id)
              </code>
            </div>
          </div>
        </div>
      ) : (
        <TableCard
          title="Top users"
          hint={attribution != null ? `${fmtPct(attribution)} of requests attributed` : undefined}
          onCsv={exportUsers}
        >
          <Table>
            <THead>
              <Tr>
                <Th className="w-8">#</Th><Th>User</Th>
                <Th className="text-right w-36">Requests</Th>
                <Th className="text-right">Err rate</Th>
                <Th className="text-right">Services</Th><Th className="text-right">Pages</Th>
                <Th className="text-right">Last seen</Th>
              </Tr>
            </THead>
            <TBody>
              {usersQ.isLoading ? <LoadingRows colSpan={7} /> : users.length === 0 ? (
                <Tr><Td colSpan={7} className="py-8 text-center text-muted"><Users className="w-5 h-5 mx-auto mb-1.5" />No user activity in this window.</Td></Tr>
              ) : users.map((u, i) => {
                const er = u.requests > 0 ? u.errors / u.requests : 0
                return (
                  <Tr key={u.user_id}>
                    <Td className="text-xs text-muted">{i + 1}</Td>
                    <Td className="font-mono text-xs text-text truncate max-w-[18rem]" title={u.user_id}>{u.user_id}</Td>
                    <Td className="text-right">
                      <span className="font-mono text-xs tabular-nums">{fmtCount(u.requests)}</span>
                      <RankBar value={u.requests} max={maxUserReqs} />
                    </Td>
                    <Td className="text-right font-mono text-xs" style={{ color: errTone(er) }}>{fmtPct(er)}</Td>
                    <Td className="text-right font-mono text-xs tabular-nums">{u.services}</Td>
                    <Td className="text-right font-mono text-xs tabular-nums">{u.pages}</Td>
                    <Td className="text-right text-xs text-muted whitespace-nowrap">{u.last_seen ? relativeTime(u.last_seen) : '—'}</Td>
                  </Tr>
                )
              })}
            </TBody>
          </Table>
        </TableCard>
      )}
    </div>
  )
}
