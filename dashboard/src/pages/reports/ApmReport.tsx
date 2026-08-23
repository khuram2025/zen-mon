// APM report tab — printable application-performance summary:
// service health, SLO attainment, top error groups and usage highlights.
// Windows are fixed per section (stated in each header) rather than driven
// by the shared report time-range picker.

import { Activity, Boxes, Target, Bug, Users, Loader2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { ReportSection, EmptyReportState } from '@/components/reports/ReportSection'
import { KpiTile } from '@/components/reports/KpiTile'
import { Top10Table } from '@/components/reports/Top10Table'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { HealthBadge, fmtMs, fmtPct } from '@/components/apm/shared'
import { ErrorStatusBadge } from '@/components/apm/errorShared'

/* ── Types (mirror APM endpoints) ───────────────────────────────────────── */

interface ServiceRED {
  name: string; envs: string[]; health: string; request_count: number
  rps: number; error_rate: number; p50_ms: number; p95_ms: number; p99_ms: number; apdex: number
}
interface ServiceList { services: ServiceRED[]; window_seconds: number }

interface Slo {
  id: string; name: string; service_name: string; env: string | null
  operation: string | null; sli_type: string; target: number; window_days: number
}
interface BurnTier { tier: string; severity: string; breaching: boolean }
interface Budget { budget_consumed: number | null; budget_remaining: number | null; window_requests: number; tiers: BurnTier[] }

interface ErrorIssue {
  group_id: string; exception_type: string; message: string; service: string
  occurrences: number; last_seen: string; status: string
}
interface ErrorList { issues: ErrorIssue[]; counts: Record<string, number> }

interface UsageSummary {
  requests: number; unique_users: number; pages: number; services: number
  errors: number; error_rate: number; avg_ms: number; p95_ms: number
}
interface PageRow {
  route: string; service: string; hits: number; users: number
  errors: number; error_rate: number; p95_ms: number
}

/* ── Shared bits ────────────────────────────────────────────────────────── */

function SectionError({ error }: { error: unknown }) {
  return (
    <div className="m-5 rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
      {apiErrorMessage(error)}
    </div>
  )
}

function SectionLoading() {
  return (
    <div className="space-y-2 p-5">
      <Skeleton className="h-8 rounded" />
      <Skeleton className="h-8 rounded" />
      <Skeleton className="h-8 rounded" />
    </div>
  )
}

function errCls(rate: number): string {
  return rate >= 0.05 ? 'text-rose-400 font-semibold' : rate >= 0.02 ? 'text-amber-400' : ''
}

/* ── SLO attainment row (budget fetched per SLO) ────────────────────────── */

function SloRow({ slo }: { slo: Slo }) {
  const q = useQuery<Budget>({
    queryKey: ['apm', 'slo-budget', slo.id],
    queryFn: async () => (await api.get(`/apm/slos/${slo.id}/budget`)).data,
    staleTime: 60_000,
  })
  const b = q.data
  const remaining = b?.budget_remaining
  const pct = remaining != null ? Math.max(0, Math.min(100, remaining * 100)) : null
  const breaching = b?.tiers?.some((t) => t.breaching) ?? false
  const color = pct == null ? '#6b7280' : breaching || pct < 20 ? '#ef4444' : pct < 40 ? '#f59e0b' : '#22c55e'

  return (
    <Tr>
      <Td className="font-medium text-text">{slo.name}</Td>
      <Td className="text-xs text-text2">
        {slo.service_name}
        {slo.env && <span className="text-muted"> · {slo.env}</span>}
      </Td>
      <Td className="text-right font-mono text-xs">{slo.target}%</Td>
      <Td className="w-48">
        {q.isLoading ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />
        ) : pct == null ? (
          <span className="text-xs text-muted">no data</span>
        ) : (
          <div>
            <div className="mb-1 text-xs font-medium" style={{ color }}>{pct.toFixed(1)}% left</div>
            <div className="h-1.5 overflow-hidden rounded-full bg-surface2">
              <div className="h-full rounded-full" style={{ width: `${Math.max(2, pct)}%`, backgroundColor: color }} />
            </div>
          </div>
        )}
      </Td>
      <Td>
        {q.isLoading || pct == null ? (
          <span className="text-xs text-muted">—</span>
        ) : breaching ? (
          <span className="text-xs font-semibold uppercase text-danger">Burning</span>
        ) : pct < 20 ? (
          <span className="text-xs font-medium text-warning">Budget low</span>
        ) : (
          <span className="text-xs text-success">On track</span>
        )}
      </Td>
    </Tr>
  )
}

