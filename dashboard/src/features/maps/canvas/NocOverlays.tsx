import { useEffect, useMemo, useState } from 'react'
import { Activity, AlertTriangle, ChevronDown, ChevronUp, Flame } from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  STATUS_ORDER,
  formatBps,
  linkFlow,
  statusKey,
  utilHex,
  type AnnotationLink,
  type LiveLinkData,
  type ManualMapDetail,
  type NodeLiveData,
} from '../core'

function annotationLinksOf(detail: ManualMapDetail): AnnotationLink[] {
  const raw = detail.metadata?.annotation_links
  return Array.isArray(raw) ? (raw as AnnotationLink[]) : []
}

function endpointLabel(detail: ManualMapDetail, id: string, kind: 'node' | 'shape'): string {
  if (kind === 'node') {
    const n = detail.nodes.find((node) => node.id === id)
    return (n?.label || n?.hostname || '?').slice(0, 18)
  }
  const s = (detail.shapes || []).find((shape) => shape.id === id)
  return (s?.text || s?.kind || '?').slice(0, 18)
}

const STATUS_DOT: Record<string, string> = {
  up: 'bg-success', down: 'bg-danger', degraded: 'bg-warning', maintenance: 'bg-info', unknown: 'bg-muted',
}
const STATUS_TEXT: Record<string, string> = {
  up: 'text-success', down: 'text-danger', degraded: 'text-warning', maintenance: 'text-info', unknown: 'text-muted',
}

/* ── NOC status bar ─────────────────────────────────────────────────────────
 * Always-on operations summary across the top of the live canvas: fleet
 * status, aggregate traffic, hottest link, alarm totals and a live clock —
 * the numbers a wall display needs at a glance. */
export function NocStatusBar({ detail, nodesLive, liveData, updatedAt }: {
  detail: ManualMapDetail
  nodesLive: Record<string, NodeLiveData>
  liveData: Record<string, LiveLinkData>
  updatedAt: number
}) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  const stats = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const n of detail.nodes) {
      const sk = statusKey(nodesLive[n.id]?.status ?? n.status)
      counts[sk] = (counts[sk] || 0) + 1
    }
    let totalBps = 0
    let hot: { bps: number; util: number | null; label: string } | null = null
    const nodeById = new Map(detail.nodes.map((n) => [n.id, n]))
    const annLinks = annotationLinksOf(detail)
    const linkCount = detail.links.length + annLinks.length

    const considerLink = (id: string, label: string) => {
      const f = linkFlow(liveData[id])
      if (!f) return
      totalBps += f.total
      if (!hot || f.total > hot.bps) {
        hot = { bps: f.total, util: f.utilPct, label }
      }
    }

    for (const l of detail.links) {
      const s = nodeById.get(l.source_node_id)
      const t = nodeById.get(l.target_node_id)
      considerLink(
        l.id,
        `${(s?.label || s?.hostname || '?').slice(0, 18)} ⇄ ${(t?.label || t?.hostname || '?').slice(0, 18)}`,
      )
    }
    for (const al of annLinks) {
      considerLink(
        al.id,
        `${endpointLabel(detail, al.source, al.source_type)} ⇄ ${endpointLabel(detail, al.target, al.target_type)}`,
      )
    }

    let alerts = 0, critical = 0
    for (const nl of Object.values(nodesLive)) {
      alerts += nl.alerts?.active || 0
      critical += nl.alerts?.critical || 0
    }
    const faulted =
      detail.links.reduce((acc, l) => acc + (linkFlow(liveData[l.id])?.ifaceDown ? 1 : 0), 0) +
      annLinks.reduce((acc, al) => acc + (linkFlow(liveData[al.id])?.ifaceDown ? 1 : 0), 0)
    return { counts, totalBps, hot: hot && hot.bps > 0 ? hot : null, alerts, critical, faulted, linkCount }
  }, [detail, nodesLive, liveData])

  const age = Math.max(0, Math.round((now - updatedAt) / 1000))
  const clock = new Date(now).toLocaleTimeString([], { hour12: false })

  return (
    <div className="flex items-stretch gap-3 rounded-xl border border-border bg-surface/90 px-3.5 py-2 shadow-xl backdrop-blur">
      {/* LIVE beacon + clock */}
      <div className="flex flex-col justify-center pr-3">
        <div className="flex items-center gap-1.5">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-60" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-success" />
          </span>
          <span className="text-[10px] font-bold tracking-[0.18em] text-success">LIVE</span>
        </div>
        <div className="font-mono text-[15px] font-semibold leading-tight text-text">{clock}</div>
        <div className="text-[8.5px] leading-none text-muted">data {age < 2 ? 'now' : `${age}s ago`}</div>
      </div>

      <Divider />

      {/* Fleet status */}
      <div className="flex flex-col justify-center gap-1 pr-1">
        <div className="text-[8.5px] font-bold uppercase tracking-wider text-muted">Devices</div>
        <div className="flex items-center gap-2.5">
          {STATUS_ORDER.filter((s) => stats.counts[s as string]).map((s) => (
            <span key={s as string} className="flex items-center gap-1">
              <span className={cn('h-1.5 w-1.5 rounded-full', STATUS_DOT[s as string])} />
              <span className={cn('font-mono text-[13px] font-bold leading-none', STATUS_TEXT[s as string])}>{stats.counts[s as string]}</span>
            </span>
          ))}
          <span className="font-mono text-[10px] text-muted">/ {detail.nodes.length}</span>
        </div>
      </div>

      <Divider />

      {/* Aggregate traffic */}
      <div className="flex flex-col justify-center gap-0.5">
        <div className="flex items-center gap-1 text-[8.5px] font-bold uppercase tracking-wider text-muted"><Activity className="h-2.5 w-2.5" /> Traffic</div>
        <div className="font-mono text-[15px] font-bold leading-tight text-text">{formatBps(stats.totalBps)}</div>
        <div className="text-[8.5px] leading-none text-muted">{stats.linkCount} links{stats.faulted > 0 ? <span className="text-danger"> · {stats.faulted} down</span> : ''}</div>
      </div>

      {/* Busiest link */}
      {stats.hot && (
        <>
          <Divider />
          <div className="flex max-w-56 flex-col justify-center gap-0.5">
            <div className="flex items-center gap-1 text-[8.5px] font-bold uppercase tracking-wider text-muted"><Flame className="h-2.5 w-2.5" /> Busiest</div>
            <div className="truncate text-[10.5px] font-semibold leading-tight text-text2" title={stats.hot.label}>{stats.hot.label}</div>
            <div className="flex items-center gap-1.5 font-mono text-[9.5px] leading-none">
              <span className="font-bold" style={{ color: utilHex(stats.hot.util) }}>{formatBps(stats.hot.bps)}</span>
              {stats.hot.util != null && <span className="text-muted">{stats.hot.util.toFixed(1)}%</span>}
            </div>
          </div>
        </>
      )}

      <Divider />

      {/* Alerts */}
      <div className="flex flex-col justify-center gap-0.5">
        <div className="flex items-center gap-1 text-[8.5px] font-bold uppercase tracking-wider text-muted"><AlertTriangle className="h-2.5 w-2.5" /> Alerts</div>
        <div className={cn('font-mono text-[15px] font-bold leading-tight', stats.critical > 0 ? 'text-danger' : stats.alerts > 0 ? 'text-warning' : 'text-success')}>
          {stats.alerts}
        </div>
        <div className="text-[8.5px] leading-none text-muted">{stats.critical > 0 ? `${stats.critical} critical` : 'active'}</div>
      </div>
    </div>
  )
}

