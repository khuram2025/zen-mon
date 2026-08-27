import { useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Activity, ArrowDown, ArrowUp, Boxes, Bug, Download, Gauge, Loader2, RefreshCw, Search } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { HealthBadge, fmtMs, fmtPct, fmtRps } from '@/components/apm/shared'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { ApmRangePicker, rangePhrase, useApmRange } from '@/components/apm/ApmRange'
import {
  ApdexCell, ApmKpi, DeepLinks, ErrorRateCell, HealthShareBar,
  ThroughputCell, errorTone, fmtCount,
} from '@/components/apm/viz'
import {
  ApmExplorerFrame,
  ApmFacetSidebar,
  ApmUnderlineNav,
  DurationTimeline,
  EXPLORER_HEAD,
  downloadCsv,
} from '@/components/apm/explorer'
import type { ServiceListResponse, ServiceRED } from '@/types/apm'

type SortKey = 'name' | 'rps' | 'error_rate' | 'p50_ms' | 'p95_ms' | 'p99_ms' | 'apdex' | 'request_count'
const HEALTH_RANK: Record<string, number> = { critical: 0, degraded: 1, no_data: 2, healthy: 3 }
const HEALTH_FILTERS = ['all', 'critical', 'degraded', 'healthy', 'no_data'] as const

