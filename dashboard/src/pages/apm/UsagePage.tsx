// APM Usage Analytics — who uses what: traffic, pages, operations and users.

import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, Download, Users, FileSearch, Info } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { fmtMs, fmtPct } from '@/components/apm/shared'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { toCsv, downloadCsv } from '@/components/servers/tables'
import { APM_SERIES, ApmKpi, ApmTimeChart, ChartPanel, RankBar, fmtCount } from '@/components/apm/viz'

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
  const cardQ = useQuery<{ spans: number; services: number; operations: number; routes: number; versions: number; attributes: Array<{ key: string; distinct: number; spans: number }> }>({
    queryKey: ['apm', 'cardinality', { hours, service }],
    queryFn: async () => {
      const p = new URLSearchParams({ hours: String(hours) })
      if (service) p.set('service', service)
      return (await api.get(`/apm/cardinality?${p}`)).data
    },
  })

  const s = summary.data
  const pages = pagesQ.data?.pages ?? []
  const ops = opsQ.data?.operations ?? []
  const users = usersQ.data?.users ?? []

  const maxPageHits = pages.length ? pages[0].hits : 0
  const maxOpHits = ops.length ? ops[0].hits : 0
  const maxUserReqs = users.length ? users[0].requests : 0

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

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <ApmKpi label="Requests" tone="info" value={fmtCount(s?.requests)} sub="in this window" />
        <ApmKpi label="Unique users" tone="accent" value={fmtCount(s?.unique_users)} />
        <ApmKpi label="Pages" tone="primary" value={fmtCount(s?.pages)} />
        <ApmKpi label="Latency avg / p95" tone="warning" value={s ? `${fmtMs(s.avg_ms)} / ${fmtMs(s.p95_ms)}` : '—'} />
        <ApmKpi label="Error rate" tone={s && s.error_rate > 0.02 ? 'danger' : 'success'} value={fmtPct(s?.error_rate)} />
      </div>

      <ChartPanel title="Traffic" hint="requests, users and errors">
        <ApmTimeChart
          data={s?.series ?? []}
          timeKey="t"
          loading={summary.isLoading}
          empty="No traffic in this window."
          series={[
            { key: 'requests', name: 'Requests', color: APM_SERIES.requests, fmt: (v) => fmtCount(v) },
            { key: 'errors', name: 'Errors', color: APM_SERIES.errors, fmt: (v) => fmtCount(v) },
            { key: 'users', name: 'Users', color: APM_SERIES.users, yAxisIndex: 1, fmt: (v) => fmtCount(v) },
          ]}
          height={260}
        />
      </ChartPanel>

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

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 border-b border-border px-4 py-3">
          <div>
            <CardTitle className="text-sm">Ingest cardinality</CardTitle>
            <p className="mt-0.5 text-[11px] text-muted">Distinct services, operations, routes and span attribute keys in this window. High-cardinality keys inflate storage and query cost.</p>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {cardQ.isLoading ? (
            <div className="flex items-center justify-center gap-2 py-10 text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading cardinality…</div>
          ) : cardQ.isError ? (
            <QueryError label="cardinality" error={cardQ.error} />
          ) : (
            <>
              <div className="grid grid-cols-2 gap-3 border-b border-border p-4 md:grid-cols-5">
                <ApmKpi label="Spans" tone="info" value={fmtCount(cardQ.data?.spans)} />
                <ApmKpi label="Services" tone="primary" value={fmtCount(cardQ.data?.services)} />
                <ApmKpi label="Operations" tone="accent" value={fmtCount(cardQ.data?.operations)} />
                <ApmKpi label="Routes" tone="warning" value={fmtCount(cardQ.data?.routes)} />
                <ApmKpi label="Versions" tone="success" value={fmtCount(cardQ.data?.versions)} />
              </div>
              <Table>
                <THead><Tr><Th>Attribute key</Th><Th className="text-right">Distinct values</Th><Th className="text-right">Spans</Th></Tr></THead>
                <TBody>
                  {(cardQ.data?.attributes ?? []).map((a) => (
                    <Tr key={a.key}>
                      <Td className="font-mono text-xs">{a.key}</Td>
                      <Td className="text-right font-mono text-xs tabular-nums">{fmtCount(a.distinct)}</Td>
                      <Td className="text-right">
                        <span className="font-mono text-xs tabular-nums">{fmtCount(a.spans)}</span>
                        <RankBar value={a.distinct} max={Math.max(cardQ.data?.attributes[0]?.distinct || 1, 1)} color={a.distinct > 1000 ? '#db2777' : APM_SERIES.throughput} />
                      </Td>
                    </Tr>
                  ))}
                  {(cardQ.data?.attributes ?? []).length === 0 && (
                    <Tr><Td colSpan={3} className="py-8 text-center text-muted">No string attributes in this window.</Td></Tr>
                  )}
                </TBody>
              </Table>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
