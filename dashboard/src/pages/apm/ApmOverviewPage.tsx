import { useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity, AlertTriangle, ArrowRight, Boxes, Bug, CheckCircle2, Gauge, Loader2, Radio, Target,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { HEALTH_COLOR, HealthBadge, fmtMs, fmtPct, fmtRps } from '@/components/apm/shared'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { ApmRangePicker, CompareToggle, previousWindow, rangePhrase, shiftTimestamp, useApmCompare, useApmRange } from '@/components/apm/ApmRange'
import {
  APM_SERIES, ApdexCell, ApmKpi, ApmTimeChart, ChartPanel, CompareDelta, DeepDiveLink, DeepLinks,
  ErrorRateCell, HealthShareBar, LatencyCell, ThroughputCell, errorTone, fmtCount, latencyTone,
} from '@/components/apm/viz'
import type {
  DataQuality, ErrorListResponse, REDPoint, ServiceListResponse, Slo, SloBudget, SyntheticMonitor,
} from '@/types/apm'

const HEALTH_RANK: Record<string, number> = { critical: 0, degraded: 1, no_data: 2, healthy: 3 }

function needsAttention(health: string): boolean {
  return health === 'critical' || health === 'degraded' || health === 'no_data'
}

export function ApmOverviewPage() {
  const navigate = useNavigate()
  const [range, setRange] = useApmRange('1h')
  const [compare, setCompare] = useApmCompare()
  const prevWin = previousWindow(range)

  const services = useQuery<ServiceListResponse>({
    queryKey: ['apm', 'services', { range }],
    queryFn: async () => (await api.get(`/apm/services?range=${range}`)).data,
    refetchInterval: 15_000,
  })
  const prevServices = useQuery<ServiceListResponse>({
    queryKey: ['apm', 'services-prev', prevWin],
    enabled: compare,
    queryFn: async () => (await api.get(`/apm/services?from_ms=${prevWin.from_ms}&to_ms=${prevWin.to_ms}`)).data,
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
  const sloIds = useMemo(() => (slos.data?.items ?? []).slice(0, 6).map((s) => s.id), [slos.data])
  const budgets = useQuery<SloBudget[]>({
    queryKey: ['apm', 'overview-slo-budgets', sloIds],
    enabled: sloIds.length > 0,
    refetchInterval: 60_000,
    queryFn: async () => {
      const out = await Promise.all(sloIds.map(async (id) => {
        try { return (await api.get<SloBudget>(`/apm/slos/${id}/budget`)).data } catch { return null }
      }))
      return out.filter((b): b is SloBudget => b != null)
    },
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
  const prevList = prevServices.data?.services ?? []
  const health = services.data?.facets.health ?? {}
  const totalRps = list.reduce((a, s) => a + s.rps, 0)
  const totalReqs = list.reduce((a, s) => a + s.request_count, 0)
  const totalErrs = list.reduce((a, s) => a + s.request_count * s.error_rate, 0)
  const fleetErrorRate = totalReqs > 0 ? totalErrs / totalReqs : 0
  const worstP95 = list.reduce((a, s) => Math.max(a, s.p95_ms), 0)
  const prevRps = prevList.reduce((a, s) => a + s.rps, 0)
  const prevReqs = prevList.reduce((a, s) => a + s.request_count, 0)
  const prevErrs = prevList.reduce((a, s) => a + s.request_count * s.error_rate, 0)
  const prevErrorRate = prevReqs > 0 ? prevErrs / prevReqs : 0
  const prevWorstP95 = prevList.reduce((a, s) => Math.max(a, s.p95_ms), 0)
  const healthyCount = health.healthy ?? 0
  const degradedCount = health.degraded ?? 0
  const criticalCount = health.critical ?? 0
  const noDataCount = health.no_data ?? Math.max(0, list.length - healthyCount - degradedCount - criticalCount)

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
  const busiest = useMemo(
    () => [...list].sort((a, b) => b.request_count - a.request_count).slice(0, 8),
    [list],
  )
  const maxRps = Math.max(...list.map((s) => s.rps), 0.0001)

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
      const merged = new Map<string, { rps: number; reqs: number; errs: number; p95: number; p50: number }>()
      series.forEach((points, i) => {
        const svc = list.find((s) => s.name === topServices[i])
        const bucketReqs = svc && points.length ? svc.request_count / points.length : 0
        points.forEach((p) => {
          const slot = merged.get(p.timestamp) ?? { rps: 0, reqs: 0, errs: 0, p95: 0, p50: 0 }
          slot.rps += p.rps
          slot.reqs += bucketReqs
          slot.errs += bucketReqs * p.error_rate
          slot.p95 = Math.max(slot.p95, p.p95_ms)
          slot.p50 = Math.max(slot.p50, p.p50_ms)
          merged.set(p.timestamp, slot)
        })
      })
      return [...merged.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([timestamp, v]) => ({
          timestamp,
          rps: Number(v.rps.toFixed(3)),
          error_rate: v.reqs > 0 ? Number((v.errs / v.reqs).toFixed(5)) : 0,
          p50_ms: Number(v.p50.toFixed(2)),
          p95_ms: Number(v.p95.toFixed(2)),
        }))
    },
  })
  const prevTrends = useQuery<REDPoint[]>({
    queryKey: ['apm', 'fleet-red-prev', prevWin, topServices],
    enabled: compare && topServices.length > 0,
    queryFn: async () => {
      const series = await Promise.all(topServices.map(async (name) =>
        (await api.get<REDPoint[]>(`/apm/services/${encodeURIComponent(name)}/red?from_ms=${prevWin.from_ms}&to_ms=${prevWin.to_ms}`)).data))
      const merged = new Map<string, { rps: number; p95: number; p50: number; error_rate: number }>()
      series.forEach((points) => {
        points.forEach((p) => {
          const ts = shiftTimestamp(p.timestamp, prevWin.windowMs)
          const slot = merged.get(ts) ?? { rps: 0, p95: 0, p50: 0, error_rate: 0 }
          slot.rps += p.rps
          slot.p95 = Math.max(slot.p95, p.p95_ms)
          slot.p50 = Math.max(slot.p50, p.p50_ms)
          slot.error_rate = Math.max(slot.error_rate, p.error_rate)
          merged.set(ts, slot)
        })
      })
      return [...merged.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([timestamp, v]) => ({
        timestamp, rps: v.rps, error_rate: v.error_rate, p50_ms: v.p50, p95_ms: v.p95,
      }))
    },
  })
  const trendPoints = useMemo(() => {
    const curr = trends.data ?? []
    if (!compare || !prevTrends.data?.length) return curr
    const prevMap = new Map(prevTrends.data.map((p) => [p.timestamp, p]))
    const keys = new Set([...curr.map((p) => p.timestamp), ...prevMap.keys()])
    return [...keys].sort().map((timestamp) => {
      const c = curr.find((p) => p.timestamp === timestamp)
      const p = prevMap.get(timestamp)
      return {
        timestamp,
        rps: c?.rps ?? null,
        error_rate: c?.error_rate ?? null,
        p50_ms: c?.p50_ms ?? null,
        p95_ms: c?.p95_ms ?? null,
        rps_prev: p?.rps ?? null,
        error_rate_prev: p?.error_rate ?? null,
        p50_prev: p?.p50_ms ?? null,
        p95_prev: p?.p95_ms ?? null,
      }
    })
  }, [trends.data, prevTrends.data, compare])

  const openIssues = errors.data?.issues ?? []
  const monitors = synthetics.data?.monitors ?? []
  const monitorsDown = monitors.filter((m) => m.status === 'down').length
  const ingestIssues = quality.data?.issues ?? []
  const noServices = !services.isLoading && list.length === 0
  const slosAtRisk = (budgets.data ?? []).filter((b) => (b.budget_remaining ?? 1) < 0.25).length
  const openCount = errors.data?.counts?.unresolved ?? openIssues.length

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="Fleet overview"
        description={`Golden signals across every instrumented service for ${rangePhrase(range)}. Click a KPI, chart or row to deep-dive.`}
        article="overview"
        actions={<><CompareToggle value={compare} onChange={setCompare} /><ApmRangePicker value={range} onChange={setRange} /></>}
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
            <ApmKpi
              to="/apm/services"
              label="Services"
              icon={<Boxes className="h-4 w-4" />}
              tone={criticalCount ? 'danger' : degradedCount ? 'warning' : 'success'}
              value={<>{healthyCount}<span className="text-base font-medium text-muted">/{list.length}</span></>}
              sub={criticalCount ? `${criticalCount} critical` : degradedCount ? `${degradedCount} degraded` : 'all healthy'}
              foot={<HealthShareBar healthy={healthyCount} degraded={degradedCount} critical={criticalCount} noData={noDataCount} />}
            />
            <ApmKpi
              to="/apm/services"
              label="Throughput"
              icon={<Activity className="h-4 w-4" />}
              tone="info"
              value={fmtRps(totalRps)}
              sub={compare ? <CompareDelta current={totalRps} previous={prevRps} /> : `${fmtCount(totalReqs)} requests · ${range}`}
            />
            <ApmKpi
              to="/apm/errors"
              label="Error rate"
              icon={<Bug className="h-4 w-4" />}
              tone={errorTone(fleetErrorRate)}
              value={fmtPct(fleetErrorRate)}
              sub={compare ? <CompareDelta current={fleetErrorRate} previous={prevErrorRate} invert /> : `${openCount} unresolved issues · 24h`}
            />
            <ApmKpi
              to="/apm/services"
              label="Worst p95"
              icon={<Gauge className="h-4 w-4" />}
              tone={latencyTone(worstP95)}
              value={fmtMs(worstP95)}
              sub={compare ? <CompareDelta current={worstP95} previous={prevWorstP95} invert /> : 'slowest service in the fleet'}
            />
            <ApmKpi
              to="/apm/slos"
              label="SLO budgets"
              icon={<Target className="h-4 w-4" />}
              tone={slosAtRisk ? 'danger' : sloIds.length ? 'success' : 'primary'}
              value={sloIds.length ? <>{slosAtRisk}<span className="text-base font-medium text-muted"> at risk</span></> : '—'}
              sub={`${sloIds.length} objectives tracked`}
            />
            <ApmKpi
              to="/apm/synthetics"
              label="Synthetics"
              icon={<Activity className="h-4 w-4" />}
              tone={monitorsDown ? 'danger' : monitors.length ? 'success' : 'primary'}
              value={monitors.length ? <>{monitors.length - monitorsDown}<span className="text-base font-medium text-muted">/{monitors.length}</span></> : '—'}
              sub={monitorsDown ? `${monitorsDown} journeys failing` : 'journeys passing'}
            />
          </div>

          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            <ChartPanel
              title="Latency"
              hint={`p50 / p95 · top ${topServices.length} services`}
              right={<DeepDiveLink to={`/apm/traces?mode=indexed&range=${range}`}>Traces</DeepDiveLink>}
            >
              <ApmTimeChart
                data={trendPoints}
                loading={trends.isLoading}
                series={[
                  { key: 'p50_ms', name: 'p50', color: APM_SERIES.latencyP50, fmt: fmtMs },
                  { key: 'p95_ms', name: 'p95', color: APM_SERIES.latency, fmt: fmtMs },
                  ...(compare ? [
                    { key: 'p50_prev', name: 'p50 prev', color: APM_SERIES.latencyP50, fmt: fmtMs, dashed: true, area: false },
                    { key: 'p95_prev', name: 'p95 prev', color: APM_SERIES.latency, fmt: fmtMs, dashed: true, area: false },
                  ] : []),
                ]}
                height={268}
                onPointClick={() => navigate(`/apm/traces?mode=indexed&range=${range}`)}
              />
            </ChartPanel>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-1">
              <ChartPanel title="Throughput" hint="requests / second">
                <ApmTimeChart
                  data={trendPoints}
                  loading={trends.isLoading}
                  series={[
                    { key: 'rps', name: 'req/s', color: APM_SERIES.throughput, fmt: (v) => v.toFixed(2) },
                    ...(compare ? [{ key: 'rps_prev', name: 'prev', color: APM_SERIES.throughput, fmt: (v: number) => v.toFixed(2), dashed: true, area: false }] : []),
                  ]}
                  height={120}
                />
              </ChartPanel>
              <ChartPanel title="Error rate" hint="request-weighted fleet">
                <ApmTimeChart
                  data={trendPoints}
                  loading={trends.isLoading}
                  series={[
                    { key: 'error_rate', name: 'errors', color: APM_SERIES.errors, fmt: fmtPct },
                    ...(compare ? [{ key: 'error_rate_prev', name: 'prev', color: APM_SERIES.errors, fmt: fmtPct, dashed: true, area: false }] : []),
                  ]}
                  height={120}
                />
              </ChartPanel>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 xl:grid-cols-12">
            <Card className="overflow-hidden xl:col-span-7">
              <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
                <div>
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-muted">Needs attention</div>
                  <div className="text-[11px] text-text2">{attention.length} of {list.length} services</div>
                </div>
                <DeepDiveLink to={`/apm/services?range=${range}`}>All services</DeepDiveLink>
              </div>
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
                      <Th>Service</Th>
                      <Th>Health</Th>
                      <Th className="text-right">Throughput</Th>
                      <Th className="text-right">Error rate</Th>
                      <Th className="text-right">p95</Th>
                      <Th className="text-right">Apdex</Th>
                      <Th className="text-right">Deep dive</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {attention.map((s) => (
                      <Tr key={s.name} className="cursor-pointer" onClick={() => navigate(`/apm/services/${encodeURIComponent(s.name)}?range=${range}`)}>
                        <Td className="font-medium text-text">
                          <div>{s.name}</div>
                          <div className="text-[10px] text-muted">{s.envs.join(', ') || '—'}</div>
                        </Td>
                        <Td><HealthBadge health={s.health} /></Td>
                        <Td className="text-right"><ThroughputCell rps={s.rps} count={s.request_count} maxRps={maxRps} /></Td>
                        <Td className="text-right"><ErrorRateCell rate={s.error_rate} /></Td>
                        <Td className="text-right"><LatencyCell ms={s.p95_ms} /></Td>
                        <Td className="text-right"><ApdexCell value={s.apdex} /></Td>
                        <Td><DeepLinks service={s.name} range={range} /></Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              )}
            </Card>

            <Card className="overflow-hidden xl:col-span-5">
              <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
                <div>
                  <div className="text-[11px] font-semibold uppercase tracking-wider text-muted">Busiest services</div>
                  <div className="text-[11px] text-text2">by request volume</div>
                </div>
                <DeepDiveLink to={`/apm/services?range=${range}`}>Catalog</DeepDiveLink>
              </div>
              <Table>
                <THead>
                  <Tr>
                    <Th>Service</Th>
                    <Th className="text-right">Volume</Th>
                    <Th className="text-right">p95</Th>
                    <Th className="text-right">Errors</Th>
                  </Tr>
                </THead>
                <TBody>
                  {busiest.map((s) => (
                    <Tr key={s.name} className="cursor-pointer" onClick={() => navigate(`/apm/services/${encodeURIComponent(s.name)}?range=${range}`)}>
                      <Td className="font-medium text-text">{s.name}</Td>
                      <Td className="text-right"><ThroughputCell rps={s.rps} count={s.request_count} maxRps={maxRps} /></Td>
                      <Td className="text-right"><LatencyCell ms={s.p95_ms} /></Td>
                      <Td className="text-right"><ErrorRateCell rate={s.error_rate} /></Td>
                    </Tr>
                  ))}
                </TBody>
              </Table>
            </Card>
          </div>

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            <Card className="overflow-hidden">
              <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
                <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
                  <Bug className="h-3.5 w-3.5 text-danger" /> Open errors · 24h
                </div>
                <DeepDiveLink to="/apm/errors">Inbox</DeepDiveLink>
              </div>
              <div className="space-y-0.5 px-2 py-2">
                {openIssues.length === 0 ? (
                  <p className="px-2 py-6 text-center text-sm text-muted">No unresolved errors.</p>
                ) : openIssues.slice(0, 6).map((e) => (
                  <Link
                    key={e.group_id}
                    to={`/apm/errors/${e.group_id}`}
                    className="flex items-start gap-2.5 rounded-md px-2 py-1.5 hover:bg-surface2"
                  >
                    <span className="mt-0.5 w-1 self-stretch rounded-full bg-danger" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-mono text-xs font-semibold text-text">{e.exception_type}</div>
                      <div className="truncate text-[11px] text-muted">{e.service}{e.http_route ? ` · ${e.http_route}` : ''}</div>
                    </div>
                    <span className="shrink-0 rounded bg-danger/15 px-1.5 font-mono text-[10px] font-bold text-danger">
                      ×{fmtCount(e.occurrences)}
                    </span>
                  </Link>
                ))}
              </div>
            </Card>

            <Card className="overflow-hidden">
              <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
                <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
                  <Target className="h-3.5 w-3.5 text-primary" /> Error budgets
                </div>
                <DeepDiveLink to="/apm/slos">SLOs</DeepDiveLink>
              </div>
              <div className="space-y-2.5 px-4 py-3">
                {(budgets.data ?? []).length === 0 && (
                  <p className="py-4 text-center text-sm text-muted">
                    No SLO defined yet — nothing pages on error-budget burn.
                  </p>
                )}
                {(budgets.data ?? []).map((b) => {
                  const remaining = b.budget_remaining
                  const pct = remaining != null ? Math.max(0, Math.min(100, remaining * 100)) : null
                  const color = pct == null ? HEALTH_COLOR.no_data : pct < 10 ? HEALTH_COLOR.critical : pct < 25 ? HEALTH_COLOR.degraded : HEALTH_COLOR.healthy
                  return (
                    <div key={b.slo.id}>
                      <div className="flex items-baseline justify-between gap-2 text-xs">
                        <span className="truncate font-medium text-text">{b.slo.name}</span>
                        <span className="shrink-0 font-mono text-[11px] tabular-nums" style={{ color }}>
                          {pct != null ? `${pct.toFixed(0)}% left` : 'no traffic'}
                        </span>
                      </div>
                      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface2">
                        <div className="h-full rounded-full" style={{ width: `${Math.max(2, pct ?? 0)}%`, backgroundColor: color }} />
                      </div>
                      <div className="mt-0.5 text-[10px] text-muted">{b.slo.service_name} · {b.slo.sli_type} · {b.window_days}d</div>
                    </div>
                  )
                })}
              </div>
            </Card>

            <Card className="overflow-hidden">
              <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
                <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
                  <AlertTriangle className="h-3.5 w-3.5 text-warning" /> Pipeline
                </div>
                <DeepDiveLink to="/apm/settings">Details</DeepDiveLink>
              </div>
              <div className="space-y-2 px-4 py-3 text-xs">
                <div className="flex justify-between">
                  <span className="text-muted">Spans accepted (24h)</span>
                  <span className="font-medium tabular-nums text-text">{fmtCount(quality.data?.ingest.accepted)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted">Rejected / skewed</span>
                  <span className={`font-medium tabular-nums ${(quality.data?.ingest.rejected ?? 0) ? 'text-warning' : 'text-text'}`}>
                    {fmtCount(quality.data?.ingest.rejected)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted">Scenarios down</span>
                  <span className={`font-medium ${monitorsDown ? 'text-danger' : 'text-text'}`}>{monitorsDown}</span>
                </div>
                {ingestIssues.length === 0 ? (
                  <p className="flex items-center gap-1.5 pt-1 text-[11px] text-success">
                    <CheckCircle2 className="h-3 w-3" /> Telemetry pipeline healthy
                  </p>
                ) : ingestIssues.slice(0, 3).map((i) => (
                  <p key={i} className="pt-0.5 text-[11px] text-warning">• {i}</p>
                ))}
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  )
}