function Divider() {
  return <div className="w-px self-stretch bg-border/70" />
}

/* ── Legend (collapsible) ─────────────────────────────────────────────────── */
export function MapLegend() {
  const [open, setOpen] = useState(true)
  return (
    <div className="rounded-lg border border-border bg-surface/90 shadow-lg backdrop-blur">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-3 px-2.5 py-1 text-[9.5px] font-bold uppercase tracking-wider text-muted transition hover:text-text"
      >
        Legend {open ? <ChevronDown className="h-3 w-3" /> : <ChevronUp className="h-3 w-3" />}
      </button>
      {open && (
        <div className="flex items-center gap-4 border-t border-border/60 px-3 py-1.5">
          <div className="flex items-center gap-2.5">
            {(['up', 'degraded', 'down', 'maintenance', 'unknown'] as const).map((s) => (
              <span key={s} className="flex items-center gap-1 text-[9px] capitalize text-text2">
                <span className={cn('h-2 w-2 rounded-full', STATUS_DOT[s])} /> {s === 'maintenance' ? 'maint' : s}
              </span>
            ))}
          </div>
          <div className="h-4 w-px bg-border/70" />
          <div className="flex items-center gap-1.5">
            <span className="text-[9px] text-muted">Load</span>
            <div className="h-2 w-24 rounded-full" style={{ background: 'linear-gradient(90deg, #22c55e 0%, #a3e635 35%, #f59e0b 65%, #ef4444 90%)' }} />
            <span className="font-mono text-[8.5px] text-muted">0→100%</span>
          </div>
          <div className="h-4 w-px bg-border/70" />
          <span className="flex items-center gap-1.5 text-[9px] text-text2">
            <svg width="26" height="8" className="overflow-visible">
              <line x1="0" y1="4" x2="26" y2="4" className="stroke-success/60" strokeWidth="2" />
              <circle cx="6" cy="4" r="2.4" fill="#22c55e"><animate attributeName="cx" values="2;24" dur="1.6s" repeatCount="indefinite" /></circle>
            </svg>
            traffic flow
          </span>
        </div>
      )}
    </div>
  )
}
