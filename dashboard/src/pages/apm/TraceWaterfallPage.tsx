import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, ArrowLeft, AlertCircle, Database, Server, ArrowRightLeft } from 'lucide-react'
import { api } from '@/lib/api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'

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
  const [selected, setSelected] = useState<SpanNode | null>(null)

  const query = useQuery<TraceDetail>({
    queryKey: ['apm', 'trace', traceId],
    queryFn: async () => (await api.get(`/apm/traces/${traceId}`)).data,
    enabled: !!traceId,
  })

  if (query.isLoading) {
    return <div className="flex items-center justify-center gap-2 text-[var(--text-muted)] p-12"><Loader2 className="w-4 h-4 animate-spin" /> Loading trace…</div>
  }
  if (query.isError || !query.data) {
    return (
      <div className="p-6">
        <Button variant="ghost" onClick={() => navigate('/apm/traces')}><ArrowLeft className="w-4 h-4 mr-1" /> Back</Button>
        <div className="text-center text-[var(--text-muted)] py-12">Trace not found.</div>
      </div>
    )
  }

  const trace = query.data
  const total = Math.max(trace.duration_ms, 0.001)

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate('/apm/traces')}><ArrowLeft className="w-4 h-4 mr-1" /> Traces</Button>
        <h1 className="text-lg font-semibold text-[var(--text-primary)]">Trace</h1>
        <code className="text-xs text-[var(--text-muted)] font-mono">{trace.trace_id}</code>
      </div>

      <div className="flex flex-wrap gap-6 text-sm">
        <div><span className="text-[var(--text-muted)]">Duration</span> <span className="font-mono font-medium text-[var(--text-primary)]">{trace.duration_ms.toFixed(1)} ms</span></div>
        <div><span className="text-[var(--text-muted)]">Spans</span> <span className="font-medium text-[var(--text-primary)]">{trace.span_count}</span></div>
        <div className="flex items-center gap-1.5">
          <span className="text-[var(--text-muted)]">Services</span>
          {trace.services.map((s) => <Badge key={s} variant="outline">{s}</Badge>)}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-2">
          <CardContent className="p-0">
            <div className="divide-y divide-[var(--bg-elevated)]">
              {trace.spans.map((s) => {
                const leftPct = (s.start_offset_ms / total) * 100
                const widthPct = Math.max((s.duration_ms / total) * 100, 0.6)
                const color = s.has_error ? 'var(--danger)' : s.db_system ? '#a78bfa' : 'var(--accent)'
                const active = selected?.span_id === s.span_id
                return (
                  <div
                    key={s.span_id}
                    onClick={() => setSelected(s)}
                    className={`flex items-center gap-2 px-3 py-1.5 cursor-pointer text-xs ${active ? 'bg-[var(--bg-tertiary)]' : 'hover:bg-[var(--bg-tertiary)]/50'}`}
                  >
                    <div className="flex items-center gap-1.5 min-w-0" style={{ width: '40%', paddingLeft: `${s.depth * 14}px` }}>
                      {s.has_error ? <AlertCircle className="w-3.5 h-3.5 text-[var(--danger)] shrink-0" /> : kindIcon(s.span_kind, s.db_system)}
                      <span className="text-[var(--text-muted)] shrink-0">{s.service_name}</span>
                      <span className="truncate text-[var(--text-primary)]">{s.name}</span>
                    </div>
                    <div className="relative flex-1 h-4">
                      <div
                        className="absolute top-0.5 h-3 rounded-sm"
                        style={{ left: `${leftPct}%`, width: `${widthPct}%`, backgroundColor: color, minWidth: '2px' }}
                        title={`${s.duration_ms.toFixed(2)} ms`}
                      />
                    </div>
                    <span className="font-mono text-[var(--text-muted)] w-16 text-right shrink-0">{s.duration_ms.toFixed(1)}ms</span>
                  </div>
                )
              })}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="py-4">
            {!selected ? (
              <div className="text-center text-[var(--text-muted)] text-sm py-8">Select a span to see details.</div>
            ) : (
              <div className="space-y-3 text-sm">
                <div className="flex items-center gap-2">
                  {selected.has_error && <AlertCircle className="w-4 h-4 text-[var(--danger)]" />}
                  <span className="font-medium text-[var(--text-primary)]">{selected.name}</span>
                </div>
                <div className="grid grid-cols-2 gap-y-1 text-xs">
                  <span className="text-[var(--text-muted)]">Service</span><span>{selected.service_name}</span>
                  <span className="text-[var(--text-muted)]">Kind</span><span>{selected.span_kind}</span>
                  <span className="text-[var(--text-muted)]">Status</span>
                  <span><Badge variant={selected.has_error ? 'danger' : 'success'}>{selected.status_code}</Badge></span>
                  <span className="text-[var(--text-muted)]">Duration</span><span className="font-mono">{selected.duration_ms.toFixed(3)} ms</span>
                  <span className="text-[var(--text-muted)]">Start</span><span className="font-mono">+{selected.start_offset_ms.toFixed(3)} ms</span>
                  {selected.http_route && (<><span className="text-[var(--text-muted)]">HTTP</span><span>{selected.http_method} {selected.http_route} → {selected.http_status_code || '—'}</span></>)}
                  {selected.db_system && (<><span className="text-[var(--text-muted)]">DB</span><span>{selected.db_system} {selected.db_operation}</span></>)}
                </div>
                {selected.db_statement && (
                  <div><div className="text-[var(--text-muted)] text-xs mb-1">Statement</div><code className="block bg-[var(--bg-tertiary)] p-2 rounded text-xs break-all">{selected.db_statement}</code></div>
                )}
                {selected.status_message && (
                  <div><div className="text-[var(--text-muted)] text-xs mb-1">Message</div><div className="text-xs text-[var(--danger)]">{selected.status_message}</div></div>
                )}
                {Object.keys(selected.attributes).length > 0 && (
                  <div>
                    <div className="text-[var(--text-muted)] text-xs mb-1">Attributes</div>
                    <div className="space-y-0.5 max-h-48 overflow-y-auto">
                      {Object.entries(selected.attributes).map(([k, v]) => (
                        <div key={k} className="grid grid-cols-2 gap-2 text-xs">
                          <span className="text-[var(--text-muted)] truncate">{k}</span>
                          <span className="truncate font-mono">{String(v)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {selected.events.length > 0 && (
                  <div>
                    <div className="text-[var(--text-muted)] text-xs mb-1">Events</div>
                    {selected.events.map((e, i) => (
                      <div key={i} className="text-xs"><span className="font-mono text-[var(--text-muted)]">+{e.offset_ms.toFixed(2)}ms</span> {e.name}</div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
