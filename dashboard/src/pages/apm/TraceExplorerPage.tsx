import { useSearchParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, Search, AlertCircle, GitBranch } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { APM_RANGES, RANGE_MS, type ApmRangeKey } from '@/components/apm/ApmRange'
import { ApmKpi, LatencyCell, RankBar, fmtCount } from '@/components/apm/viz'
import type { TraceSummary } from '@/types/apm'

/** Indexed mode reuses the module-wide range vocabulary. */
const RANGES: Record<string, number> = RANGE_MS
const RANGE_KEYS: readonly ApmRangeKey[] = APM_RANGES

export function TraceExplorerPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()

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

  const traces = query.data?.traces ?? []
  const errorTraces = traces.filter((t) => t.has_error).length
  const maxDuration = Math.max(...traces.map((t) => t.duration_ms), 1)
  const p95Dur = traces.length
    ? [...traces].sort((a, b) => a.duration_ms - b.duration_ms)[Math.min(traces.length - 1, Math.floor(traces.length * 0.95))]?.duration_ms ?? 0
    : 0

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="Trace explorer"
        description="Search distributed traces and open any one as a waterfall to see exactly where a request spent its time."
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
                {RANGE_KEYS.map((r) => <option key={r} value={r}>Last {r}</option>)}
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
          {facets.data && (
            <div className="mt-3 flex flex-wrap gap-4 border-t border-border pt-3">
              <FacetChips label="Env" values={facets.data.envs} selected={env} onSelect={(v) => set('env', v)} />
              <FacetChips label="Version" values={facets.data.versions} selected={version} onSelect={(v) => set('service_version', v)} />
              <FacetChips label="HTTP" values={facets.data.http_status.filter((v) => v.value !== '0')} selected={httpStatus} onSelect={(v) => set('http_status_code', v)} />
              <FacetChips label="Status" values={facets.data.status_code} selected={statusCode} onSelect={(v) => set('status_code', v)} />
              {!service && <FacetChips label="Service" values={facets.data.services} selected={service} onSelect={(v) => set('service', v)} />}
            </div>
          )}
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
                    <Td className="min-w-[8rem] text-right">
                      <LatencyCell ms={t.duration_ms} />
                      <RankBar value={t.duration_ms} max={maxDuration} color={t.has_error ? '#db2777' : '#7c3aed'} />
                    </Td>
                    <Td className="text-right">{t.span_count}</Td>
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
                    <Td className="text-right text-xs text-muted">{relativeTime(t.start_time)}</Td>
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

function FacetChips({ label, values, selected, onSelect }: {
  label: string
  values: Array<{ value: string; count: number }>
  selected: string
  onSelect: (v: string | null) => void
}) {
  if (!values.length) return null
  return (
    <div className="min-w-0">
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="flex flex-wrap gap-1">
        {values.slice(0, 8).map((v) => {
          const active = selected === v.value
          return (
            <button
              key={v.value}
              onClick={() => onSelect(active ? null : v.value)}
              className={`rounded-full border px-2 py-0.5 text-[11px] ${active ? 'border-primary bg-primary/15 text-text' : 'border-border text-muted hover:text-text'}`}
            >
              {v.value} <span className="tabular-nums opacity-70">{v.count}</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
