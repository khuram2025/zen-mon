import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, Bell, Cable, Cpu, ExternalLink, MemoryStick, Thermometer, Timer, X } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { NetworkIcon } from '@/components/network-icons'
import {
  STATUS_HEX, STATUS_LABEL, entityHref, formatAgo, formatBps, formatUptime, iconForNode, linkFlow, nodeKindOf, statusKey, utilHex,
  type LiveLinkData, type ManualMapNode, type NodeLiveData,
} from '../core'

export type InspectorLink = { id: string; peer: string; srcIf?: string | null; dstIf?: string | null; speed?: string | null }

type AlertRow = { id: string; severity: string; message: string; triggered_at: string }

/* Live-mode side panel for one device, server or check: status, health
 * gauges, every cable it terminates with current throughput and utilisation,
 * and its newest active alerts. Replaces the pinned hover card. */
export function NodeInspector({ node, nodeLive, links, liveData, onClose, onFocusLink }: {
  node: ManualMapNode
  nodeLive?: NodeLiveData
  links: InspectorLink[]
  liveData: Record<string, LiveLinkData>
  onClose: () => void
  onFocusLink: (id: string) => void
}) {
  const sk = statusKey(nodeLive?.status ?? node.status)
  const kind = nodeKindOf(node)
  const entityParam = kind === 'server' ? 'server_id' : kind === 'service_check' ? 'service_check_id' : 'device_id'
  const entityId = node.device_id || node.server_id || node.service_check_id
  const alertsQ = useQuery<{ data: AlertRow[] }>({
    queryKey: ['map-inspector-alerts', entityParam, entityId],
    enabled: !!entityId && (nodeLive?.alerts.active || 0) > 0,
    refetchInterval: 30_000,
    retry: false,
    queryFn: async () => {
      try {
        return (await api.get('/alerts', { params: { [entityParam]: entityId, status: 'active', limit: 5, sort: 'newest' } })).data
      } catch {
        return { data: [] }
      }
    },
  })

  const rows = useMemo(() => links.map((l) => {
    const flow = linkFlow(liveData[l.id])
    return { ...l, flow }
  }).sort((a, b) => (b.flow?.ifaceDown ? 1 : 0) - (a.flow?.ifaceDown ? 1 : 0) || (b.flow?.total || 0) - (a.flow?.total || 0)), [links, liveData])

  const tone = STATUS_HEX[sk] || STATUS_HEX.unknown
  return (
    <div className="w-80 overflow-hidden rounded-xl border border-border bg-surface/95 shadow-2xl backdrop-blur animate-fade-in">
      <div className="flex items-start gap-2.5 border-b border-border px-3 py-2.5" style={{ background: `linear-gradient(90deg, ${tone}26, transparent 80%)` }}>
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border-2 bg-surface" style={{ borderColor: tone }}>
          {typeof node.metadata?.image_src === 'string' && node.metadata.image_src
            ? <img src={node.metadata.image_src} alt="" className="h-7 w-7 object-contain" />
            : <NetworkIcon name={iconForNode(node)} className="h-7 w-7" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-bold text-text" title={node.label || node.hostname}>{node.label || node.hostname}</div>
          <div className="truncate font-mono text-2xs text-muted">{node.ip_address}{node.vendor ? ` · ${node.vendor}` : ''}{node.model ? ` ${node.model}` : ''}</div>
          <div className="mt-1 flex items-center gap-1.5">
            <span className="rounded px-1.5 py-0.5 text-2xs font-bold uppercase leading-none text-white" style={{ background: tone }}>{STATUS_LABEL[sk] || sk}</span>
            <span className="text-2xs text-muted">seen {formatAgo(nodeLive?.last_seen ?? node.last_seen)}</span>
          </div>
        </div>
        <button type="button" onClick={onClose} className="rounded p-1 text-muted hover:bg-surface2 hover:text-text" title="Close (Esc)"><X className="h-4 w-4" /></button>
      </div>

      <div className="grid grid-cols-2 gap-1.5 p-2.5">
        <Metric icon={<Timer className="h-3.5 w-3.5" />} label="Response" value={nodeLive?.rtt_ms != null ? `${nodeLive.rtt_ms.toFixed(1)} ms` : '—'} />
        <Metric icon={<Activity className="h-3.5 w-3.5" />} label="Uptime" value={formatUptime(nodeLive?.uptime_seconds)} />
        <Metric icon={<Cpu className="h-3.5 w-3.5" />} label="CPU" value={nodeLive?.cpu_pct != null ? `${nodeLive.cpu_pct.toFixed(0)}%` : '—'} pct={nodeLive?.cpu_pct} />
        <Metric icon={<MemoryStick className="h-3.5 w-3.5" />} label="Memory" value={nodeLive?.mem_pct != null ? `${nodeLive.mem_pct.toFixed(0)}%` : '—'} pct={nodeLive?.mem_pct} />
        {nodeLive?.temperature_c != null && <Metric icon={<Thermometer className="h-3.5 w-3.5" />} label="Temperature" value={`${nodeLive.temperature_c.toFixed(1)} °C`} />}
      </div>

      {rows.length > 0 && (
        <div className="border-t border-border px-2.5 py-2">
          <div className="mb-1 flex items-center gap-1 text-2xs font-bold uppercase tracking-wider text-muted"><Cable className="h-3 w-3" /> Links ({rows.length})</div>
          <div className="max-h-48 space-y-0.5 overflow-y-auto">
            {rows.map((r) => {
              const u = r.flow?.utilPct ?? null
              const down = !!r.flow?.ifaceDown
              return (
                <button key={r.id} type="button" onClick={() => onFocusLink(r.id)} className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-xs hover:bg-primary/10">
                  <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: down ? STATUS_HEX.down : utilHex(u) }} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium text-text2">{r.peer}</span>
                    {(r.srcIf || r.dstIf) && <span className="block truncate font-mono text-2xs text-muted">{r.srcIf || '?'} ⇄ {r.dstIf || '?'}{r.speed ? ` · ${r.speed}` : ''}</span>}
                  </span>
                  <span className={cn('shrink-0 text-right font-mono text-2xs font-semibold', down ? 'text-danger' : 'text-text2')}>
                    {down ? 'DOWN' : r.flow ? formatBps(r.flow.total) : '—'}
                    {!down && u != null && <span className="block" style={{ color: utilHex(u) }}>{u.toFixed(1)}%</span>}
                  </span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {(nodeLive?.alerts.active || 0) > 0 && (
        <div className="border-t border-border px-2.5 py-2">
          <div className="mb-1 flex items-center gap-1 text-2xs font-bold uppercase tracking-wider text-muted">
            <Bell className="h-3 w-3" /> Active alerts ({nodeLive!.alerts.active}{nodeLive!.alerts.critical ? ` · ${nodeLive!.alerts.critical} critical` : ''})
          </div>
          <div className="space-y-1">
            {(alertsQ.data?.data || []).map((a) => (
              <a key={a.id} href={`/alerts/${a.id}`} target="_blank" rel="noreferrer" className="flex items-start gap-1.5 rounded px-1 py-0.5 text-xs hover:bg-primary/10">
                <span className={cn('mt-1 h-1.5 w-1.5 shrink-0 rounded-full', a.severity === 'critical' ? 'bg-danger' : a.severity === 'warning' ? 'bg-warning' : 'bg-info')} />
                <span className="min-w-0 flex-1 text-text2">{a.message}</span>
                <span className="shrink-0 text-2xs text-muted">{formatAgo(a.triggered_at)}</span>
              </a>
            ))}
            {alertsQ.isLoading && <div className="text-2xs text-muted">Loading…</div>}
          </div>
        </div>
      )}

      <div className="flex gap-1.5 border-t border-border p-2.5">
        <a href={entityHref(node)} target="_blank" rel="noreferrer" className="flex flex-1 items-center justify-center gap-1 rounded-md border border-border bg-surface px-2 py-1 text-xs font-semibold text-text2 hover:border-primary/50 hover:text-text">
          {kind === 'device' ? 'Device page' : kind === 'server' ? 'Server page' : 'Check page'} <ExternalLink className="h-3 w-3" />
        </a>
        {kind === 'device' && (
          <a href={`/alerts?device=${node.device_id}`} target="_blank" rel="noreferrer" className="flex flex-1 items-center justify-center gap-1 rounded-md border border-border bg-surface px-2 py-1 text-xs font-semibold text-text2 hover:border-primary/50 hover:text-text">
            Alerts <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </div>
    </div>
  )
}

function Metric({ icon, label, value, pct }: { icon: React.ReactNode; label: string; value: string; pct?: number | null }) {
  return (
    <div className="rounded-lg border border-border/70 bg-surface2/50 px-2 py-1.5">
      <div className="flex items-center gap-1 text-2xs font-semibold uppercase tracking-wide text-muted">{icon}{label}</div>
      <div className="font-mono text-sm font-bold text-text" style={pct != null && pct >= 60 ? { color: utilHex(pct) } : undefined}>{value}</div>
      {pct != null && (
        <div className="mt-1 h-1 overflow-hidden rounded-full bg-surface3">
          <div className="h-full rounded-full" style={{ width: `${Math.max(3, Math.min(100, pct))}%`, background: utilHex(pct) }} />
        </div>
      )}
    </div>
  )
}