export function ServicesPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const [range, setRange] = useApmRange('1h')
  const env = params.get('env') || ''
  const search = params.get('q') || ''
  const healthFilter = params.get('health') || 'all'
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
    const filtered = all.filter((s) => {
      if (search && !s.name.toLowerCase().includes(search.toLowerCase())) return false
      if (healthFilter !== 'all' && s.health !== healthFilter) return false
      return true
    })
    if (!sort) {
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
  }, [all, search, sort, healthFilter])

  const totalRps = all.reduce((a, s) => a + s.rps, 0)
  const totalReqs = all.reduce((a, s) => a + s.request_count, 0)
  const totalErrs = all.reduce((a, s) => a + s.request_count * s.error_rate, 0)
  const fleetErrorRate = totalReqs > 0 ? totalErrs / totalReqs : 0
  const worstP95 = all.reduce((a, s) => Math.max(a, s.p95_ms), 0)
  const health = q.data?.facets.health ?? {}
  const envs = Object.keys(q.data?.facets.env ?? {})
  const maxRps = Math.max(...all.map((s) => s.rps), 0.0001)
  const healthyCount = health.healthy ?? 0
  const degradedCount = health.degraded ?? 0
  const criticalCount = health.critical ?? 0

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
        title="Service catalog"
        description="Rate, errors, duration and apdex for every instrumented service — worst first, with a one-click deep dive into traces and errors."
        article="services"
        actions={<ApmRangePicker value={range} onChange={setRange} />}
      />

      {q.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load services — {apiErrorMessage(q.error)}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <ApmKpi
          label="Services" icon={<Boxes className="h-4 w-4" />}
          tone={criticalCount ? 'danger' : degradedCount ? 'warning' : 'success'}
          value={all.length}
          sub={`${healthyCount} healthy`}
          foot={<HealthShareBar healthy={healthyCount} degraded={degradedCount} critical={criticalCount} noData={health.no_data ?? 0} />}
        />
        <ApmKpi label="Throughput" icon={<Activity className="h-4 w-4" />} tone="info" value={fmtRps(totalRps)} sub={`${fmtCount(totalReqs)} requests`} />
          <ApmKpi label="Fleet error rate" icon={<Bug className="h-4 w-4" />} tone={errorTone(fleetErrorRate)} value={fmtPct(fleetErrorRate)} sub={`${criticalCount} critical`} />
        <ApmKpi label="Worst p95" icon={<Gauge className="h-4 w-4" />} tone={worstP95 >= 800 ? 'warning' : 'success'} value={fmtMs(worstP95)} sub="across the catalog" />
        <ApmKpi label="Degraded + critical" tone={criticalCount + degradedCount ? 'danger' : 'success'} value={criticalCount + degradedCount} sub={`${degradedCount} degraded · ${criticalCount} critical`} />
      </div>

      <ApmUnderlineNav
        items={HEALTH_FILTERS.map((h) => ({
          key: h,
          label: h === 'no_data' ? 'No data' : h === 'all' ? 'All' : h.replace(/^./, (c) => c.toUpperCase()),
          count: h === 'all' ? all.length : (health[h] ?? 0),
          current: healthFilter === h,
          onSelect: () => set('health', h === 'all' ? '' : h),
        }))}
      />

      <ApmExplorerFrame
        search={
          <div className="relative min-w-[14rem] flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input className="pl-8" placeholder="Search services…" value={search} onChange={(e) => set('q', e.target.value)} />
          </div>
        }
        actions={
          <>
            <Button variant="outline" size="sm" onClick={() => q.refetch()} disabled={q.isFetching}>
              <RefreshCw className={`h-3.5 w-3.5 ${q.isFetching ? 'animate-spin' : ''}`} /> Refresh
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={!services.length}
              onClick={() => downloadCsv(
                'apm-services.csv',
                ['name', 'health', 'rps', 'error_rate', 'p50_ms', 'p95_ms', 'p99_ms', 'apdex', 'requests'],
                services.map((s) => [s.name, s.health, s.rps, s.error_rate, s.p50_ms, s.p95_ms, s.p99_ms, s.apdex, s.request_count]),
              )}
            >
              <Download className="h-3.5 w-3.5" /> Export
            </Button>
          </>
        }
        summary={<>Displaying {fmtCount(services.length)} of {fmtCount(all.length)} services · {rangePhrase(range)}</>}
        sidebar={
          <ApmFacetSidebar
            title="Service analytics"
            groups={[{
              title: 'Environment',
              items: envs.map((value) => ({
                value,
                count: all.filter((s) => s.envs.includes(value)).length,
                active: env === value,
                onSelect: () => set('env', env === value ? '' : value),
              })),
            }]}
          />
        }
      >
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
              <span className="text-sm">No service matches the current filters.</span>
            )}
          </div>
        ) : (
          <Table>
            <THead className={EXPLORER_HEAD}>
              <Tr>
                <SortTh k="name" align="left">Service</SortTh>
                <Th>Health</Th>
                <SortTh k="rps">Throughput</SortTh>
                <SortTh k="error_rate">Error rate</SortTh>
                <SortTh k="p95_ms">p95</SortTh>
                <SortTh k="p99_ms">p99</SortTh>
                <SortTh k="apdex">Apdex</SortTh>
                <Th>Env</Th>
                <Th className="text-right">Deep dive</Th>
              </Tr>
            </THead>
            <TBody>
              {services.map((s) => (
                <Tr key={s.name} className="cursor-pointer" tabIndex={0} aria-label={`Open service ${s.name}`}
                  onClick={() => navigate(`/apm/services/${encodeURIComponent(s.name)}?range=${range}`)}>
                  <Td className="font-medium text-text">
                    <div>{s.name}</div>
                    <div className="text-[10px] text-muted">{fmtCount(s.request_count)} requests</div>
                  </Td>
                  <Td><HealthBadge health={s.health} /></Td>
                  <Td className="text-right"><ThroughputCell rps={s.rps} maxRps={maxRps} /></Td>
                  <Td className="text-right"><ErrorRateCell rate={s.error_rate} /></Td>
                  <Td className="min-w-[6.5rem] text-right">
                    <DurationTimeline ms={s.p95_ms} maxMs={worstP95 || 1} significant={s.health === 'critical' || s.p95_ms >= 800} />
                  </Td>
                  <Td className="text-right font-mono text-xs tabular-nums">{fmtMs(s.p99_ms)}</Td>
                  <Td className="text-right"><ApdexCell value={s.apdex} /></Td>
                  <Td className="text-xs text-muted">{(s.envs ?? []).join(', ') || '—'}</Td>
                  <Td><DeepLinks service={s.name} range={range} /></Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </ApmExplorerFrame>
    </div>
  )
}
