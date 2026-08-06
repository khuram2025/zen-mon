import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, ArrowLeft, GitBranch } from 'lucide-react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { HealthBadge, KpiTile, fmtMs, fmtRps, fmtPct, HEALTH_COLOR } from '@/components/apm/shared'
import { ErrorStatusBadge } from '@/components/apm/errorShared'
import { ApmRangePicker, type ApmRangeKey } from '@/components/apm/ApmRange'
import { KbLink } from '@/components/apm/KbLink'
import type { OperationRED as Op, REDPoint, ServiceRED } from '@/types/apm'

const TABS = ['overview', 'performance', 'errors'] as const

function REDChart({ data, dataKey, color, label, fmt, domain }: { data: REDPoint[]; dataKey: string; color: string; label: string; fmt: (v: number) => string; domain?: [number, (max: number) => number] }) {
  return (
    <Card>
      <CardHeader className="pb-1"><CardTitle className="text-sm text-muted">{label}</CardTitle></CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={160}>
          <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -8 }}>
            <defs>
              <linearGradient id={`g-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity={0.4} />
                <stop offset="100%" stopColor={color} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.15)" vertical={false} />
            <XAxis dataKey="timestamp" tick={{ fontSize: 10, fill: '#94a3b8' }}
              tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} minTickGap={40} />
            <YAxis tick={{ fontSize: 10, fill: '#94a3b8' }} width={48} tickFormatter={fmt} domain={domain} />
            <Tooltip contentStyle={{ background: '#0d121b', border: '1px solid #1e293b', fontSize: 12, color: '#e5e7eb' }}
              labelFormatter={(t) => new Date(t).toLocaleString()} formatter={(v: any) => fmt(Number(v))} />
            <Area type="monotone" dataKey={dataKey} stroke={color} fill={`url(#g-${dataKey})`} strokeWidth={2} />
          </AreaChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  )
}

