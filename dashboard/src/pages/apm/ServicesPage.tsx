import { useSearchParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, Boxes } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { HealthBadge, KpiTile, fmtMs, fmtRps, fmtPct, HEALTH_COLOR } from '@/components/apm/shared'

interface ServiceRED {
  name: string; envs: string[]; health: string; request_count: number
  rps: number; error_rate: number; p50_ms: number; p95_ms: number; p99_ms: number; apdex: number
}
interface ServiceList {
  services: ServiceRED[]
  facets: { env: Record<string, number>; health: Record<string, number> }
  window_seconds: number
}

const RANGES = ['15m', '1h', '6h', '24h']

export function ServicesPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const range = params.get('range') || '1h'
  const env = params.get('env') || ''
  const search = params.get('q') || ''

  const set = (k: string, v: string) => {
    const n = new URLSearchParams(params)
    if (v) n.set(k, v); else n.delete(k)
    setParams(n, { replace: true })
  }

  const q = useQuery<ServiceList>({
    queryKey: ['apm', 'services', { range, env }],
    queryFn: async () => {
      const qp = new URLSearchParams({ range })
      if (env) qp.set('env', env)
      return (await api.get(`/apm/services?${qp}`)).data
    },
    refetchInterval: 15000,
  })

  const services = (q.data?.services ?? []).filter((s) => !search || s.name.toLowerCase().includes(search.toLowerCase()))
  const totalRps = (q.data?.services ?? []).reduce((a, s) => a + s.rps, 0)
  const health = q.data?.facets.health ?? {}

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold text-text">Services</h1>
        <p className="text-sm text-muted mt-1">Golden-signal health for every instrumented application service.</p>
      </div>

      {q.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load services — {apiErrorMessage(q.error)}
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <KpiTile label="Services" value={q.data?.services.length ?? 0} />
        <KpiTile label="Healthy" value={health.healthy ?? 0} accent={HEALTH_COLOR.healthy} />
        <KpiTile label="Degraded" value={health.degraded ?? 0} accent={HEALTH_COLOR.degraded} />
        <KpiTile label="Critical" value={health.critical ?? 0} accent={HEALTH_COLOR.critical} />
        <KpiTile label="Throughput" value={fmtRps(totalRps)} />
      </div>

      <Card>
        <CardContent className="py-3">
          <div className="flex flex-wrap items-center gap-3">
            <select value={range} onChange={(e) => set('range', e.target.value)}
              className="h-9 rounded-md bg-surface2 border border-border text-sm px-2 text-text">
              {RANGES.map((r) => <option key={r} value={r}>Last {r}</option>)}
            </select>
            <select value={env} onChange={(e) => set('env', e.target.value)}
              className="h-9 rounded-md bg-surface2 border border-border text-sm px-2 text-text">
              <option value="">All environments</option>
              {Object.keys(q.data?.facets.env ?? {}).map((e) => <option key={e} value={e}>{e}</option>)}
            </select>
            <Input className="w-56" placeholder="Search services…" value={search} onChange={(e) => set('q', e.target.value)} />
            {q.isFetching && <Loader2 className="w-4 h-4 animate-spin text-muted" />}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          {q.isLoading ? (
            <div className="flex items-center justify-center gap-2 text-muted py-12"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>
          ) : services.length === 0 ? (
            <div className="flex flex-col items-center gap-2 text-muted py-12"><Boxes className="w-6 h-6" /> No services reporting. Send traces via the OTLP ingest.</div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Service</Th><Th>Health</Th>
                  <Th className="text-right">Throughput</Th><Th className="text-right">Error rate</Th>
                  <Th className="text-right">p50</Th><Th className="text-right">p95</Th><Th className="text-right">p99</Th>
                  <Th className="text-right">Apdex</Th><Th>Env</Th>
                </Tr>
              </THead>
              <TBody>
                {services.map((s) => (
                  <Tr key={s.name} className="cursor-pointer hover:bg-surface2" onClick={() => navigate(`/apm/services/${encodeURIComponent(s.name)}`)}>
                    <Td className="font-medium text-text">{s.name}</Td>
                    <Td><HealthBadge health={s.health} /></Td>
                    <Td className="text-right font-mono text-xs">{fmtRps(s.rps)}</Td>
                    <Td className="text-right font-mono text-xs" style={{ color: s.error_rate >= 0.05 ? HEALTH_COLOR.critical : s.error_rate >= 0.01 ? HEALTH_COLOR.degraded : undefined }}>{fmtPct(s.error_rate)}</Td>
                    <Td className="text-right font-mono text-xs">{fmtMs(s.p50_ms)}</Td>
                    <Td className="text-right font-mono text-xs">{fmtMs(s.p95_ms)}</Td>
                    <Td className="text-right font-mono text-xs">{fmtMs(s.p99_ms)}</Td>
                    <Td className="text-right font-mono text-xs">{s.apdex.toFixed(2)}</Td>
                    <Td className="text-xs text-muted">{s.envs.join(', ')}</Td>
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
