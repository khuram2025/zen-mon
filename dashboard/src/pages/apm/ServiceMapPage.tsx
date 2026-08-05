import { useMemo } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import { Loader2, Network } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { HEALTH_COLOR, fmtMs, fmtPct } from '@/components/apm/shared'

interface MapNode { name: string; health: string; rps: number; error_rate: number; p95_ms: number }
interface MapEdge { client: string; server: string; calls: number; error_rate: number; p95_ms: number }
interface ServiceMap { nodes: MapNode[]; edges: MapEdge[] }

const RANGES = ['15m', '1h', '6h', '24h']

export function ServiceMapPage() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const range = params.get('range') || '1h'
  const set = (k: string, v: string) => { const n = new URLSearchParams(params); n.set(k, v); setParams(n, { replace: true }) }

  const q = useQuery<ServiceMap>({
    queryKey: ['apm', 'service-map', { range }],
    queryFn: async () => (await api.get(`/apm/service-map?range=${range}`)).data,
    refetchInterval: 15000,
  })

  const option = useMemo(() => {
    const nodes = q.data?.nodes ?? []
    const edges = q.data?.edges ?? []
    const maxRps = Math.max(...nodes.map((n) => n.rps), 0.0001)
    return {
      tooltip: {
        backgroundColor: '#0d121b',
        borderColor: '#1e293b',
        textStyle: { color: '#e5e7eb', fontSize: 12 },
        formatter: (p: any) => {
          if (p.dataType === 'edge') return `${p.data.source} → ${p.data.target}<br/>calls: ${p.data.calls}<br/>err: ${(p.data.error_rate * 100).toFixed(1)}%<br/>p95: ${p.data.p95_ms} ms`
          return `<b>${p.data.name}</b><br/>health: ${p.data.health}<br/>rps: ${p.data.rps}<br/>err: ${(p.data.error_rate * 100).toFixed(1)}%<br/>p95: ${p.data.p95_ms} ms`
        },
      },
      series: [{
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        force: { repulsion: 320, edgeLength: 150, gravity: 0.08 },
        label: { show: true, color: '#e5e7eb', fontSize: 11, position: 'bottom' },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: 8,
        emphasis: { focus: 'adjacency' },
        data: nodes.map((n) => ({
          name: n.name, health: n.health, rps: n.rps, error_rate: n.error_rate, p95_ms: n.p95_ms,
          symbolSize: 28 + (n.rps / maxRps) * 34,
          itemStyle: { color: HEALTH_COLOR[(n.health as keyof typeof HEALTH_COLOR)] || HEALTH_COLOR.no_data },
        })),
        links: edges.map((e) => ({
          source: e.client, target: e.server, calls: e.calls, error_rate: e.error_rate, p95_ms: e.p95_ms,
          lineStyle: {
            width: Math.min(1 + Math.log2(e.calls + 1), 6),
            color: e.error_rate >= 0.05 ? HEALTH_COLOR.critical : e.error_rate >= 0.01 ? HEALTH_COLOR.degraded : '#6b7280',
            curveness: 0.1, opacity: 0.8,
          },
        })),
      }],
    }
  }, [q.data])

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-semibold text-text">Service Map</h1>
        <div className="flex-1" />
        <select value={range} onChange={(e) => set('range', e.target.value)}
          className="h-9 rounded-md bg-surface2 border border-border text-sm px-2 text-text">
          {RANGES.map((r) => <option key={r} value={r}>Last {r}</option>)}
        </select>
        {q.isFetching && <Loader2 className="w-4 h-4 animate-spin text-muted" />}
      </div>
      <p className="text-sm text-muted">
        Auto-derived dependency topology — nodes are services (color = health, size = throughput), edges are calls (width = volume, color = error rate).
      </p>

      {q.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load service map — {apiErrorMessage(q.error)}
        </div>
      )}

      <Card>
        <CardContent className="p-2">
          {q.isLoading ? (
            <div className="flex items-center justify-center gap-2 text-muted py-24"><Loader2 className="w-4 h-4 animate-spin" /> Loading…</div>
          ) : (q.data?.nodes.length ?? 0) === 0 ? (
            <div className="flex flex-col items-center gap-2 text-muted py-24"><Network className="w-6 h-6" /> No service dependencies yet.</div>
          ) : (
            <ReactECharts
              option={option}
              style={{ height: 560 }}
              onEvents={{ click: (p: any) => { if (p.dataType === 'node') navigate(`/apm/services/${encodeURIComponent(p.data.name)}`) } }}
            />
          )}
        </CardContent>
      </Card>
    </div>
  )
}
