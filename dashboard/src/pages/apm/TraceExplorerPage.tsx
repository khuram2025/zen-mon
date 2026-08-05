import { useSearchParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, Search, AlertCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'

interface TraceSummary {
  trace_id: string
  root_service: string
  root_operation: string
  start_time: string
  duration_ms: number
  span_count: number
  error_count: number
  has_error: boolean
  services: string[]
}

const RANGES: Record<string, number> = {
  '15m': 15 * 60_000, '1h': 60 * 60_000, '6h': 6 * 60 * 60_000, '24h': 24 * 60 * 60_000,
}

function relTime(iso: string): string {
  const d = Date.now() - new Date(iso).getTime()
  if (d < 60_000) return `${Math.round(d / 1000)}s ago`
  if (d < 3_600_000) return `${Math.round(d / 60_000)}m ago`
  if (d < 86_400_000) return `${Math.round(d / 3_600_000)}h ago`
  return new Date(iso).toLocaleString()
}

export function TraceExplorerPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()

  const mode = params.get('mode') || 'live'
  const service = params.get('service') || ''
  const operation = params.get('operation') || ''
  const errorsOnly = params.get('errors_only') === 'true'
  const minDur = params.get('min_duration_ms') || ''
  const range = params.get('range') || '1h'

  const set = (k: string, v: string | null) => {
    const next = new URLSearchParams(params)
    if (v === null || v === '') next.delete(k); else next.set(k, v)
    setParams(next, { replace: true })
  }

  const query = useQuery<{ traces: TraceSummary[]; count: number }>({
    queryKey: ['apm', 'traces', { mode, service, operation, errorsOnly, minDur, range }],
    queryFn: async () => {
      const qp = new URLSearchParams({ mode })
      if (service) qp.set('service', service)
      if (operation) qp.set('operation', operation)
      if (errorsOnly) qp.set('errors_only', 'true')
      if (minDur) qp.set('min_duration_ms', minDur)
      if (mode === 'indexed') {
        const now = Date.now()
        qp.set('from_ms', String(now - (RANGES[range] || RANGES['1h'])))
        qp.set('to_ms', String(now))
      }
      return (await api.get(`/apm/traces?${qp.toString()}`)).data
    },
    refetchInterval: mode === 'live' ? 5000 : false,
  })

  const traces = query.data?.traces ?? []

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold text-text">Traces</h1>
        <p className="text-sm text-muted mt-1">
          Search distributed traces and open any trace as a waterfall.
        </p>
      </div>

      {query.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load traces — {apiErrorMessage(query.error)}
        </div>
      )}

      <Card>
        <CardContent className="py-4">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex rounded-md overflow-hidden border border-border">
              {['live', 'indexed'].map((m) => (
                <button
                  key={m}
                  onClick={() => set('mode', m)}
                  className={`px-3 py-1.5 text-xs font-medium ${mode === m ? 'bg-primary text-white' : 'text-muted hover:bg-surface2'}`}
                >
                  {m === 'live' ? 'Live (15m)' : 'Indexed'}
                </button>
              ))}
            </div>
            {mode === 'indexed' && (
              <select
                value={range}
                onChange={(e) => set('range', e.target.value)}
                className="h-9 rounded-md bg-surface2 border border-border text-sm px-2 text-text"
              >
                {Object.keys(RANGES).map((r) => <option key={r} value={r}>Last {r}</option>)}
              </select>
            )}
            <Input className="w-40" placeholder="service" value={service} onChange={(e) => set('service', e.target.value)} />
            <Input className="w-44" placeholder="operation" value={operation} onChange={(e) => set('operation', e.target.value)} />
            <Input className="w-32" type="number" placeholder="min ms" value={minDur} onChange={(e) => set('min_duration_ms', e.target.value)} />
            <label className="flex items-center gap-2 text-xs text-muted cursor-pointer select-none">
              <input type="checkbox" checked={errorsOnly} onChange={(e) => set('errors_only', e.target.checked ? 'true' : null)} />
              Errors only
            </label>
            {query.isFetching && <Loader2 className="w-4 h-4 animate-spin text-muted" />}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          {query.isLoading ? (
            <div className="flex items-center justify-center gap-2 text-muted py-12">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading…
            </div>
          ) : traces.length === 0 ? (
            <div className="flex flex-col items-center gap-2 text-muted py-12">
              <Search className="w-6 h-6" /> No traces match. Send traces via the OTLP ingest, or widen the window.
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Root</Th>
                  <Th className="text-right">Duration</Th>
                  <Th className="text-right">Spans</Th>
                  <Th className="text-right">Errors</Th>
                  <Th>Services</Th>
                  <Th className="text-right">Started</Th>
                </Tr>
              </THead>
              <TBody>
                {traces.map((t) => (
                  <Tr
                    key={t.trace_id}
                    className="cursor-pointer hover:bg-surface2"
                    onClick={() => navigate(`/apm/traces/${t.trace_id}`)}
                  >
                    <Td>
                      <div className="flex items-center gap-2">
                        {t.has_error && <AlertCircle className="w-4 h-4 text-danger" />}
                        <span className="font-medium text-text">{t.root_service}</span>
                        <span className="text-muted">{t.root_operation}</span>
                      </div>
                    </Td>
                    <Td className="text-right font-mono text-xs">{t.duration_ms.toFixed(1)} ms</Td>
                    <Td className="text-right">{t.span_count}</Td>
                    <Td className="text-right">
                      {t.error_count > 0 ? <Badge variant="danger">{t.error_count}</Badge> : <span className="text-muted">0</span>}
                    </Td>
                    <Td>
                      <div className="flex flex-wrap gap-1">
                        {t.services.slice(0, 4).map((s) => <Badge key={s} variant="outline">{s}</Badge>)}
                        {t.services.length > 4 && <span className="text-xs text-muted">+{t.services.length - 4}</span>}
                      </div>
                    </Td>
                    <Td className="text-right text-xs text-muted">{relTime(t.start_time)}</Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
