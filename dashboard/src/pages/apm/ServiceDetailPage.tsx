import { useMemo, useState } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, ArrowLeft, Database, Flame, GitBranch, ShieldCheck, Activity, Bug, Gauge, Network, Bell, Server } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { HealthBadge, fmtMs, fmtRps, fmtPct } from '@/components/apm/shared'
import { ErrorStatusBadge } from '@/components/apm/errorShared'
import { ApmRangePicker, CompareToggle, previousWindow, shiftTimestamp, type ApmRangeKey } from '@/components/apm/ApmRange'
import { KbLink } from '@/components/apm/KbLink'
import { CreateMonitorDialog } from '@/components/apm/CreateMonitorDialog'
import { openExemplarTrace } from '@/components/apm/exemplar'
import {
  APM_SERIES, ApmKpi, ApmLatencyHeatmap, ApmTimeChart, ChartPanel, CompareDelta, DeepLinks,
  ErrorRateCell, LatencyCell, RankBar, apdexTone, errorTone, fmtCount, latencyTone,
} from '@/components/apm/viz'
import type { OperationRED as Op, REDPoint, ServiceRED } from '@/types/apm'

const TABS = [
  { key: 'overview', label: 'Overview' },
  { key: 'performance', label: 'Performance' },
  { key: 'errors', label: 'Errors' },
  { key: 'database', label: 'Database' },
  { key: 'profiling', label: 'Profiling' },
] as const

const LATENCY_EDGES = [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000]

function bucketMidMs(index: number): number {
  if (index <= 0) return LATENCY_EDGES[0] / 2
  if (index >= LATENCY_EDGES.length) return LATENCY_EDGES[LATENCY_EDGES.length - 1] * 1.5
  return (LATENCY_EDGES[index - 1] + LATENCY_EDGES[index]) / 2
}

interface DatabaseQuery {
  query_digest: string; statement: string; db_system: string; operation: string; calls: number
  error_rate: number; p95_ms: number; total_ms: number; trace_id: string; last_seen: string
}
interface Profile {
  profile_id: string; timestamp: string; profile_type: string; service_version: string; sample_count: number
  encoding: string; trace_id: string; span_id: string; samples: { stack: string; value: number }[]
}

