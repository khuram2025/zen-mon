import { useEffect, useMemo, useState } from 'react'
import { Activity, AlertTriangle, ChevronDown, ChevronUp, Flame, Link2Off, ServerCrash, ShieldCheck, Unplug } from 'lucide-react'
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
export function NocStatusBar({ detail, nodesLive, liveData, updatedAt, onOpenAlerts }: {
  detail: ManualMapDetail
  nodesLive: Record<string, NodeLiveData>
  liveData: Record<string, LiveLinkData>
  updatedAt: number
  onOpenAlerts?: () => void
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
    let bound = 0

    const considerLink = (id: string, label: string) => {
      const ld = liveData[id]
      if (ld && (ld.source.matched || ld.target.matched)) bound++
      const f = linkFlow(ld)
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
    return { counts, totalBps, hot: hot && hot.bps > 0 ? hot : null, alerts, critical, faulted, linkCount, bound }
  }, [detail, nodesLive, liveData])

  const age = Math.max(0, Math.round((now - updatedAt) / 1000))
  const clock = new Date(now).toLocaleTimeString([], { hour12: false })
  const stale = age > 60

  return (
    <div className="flex items-stretch gap-3 rounded-xl border border-border bg-surface/90 px-3.5 py-2 shadow-xl backdrop-blur">
      {/* LIVE beacon + clock */}
      <div className="flex flex-col justify-center pr-3">
        <div className="flex items-center gap-1.5">
          <span className="relative flex h-2 w-2">
            <span className={cn('absolute inline-flex h-full w-full animate-ping rounded-full opacity-60', stale ? 'bg-warning' : 'bg-success')} />
            <span className={cn('relative inline-flex h-2 w-2 rounded-full', stale ? 'bg-warning' : 'bg-success')} />
          </span>
          <span className={cn('text-[10px] font-bold tracking-[0.18em]', stale ? 'text-warning' : 'text-success')}>{stale ? 'STALE' : 'LIVE'}</span>
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
            <span key={s as string} className="flex items-center gap-1" title={String(s)}>
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
        <div className="text-[8.5px] leading-none text-muted">
          {stats.bound}/{stats.linkCount} links bound
          {stats.faulted > 0 ? <span className="text-danger"> · {stats.faulted} down</span> : ''}
        </div>
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

      {/* Alerts (click → alert list) */}
      <button
        type="button"
        onClick={onOpenAlerts}
        className={cn('flex flex-col justify-center gap-0.5 rounded-md px-1 text-left transition', onOpenAlerts && 'hover:bg-primary/10')}
        title="Open active alerts"
      >
        <div className="flex items-center gap-1 text-[8.5px] font-bold uppercase tracking-wider text-muted"><AlertTriangle className="h-2.5 w-2.5" /> Alerts</div>
        <div className={cn('font-mono text-[15px] font-bold leading-tight', stats.critical > 0 ? 'text-danger' : stats.alerts > 0 ? 'text-warning' : 'text-success')}>
          {stats.alerts}
        </div>
        <div className="text-[8.5px] leading-none text-muted">{stats.critical > 0 ? `${stats.critical} critical` : 'active'}</div>
      </button>
    </div>
  )
}

function Divider() {
  return <div className="w-px self-stretch bg-border/70" />
}

/* ── Problems panel ─────────────────────────────────────────────────────────
 * The "what's wrong right now" list for a wall display: devices that are
 * down/degraded, links whose interface is down, links running hot, and links
 * with no interface bound (so the operator knows why a cable shows nothing).
 * Rows are clickable → the canvas centres on the item. */
export type Problem = {
  id: string
  kind: 'device' | 'link'
  severity: 'critical' | 'warning' | 'info'
  title: string
  detail: string
  /** Node id (device) or link id → the canvas resolves the position. */
  targetId: string
}

