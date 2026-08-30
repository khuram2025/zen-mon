import { useState } from 'react'
import { useLocation, useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, ArrowLeft, AlertCircle, Check, Copy, Database, Flame, Server, ArrowRightLeft, ScrollText } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { fmtMs } from '@/components/apm/shared'
import { hopsFromTraceSpans, RequestFlow } from '@/components/apm/explorer'
import { KbLink } from '@/components/apm/KbLink'
import { ApmKpi, LatencyCell, fmtCount } from '@/components/apm/viz'

interface SpanNode {
  span_id: string
  parent_span_id: string
  name: string
  service_name: string
  span_kind: string
  status_code: string
  status_message: string
  has_error: boolean
  depth: number
  start_offset_ms: number
  duration_ms: number
  http_method: string
  http_route: string
  http_status_code: number
  db_system: string
  db_operation: string
  db_statement: string
  rpc_method: string
  attributes: Record<string, any>
  events: { name: string; offset_ms: number; attributes: string }[]
}

interface TraceDetail {
  trace_id: string
  start_time: string
  duration_ms: number
  span_count: number
  services: string[]
  spans: SpanNode[]
}

function kindIcon(kind: string, db: string) {
  if (db) return <Database className="w-3.5 h-3.5" />
  if (kind === 'SERVER') return <Server className="w-3.5 h-3.5" />
  if (kind === 'CLIENT') return <ArrowRightLeft className="w-3.5 h-3.5" />
  return null
}