export function ServiceDetailPage() {
  const { name = '' } = useParams()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const [monitorOpen, setMonitorOpen] = useState(false)
  const tab = (params.get('tab') as typeof TABS[number]['key']) || 'overview'
  const range = (params.get('range') || '1h') as ApmRangeKey
  const compare = params.get('compare') === '1'
  const setParam = (k: string, v: string) => { const n = new URLSearchParams(params); n.set(k, v); setParams(n, { replace: true }) }
  const setCompare = (v: boolean) => {
    const n = new URLSearchParams(params)
    if (v) n.set('compare', '1'); else n.delete('compare')
    setParams(n, { replace: true })
  }

  const summary = useQuery<ServiceRED>({ queryKey: ['apm', 'service', name, { range }], queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}?range=${range}`)).data, refetchInterval: 15000 })
  const red = useQuery<REDPoint[]>({ queryKey: ['apm', 'service-red', name, { range }], queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}/red?range=${range}`)).data, refetchInterval: 15_000 })
  const ops = useQuery<Op[]>({ queryKey: ['apm', 'service-ops', name, { range }], queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}/operations?range=${range}`)).data, refetchInterval: 15_000 })
  const errs = useQuery<{ issues: any[] }>({ queryKey: ['apm', 'errors', { service: name }], queryFn: async () => (await api.get(`/apm/errors?range_=24h&service=${encodeURIComponent(name)}`)).data, enabled: tab === 'errors' })
  const database = useQuery<{ queries: DatabaseQuery[] }>({ queryKey: ['apm', 'database', name, range], queryFn: async () => (await api.get(`/apm/database?service=${encodeURIComponent(name)}&range=${range}`)).data, enabled: tab === 'database' })
  const profileTrace = params.get('trace') || ''
  const profiles = useQuery<{ profiles: Profile[] }>({ queryKey: ['apm', 'profiles', name, range, profileTrace], queryFn: async () => (await api.get(`/apm/profiles?service=${encodeURIComponent(name)}&range=${range}${profileTrace ? `&trace_id=${profileTrace}` : ''}`)).data, enabled: tab === 'profiling' })
  const prevWin = previousWindow(range)
  const prevSummary = useQuery<ServiceRED>({
    queryKey: ['apm', 'service-prev', name, prevWin],
    enabled: compare,
    queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}?from_ms=${prevWin.from_ms}&to_ms=${prevWin.to_ms}`)).data,
  })
  const prevRed = useQuery<REDPoint[]>({
    queryKey: ['apm', 'service-red-prev', name, prevWin],
    enabled: compare,
    queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}/red?from_ms=${prevWin.from_ms}&to_ms=${prevWin.to_ms}`)).data,
  })
  const heatmap = useQuery<{ buckets: string[]; points: Array<{ timestamp: string; counts: number[] }>; histogram: Array<{ bucket: string; count: number }> }>({
    queryKey: ['apm', 'heatmap', name, { range }],
    queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}/heatmap?range=${range}`)).data,
    enabled: tab === 'overview' || tab === 'performance',
    refetchInterval: 30_000,
  })
  const deploys = useQuery<{ items: Array<{ version: string; first_seen: string; source: string; traces: number }> }>({
    queryKey: ['apm', 'deploys', name, { range }],
    queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}/deployments?range=${range}`)).data,
    refetchInterval: 60_000,
  })
  const hosts = useQuery<{ processes: Array<{ id: string; server_name: string; hostname: string; pid: number; runtime: string; instrumentation_state: string; last_seen_at: string; traces_15m: number }> }>({
    queryKey: ['apm', 'hosts', name],
    queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}/hosts`)).data,
    enabled: tab === 'overview',
  })

  const s = summary.data
  const prev = prevSummary.data
  const points = useMemo(() => {
    const curr = red.data ?? []
    if (!compare || !prevRed.data?.length) return curr
    const shifted = new Map(prevRed.data.map((p) => [shiftTimestamp(p.timestamp, prevWin.windowMs), p]))
    const keys = new Set([...curr.map((p) => p.timestamp), ...shifted.keys()])
    return [...keys].sort().map((timestamp) => {
      const c = curr.find((p) => p.timestamp === timestamp)
      const p = shifted.get(timestamp)
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
  }, [red.data, prevRed.data, compare, prevWin.windowMs])
  const markers = useMemo(
    () => (deploys.data?.items ?? [])
      .filter((d) => d.version && d.version !== '(unset)')
      .map((d) => ({ timestamp: d.first_seen, label: d.version })),
    [deploys.data],
  )
  const maxOpReqs = Math.max(...(ops.data ?? []).map((o) => o.request_count), 1)
  const tracesQ = `mode=indexed&service=${encodeURIComponent(name)}&range=${range}`
  const goExemplar = (ts: string, metric: 'p95' | 'error' | 'rps' = 'p95', targetMs?: number) => {
    void openExemplarTrace(navigate, name, range, ts, metric, targetMs)
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate(`/apm/services?range=${range}`)}>
          <ArrowLeft className="mr-1 h-4 w-4" /> Services
        </Button>
        <div className="min-w-0">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-muted">Service deep dive</div>
          <h1 className="flex flex-wrap items-center gap-2 text-xl font-semibold text-text">
            {name}
            {s && <HealthBadge health={s.health} />}
          </h1>
        </div>
        <div className="flex-1" />
        <button
          onClick={() => navigate(`/apm/traces?${tracesQ}`)}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-muted hover:text-text"
        >
          <GitBranch className="h-3.5 w-3.5" /> Traces
        </button>
        <button
          onClick={() => navigate(`/apm/errors?service=${encodeURIComponent(name)}`)}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-muted hover:text-text"
        >
          <Bug className="h-3.5 w-3.5" /> Errors
        </button>
        <button
          onClick={() => navigate(`/apm/service-map?range=${range}`)}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-muted hover:text-text"
        >
          <Network className="h-3.5 w-3.5" /> Map
        </button>
        <button
          onClick={() => setMonitorOpen(true)}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-muted hover:text-text"
        >
          <Bell className="h-3.5 w-3.5" /> Monitor
        </button>
        <CompareToggle value={compare} onChange={setCompare} />
        <ApmRangePicker value={range} onChange={(r) => setParam('range', r)} />
        <KbLink article="services" />
      </div>

      {summary.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load service — {apiErrorMessage(summary.error)}
        </div>
      )}

      {summary.isLoading ? (
        <div className="flex items-center justify-center gap-2 py-12 text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <ApmKpi label="Throughput" icon={<Activity className="h-4 w-4" />} tone="info" value={fmtRps(s?.rps ?? 0)} sub={<CompareDelta current={s?.rps ?? 0} previous={compare ? prev?.rps : undefined} />} foot={<span className="text-[11px] text-muted">{fmtCount(s?.request_count)} requests</span>} />
            <ApmKpi label="Error rate" icon={<Bug className="h-4 w-4" />} tone={errorTone(s?.error_rate ?? 0)} value={fmtPct(s?.error_rate ?? 0)} sub={<CompareDelta current={s?.error_rate ?? 0} previous={compare ? prev?.error_rate : undefined} invert />} />
            <ApmKpi label="p50" icon={<Gauge className="h-4 w-4" />} tone={latencyTone(s?.p50_ms ?? 0)} value={fmtMs(s?.p50_ms ?? 0)} sub={<CompareDelta current={s?.p50_ms ?? 0} previous={compare ? prev?.p50_ms : undefined} invert />} />
            <ApmKpi label="p95" icon={<Gauge className="h-4 w-4" />} tone={latencyTone(s?.p95_ms ?? 0)} value={fmtMs(s?.p95_ms ?? 0)} sub={<CompareDelta current={s?.p95_ms ?? 0} previous={compare ? prev?.p95_ms : undefined} invert />} />
            <ApmKpi label="p99" tone={latencyTone(s?.p99_ms ?? 0)} value={fmtMs(s?.p99_ms ?? 0)} sub={<CompareDelta current={s?.p99_ms ?? 0} previous={compare ? prev?.p99_ms : undefined} invert />} />
            <ApmKpi label="Apdex" tone={apdexTone(s?.apdex ?? 0)} value={(s?.apdex ?? 0).toFixed(2)} sub={<CompareDelta current={s?.apdex ?? 0} previous={compare ? prev?.apdex : undefined} />} />
          </div>

          <nav className="flex items-center gap-1 overflow-x-auto rounded-lg border border-border bg-surface2/50 p-1">
            {TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => setParam('tab', t.key)}
                className={cn(
                  'rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors',
                  tab === t.key ? 'bg-surface text-text shadow-sm ring-1 ring-border' : 'text-muted hover:text-text',
                )}
              >
                {t.label}
              </button>
            ))}
          </nav>

          {tab === 'overview' && (
            <div className="space-y-4">
              <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
                <ChartPanel title="Latency" hint="Click a point to open an exemplar trace. Amber lines are version changes." right={<DeepLinks service={name} range={range} />}>
                  <ApmTimeChart
                    data={points}
                    series={[
                      { key: 'p50_ms', name: 'p50', color: APM_SERIES.latencyP50, fmt: fmtMs },
                      { key: 'p95_ms', name: 'p95', color: APM_SERIES.latency, fmt: fmtMs },
                      ...(compare ? [
                        { key: 'p50_prev', name: 'p50 prev', color: APM_SERIES.latencyP50, fmt: fmtMs, dashed: true, area: false },
                        { key: 'p95_prev', name: 'p95 prev', color: APM_SERIES.latency, fmt: fmtMs, dashed: true, area: false },
                      ] : []),
                    ]}
                    height={268}
                    markers={markers}
                    onPointClick={(ts) => goExemplar(ts, 'p95')}
                  />
                </ChartPanel>
                <div className="grid gap-3">
                  <ChartPanel title="Throughput" hint="requests / second">
                    <ApmTimeChart
                      data={points}
                      series={[
                        { key: 'rps', name: 'req/s', color: APM_SERIES.throughput, fmt: (v) => v.toFixed(2) },
                        ...(compare ? [{ key: 'rps_prev', name: 'prev', color: APM_SERIES.throughput, fmt: (v: number) => v.toFixed(2), dashed: true, area: false }] : []),
                      ]}
                      height={120}
                      markers={markers}
                      onPointClick={(ts) => goExemplar(ts, 'rps')}
                    />
                  </ChartPanel>
                  <ChartPanel title="Error rate">
                    <ApmTimeChart
                      data={points}
                      series={[
                        { key: 'error_rate', name: 'errors', color: APM_SERIES.errors, fmt: fmtPct },
                        ...(compare ? [{ key: 'error_rate_prev', name: 'prev', color: APM_SERIES.errors, fmt: fmtPct, dashed: true, area: false }] : []),
                      ]}
                      height={120}
                      onPointClick={(ts) => goExemplar(ts, 'error')}
                    />
                  </ChartPanel>
                </div>
              </div>
              <ChartPanel title="Latency heatmap" hint="time × duration bucket — click a cell for a matching trace">
                <ApmLatencyHeatmap
                  buckets={heatmap.data?.buckets ?? []}
                  points={heatmap.data?.points ?? []}
                  loading={heatmap.isLoading}
                  height={260}
                  onCellClick={(ts, bucket) => goExemplar(ts, 'p95', bucketMidMs(bucket))}
                />
              </ChartPanel>
              <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
                <Card className="overflow-hidden">
                  <CardHeader className="border-b border-border py-3">
                    <CardTitle className="text-[11px] font-semibold uppercase tracking-wider text-muted">Top operations</CardTitle>
                  </CardHeader>
                  <CardContent className="p-0">
                    <Table>
                      <THead>
                        <Tr>
                          <Th>Operation</Th>
                          <Th className="text-right">Requests</Th>
                          <Th className="text-right">Error rate</Th>
                          <Th className="text-right">p95</Th>
                          <Th className="text-right">Deep dive</Th>
                        </Tr>
                      </THead>
                      <TBody>
                        {(ops.data ?? []).map((o) => (
                          <Tr
                            key={o.operation}
                            className="cursor-pointer"
                            onClick={() => navigate(`/apm/traces?mode=indexed&service=${encodeURIComponent(name)}&operation=${encodeURIComponent(o.operation)}&range=${range}`)}
                          >
                            <Td className="font-mono text-xs text-text">{o.operation}</Td>
                            <Td className="text-right">
                              <div className="font-mono text-xs tabular-nums">{fmtCount(o.request_count)}</div>
                              <RankBar value={o.request_count} max={maxOpReqs} />
                            </Td>
                            <Td className="text-right"><ErrorRateCell rate={o.error_rate} /></Td>
                            <Td className="text-right"><LatencyCell ms={o.p95_ms} /></Td>
                            <Td><DeepLinks service={name} range={range} operation={o.operation} /></Td>
                          </Tr>
                        ))}
                        {(ops.data ?? []).length === 0 && <Tr><Td colSpan={5} className="py-6 text-center text-muted">No operations in range.</Td></Tr>}
                      </TBody>
                    </Table>
                  </CardContent>
                </Card>
                <Card className="overflow-hidden">
                  <CardHeader className="border-b border-border py-3">
                    <CardTitle className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
                      <Server className="h-3.5 w-3.5" /> Hosts & processes
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="p-0">
                    <Table>
                      <THead><Tr><Th>Host</Th><Th>Runtime</Th><Th>Instrumentation</Th><Th className="text-right">Last seen</Th></Tr></THead>
                      <TBody>
                        {(hosts.data?.processes ?? []).map((p) => (
                          <Tr key={p.id}>
                            <Td>
                              <div className="text-sm text-text">{p.server_name || p.hostname}</div>
                              <div className="font-mono text-[11px] text-muted">pid {p.pid}</div>
                            </Td>
                            <Td className="text-xs">{p.runtime}</Td>
                            <Td className="text-xs capitalize">{p.instrumentation_state}</Td>
                            <Td className="text-right text-xs text-muted">{p.last_seen_at ? relativeTime(p.last_seen_at) : '—'}</Td>
                          </Tr>
                        ))}
                        {(hosts.data?.processes ?? []).length === 0 && (
                          <Tr><Td colSpan={4} className="py-6 text-center text-muted">No agent-discovered process matches this service name yet.</Td></Tr>
                        )}
                      </TBody>
                    </Table>
                  </CardContent>
                </Card>
              </div>
            </div>
          )}

          {tab === 'performance' && (
            <div className="space-y-3">
              <ChartPanel title="Latency heatmap" hint="click a cell to open an exemplar">
                <ApmLatencyHeatmap
                  buckets={heatmap.data?.buckets ?? []}
                  points={heatmap.data?.points ?? []}
                  loading={heatmap.isLoading}
                  height={280}
                  onCellClick={(ts, bucket) => goExemplar(ts, 'p95', bucketMidMs(bucket))}
                />
              </ChartPanel>
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                <ChartPanel title="Latency p50">
                  <ApmTimeChart data={points} series={[{ key: 'p50_ms', name: 'p50', color: APM_SERIES.latencyP50, fmt: fmtMs }]} height={220} markers={markers} onPointClick={(ts) => goExemplar(ts, 'p50')} />
                </ChartPanel>
                <ChartPanel title="Latency p95">
                  <ApmTimeChart data={points} series={[{ key: 'p95_ms', name: 'p95', color: APM_SERIES.latency, fmt: fmtMs }]} height={220} markers={markers} onPointClick={(ts) => goExemplar(ts, 'p95')} />
                </ChartPanel>
                <ChartPanel title="Throughput">
                  <ApmTimeChart data={points} series={[{ key: 'rps', name: 'req/s', color: APM_SERIES.throughput, fmt: (v) => v.toFixed(2) }]} height={220} onPointClick={(ts) => goExemplar(ts, 'rps')} />
                </ChartPanel>
                <ChartPanel title="Error rate">
                  <ApmTimeChart data={points} series={[{ key: 'error_rate', name: 'errors', color: APM_SERIES.errors, fmt: fmtPct }]} height={220} onPointClick={(ts) => goExemplar(ts, 'error')} />
                </ChartPanel>
              </div>
            </div>
          )}

          {tab === 'errors' && (
            <Card className="overflow-hidden">
              <CardContent className="p-0">
                <Table>
                  <THead><Tr><Th>Error</Th><Th className="text-right">Events</Th><Th className="text-right">Traces</Th><Th>Status</Th></Tr></THead>
                  <TBody>
                    {(errs.data?.issues ?? []).map((e) => (
                      <Tr key={e.group_id} className="cursor-pointer" onClick={() => navigate(`/apm/errors/${e.group_id}`)}>
                        <Td>
                          <div className="font-medium text-text">{e.exception_type}</div>
                          <div className="max-w-md truncate text-xs text-muted">{e.message}</div>
                        </Td>
                        <Td className="text-right font-mono text-xs">{fmtCount(e.occurrences)}</Td>
                        <Td className="text-right font-mono text-xs">{fmtCount(e.traces)}</Td>
                        <Td><ErrorStatusBadge status={e.status} /></Td>
                      </Tr>
                    ))}
                    {(errs.data?.issues ?? []).length === 0 && <Tr><Td colSpan={4} className="py-6 text-center text-muted">No errors for this service.</Td></Tr>}
                  </TBody>
                </Table>
              </CardContent>
            </Card>
          )}

          {tab === 'database' && <DatabaseInsights queries={database.data?.queries ?? []} loading={database.isLoading} onTrace={(id) => navigate(`/apm/traces/${id}`)} />}
          {tab === 'profiling' && <Profiling profiles={profiles.data?.profiles ?? []} loading={profiles.isLoading} onTrace={(id) => navigate(`/apm/traces/${id}`)} />}
        </>
      )}
      <CreateMonitorDialog open={monitorOpen} onOpenChange={setMonitorOpen} service={name} suggestedThreshold={s?.p95_ms} />
    </div>
  )
}