export function computeProblems(detail: ManualMapDetail, nodesLive: Record<string, NodeLiveData>, liveData: Record<string, LiveLinkData>): Problem[] {
  const out: Problem[] = []
  const nodeById = new Map(detail.nodes.map((n) => [n.id, n]))
  for (const n of detail.nodes) {
    const nl = nodesLive[n.id]
    const sk = statusKey(nl?.status ?? n.status)
    const name = n.label || n.hostname
    if (sk === 'down') out.push({ id: `dev-${n.id}`, kind: 'device', severity: 'critical', title: name, detail: `Device down · ${n.ip_address}`, targetId: n.id })
    else if (sk === 'degraded') out.push({ id: `dev-${n.id}`, kind: 'device', severity: 'warning', title: name, detail: `Degraded · ${n.ip_address}`, targetId: n.id })
    if (nl && nl.alerts.critical > 0 && sk !== 'down') out.push({ id: `alt-${n.id}`, kind: 'device', severity: 'critical', title: name, detail: `${nl.alerts.critical} critical alert${nl.alerts.critical > 1 ? 's' : ''}`, targetId: n.id })
    if (nl?.cpu_pct != null && nl.cpu_pct >= 90) out.push({ id: `cpu-${n.id}`, kind: 'device', severity: 'warning', title: name, detail: `CPU ${Math.round(nl.cpu_pct)}%`, targetId: n.id })
  }
  const annLinks = annotationLinksOf(detail)
  const linkLabel = (sId: string, tId: string, sKind: 'node' | 'shape' = 'node', tKind: 'node' | 'shape' = 'node') =>
    `${sKind === 'node' ? (nodeById.get(sId)?.label || nodeById.get(sId)?.hostname || '?') : endpointLabel(detail, sId, 'shape')} ⇄ ${tKind === 'node' ? (nodeById.get(tId)?.label || nodeById.get(tId)?.hostname || '?') : endpointLabel(detail, tId, 'shape')}`
  const consider = (id: string, label: string) => {
    const ld = liveData[id]
    if (!ld) return
    const f = linkFlow(ld)
    if (f?.ifaceDown) out.push({ id: `lnk-${id}`, kind: 'link', severity: 'critical', title: label, detail: `Interface down (${f.srcDown ? ld.source.if_name || 'A-end' : ld.target.if_name || 'B-end'})`, targetId: id })
    else if (f?.utilPct != null && f.utilPct >= 85) out.push({ id: `hot-${id}`, kind: 'link', severity: 'critical', title: label, detail: `Saturated · ${f.utilPct.toFixed(0)}% · ${formatBps(f.total)}`, targetId: id })
    else if (f?.utilPct != null && f.utilPct >= 60) out.push({ id: `hot-${id}`, kind: 'link', severity: 'warning', title: label, detail: `High load · ${f.utilPct.toFixed(0)}% · ${formatBps(f.total)}`, targetId: id })
    else if (!ld.source.matched && !ld.target.matched) out.push({ id: `unb-${id}`, kind: 'link', severity: 'info', title: label, detail: 'No interface bound — no live data', targetId: id })
  }
  for (const l of detail.links) consider(l.id, linkLabel(l.source_node_id, l.target_node_id))
  for (const al of annLinks) consider(al.id, linkLabel(al.source, al.target, al.source_type, al.target_type))
  const rank = { critical: 0, warning: 1, info: 2 }
  return out.sort((a, b) => rank[a.severity] - rank[b.severity])
}

export function ProblemsPanel({ problems, onFocus }: { problems: Problem[]; onFocus: (p: Problem) => void }) {
  const [open, setOpen] = useState(true)
  const crit = problems.filter((p) => p.severity === 'critical').length
  const warn = problems.filter((p) => p.severity === 'warning').length
  const info = problems.length - crit - warn
  const allClear = problems.length === 0
  return (
    <div className="w-72 max-w-[80vw] rounded-lg border border-border bg-surface/92 shadow-xl backdrop-blur">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-[10px] font-bold uppercase tracking-wider text-muted transition hover:text-text"
      >
        <span className="flex items-center gap-1.5">
          {allClear ? <ShieldCheck className="h-3.5 w-3.5 text-success" /> : <AlertTriangle className={cn('h-3.5 w-3.5', crit ? 'text-danger' : 'text-warning')} />}
          {allClear ? 'All clear' : 'Attention'}
        </span>
        <span className="flex items-center gap-1.5 font-mono text-[10px]">
          {crit > 0 && <span className="rounded bg-danger/15 px-1 text-danger">{crit}</span>}
          {warn > 0 && <span className="rounded bg-warning/15 px-1 text-warning">{warn}</span>}
          {info > 0 && <span className="rounded bg-surface2 px-1 text-muted">{info}</span>}
          {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        </span>
      </button>
      {open && !allClear && (
        <div className="max-h-[38vh] overflow-y-auto border-t border-border/60 py-1">
          {problems.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => onFocus(p)}
              className="flex w-full items-start gap-2 px-2.5 py-1.5 text-left transition hover:bg-primary/10"
              title="Locate on map"
            >
              <span className={cn('mt-0.5 shrink-0', p.severity === 'critical' ? 'text-danger' : p.severity === 'warning' ? 'text-warning' : 'text-muted')}>
                {p.kind === 'device' ? <ServerCrash className="h-3.5 w-3.5" /> : p.severity === 'info' ? <Unplug className="h-3.5 w-3.5" /> : <Link2Off className="h-3.5 w-3.5" />}
              </span>
              <span className="min-w-0">
                <span className="block truncate text-[11px] font-semibold text-text">{p.title}</span>
                <span className="block truncate text-[9.5px] text-muted">{p.detail}</span>
              </span>
            </button>
          ))}
        </div>
      )}
      {open && allClear && (
        <div className="border-t border-border/60 px-2.5 py-2 text-[10px] text-muted">Every device is up, no link is faulted or saturated.</div>
      )}
    </div>
  )
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
        <div className="flex flex-wrap items-center gap-4 border-t border-border/60 px-3 py-1.5">
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
            traffic
          </span>
          <span className="flex items-center gap-1.5 text-[9px] text-text2">
            <svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" className="stroke-danger" strokeWidth="2" strokeDasharray="5 3" /></svg>
            iface down
          </span>
          <span className="flex items-center gap-1.5 text-[9px] text-text2">
            <svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" className="stroke-muted/70" strokeWidth="2" strokeDasharray="2 4" /></svg>
            unbound
          </span>
        </div>
      )}
    </div>
  )
}
