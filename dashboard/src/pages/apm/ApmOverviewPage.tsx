import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity, AlertTriangle, ArrowRight, Bug, CheckCircle2, Loader2, Radio, Target,
} from 'lucide-react'
import {
  Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { HEALTH_COLOR, HealthBadge, KpiTile, fmtMs, fmtPct, fmtRps } from '@/components/apm/shared'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { ApmRangePicker, rangePhrase, useApmRange } from '@/components/apm/ApmRange'
import type {
  DataQuality, ErrorListResponse, REDPoint, ServiceListResponse, Slo, SloBudget, SyntheticMonitor,
} from '@/types/apm'

const HEALTH_RANK: Record<string, number> = { critical: 0, degraded: 1, no_data: 2, healthy: 3 }

/** A service earns a row in "needs attention" only if something is actually wrong. */
function needsAttention(health: string): boolean {
  return health === 'critical' || health === 'degraded' || health === 'no_data'
}

function FleetChart({ data, dataKey, color, label, fmt }: {
  data: REDPoint[]; dataKey: string; color: string; label: string; fmt: (v: number) => string
}) {
  const gid = `apm-ov-${dataKey}`
  return (
    <Card>
      <CardHeader className="pb-1"><CardTitle className="text-xs font-medium text-muted">{label}</CardTitle></CardHeader>
      <CardContent className="pt-0">
        <ResponsiveContainer width="100%" height={132}>
          <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -12 }}>
            <defs>
              <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity={0.38} />
                <stop offset="100%" stopColor={color} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.14)" vertical={false} />
            <XAxis
              dataKey="timestamp" tick={{ fontSize: 10, fill: '#94a3b8' }} minTickGap={44}
              tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            />
            <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} width={46} tickFormatter={fmt} />
            <Tooltip
              contentStyle={{ background: '#0d121b', border: '1px solid #1e293b', fontSize: 12, color: '#e5e7eb' }}
              labelFormatter={(t) => new Date(t).toLocaleString()}
              formatter={(v: unknown) => fmt(Number(v))}
            />
            <Area type="monotone" dataKey={dataKey} stroke={color} fill={`url(#${gid})`} strokeWidth={2} />
          </AreaChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  )
}

