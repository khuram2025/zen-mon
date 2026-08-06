import { useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, Boxes, Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { HEALTH_COLOR, HealthBadge, KpiTile, fmtMs, fmtPct, fmtRps } from '@/components/apm/shared'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { ApmRangePicker, rangePhrase, useApmRange } from '@/components/apm/ApmRange'
import type { ServiceListResponse, ServiceRED } from '@/types/apm'

type SortKey = 'name' | 'rps' | 'error_rate' | 'p50_ms' | 'p95_ms' | 'p99_ms' | 'apdex'
const HEALTH_RANK: Record<string, number> = { critical: 0, degraded: 1, no_data: 2, healthy: 3 }

export function ServicesPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const [range, setRange] = useApmRange('1h')
  const env = params.get('env') || ''
  const search = params.get('q') || ''
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean } | null>(null)

  const set = (k: string, v: string) => {
    const n = new URLSearchParams(params)
    if (v) n.set(k, v); else n.delete(k)
    setParams(n, { replace: true })
  }

  const q = useQuery<ServiceListResponse>({
    queryKey: ['apm', 'services', { range, env }],
    queryFn: async () => {
      const qp = new URLSearchParams({ range })
      if (env) qp.set('env', env)
      return (await api.get(`/apm/services?${qp}`)).data
    },
    refetchInterval: 15_000,
  })

  const all = q.data?.services ?? []
  const services = useMemo(() => {
    const filtered = all.filter((s) => !search || s.name.toLowerCase().includes(search.toLowerCase()))
    if (!sort) {
      // Default: worst first. A flat alphabetical list buries the one service
      // that is on fire behind six that are fine.
      return [...filtered].sort((a, b) =>
        (HEALTH_RANK[a.health] ?? 9) - (HEALTH_RANK[b.health] ?? 9) || b.request_count - a.request_count)
    }
    const dir = sort.desc ? -1 : 1
    return [...filtered].sort((a, b) => {
      const av = a[sort.key as keyof ServiceRED]
      const bv = b[sort.key as keyof ServiceRED]
      if (typeof av === 'string' && typeof bv === 'string') return av.localeCompare(bv) * dir
      return ((av as number) - (bv as number)) * dir
    })
  }, [all, search, sort])

  const totalRps = all.reduce((a, s) => a + s.rps, 0)
  const health = q.data?.facets.health ?? {}
  const envs = Object.keys(q.data?.facets.env ?? {})

  const SortTh = ({ k, children, align = 'right' }: { k: SortKey; children: React.ReactNode; align?: 'left' | 'right' }) => {
    const active = sort?.key === k
    return (
      <Th className={align === 'right' ? 'text-right' : ''}>
        <button
          onClick={() => setSort(active ? { key: k, desc: !sort!.desc } : { key: k, desc: true })}
          className={`inline-flex items-center gap-1 ${active ? 'text-text' : 'hover:text-text'}`}
        >
          {children}
          {active && (sort!.desc ? <ArrowDown className="h-3 w-3" /> : <ArrowUp className="h-3 w-3" />)}
        </button>
      </Th>
    )
  }

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="Services"
        description="Golden-signal health for every instrumented application service — rate, errors, duration and apdex, measured on inbound requests."
        article="services"
        actions={<ApmRangePicker value={range} onChange={setRange} />}
      />

      {q.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load services — {apiErrorMessage(q.error)}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <KpiTile label="Services" value={all.length} />
        <KpiTile label="Healthy" value={health.healthy ?? 0} accent={HEALTH_COLOR.healthy} />
        <KpiTile label="Degraded" value={health.degraded ?? 0} accent={health.degraded ? HEALTH_COLOR.degraded : undefined} />
        <KpiTile label="Critical" value={health.critical ?? 0} accent={health.critical ? HEALTH_COLOR.critical : undefined} />
        <KpiTile label="Throughput" value={fmtRps(totalRps)} />
      </div>

      <Card>
        <CardContent className="py-3">
          <div className="flex flex-wrap items-center gap-3">
            <select
              value={env} onChange={(e) => set('env', e.target.value)}
              className="h-9 rounded-md border border-border bg-surface2 px-2 text-sm text-text"
            >
              <option value="">All environments</option>
              {envs.map((e) => <option key={e} value={e}>{e}</option>)}
            </select>
            <Input className="w-56" placeholder="Search services…" value={search} onChange={(e) => set('q', e.target.value)} />
            {search && (
              <span className="text-xs text-muted">{services.length} of {all.length}</span>
            )}
            {q.isFetching && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          {q.isLoading ? (
            <div className="flex items-center justify-center gap-2 py-12 text-muted">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : services.length === 0 ? (
            <div className="flex flex-col items-center gap-2 py-12 text-center text-muted">
              <Boxes className="h-6 w-6" />
              {all.length === 0 ? (
                <>
                  <span className="text-sm">No service reported in {rangePhrase(range)}.</span>
                  <span className="text-xs">Send traces to the OTLP ingest, or widen the time range.</span>
                </>
              ) : (
                <span className="text-sm">No service matches “{search}”.</span>
              )}
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <SortTh k="name" align="left">Service</SortTh>
                  <Th>Health</Th>
                  <SortTh k="rps">Throughput</SortTh>
                  <SortTh k="error_rate">Error rate</SortTh>
                  <SortTh k="p50_ms">p50</SortTh>
                  <SortTh k="p95_ms">p95</SortTh>
                  <SortTh k="p99_ms">p99</SortTh>
                  <SortTh k="apdex">Apdex</SortTh>
                  <Th>Env</Th>
                </Tr>
              </THead>
              <TBody>
                {services.map((s) => (
                  <Tr key={s.name} className="cursor-pointer hover:bg-surface2"
                    onClick={() => navigate(`/apm/services/${encodeURIComponent(s.name)}?range=${range}`)}>
                    <Td className="font-medium text-text">{s.name}</Td>
                    <Td><HealthBadge health={s.health} /></Td>
                    <Td className="text-right font-mono text-xs">{fmtRps(s.rps)}</Td>
                    <Td className="text-right font-mono text-xs"
                      style={{ color: s.error_rate >= 0.05 ? HEALTH_COLOR.critical : s.error_rate >= 0.01 ? HEALTH_COLOR.degraded : undefined }}>
                      {fmtPct(s.error_rate)}
                    </Td>
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