export function TraceWaterfallPage() {
  const { traceId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const stateReturn = (location.state as { returnTo?: unknown } | null)?.returnTo
  const queryReturn = new URLSearchParams(location.search).get('rum_return')
  const rumReturn = [stateReturn, queryReturn].find((value): value is string => (
    typeof value === 'string' && value.startsWith('/apm/rum') && !value.startsWith('//')
  ))
  const backTarget = rumReturn || '/apm/traces'
  const backLabel = rumReturn ? 'Back to RUM' : 'Traces'
  const [selected, setSelected] = useState<SpanNode | null>(null)
  const [copied, setCopied] = useState(false)
  const [view, setView] = useState<'waterfall' | 'logs'>('waterfall')

  const query = useQuery<TraceDetail>({
    queryKey: ['apm', 'trace', traceId],
    queryFn: async () => (await api.get(`/apm/traces/${traceId}`)).data,
    enabled: !!traceId,
  })
  const logs = useQuery<{ items: Array<{ source: string; timestamp: string; service_name: string; span_id: string; name: string; message: string; level: string }>; note: string }>({
    queryKey: ['apm', 'trace-logs', traceId],
    queryFn: async () => (await api.get(`/apm/traces/${traceId}/logs`)).data,
    enabled: !!traceId,
  })

  if (query.isLoading) {
    return <div className="flex items-center justify-center gap-2 text-muted p-12"><Loader2 className="w-4 h-4 animate-spin" /> Loading trace…</div>
  }
  if (query.isError || !query.data) {
    const status = (query.error as { response?: { status?: number } } | null)?.response?.status
    const notFound = !query.isError || status === 404
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="sm" onClick={() => navigate(backTarget)}><ArrowLeft className="w-4 h-4 mr-1" /> {backLabel}</Button>
          <h1 className="text-lg font-semibold text-text">Trace</h1>
        </div>
        <Card className="border-warning/30">
          <CardContent className="flex flex-col items-center py-12 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-warning/10 text-warning"><AlertCircle className="h-6 w-6" /></span>
            <div className="mt-3 text-sm font-semibold text-text">{notFound ? 'Backend trace not available' : 'Could not load trace'}</div>
            <p className="mt-1 max-w-lg text-xs leading-relaxed text-muted">
              {notFound
                ? 'This trace is no longer stored. Backend spans are retained for 14 days, so a real-user event older than that window — or a request that was sampled out or never reached an instrumented backend — has no viewable trace.'
                : apiErrorMessage(query.error, 'The trace service did not respond.')}
            </p>
            {traceId && (
              <button
                onClick={() => { navigator.clipboard?.writeText(traceId); setCopied(true); window.setTimeout(() => setCopied(false), 1500) }}
                title="Copy trace ID"
                className="mt-3 inline-flex items-center gap-1.5 rounded border border-border px-2 py-1 font-mono text-xs text-muted hover:text-text"
              >
                {traceId}
                {copied ? <Check className="h-3 w-3 text-success" /> : <Copy className="h-3 w-3" />}
              </button>
            )}
            <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
              <Button variant="outline" size="sm" onClick={() => navigate(backTarget)}><ArrowLeft className="w-3.5 h-3.5 mr-1" /> {backLabel}</Button>
              <Button variant="outline" size="sm" onClick={() => navigate('/apm/traces')}>Search traces</Button>
              {query.isError && <Button variant="outline" size="sm" onClick={() => query.refetch()}>Retry</Button>}
            </div>
          </CardContent>
        </Card>
      </div>
    )
  }

  const trace = query.data
  const total = Math.max(trace.duration_ms, 0.001)

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate(backTarget)}><ArrowLeft className="w-4 h-4 mr-1" /> {backLabel}</Button>
        <h1 className="text-lg font-semibold text-text">Trace</h1>
        <button
          onClick={() => { navigator.clipboard?.writeText(trace.trace_id); setCopied(true); window.setTimeout(() => setCopied(false), 1500) }}
          title="Copy trace ID"
          className="inline-flex items-center gap-1.5 rounded border border-transparent px-1.5 py-0.5 font-mono text-xs text-muted hover:border-border hover:text-text"
        >
          {trace.trace_id}
          {copied ? <Check className="h-3 w-3 text-success" /> : <Copy className="h-3 w-3" />}
        </button>
        <div className="flex-1" />
        <div className="inline-flex rounded-md border border-border p-0.5">
          <button onClick={() => setView('waterfall')} className={`rounded px-2 py-1 text-[11px] font-semibold ${view === 'waterfall' ? 'bg-primary text-black' : 'text-muted'}`}>Waterfall</button>
          <button onClick={() => setView('logs')} className={`inline-flex items-center gap-1 rounded px-2 py-1 text-[11px] font-semibold ${view === 'logs' ? 'bg-primary text-black' : 'text-muted'}`}>
            <ScrollText className="h-3 w-3" /> Logs
          </button>
        </div>
        <KbLink article="traces" />
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <ApmKpi label="Duration" tone={trace.duration_ms >= 2000 ? 'danger' : trace.duration_ms >= 800 ? 'warning' : 'success'} value={<LatencyCell ms={trace.duration_ms} />} />
        <ApmKpi label="Spans" tone="info" value={fmtCount(trace.span_count)} />
        <ApmKpi label="Services" tone="primary" value={fmtCount(trace.services.length)} />
        <ApmKpi label="Errors" tone={trace.spans.some((s) => s.has_error) ? 'danger' : 'success'} value={fmtCount(trace.spans.filter((s) => s.has_error).length)} />
      </div>

      <RequestFlow hops={hopsFromTraceSpans(trace.spans)} totalLabel={fmtMs(trace.duration_ms)} />

      <div className="flex flex-wrap items-center gap-1.5">
        {trace.services.map((s) => (
          <button key={s} onClick={() => navigate(`/apm/services/${encodeURIComponent(s)}`)}>
            <Badge variant="outline">{s}</Badge>
          </button>
        ))}
      </div>

      {view === 'logs' ? (
        <Card>
          <CardContent className="py-4">
            <p className="mb-3 text-[11px] text-muted">{logs.data?.note || 'Span events and exceptions correlated to this trace.'}</p>
            {logs.isLoading ? (
              <div className="flex items-center gap-2 py-8 text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading logs…</div>
            ) : (logs.data?.items ?? []).length === 0 ? (
              <div className="py-10 text-center text-sm text-muted">No span events or exceptions on this trace. OTLP logs are not ingested yet.</div>
            ) : (
              <div className="space-y-1 font-mono text-xs">
                {(logs.data?.items ?? []).map((line, i) => (
                  <div key={`${line.span_id}-${i}`} className="grid grid-cols-[9rem_7rem_1fr] gap-2 rounded px-2 py-1 hover:bg-surface2">
                    <span className="text-muted">{line.timestamp ? new Date(line.timestamp).toLocaleTimeString() : '—'}</span>
                    <span className={line.level === 'error' ? 'text-[#ef4444]' : 'text-muted'}>{line.source}</span>
                    <span>
                      <span className="text-text2">{line.service_name} </span>
                      <span className="text-text">{line.name}</span>
                      {line.message && <span className="text-muted"> — {line.message}</span>}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      ) : (
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-2">
          <CardContent className="p-0">
            <div className="divide-y divide-border">
              {trace.spans.map((s) => {
                const leftPct = (s.start_offset_ms / total) * 100
                const widthPct = Math.max((s.duration_ms / total) * 100, 0.6)
                const color = s.has_error ? '#ef4444' : s.db_system ? '#a78bfa' : '#3b82f6'
                const active = selected?.span_id === s.span_id
                return (
                  <div
                    key={s.span_id}
                    onClick={() => setSelected(s)}
                    className={`flex items-center gap-2 px-3 py-1.5 cursor-pointer text-xs ${active ? 'bg-surface2' : 'hover:bg-surface2/50'}`}
                  >
                    <div className="flex items-center gap-1.5 min-w-0" style={{ width: '40%', paddingLeft: `${s.depth * 14}px` }}>
                      {s.has_error ? <AlertCircle className="w-3.5 h-3.5 text-danger shrink-0" /> : kindIcon(s.span_kind, s.db_system)}
                      <span className="text-muted shrink-0">{s.service_name}</span>
                      <span className="truncate text-text">{s.name}</span>
                    </div>
                    <div className="relative flex-1 h-4">
                      <div
                        className="absolute top-0.5 h-3 rounded-sm"
                        style={{ left: `${leftPct}%`, width: `${widthPct}%`, backgroundColor: color, minWidth: '2px' }}
                        title={`${s.duration_ms.toFixed(2)} ms`}
                      />
                    </div>
                    <span className="font-mono text-muted w-16 text-right shrink-0">{s.duration_ms.toFixed(1)}ms</span>
                  </div>
                )
              })}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="py-4">
            {!selected ? (
              <div className="text-center text-muted text-sm py-8">Select a span to see details.</div>
            ) : (
              <div className="space-y-3 text-sm">
                <div className="flex items-center gap-2">
                  {selected.has_error && <AlertCircle className="w-4 h-4 text-danger" />}
                  <span className="font-medium text-text">{selected.name}</span>
                </div>
                <div className="grid grid-cols-2 gap-y-1 text-xs">
                  <span className="text-muted">Service</span><span>{selected.service_name}</span>
                  <span className="text-muted">Kind</span><span>{selected.span_kind}</span>
                  <span className="text-muted">Status</span>
                  <span><Badge variant={selected.has_error ? 'danger' : 'success'}>{selected.status_code}</Badge></span>
                  <span className="text-muted">Duration</span><span className="font-mono">{selected.duration_ms.toFixed(3)} ms</span>
                  <span className="text-muted">Start</span><span className="font-mono">+{selected.start_offset_ms.toFixed(3)} ms</span>
                  {selected.http_route && (<><span className="text-muted">HTTP</span><span>{selected.http_method} {selected.http_route} → {selected.http_status_code || '—'}</span></>)}
                  {selected.db_system && (<><span className="text-muted">DB</span><span>{selected.db_system} {selected.db_operation}</span></>)}
                </div>
                <Button variant="outline" size="sm" onClick={() => navigate(`/apm/services/${encodeURIComponent(selected.service_name)}?tab=profiling&trace=${trace.trace_id}`)}>
                  <Flame className="mr-1 h-3.5 w-3.5" /> View linked profile
                </Button>
                {selected.db_statement && (
                  <div><div className="text-muted text-xs mb-1">Statement</div><code className="block bg-surface2 p-2 rounded text-xs break-all">{selected.db_statement}</code></div>
                )}
                {selected.status_message && (
                  <div><div className="text-muted text-xs mb-1">Message</div><div className="text-xs text-danger">{selected.status_message}</div></div>
                )}
                {Object.keys(selected.attributes).length > 0 && (
                  <div>
                    <div className="text-muted text-xs mb-1">Attributes</div>
                    <div className="space-y-0.5 max-h-48 overflow-y-auto">
                      {Object.entries(selected.attributes).map(([k, v]) => (
                        <div key={k} className="grid grid-cols-2 gap-2 text-xs">
                          <span className="text-muted truncate">{k}</span>
                          <span className="truncate font-mono">{String(v)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {selected.events.length > 0 && (
                  <div>
                    <div className="text-muted text-xs mb-1">Events</div>
                    {selected.events.map((e, i) => (
                      <div key={i} className="text-xs"><span className="font-mono text-muted">+{e.offset_ms.toFixed(2)}ms</span> {e.name}</div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
      )}
    </div>
  )
}
