import { Fragment, useMemo, useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertCircle, Download, GitBranch, Loader2, RefreshCw, Search } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { APM_RANGES, RANGE_MS, type ApmRangeKey } from '@/components/apm/ApmRange'
import { ApmKpi, LatencyCell, fmtCount } from '@/components/apm/viz'
import { fmtMs } from '@/components/apm/shared'
import {
  ApmExplorerFrame,
  ApmFacetSidebar,
  ApmUnderlineNav,
  DurationTimeline,
  EXPLORER_HEAD,
  ExpandToggle,
  RequestFlow,
  VolumeHistogram,
  bucketByTime,
  downloadCsv,
  hopsFromTraceSpans,
  hopsFromTraceSummary,
  type TraceSpanLike,
} from '@/components/apm/explorer'
import type { TraceSummary } from '@/types/apm'

const RANGES: Record<string, number> = RANGE_MS
const RANGE_KEYS: readonly ApmRangeKey[] = APM_RANGES

type TraceDetail = {
  trace_id: string
  duration_ms: number
  spans: TraceSpanLike[]
}

export function TraceExplorerPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const mode = params.get('mode') || 'live'
  const service = params.get('service') || ''
  const operation = params.get('operation') || ''
  const errorsOnly = params.get('errors_only') === 'true'
  const minDur = params.get('min_duration_ms') || ''
  const range = params.get('range') || '1h'
  const env = params.get('env') || ''
  const version = params.get('service_version') || ''
  const httpStatus = params.get('http_status_code') || ''
  const statusCode = params.get('status_code') || ''
  const search = params.get('q') || ''

  const set = (k: string, v: string | null) => {
    const next = new URLSearchParams(params)
    if (v === null || v === '') next.delete(k); else next.set(k, v)
    setParams(next, { replace: true })
  }

  const query = useQuery<{ traces: TraceSummary[]; count: number }>({
    queryKey: ['apm', 'traces', { mode, service, operation, errorsOnly, minDur, range, env, version, httpStatus, statusCode }],
    queryFn: async () => {
      const qp = new URLSearchParams({ mode })
      if (service) qp.set('service', service)
      if (operation) qp.set('operation', operation)
      if (errorsOnly) qp.set('errors_only', 'true')
      if (minDur) qp.set('min_duration_ms', minDur)
      if (env) qp.set('env', env)
      if (version) qp.set('service_version', version)
      if (httpStatus) qp.set('http_status_code', httpStatus)
      if (statusCode) qp.set('status_code', statusCode)
      if (mode === 'indexed') {
        const now = Date.now()
        qp.set('from_ms', String(now - (RANGES[range] || RANGES['1h'])))
        qp.set('to_ms', String(now))
      }
      return (await api.get(`/apm/traces?${qp.toString()}`)).data
    },
    refetchInterval: mode === 'live' ? 5000 : false,
  })
  const facets = useQuery<{
    services: Array<{ value: string; count: number }>
    operations: Array<{ value: string; count: number }>
    envs: Array<{ value: string; count: number }>
    versions: Array<{ value: string; count: number }>
    http_status: Array<{ value: string; count: number }>
    status_code: Array<{ value: string; count: number }>
  }>({
    queryKey: ['apm', 'trace-facets', { range, mode, service }],
    queryFn: async () => {
      const qp = new URLSearchParams({ range: mode === 'live' ? '15m' : range })
      if (service) qp.set('service', service)
      return (await api.get(`/apm/trace-facets?${qp}`)).data
    },
  })
  const expanded = useQuery<TraceDetail>({
    queryKey: ['apm', 'trace', expandedId],
    queryFn: async () => (await api.get(`/apm/traces/${expandedId}`)).data,
    enabled: !!expandedId,
  })

  const traces = useMemo(() => {
    const rows = query.data?.traces ?? []
    const needle = search.trim().toLowerCase()
    if (!needle) return rows
    return rows.filter((trace) =>
      trace.trace_id.toLowerCase().includes(needle)
      || (trace.root_service || '').toLowerCase().includes(needle)
      || (trace.root_operation || '').toLowerCase().includes(needle)
      || (trace.services ?? []).some((name) => (name || '').toLowerCase().includes(needle)))
  }, [query.data?.traces, search])
  const errorTraces = traces.filter((t) => t.has_error).length
  const maxDuration = Math.max(...traces.map((t) => t.duration_ms), 1)
  const p95Dur = traces.length
    ? [...traces].sort((a, b) => a.duration_ms - b.duration_ms)[Math.min(traces.length - 1, Math.floor(traces.length * 0.95))]?.duration_ms ?? 0
    : 0

  const toggleFacet = (key: string, value: string, current: string) => set(key, current === value ? null : value)

  const exportRows = () => downloadCsv(
    'apm-traces.csv',
    ['trace_id', 'service', 'operation', 'duration_ms', 'spans', 'errors', 'started'],
    traces.map((trace) => [trace.trace_id, trace.root_service, trace.root_operation, Math.round(trace.duration_ms), trace.span_count, trace.error_count, trace.start_time]),
  )

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="Trace explorer"
        description="Search distributed traces and expand any row to see the request path — then open the waterfall for span-level timing."
        article="traces"
        actions={query.isFetching ? <Loader2 className="h-4 w-4 animate-spin text-muted" /> : undefined}
      />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <ApmKpi label="Traces" icon={<GitBranch className="h-4 w-4" />} tone="info" value={fmtCount(traces.length)} sub={mode === 'live' ? 'live 15m window' : `indexed · ${range}`} />
        <ApmKpi label="With errors" tone={errorTraces ? 'danger' : 'success'} value={fmtCount(errorTraces)} sub="failed root or child span" />
        <ApmKpi label="Slowest" tone={maxDuration >= 2000 ? 'danger' : 'warning'} value={<LatencyCell ms={maxDuration} />} sub="in this result set" />
        <ApmKpi label="p95 duration" tone="accent" value={<LatencyCell ms={p95Dur} />} sub="of listed traces" />
      </div>

      {query.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load traces — {apiErrorMessage(query.error)}
        </div>
      )}

      <ApmUnderlineNav
        items={[
          { key: 'live', label: 'Live (15m)', current: mode === 'live', onSelect: () => set('mode', 'live') },
          { key: 'indexed', label: 'Indexed', current: mode === 'indexed', onSelect: () => set('mode', 'indexed') },
        ]}
      />

      <ApmExplorerFrame
        search={
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
            <div className="relative min-w-[12rem] flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <Input className="pl-8" placeholder="Search service, operation, or trace ID" value={search} onChange={(e) => set('q', e.target.value)} />
            </div>
            <Input className="w-40" placeholder="operation" value={operation} onChange={(e) => set('operation', e.target.value)} />
            <Input className="w-28" type="number" placeholder="min ms" value={minDur} onChange={(e) => set('min_duration_ms', e.target.value)} />
            <label className="flex items-center gap-2 text-xs text-muted">
              <input type="checkbox" checked={errorsOnly} onChange={(e) => set('errors_only', e.target.checked ? 'true' : null)} />
              Significant only
            </label>
          </div>
        }
        actions={
          <>
            {mode === 'indexed' && (
              <select
                value={range}
                onChange={(e) => set('range', e.target.value)}
                className="h-9 rounded-md border border-border bg-surface2 px-2 text-sm text-text"
              >
                {RANGE_KEYS.map((r) => <option key={r} value={r}>Last {r}</option>)}
              </select>
            )}
            <Button variant="outline" size="sm" onClick={() => query.refetch()} disabled={query.isFetching}>
              <RefreshCw className={`h-3.5 w-3.5 ${query.isFetching ? 'animate-spin' : ''}`} /> Refresh
            </Button>
            <Button variant="outline" size="sm" onClick={exportRows} disabled={!traces.length}>
              <Download className="h-3.5 w-3.5" /> Export
            </Button>
          </>
        }
        summary={<>Displaying {fmtCount(traces.length)} traces{search ? ` matching “${search}”` : ''}{mode === 'live' ? ' · past 15 minutes' : ` · past ${range}`}</>}
        histogram={<VolumeHistogram buckets={bucketByTime(traces, (trace) => trace.start_time, (trace) => trace.has_error)} okLabel="Healthy" errLabel="Significant" />}
        sidebar={
          <ApmFacetSidebar
            title="Trace analytics"
            groups={[
              { title: 'Service', items: (facets.data?.services ?? []).map((item) => ({ ...item, active: service === item.value, onSelect: () => toggleFacet('service', item.value, service) })) },
              { title: 'Operation', items: (facets.data?.operations ?? []).map((item) => ({ ...item, active: operation === item.value, onSelect: () => toggleFacet('operation', item.value, operation) })) },
              { title: 'Environment', items: (facets.data?.envs ?? []).map((item) => ({ ...item, active: env === item.value, onSelect: () => toggleFacet('env', item.value, env) })) },
              { title: 'Version', items: (facets.data?.versions ?? []).map((item) => ({ ...item, active: version === item.value, onSelect: () => toggleFacet('service_version', item.value, version) })) },
              { title: 'HTTP status', items: (facets.data?.http_status ?? []).filter((item) => item.value !== '0').map((item) => ({ ...item, active: httpStatus === item.value, onSelect: () => toggleFacet('http_status_code', item.value, httpStatus) })) },
              { title: 'Span status', items: (facets.data?.status_code ?? []).map((item) => ({ ...item, active: statusCode === item.value, onSelect: () => toggleFacet('status_code', item.value, statusCode) })) },
            ]}
          />
        }
      >
        {query.isLoading ? (
          <div className="flex items-center justify-center gap-2 py-12 text-muted">
            <Loader2 className="w-4 h-4 animate-spin" /> Loading…
          </div>
        ) : traces.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-12 text-muted">
            <Search className="w-6 h-6" /> No traces match. Send traces via the OTLP ingest, or widen the window.
          </div>
        ) : (
          <Table>
            <THead className={EXPLORER_HEAD}>
              <Tr>
                <Th className="w-10" />
                <Th>Timestamp</Th>
                <Th>Root</Th>
                <Th className="text-right">Spans</Th>
                <Th className="text-right">Errors</Th>
                <Th>Services</Th>
                <Th className="text-right">Duration</Th>
              </Tr>
            </THead>
            <TBody>
              {traces.map((t) => {
                const open = expandedId === t.trace_id
                return (
                  <Fragment key={t.trace_id}>
                    <Tr
                      className="cursor-pointer"
                      tabIndex={0}
                      aria-label={`Expand trace ${t.root_service} ${t.root_operation}`}
                      data-state={open ? 'selected' : undefined}
                      onClick={() => setExpandedId(open ? null : t.trace_id)}
                    >
                      <Td><ExpandToggle open={open} onClick={() => setExpandedId(open ? null : t.trace_id)} /></Td>
                      <Td className="whitespace-nowrap font-mono text-[11px] text-muted">{relativeTime(t.start_time)}</Td>
                      <Td>
                        <div className="flex items-center gap-2">
                          {t.has_error && <AlertCircle className="h-4 w-4 shrink-0 text-danger" />}
                          <span className="font-medium text-text">{t.root_service}</span>
                          <span className="truncate text-muted">{t.root_operation}</span>
                        </div>
                      </Td>
                      <Td className="text-right font-mono text-xs tabular-nums">{t.span_count}</Td>
                      <Td className="text-right">
                        {t.error_count > 0 ? <Badge variant="danger">{t.error_count}</Badge> : <span className="text-muted">0</span>}
                      </Td>
                      <Td>
                        <div className="flex flex-wrap gap-1">
                          {t.services.slice(0, 4).map((s) => (
                            <button
                              key={s}
                              className="inline-flex"
                              onClick={(e) => { e.stopPropagation(); navigate(`/apm/services/${encodeURIComponent(s)}?range=${range}`) }}
                            >
                              <Badge variant="outline">{s}</Badge>
                            </button>
                          ))}
                          {t.services.length > 4 && <span className="text-xs text-muted">+{t.services.length - 4}</span>}
                        </div>
                      </Td>
                      <Td className="text-right">
                        <DurationTimeline ms={t.duration_ms} maxMs={maxDuration} significant={t.has_error || t.duration_ms >= 1000} />
                      </Td>
                    </Tr>
                    {open && (
                      <Tr className="hover:bg-transparent">
                        <Td colSpan={7} className="bg-surface2/40 p-4">
                          {expanded.isLoading ? (
                            <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading request path…</div>
                          ) : (
                            <div className="space-y-3">
                              {t.has_error && (
                                <div className="text-[11px] font-medium text-danger">Significant: this trace contains at least one failed span.</div>
                              )}
                              <RequestFlow
                                hops={expanded.data?.spans?.length ? hopsFromTraceSpans(expanded.data.spans) : hopsFromTraceSummary(t)}
                                totalLabel={fmtMs(expanded.data?.duration_ms ?? t.duration_ms)}
                              />
                              <div className="flex flex-wrap items-center gap-2">
                                <Button size="sm" onClick={(event) => { event.stopPropagation(); navigate(`/apm/traces/${t.trace_id}`) }}>
                                  Open waterfall
                                </Button>
                                <span className="font-mono text-[10px] text-muted">{t.trace_id}</span>
                              </div>
                            </div>
                          )}
                        </Td>
                      </Tr>
                    )}
                  </Fragment>
                )
              })}
            </TBody>
          </Table>
        )}
      </ApmExplorerFrame>
    </div>
  )
}