export function ServiceDetailPage() {
  const { name = '' } = useParams()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const tab = (params.get('tab') as typeof TABS[number]) || 'overview'
  const range = (params.get('range') || '1h') as ApmRangeKey
  const setParam = (k: string, v: string) => { const n = new URLSearchParams(params); n.set(k, v); setParams(n, { replace: true }) }

  const summary = useQuery<ServiceRED>({ queryKey: ['apm', 'service', name, { range }], queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}?range=${range}`)).data, refetchInterval: 15000 })
  const red = useQuery<REDPoint[]>({ queryKey: ['apm', 'service-red', name, { range }], queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}/red?range=${range}`)).data, refetchInterval: 15_000 })
  const ops = useQuery<Op[]>({ queryKey: ['apm', 'service-ops', name, { range }], queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}/operations?range=${range}`)).data, refetchInterval: 15_000 })
  const errs = useQuery<{ issues: any[] }>({ queryKey: ['apm', 'errors', { service: name }], queryFn: async () => (await api.get(`/apm/errors?range_=24h&service=${encodeURIComponent(name)}`)).data, enabled: tab === 'errors' })

  const s = summary.data
  const points = red.data ?? []

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate('/apm/services')}><ArrowLeft className="w-4 h-4 mr-1" /> Services</Button>
        <h1 className="text-lg font-semibold text-text">{name}</h1>
        {s && <HealthBadge health={s.health} />}
        <div className="flex-1" />
        <button
          onClick={() => navigate(`/apm/traces?mode=indexed&service=${encodeURIComponent(name)}&range=${range}`)}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-muted hover:text-text"
        >
          <GitBranch className="h-3.5 w-3.5" /> View traces
        </button>
        <ApmRangePicker value={range} onChange={(r) => setParam('range', r)} />
        <KbLink article="services" />
      </div>

      {summary.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load service — {apiErrorMessage(summary.error)}
        </div>
      )}

      {summary.isLoading ? (
        <div className="flex items-center gap-2 text-muted py-12 justify-center"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>
      ) : (
        <>
          <div className="grid grid-cols-3 md:grid-cols-6 gap-3">
            <KpiTile label="Throughput" value={fmtRps(s?.rps ?? 0)} />
            <KpiTile label="Error rate" value={fmtPct(s?.error_rate ?? 0)} accent={(s?.error_rate ?? 0) >= 0.05 ? HEALTH_COLOR.critical : undefined} />
            <KpiTile label="p50" value={fmtMs(s?.p50_ms ?? 0)} />
            <KpiTile label="p95" value={fmtMs(s?.p95_ms ?? 0)} />
            <KpiTile label="p99" value={fmtMs(s?.p99_ms ?? 0)} />
            <KpiTile label="Apdex" value={(s?.apdex ?? 0).toFixed(2)} />
          </div>

          <div className="flex gap-1 border-b border-border">
            {TABS.map((t) => (
              <button key={t} onClick={() => setParam('tab', t)}
                className={`px-4 py-2 text-sm capitalize border-b-2 -mb-px ${tab === t ? 'border-primary text-text' : 'border-transparent text-muted hover:text-text'}`}>
                {t}
              </button>
            ))}
          </div>

          {tab === 'overview' && (
            <div className="space-y-4">
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                <REDChart data={points} dataKey="rps" color="#3b82f6" label="Throughput (req/s)" fmt={(v) => v.toFixed(2)} />
                <REDChart data={points} dataKey="error_rate" color={HEALTH_COLOR.critical} label="Error rate" fmt={(v) => `${(v * 100).toFixed(1)}%`} domain={[0, (max: number) => Math.max(max, 0.01)]} />
                <REDChart data={points} dataKey="p95_ms" color={HEALTH_COLOR.degraded} label="Latency p95" fmt={(v) => `${v.toFixed(0)}ms`} />
              </div>
              <Card>
                <CardHeader className="pb-1"><CardTitle className="text-sm">Top operations</CardTitle></CardHeader>
                <CardContent className="p-0">
                  <Table>
                    <THead><Tr><Th>Operation</Th><Th className="text-right">Requests</Th><Th className="text-right">Error rate</Th><Th className="text-right">p95</Th></Tr></THead>
                    <TBody>
                      {(ops.data ?? []).map((o) => (
                        <Tr key={o.operation}>
                          <Td className="font-mono text-xs text-text">{o.operation}</Td>
                          <Td className="text-right">{o.request_count}</Td>
                          <Td className="text-right font-mono text-xs">{fmtPct(o.error_rate)}</Td>
                          <Td className="text-right font-mono text-xs">{fmtMs(o.p95_ms)}</Td>
                        </Tr>
                      ))}
                      {(ops.data ?? []).length === 0 && <Tr><Td colSpan={4} className="text-center text-muted py-6">No operations in range.</Td></Tr>}
                    </TBody>
                  </Table>
                </CardContent>
              </Card>
            </div>
          )}

          {tab === 'performance' && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <REDChart data={points} dataKey="p50_ms" color="#3b82f6" label="Latency p50" fmt={(v) => `${v.toFixed(0)}ms`} />
              <REDChart data={points} dataKey="p95_ms" color={HEALTH_COLOR.degraded} label="Latency p95" fmt={(v) => `${v.toFixed(0)}ms`} />
              <REDChart data={points} dataKey="rps" color="#3b82f6" label="Throughput (req/s)" fmt={(v) => v.toFixed(2)} />
              <REDChart data={points} dataKey="error_rate" color={HEALTH_COLOR.critical} label="Error rate" fmt={(v) => `${(v * 100).toFixed(1)}%`} domain={[0, (max: number) => Math.max(max, 0.01)]} />
            </div>
          )}

          {tab === 'errors' && (
            <Card>
              <CardContent className="p-0">
                <Table>
                  <THead><Tr><Th>Error</Th><Th className="text-right">Events</Th><Th className="text-right">Traces</Th><Th>Status</Th></Tr></THead>
                  <TBody>
                    {(errs.data?.issues ?? []).map((e) => (
                      <Tr key={e.group_id} className="cursor-pointer hover:bg-surface2" onClick={() => navigate(`/apm/errors/${e.group_id}`)}>
                        <Td><div className="font-medium text-text">{e.exception_type}</div><div className="text-xs text-muted truncate max-w-md">{e.message}</div></Td>
                        <Td className="text-right font-mono text-xs">{e.occurrences}</Td>
                        <Td className="text-right font-mono text-xs">{e.traces}</Td>
                        <Td><ErrorStatusBadge status={e.status} /></Td>
                      </Tr>
                    ))}
                    {(errs.data?.issues ?? []).length === 0 && <Tr><Td colSpan={4} className="text-center text-muted py-6">No errors for this service.</Td></Tr>}
                  </TBody>
                </Table>
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  )
}
