import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Loader2, AlertTriangle, GitBranch, Network, Boxes, Target } from 'lucide-react'
import { api } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { HealthBadge, KpiTile, fmtMs, fmtRps, fmtPct, HEALTH_COLOR } from '@/components/apm/shared'

interface ServiceRED { name: string; health: string; rps: number; error_rate: number; p95_ms: number; apdex: number }
interface ServiceList { services: ServiceRED[]; facets: { health: Record<string, number> } }

const HEALTH_RANK: Record<string, number> = { critical: 0, degraded: 1, no_data: 2, healthy: 3 }

export function ApmOverviewPage() {
  const navigate = useNavigate()
  const q = useQuery<ServiceList>({
    queryKey: ['apm', 'services', { range: '1h', env: '' }],
    queryFn: async () => (await api.get('/apm/services?range=1h')).data,
    refetchInterval: 15000,
  })
  const services = q.data?.services ?? []
  const health = q.data?.facets.health ?? {}
  const totalRps = services.reduce((a, s) => a + s.rps, 0)
  const worst = [...services].sort((a, b) => (HEALTH_RANK[a.health] - HEALTH_RANK[b.health]) || (b.error_rate - a.error_rate) || (b.p95_ms - a.p95_ms)).slice(0, 8)

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-[var(--text-primary)]">Application Monitoring</h1>
        <p className="text-sm text-[var(--text-muted)] mt-1">Fleet-wide application health from OpenTelemetry traces.</p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <KpiTile label="Services" value={services.length} />
        <KpiTile label="Healthy" value={health.healthy ?? 0} accent={HEALTH_COLOR.healthy} />
        <KpiTile label="Degraded" value={health.degraded ?? 0} accent={HEALTH_COLOR.degraded} />
        <KpiTile label="Critical" value={health.critical ?? 0} accent={HEALTH_COLOR.critical} />
        <KpiTile label="Throughput" value={fmtRps(totalRps)} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <button onClick={() => navigate('/apm/services')} className="flex items-center gap-3 rounded-lg border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] px-4 py-3 hover:bg-[var(--bg-tertiary)] text-left">
          <Boxes className="w-5 h-5 text-[var(--accent)]" /><div><div className="font-medium text-[var(--text-primary)]">Services</div><div className="text-xs text-[var(--text-muted)]">RED dashboards & apdex</div></div>
        </button>
        <button onClick={() => navigate('/apm/slos')} className="flex items-center gap-3 rounded-lg border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] px-4 py-3 hover:bg-[var(--bg-tertiary)] text-left">
          <Target className="w-5 h-5 text-[var(--accent)]" /><div><div className="font-medium text-[var(--text-primary)]">SLOs</div><div className="text-xs text-[var(--text-muted)]">Error budgets & burn alerts</div></div>
        </button>
        <button onClick={() => navigate('/apm/service-map')} className="flex items-center gap-3 rounded-lg border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] px-4 py-3 hover:bg-[var(--bg-tertiary)] text-left">
          <Network className="w-5 h-5 text-[var(--accent)]" /><div><div className="font-medium text-[var(--text-primary)]">Service Map</div><div className="text-xs text-[var(--text-muted)]">Dependency topology</div></div>
        </button>
        <button onClick={() => navigate('/apm/traces')} className="flex items-center gap-3 rounded-lg border border-[var(--bg-elevated)] bg-[var(--bg-secondary)] px-4 py-3 hover:bg-[var(--bg-tertiary)] text-left">
          <GitBranch className="w-5 h-5 text-[var(--accent)]" /><div><div className="font-medium text-[var(--text-primary)]">Traces</div><div className="text-xs text-[var(--text-muted)]">Search & waterfall</div></div>
        </button>
      </div>

      <Card>
        <CardHeader className="pb-1"><CardTitle className="flex items-center gap-2 text-sm"><AlertTriangle className="w-4 h-4 text-[var(--danger)]" /> Services needing attention</CardTitle></CardHeader>
        <CardContent className="p-0">
          {q.isLoading ? (
            <div className="flex items-center justify-center gap-2 text-[var(--text-muted)] py-10"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>
          ) : worst.length === 0 ? (
            <div className="text-center text-[var(--text-muted)] py-10">No services reporting yet.</div>
          ) : (
            <Table>
              <THead><Tr><Th>Service</Th><Th>Health</Th><Th className="text-right">Throughput</Th><Th className="text-right">Error rate</Th><Th className="text-right">p95</Th><Th className="text-right">Apdex</Th></Tr></THead>
              <TBody>
                {worst.map((s) => (
                  <Tr key={s.name} className="cursor-pointer hover:bg-[var(--bg-tertiary)]" onClick={() => navigate(`/apm/services/${encodeURIComponent(s.name)}`)}>
                    <Td className="font-medium text-[var(--text-primary)]">{s.name}</Td>
                    <Td><HealthBadge health={s.health} /></Td>
                    <Td className="text-right font-mono text-xs">{fmtRps(s.rps)}</Td>
                    <Td className="text-right font-mono text-xs">{fmtPct(s.error_rate)}</Td>
                    <Td className="text-right font-mono text-xs">{fmtMs(s.p95_ms)}</Td>
                    <Td className="text-right font-mono text-xs">{s.apdex.toFixed(2)}</Td>
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