/* ── Report ─────────────────────────────────────────────────────────────── */

export default function ApmReport() {
  const services = useQuery<ServiceList>({
    queryKey: ['report', 'apm', 'services'],
    queryFn: async () => (await api.get('/apm/services?range=24h')).data,
  })
  const slos = useQuery<{ items: Slo[] }>({
    queryKey: ['report', 'apm', 'slos'],
    queryFn: async () => (await api.get('/apm/slos')).data,
  })
  const errors = useQuery<ErrorList>({
    queryKey: ['report', 'apm', 'errors'],
    queryFn: async () => (await api.get('/apm/errors?range_=7d')).data,
  })
  const usage = useQuery<UsageSummary>({
    queryKey: ['report', 'apm', 'usage-summary'],
    queryFn: async () => (await api.get('/apm/usage/summary?hours=168')).data,
  })
  const pages = useQuery<{ pages: PageRow[] }>({
    queryKey: ['report', 'apm', 'usage-pages'],
    queryFn: async () => (await api.get('/apm/usage/pages?hours=168&limit=10')).data,
  })

  const svcRows = services.data?.services ?? []
  const sloRows = slos.data?.items ?? []
  const topErrors = [...(errors.data?.issues ?? [])].sort((a, b) => b.occurrences - a.occurrences).slice(0, 10)
  const u = usage.data
  const pageRows = pages.data?.pages ?? []

  return (
    <div className="space-y-5">
      {/* 1 · Service health summary */}
      <ReportSection
        title="Service Health Summary"
        icon={<Boxes className="h-4 w-4" />}
        description="Golden signals per instrumented service · last 24 hours"
        padded={false}
      >
        {services.isLoading ? (
          <SectionLoading />
        ) : services.isError ? (
          <SectionError error={services.error} />
        ) : svcRows.length === 0 ? (
          <div className="px-5 pb-5"><EmptyReportState message="No services reported traces in the last 24 hours." /></div>
        ) : (
          <Top10Table
            rows={svcRows}
            columns={[
              { key: 'name', header: 'Service', render: (r) => <span className="font-medium text-text">{r.name}</span> },
              { key: 'health', header: 'Health', render: (r) => <HealthBadge health={r.health} /> },
              {
                key: 'request_count', header: 'Requests', align: 'right',
                render: (r) => <span className="font-mono text-xs tabular-nums">{r.request_count.toLocaleString()}</span>,
              },
              {
                key: 'error_rate', header: 'Error rate', align: 'right',
                render: (r) => <span className={`font-mono text-xs ${errCls(r.error_rate)}`}>{fmtPct(r.error_rate)}</span>,
              },
              { key: 'p50_ms', header: 'p50', align: 'right', render: (r) => <span className="font-mono text-xs">{fmtMs(r.p50_ms)}</span> },
              { key: 'p95_ms', header: 'p95', align: 'right', render: (r) => <span className="font-mono text-xs">{fmtMs(r.p95_ms)}</span> },
              {
                key: 'apdex', header: 'Apdex', align: 'right',
                render: (r) => <span className="font-mono text-xs">{r.apdex.toFixed(2)}</span>,
              },
            ]}
            defaultSortKey="request_count"
            defaultSortDir="desc"
            rowKey={(r) => r.name}
          />
        )}
      </ReportSection>

      {/* 2 · SLO attainment */}
      <ReportSection
        title="SLO Attainment"
        icon={<Target className="h-4 w-4" />}
        description="Error-budget status per service-level objective · rolling SLO windows"
        padded={false}
      >
        {slos.isLoading ? (
          <SectionLoading />
        ) : slos.isError ? (
          <SectionError error={slos.error} />
        ) : sloRows.length === 0 ? (
          <div className="px-5 pb-5"><EmptyReportState message="No SLOs defined." /></div>
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>SLO</Th><Th>Service</Th>
                <Th className="text-right">Target</Th>
                <Th>Budget remaining</Th><Th>Burn status</Th>
              </Tr>
            </THead>
            <TBody>
              {sloRows.map((s) => <SloRow key={s.id} slo={s} />)}
            </TBody>
          </Table>
        )}
      </ReportSection>

      {/* 3 · Top error groups */}
      <ReportSection
        title="Top Error Groups"
        icon={<Bug className="h-4 w-4" />}
        description="Most frequent error groups across services · last 7 days"
        padded={false}
      >
        {errors.isLoading ? (
          <SectionLoading />
        ) : errors.isError ? (
          <SectionError error={errors.error} />
        ) : topErrors.length === 0 ? (
          <div className="px-5 pb-5"><EmptyReportState message="No errors captured in the last 7 days. ✓" /></div>
        ) : (
          <Top10Table
            rows={topErrors}
            columns={[
              {
                key: 'exception_type', header: 'Error',
                render: (r) => (
                  <div className="min-w-0">
                    <div className="font-medium text-text">{r.exception_type}</div>
                    <div className="max-w-md truncate text-xs text-muted" title={r.message}>{r.message}</div>
                  </div>
                ),
              },
              { key: 'service', header: 'Service', render: (r) => <span className="text-xs text-text2">{r.service}</span> },
              {
                key: 'occurrences', header: 'Count', align: 'right',
                render: (r) => <span className="font-mono text-xs tabular-nums">{r.occurrences.toLocaleString()}</span>,
              },
              {
                key: 'last_seen', header: 'Last seen',
                render: (r) => <span className="text-xs text-muted">{relativeTime(r.last_seen)}</span>,
                sortValue: (r) => (r.last_seen ? Date.parse(r.last_seen) : 0),
              },
              { key: 'status', header: 'Status', render: (r) => <ErrorStatusBadge status={r.status} /> },
            ]}
            defaultSortKey="occurrences"
            defaultSortDir="desc"
            rowKey={(r) => r.group_id}
          />
        )}
      </ReportSection>

      {/* 4 · Usage highlights */}
      <ReportSection
        title="Usage Highlights"
        icon={<Users className="h-4 w-4" />}
        description="Traffic, audience and busiest pages · last 7 days"
        padded={false}
      >
        {usage.isLoading ? (
          <SectionLoading />
        ) : usage.isError ? (
          <SectionError error={usage.error} />
        ) : (
          <div className="space-y-4 p-5">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <KpiTile
                label="Requests"
                value={(u?.requests ?? 0).toLocaleString()}
                accent="primary"
                icon={<Activity className="h-4 w-4" />}
                subtitle={`${u?.services ?? 0} services · ${u?.pages ?? 0} pages`}
              />
              <KpiTile
                label="Unique Users"
                value={(u?.unique_users ?? 0).toLocaleString()}
                accent="info"
                icon={<Users className="h-4 w-4" />}
                subtitle="Attributed via enduser.id"
              />
              <KpiTile
                label="Error Rate"
                value={fmtPct(u?.error_rate)}
                accent={(u?.error_rate ?? 0) > 0.02 ? 'danger' : 'success'}
                icon={<Bug className="h-4 w-4" />}
                subtitle={`${(u?.errors ?? 0).toLocaleString()} failed requests`}
              />
              <KpiTile
                label="Latency p95"
                value={fmtMs(u?.p95_ms)}
                accent={(u?.p95_ms ?? 0) > 1000 ? 'warning' : 'primary'}
                icon={<Target className="h-4 w-4" />}
                subtitle={`avg ${fmtMs(u?.avg_ms)}`}
              />
            </div>

            {pages.isError ? (
              <SectionError error={pages.error} />
            ) : pageRows.length === 0 && !pages.isLoading ? (
              <EmptyReportState message="No page traffic recorded in the last 7 days." />
            ) : (
              <div className="rounded-lg border border-border">
                <Top10Table
                  rows={pageRows}
                  columns={[
                    { key: 'route', header: 'Route', render: (r) => <span className="font-mono text-xs text-text">{r.route}</span> },
                    { key: 'service', header: 'Service', render: (r) => <span className="text-xs text-text2">{r.service}</span> },
                    {
                      key: 'hits', header: 'Hits', align: 'right',
                      render: (r) => <span className="font-mono text-xs tabular-nums">{r.hits.toLocaleString()}</span>,
                    },
                    {
                      key: 'users', header: 'Users', align: 'right',
                      render: (r) => <span className="font-mono text-xs tabular-nums">{r.users.toLocaleString()}</span>,
                    },
                    {
                      key: 'error_rate', header: 'Error rate', align: 'right',
                      render: (r) => <span className={`font-mono text-xs ${errCls(r.error_rate)}`}>{fmtPct(r.error_rate)}</span>,
                    },
                    { key: 'p95_ms', header: 'p95', align: 'right', render: (r) => <span className="font-mono text-xs">{fmtMs(r.p95_ms)}</span> },
                  ]}
                  defaultSortKey="hits"
                  defaultSortDir="desc"
                  rowKey={(r) => r.route}
                />
              </div>
            )}
          </div>
        )}
      </ReportSection>
    </div>
  )
}