export function ApmOverviewPage() {
  const navigate = useNavigate()
  const [range, setRange] = useApmRange('1h')

  const services = useQuery<ServiceListResponse>({
    queryKey: ['apm', 'services', { range }],
    queryFn: async () => (await api.get(`/apm/services?range=${range}`)).data,
    refetchInterval: 15_000,
  })
  const errors = useQuery<ErrorListResponse>({
    queryKey: ['apm', 'errors', { range: '24h', status: 'unresolved' }],
    queryFn: async () => (await api.get('/apm/errors?range_=24h&status=unresolved')).data,
    refetchInterval: 30_000,
  })
  const slos = useQuery<{ items: Slo[] }>({
    queryKey: ['apm', 'slos'],
    queryFn: async () => (await api.get('/apm/slos')).data,
    refetchInterval: 60_000,
  })
  const synthetics = useQuery<{ monitors: SyntheticMonitor[]; summary: Record<string, number> }>({
    queryKey: ['apm', 'synthetics', 'summary'],
    queryFn: async () => (await api.get('/apm/synthetics')).data,
    refetchInterval: 60_000,
  })
  const quality = useQuery<DataQuality>({
    queryKey: ['apm', 'data-quality', 'overview'],
    queryFn: async () => (await api.get('/apm/data-quality?hours=24')).data,
    refetchInterval: 60_000,
  })

  const list = services.data?.services ?? []
  const health = services.data?.facets.health ?? {}
  const totalRps = list.reduce((a, s) => a + s.rps, 0)
  const totalReqs = list.reduce((a, s) => a + s.request_count, 0)
  const totalErrs = list.reduce((a, s) => a + s.request_count * s.error_rate, 0)
  const fleetErrorRate = totalReqs > 0 ? totalErrs / totalReqs : 0
  // Fleet p95 has no exact closed form from per-service digests; the worst
  // service's p95 is the honest headline — it is what a user actually waits on.
  const worstP95 = list.reduce((a, s) => Math.max(a, s.p95_ms), 0)

  const attention = useMemo(
    () => list
      .filter((s) => needsAttention(s.health))
      .sort((a, b) =>
        (HEALTH_RANK[a.health] ?? 9) - (HEALTH_RANK[b.health] ?? 9)
        || b.error_rate - a.error_rate
        || b.p95_ms - a.p95_ms)
      .slice(0, 8),
    [list],
  )

  // Fleet trend: sum the busiest services' RED series. Charting every service
  // would be unreadable and would fan out one request per service.
  const topServices = useMemo(
    () => [...list].sort((a, b) => b.request_count - a.request_count).slice(0, 5).map((s) => s.name),
    [list],
  )
  const trends = useQuery<REDPoint[]>({
    queryKey: ['apm', 'fleet-red', { range, topServices }],
    enabled: topServices.length > 0,
    refetchInterval: 30_000,
    queryFn: async () => {
      const series = await Promise.all(topServices.map(async (name) =>
        (await api.get<REDPoint[]>(`/apm/services/${encodeURIComponent(name)}/red?range=${range}`)).data))
      const merged = new Map<string, { rps: number; reqs: number; errs: number; p95: number }>()
      series.forEach((points, i) => {
        const svc = list.find((s) => s.name === topServices[i])
        const bucketReqs = svc && points.length ? svc.request_count / points.length : 0
        points.forEach((p) => {
          const slot = merged.get(p.timestamp) ?? { rps: 0, reqs: 0, errs: 0, p95: 0 }
          slot.rps += p.rps
          slot.reqs += bucketReqs
          slot.errs += bucketReqs * p.error_rate
          slot.p95 = Math.max(slot.p95, p.p95_ms)
          merged.set(p.timestamp, slot)
        })
      })
      return [...merged.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([timestamp, v]) => ({
          timestamp,
          rps: Number(v.rps.toFixed(3)),
          error_rate: v.reqs > 0 ? Number((v.errs / v.reqs).toFixed(5)) : 0,
          p50_ms: 0,
          p95_ms: Number(v.p95.toFixed(2)),
        }))
    },
  })

  const openIssues = errors.data?.issues ?? []
  const monitors = synthetics.data?.monitors ?? []
  const monitorsDown = monitors.filter((m) => m.status === 'down').length
  const ingestIssues = quality.data?.issues ?? []
  const noServices = !services.isLoading && list.length === 0

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="Overview"
        description="Fleet-wide application health — golden signals, open errors, reliability budgets and journey checks in one place."
        article="overview"
        actions={<ApmRangePicker value={range} onChange={setRange} />}
      />

      {services.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load services — {apiErrorMessage(services.error)}
        </div>
      )}

      {noServices ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-16 text-center">
            <Radio className="h-8 w-8 text-muted" />
            <div className="text-sm font-medium text-text">No service is reporting telemetry yet</div>
            <p className="max-w-lg text-sm text-muted">
              APM builds every screen from OpenTelemetry spans. Create an ingest key, point an SDK or the
              ZenPlus agent at this appliance, and services appear here within a minute.
            </p>
            <div className="flex flex-wrap justify-center gap-2 pt-1">
              <button
                onClick={() => navigate('/apm/settings')}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-semibold text-black"
              >
                Create an ingest key <ArrowRight className="h-3.5 w-3.5" />
              </button>
              <a
                href="https://zentryc.com/kb/zenplus/apm/getting-started/"
                target="_blank" rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted hover:text-text"
              >
                Read the setup guide
              </a>
            </div>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <KpiTile label="Services" value={services.isLoading ? '—' : list.length} />
            <KpiTile label="Healthy" value={health.healthy ?? 0} accent={HEALTH_COLOR.healthy} />
            <KpiTile label="Degraded" value={health.degraded ?? 0} accent={health.degraded ? HEALTH_COLOR.degraded : undefined} />
            <KpiTile label="Critical" value={health.critical ?? 0} accent={health.critical ? HEALTH_COLOR.critical : undefined} />
            <KpiTile label="Throughput" value={fmtRps(totalRps)} />
            <KpiTile
              label="Error rate"
              value={fmtPct(fleetErrorRate)}
              accent={fleetErrorRate >= 0.05 ? HEALTH_COLOR.critical : fleetErrorRate >= 0.01 ? HEALTH_COLOR.degraded : undefined}
            />
          </div>

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            {trends.isLoading ? (
              <Card className="lg:col-span-3">
                <CardContent className="flex items-center justify-center gap-2 py-16 text-muted">
                  <Loader2 className="h-4 w-4 animate-spin" /> Building fleet trend…
                </CardContent>
              </Card>
            ) : (
              <>
                <FleetChart data={trends.data ?? []} dataKey="rps" color="#3b82f6"
                  label={`Throughput — top ${topServices.length} services (req/s)`} fmt={(v) => v.toFixed(2)} />
                <FleetChart data={trends.data ?? []} dataKey="error_rate" color={HEALTH_COLOR.critical}
                  label="Error rate" fmt={(v) => `${(v * 100).toFixed(1)}%`} />
                <FleetChart data={trends.data ?? []} dataKey="p95_ms" color={HEALTH_COLOR.degraded}
                  label={`Worst p95 (${fmtMs(worstP95)} now)`} fmt={(v) => `${v.toFixed(0)}ms`} />
              </>
            )}
          </div>

          <div className="grid grid-cols-1 gap-3 xl:grid-cols-3">
            <Card className="xl:col-span-2">
              <CardHeader className="flex flex-row items-center justify-between pb-1">
                <CardTitle className="flex items-center gap-2 text-sm">
                  <AlertTriangle className="h-4 w-4 text-warning" /> Services needing attention
                </CardTitle>
                <button onClick={() => navigate('/apm/services')} className="text-xs font-medium text-primary hover:underline">
                  All services
                </button>
              </CardHeader>
              <CardContent className="p-0">
                {services.isLoading ? (
                  <div className="flex items-center justify-center gap-2 py-12 text-muted">
                    <Loader2 className="h-4 w-4 animate-spin" /> Loading…
                  </div>
                ) : attention.length === 0 ? (
                  <div className="flex flex-col items-center gap-2 py-12 text-muted">
                    <CheckCircle2 className="h-6 w-6 text-success" />
                    <span className="text-sm">All {list.length} services healthy in {rangePhrase(range)}.</span>
                  </div>
                ) : (
                  <Table>
                    <THead>
                      <Tr>
                        <Th>Service</Th><Th>Health</Th>
                        <Th className="text-right">Throughput</Th><Th className="text-right">Error rate</Th>
                        <Th className="text-right">p95</Th><Th className="text-right">Apdex</Th>
                      </Tr>
                    </THead>
                    <TBody>
                      {attention.map((s) => (
                        <Tr key={s.name} className="cursor-pointer hover:bg-surface2"
                          onClick={() => navigate(`/apm/services/${encodeURIComponent(s.name)}`)}>
                          <Td className="font-medium text-text">{s.name}</Td>
                          <Td><HealthBadge health={s.health} /></Td>
                          <Td className="text-right font-mono text-xs">{fmtRps(s.rps)}</Td>
                          <Td className="text-right font-mono text-xs">{fmtPct(s.error_rate)}</Td>
                          <Td className="text-right font-mono text-xs">{fmtMs(s.p95_ms)}</Td>
                          <Td className="text-right font-mono text-xs">{s.apdex.toFixed(2)}</Td>
                        </Tr>
                      ))}
                    </TBody>
                  </Table>
                )}
              </CardContent>
            </Card>

            <div className="space-y-3">
              <Card>
                <CardHeader className="flex flex-row items-center justify-between pb-1">
                  <CardTitle className="flex items-center gap-2 text-sm">
                    <Bug className="h-4 w-4 text-danger" /> Open errors (24h)
                  </CardTitle>
                  <button onClick={() => navigate('/apm/errors')} className="text-xs font-medium text-primary hover:underline">
                    Inbox
                  </button>
                </CardHeader>
                <CardContent className="space-y-2 pt-1">
                  {openIssues.length === 0 ? (
                    <p className="py-3 text-sm text-muted">No unresolved errors.</p>
                  ) : openIssues.slice(0, 4).map((e) => (
                    <button key={e.group_id} onClick={() => navigate(`/apm/errors/${e.group_id}`)}
                      className="flex w-full items-start justify-between gap-3 rounded-md px-2 py-1.5 text-left hover:bg-surface2">
                      <div className="min-w-0">
                        <div className="truncate text-xs font-medium text-text">{e.exception_type}</div>
                        <div className="truncate text-[11px] text-muted">{e.service}</div>
                      </div>
                      <span className="shrink-0 font-mono text-xs text-danger">{e.occurrences}</span>
                    </button>
                  ))}
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="flex flex-row items-center justify-between pb-1">
                  <CardTitle className="flex items-center gap-2 text-sm">
                    <Target className="h-4 w-4 text-primary" /> Reliability
                  </CardTitle>
                  <button onClick={() => navigate('/apm/slos')} className="text-xs font-medium text-primary hover:underline">
                    SLOs
                  </button>
                </CardHeader>
                <CardContent className="space-y-1.5 pt-1 text-xs">
                  <div className="flex justify-between">
                    <span className="text-muted">SLOs defined</span>
                    <span className="font-medium text-text">{slos.data?.items.length ?? 0}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Synthetic scenarios</span>
                    <span className="font-medium text-text">{monitors.length}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Scenarios down</span>
                    <span className={`font-medium ${monitorsDown ? 'text-danger' : 'text-text'}`}>{monitorsDown}</span>
                  </div>
                  {(slos.data?.items.length ?? 0) === 0 && (
                    <p className="pt-1 text-[11px] text-muted">
                      No SLO defined yet — until one exists, nothing pages on error-budget burn.
                    </p>
                  )}
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="flex flex-row items-center justify-between pb-1">
                  <CardTitle className="flex items-center gap-2 text-sm">
                    <Activity className="h-4 w-4 text-primary" /> Pipeline health
                  </CardTitle>
                  <button onClick={() => navigate('/apm/settings')} className="text-xs font-medium text-primary hover:underline">
                    Details
                  </button>
                </CardHeader>
                <CardContent className="space-y-1.5 pt-1 text-xs">
                  <div className="flex justify-between">
                    <span className="text-muted">Spans accepted (24h)</span>
                    <span className="font-medium text-text">{(quality.data?.ingest.accepted ?? 0).toLocaleString()}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Rejected / skewed</span>
                    <span className={`font-medium ${(quality.data?.ingest.rejected ?? 0) ? 'text-warning' : 'text-text'}`}>
                      {(quality.data?.ingest.rejected ?? 0).toLocaleString()}
                    </span>
                  </div>
                  {ingestIssues.length === 0 ? (
                    <p className="flex items-center gap-1.5 pt-1 text-[11px] text-success">
                      <CheckCircle2 className="h-3 w-3" /> Telemetry pipeline healthy
                    </p>
                  ) : ingestIssues.slice(0, 2).map((i) => (
                    <p key={i} className="pt-0.5 text-[11px] text-warning">• {i}</p>
                  ))}
                </CardContent>
              </Card>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