function DatabaseInsights({ queries, loading, onTrace }: { queries: DatabaseQuery[]; loading: boolean; onTrace: (id: string) => void }) {
  const maxCalls = Math.max(...queries.map((q) => q.calls), 1)
  if (loading) return <div className="flex justify-center py-12 text-muted"><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading database telemetry…</div>
  return (
    <Card className="overflow-hidden">
      <CardHeader className="border-b border-border py-3">
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2"><Database className="h-4 w-4 text-primary" /> Query performance</span>
          <span className="flex items-center gap-1 text-xs font-normal text-success"><ShieldCheck className="h-3.5 w-3.5" /> Literals obfuscated before storage</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <THead>
            <Tr>
              <Th>Normalized query</Th><Th>Database</Th>
              <Th className="text-right">Calls</Th><Th className="text-right">Errors</Th>
              <Th className="text-right">p95</Th><Th className="text-right">Total time</Th><Th>Trace</Th>
            </Tr>
          </THead>
          <TBody>
            {queries.map((q) => (
              <Tr key={q.query_digest}>
                <Td className="max-w-xl">
                  <div className="truncate font-mono text-xs text-text" title={q.statement}>{q.statement}</div>
                  <div className="mt-1 font-mono text-[10px] text-muted">{q.query_digest.slice(0, 16)}</div>
                </Td>
                <Td>
                  <span className="text-xs uppercase text-muted">{q.db_system || 'database'}</span>
                  <div className="text-xs text-text">{q.operation || 'query'}</div>
                </Td>
                <Td className="text-right">
                  <div className="font-mono text-xs">{q.calls.toLocaleString()}</div>
                  <RankBar value={q.calls} max={maxCalls} />
                </Td>
                <Td className="text-right"><ErrorRateCell rate={q.error_rate} /></Td>
                <Td className="text-right"><LatencyCell ms={q.p95_ms} /></Td>
                <Td className="text-right font-mono text-xs">{fmtMs(q.total_ms)}</Td>
                <Td>
                  <button className="font-mono text-xs text-primary hover:underline" onClick={() => onTrace(q.trace_id)}>
                    {q.trace_id.slice(0, 8)}…
                  </button>
                </Td>
              </Tr>
            ))}
            {!queries.length && (
              <Tr>
                <Td colSpan={7} className="py-10 text-center">
                  <Database className="mx-auto mb-2 h-7 w-7 text-muted" />
                  <div className="font-medium text-text">No database spans in this range</div>
                  <div className="mt-1 text-xs text-muted">Database calls appear when instrumentation emits db.* semantic attributes.</div>
                </Td>
              </Tr>
            )}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  )
}

