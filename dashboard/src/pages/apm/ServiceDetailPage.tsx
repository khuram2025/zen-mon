import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, ArrowLeft } from 'lucide-react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { api } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { HealthBadge, KpiTile, fmtMs, fmtRps, fmtPct, HEALTH_COLOR } from '@/components/apm/shared'

interface ServiceRED { name: string; envs: string[]; health: string; request_count: number; rps: number; error_rate: number; p50_ms: number; p95_ms: number; p99_ms: number; apdex: number }
interface REDPoint { timestamp: string; rps: number; error_rate: number; p50_ms: number; p95_ms: number }
interface Op { operation: string; request_count: number; rps: number; error_rate: number; p95_ms: number }

const RANGES = ['15m', '1h', '6h', '24h']
const TABS = ['overview', 'performance'] as const

function REDChart({ data, dataKey, color, label, fmt }: { data: REDPoint[]; dataKey: string; color: string; label: string; fmt: (v: number) => string }) {
  return (
    <Card>
      <CardHeader className="pb-1"><CardTitle className="text-sm text-[var(--text-muted)]">{label}</CardTitle></CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={160}>
          <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -8 }}>
            <defs>
              <linearGradient id={`g-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity={0.4} />
                <stop offset="100%" stopColor={color} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--bg-elevated)" vertical={false} />
            <XAxis dataKey="timestamp" tick={{ fontSize: 10, fill: 'var(--text-muted)' }}
              tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} minTickGap={40} />
            <YAxis tick={{ fontSize: 10, fill: 'var(--text-muted)' }} width={48} tickFormatter={fmt} />
            <Tooltip contentStyle={{ background: 'var(--bg-secondary)', border: '1px solid var(--bg-elevated)', fontSize: 12 }}
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
  const range = params.get('range') || '1h'
  const setParam = (k: string, v: string) => { const n = new URLSearchParams(params); n.set(k, v); setParams(n, { replace: true }) }

  const summary = useQuery<ServiceRED>({ queryKey: ['apm', 'service', name, { range }], queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}?range=${range}`)).data, refetchInterval: 15000 })
  const red = useQuery<REDPoint[]>({ queryKey: ['apm', 'service-red', name, { range }], queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}/red?range=${range}`)).data })
  const ops = useQuery<Op[]>({ queryKey: ['apm', 'service-ops', name, { range }], queryFn: async () => (await api.get(`/apm/services/${encodeURIComponent(name)}/operations?range=${range}`)).data })

  const s = summary.data
  const points = red.data ?? []

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate('/apm/services')}><ArrowLeft className="w-4 h-4 mr-1" /> Services</Button>
        <h1 className="text-lg font-semibold text-[var(--text-primary)]">{name}</h1>
        {s && <HealthBadge health={s.health} />}
        <div className="flex-1" />
        <select value={range} onChange={(e) => setParam('range', e.target.value)}
          className="h-9 rounded-md bg-[var(--bg-tertiary)] border border-[var(--bg-elevated)] text-sm px-2 text-[var(--text-primary)]">
          {RANGES.map((r) => <option key={r} value={r}>Last {r}</option>)}
        </select>
      </div>

      {summary.isLoading ? (
        <div className="flex items-center gap-2 text-[var(--text-muted)] py-12 justify-center"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>
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

          <div className="flex gap-1 border-b border-[var(--bg-elevated)]">
            {TABS.map((t) => (
              <button key={t} onClick={() => setParam('tab', t)}
                className={`px-4 py-2 text-sm capitalize border-b-2 -mb-px ${tab === t ? 'border-[var(--accent)] text-[var(--text-primary)]' : 'border-transparent text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
                {t}
              </button>
            ))}
          </div>

          {tab === 'overview' && (
            <div className="space-y-4">
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                <REDChart data={points} dataKey="rps" color="var(--accent)" label="Throughput (req/s)" fmt={(v) => v.toFixed(2)} />
                <REDChart data={points} dataKey="error_rate" color={HEALTH_COLOR.critical} label="Error rate" fmt={(v) => `${(v * 100).toFixed(1)}%`} />
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
                          <Td className="font-mono text-xs text-[var(--text-primary)]">{o.operation}</Td>
                          <Td className="text-right">{o.request_count}</Td>
                          <Td className="text-right font-mono text-xs">{fmtPct(o.error_rate)}</Td>
                          <Td className="text-right font-mono text-xs">{fmtMs(o.p95_ms)}</Td>
                        </Tr>
                      ))}
                      {(ops.data ?? []).length === 0 && <Tr><Td colSpan={4} className="text-center text-[var(--text-muted)] py-6">No operations in range.</Td></Tr>}
                    </TBody>
                  </Table>
                </CardContent>
              </Card>
            </div>
          )}

          {tab === 'performance' && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <REDChart data={points} dataKey="p50_ms" color="var(--accent)" label="Latency p50" fmt={(v) => `${v.toFixed(0)}ms`} />
              <REDChart data={points} dataKey="p95_ms" color={HEALTH_COLOR.degraded} label="Latency p95" fmt={(v) => `${v.toFixed(0)}ms`} />
              <REDChart data={points} dataKey="rps" color="var(--accent)" label="Throughput (req/s)" fmt={(v) => v.toFixed(2)} />
              <REDChart data={points} dataKey="error_rate" color={HEALTH_COLOR.critical} label="Error rate" fmt={(v) => `${(v * 100).toFixed(1)}%`} />
            </div>
          )}
        </>
      )}
    </div>
  )
}
