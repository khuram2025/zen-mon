import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import { Loader2, Network } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { HEALTH_COLOR, fmtMs, fmtPct, fmtRps } from '@/components/apm/shared'
import { ApmPageHeader } from '@/components/apm/ApmPageHeader'
import { ApmRangePicker, rangePhrase, useApmRange } from '@/components/apm/ApmRange'
import type { ServiceMap } from '@/types/apm'

function Legend() {
  const items: [string, string][] = [
    [HEALTH_COLOR.healthy, 'Healthy'],
    [HEALTH_COLOR.degraded, 'Degraded'],
    [HEALTH_COLOR.critical, 'Critical'],
    [HEALTH_COLOR.no_data, 'No inbound RED'],
  ]
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-muted">
      {items.map(([c, label]) => (
        <span key={label} className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ backgroundColor: c }} />
          {label}
        </span>
      ))}
      <span className="text-border">|</span>
      <span>Node size = throughput</span>
      <span>Edge width = call volume</span>
      <span>Edge colour = error rate</span>
    </div>
  )
}

export function ServiceMapPage() {
  const navigate = useNavigate()
  const [range, setRange] = useApmRange('1h')

  const q = useQuery<ServiceMap>({
    queryKey: ['apm', 'service-map', { range }],
    queryFn: async () => (await api.get(`/apm/service-map?range=${range}`)).data,
    refetchInterval: 15_000,
  })

  const option = useMemo(() => {
    const nodes = q.data?.nodes ?? []
    const edges = q.data?.edges ?? []
    const maxRps = Math.max(...nodes.map((n) => n.rps), 0.0001)
    const maxCalls = Math.max(...edges.map((e) => e.calls), 1)
    return {
      tooltip: {
        backgroundColor: '#0d121b',
        borderColor: '#1e293b',
        textStyle: { color: '#e5e7eb', fontSize: 12 },
        formatter: (p: any) => {
          if (p.dataType === 'edge') {
            return `<b>${p.data.source} → ${p.data.target}</b><br/>`
              + `calls: ${p.data.calls.toLocaleString()}<br/>`
              + `errors: ${(p.data.error_rate * 100).toFixed(2)}%<br/>`
              + `avg latency: ${p.data.avg_ms} ms`
          }
          return `<b>${p.data.name}</b><br/>`
            + `health: ${p.data.health}<br/>`
            + `throughput: ${fmtRps(p.data.rps)}<br/>`
            + `errors: ${(p.data.error_rate * 100).toFixed(2)}%<br/>`
            + `p95: ${fmtMs(p.data.p95_ms)}`
        },
      },
      series: [{
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        force: { repulsion: 340, edgeLength: 160, gravity: 0.08 },
        label: { show: true, color: '#e5e7eb', fontSize: 11, position: 'bottom' },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: 8,
        emphasis: { focus: 'adjacency', lineStyle: { opacity: 1 } },
        data: nodes.map((n) => ({
          name: n.name, health: n.health, rps: n.rps, error_rate: n.error_rate, p95_ms: n.p95_ms,
          symbolSize: 26 + (n.rps / maxRps) * 36,
          itemStyle: { color: HEALTH_COLOR[(n.health as keyof typeof HEALTH_COLOR)] || HEALTH_COLOR.no_data },
        })),
        links: edges.map((e) => ({
          source: e.client, target: e.server, calls: e.calls,
          error_rate: e.error_rate, avg_ms: e.avg_ms,
          // Log scale: a 50k-call edge should read as heavier than a 500-call
          // one without being 100x thicker and swamping the canvas.
          lineStyle: {
            width: 1 + (Math.log2(e.calls + 1) / Math.log2(maxCalls + 1)) * 5,
            color: e.error_rate >= 0.05 ? HEALTH_COLOR.critical
              : e.error_rate >= 0.01 ? HEALTH_COLOR.degraded : '#64748b',
            curveness: 0.1, opacity: 0.75,
          },
        })),
      }],
    }
  }, [q.data])

  const nodeCount = q.data?.nodes.length ?? 0
  const edgeCount = q.data?.edges.length ?? 0

  return (
    <div className="space-y-4">
      <ApmPageHeader
        title="Service Map"
        description="Auto-derived dependency topology, built from parent/child span relationships — who calls whom, how often, and where the errors concentrate."
        article="service-map"
        actions={
          <>
            {q.isFetching && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
            <ApmRangePicker value={range} onChange={setRange} />
          </>
        }
      />

      {q.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load service map — {apiErrorMessage(q.error)}
        </div>
      )}

      <Card>
        <CardContent className="space-y-2 p-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <span className="text-xs text-muted">
              {nodeCount} service{nodeCount === 1 ? '' : 's'} · {edgeCount} dependenc{edgeCount === 1 ? 'y' : 'ies'} in {rangePhrase(range)}
            </span>
            <Legend />
          </div>
          {q.isLoading ? (
            <div className="flex items-center justify-center gap-2 py-24 text-muted">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </div>
          ) : nodeCount === 0 ? (
            <div className="flex flex-col items-center gap-2 py-24 text-center text-muted">
              <Network className="h-6 w-6" />
              <span className="text-sm">No service dependencies in {rangePhrase(range)}.</span>
              <span className="max-w-md text-xs">
                Edges appear once a traced request crosses a service boundary — one instrumented service calling another
                with trace context propagated.
              </span>
            </div>
          ) : (
            <ReactECharts
              option={option}
              style={{ height: 560 }}
              onEvents={{
                click: (p: any) => {
                  if (p.dataType === 'node') navigate(`/apm/services/${encodeURIComponent(p.data.name)}?range=${range}`)
                },
              }}
            />
          )}
        </CardContent>
      </Card>
    </div>
  )
}