function Profiling({ profiles, loading, onTrace }: { profiles: Profile[]; loading: boolean; onTrace: (id: string) => void }) {
  const selected = profiles[0]
  const total = selected?.samples.reduce((sum, sample) => sum + Number(sample.value || 0), 0) || 0
  const frames = [...(selected?.samples ?? [])].sort((a, b) => b.value - a.value).slice(0, 40)
  if (loading) return <div className="flex justify-center py-12 text-muted"><Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading profiles…</div>
  if (!selected) {
    return (
      <Card>
        <CardContent className="py-12 text-center">
          <Flame className="mx-auto mb-2 h-8 w-8 text-muted" />
          <div className="font-medium text-text">Waiting for continuous profiles</div>
          <p className="mx-auto mt-1 max-w-xl text-xs text-muted">
            This tab only becomes active after a compatible profiler sends CPU, allocation, lock, or wall samples.
          </p>
        </CardContent>
      </Card>
    )
  }
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_300px]">
      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-sm"><Flame className="h-4 w-4 text-orange-400" /> {selected.profile_type.toUpperCase()} hot paths</CardTitle></CardHeader>
        <CardContent className="space-y-1">
          {frames.map((sample, i) => {
            const pct = total ? sample.value / total * 100 : 0
            return (
              <div key={`${sample.stack}-${i}`} className="relative overflow-hidden rounded border border-border bg-surface2/30 px-3 py-2">
                <div className="absolute inset-y-0 left-0 bg-orange-500/15" style={{ width: `${Math.max(pct, 0.5)}%` }} />
                <div className="relative flex items-center justify-between gap-3">
                  <span className="truncate font-mono text-xs text-text" title={sample.stack}>{sample.stack}</span>
                  <span className="shrink-0 font-mono text-xs text-muted">{pct.toFixed(1)}%</span>
                </div>
              </div>
            )
          })}
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle className="text-sm">Profile context</CardTitle></CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div><div className="text-xs text-muted">Captured</div><div className="text-text">{new Date(selected.timestamp).toLocaleString()}</div></div>
          <div><div className="text-xs text-muted">Version</div><div className="font-mono text-xs text-text">{selected.service_version || 'not reported'}</div></div>
          <div><div className="text-xs text-muted">Samples</div><div className="text-text">{selected.sample_count.toLocaleString()}</div></div>
          {selected.trace_id && (
            <Button variant="outline" size="sm" className="w-full" onClick={() => onTrace(selected.trace_id)}>
              <GitBranch className="mr-1 h-3.5 w-3.5" /> Open linked trace
            </Button>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
