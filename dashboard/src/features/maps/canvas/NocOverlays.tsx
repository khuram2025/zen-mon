import { regionalDate } from '@/lib/regionalTime'
import { useEffect, useMemo, useState } from 'react'
import { Activity, AlertTriangle, Building2, Cable, ChevronDown, ChevronUp, Flame, HeartPulse, Link2Off, ServerCrash, ShieldCheck, Unplug } from 'lucide-react'
import { cn } from '@/lib/utils'
import { iconLabel } from '@/components/network-icons'
import {
  STATUS_HEX,
  STATUS_LABEL,
  formatBps,
  linkFlow,
  statusKey,
  utilHex,
  type AnnotationLink,
  type LiveLinkData,
  type ManualMapDetail,
  type NodeLiveData,
  type SiteLive,
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
  return shapeName(s).slice(0, 18)
}

/** Human name for an annotation: its caption/text, an unmanaged neighbour's
 *  hostname, the icon's name ("Cloud"), else its kind. */
export function shapeName(s: { text?: string | null; kind?: string; metadata?: Record<string, any> | null } | undefined): string {
  if (!s) return '?'
  const text = (s.text || '').trim().split('\n')[0]
  if (text) return text
  const md = s.metadata || {}
  if (md.unmanaged?.hostname) return String(md.unmanaged.hostname)
  if (md.icon) return (iconLabel as Record<string, string>)[md.icon] || String(md.icon)
  return s.kind || 'Annotation'
}

const STATUS_DOT: Record<string, string> = {
  up: 'bg-success', down: 'bg-danger', degraded: 'bg-warning', maintenance: 'bg-info', unknown: 'bg-muted',
}

/* ── Map statistics ─────────────────────────────────────────────────────────
 * One pass over the map and its live feeds; drives the summary tiles on the
 * desk view and the NOC wall header. */
export type MapStats = {
  counts: Record<string, number>
  nodes: number
  healthPct: number | null
  totalBps: number
  hot: { bps: number; util: number | null; label: string } | null
  alerts: number
  critical: number
  warning: number
  faulted: number
  saturated: number
  linkCount: number
  bound: number
  sites: number
  sitesWithProblems: number
  siteDevices: number
}

export function computeMapStats(
  detail: ManualMapDetail,
  nodesLive: Record<string, NodeLiveData>,
  liveData: Record<string, LiveLinkData>,
  sitesLive: Record<string, SiteLive> = {},
): MapStats {
  const counts: Record<string, number> = {}
  for (const n of detail.nodes) {
    const sk = statusKey(nodesLive[n.id]?.status ?? n.status)
    counts[sk] = (counts[sk] || 0) + 1
  }
  let totalBps = 0
  let hot: MapStats['hot'] = null
  const nodeById = new Map(detail.nodes.map((n) => [n.id, n]))
  // Context cables (annotation cables with no port on either end) never carry
  // live data by design, so they don't count against "links bound".
  const annLinks = annotationLinksOf(detail).filter((al) => al.metadata?.src_interface || al.metadata?.dst_interface)
  const linkCount = detail.links.length + annLinks.length
  let bound = 0, faulted = 0, saturated = 0

  const considerLink = (id: string, label: string) => {
    const ld = liveData[id]
    if (ld && (ld.source.matched || ld.target.matched)) bound++
    const f = linkFlow(ld)
    if (!f) return
    if (f.ifaceDown) faulted++
    else if (f.utilPct != null && f.utilPct >= 85) saturated++
    totalBps += f.total
    if (!hot || f.total > hot.bps) hot = { bps: f.total, util: f.utilPct, label }
  }
  for (const l of detail.links) {
    const s = nodeById.get(l.source_node_id)
    const t = nodeById.get(l.target_node_id)
    considerLink(l.id, `${(s?.label || s?.hostname || '?').slice(0, 18)} ⇄ ${(t?.label || t?.hostname || '?').slice(0, 18)}`)
  }
  for (const al of annLinks) {
    considerLink(al.id, `${endpointLabel(detail, al.source, al.source_type)} ⇄ ${endpointLabel(detail, al.target, al.target_type)}`)
  }

  let alerts = 0, critical = 0, warning = 0
  for (const nl of Object.values(nodesLive)) {
    alerts += nl.alerts?.active || 0
    critical += nl.alerts?.critical || 0
    warning += nl.alerts?.warning || 0
  }
  // Sites add their members to the device and alert totals.
  let sites = 0, sitesWithProblems = 0, siteDevices = 0
  for (const sh of detail.shapes || []) {
    if (sh.metadata?.widget !== 'site') continue
    sites++
    const sl = sitesLive[sh.id]
    if (!sl) continue
    siteDevices += sl.total
    if ((sl.counts.down || 0) + (sl.counts.degraded || 0) > 0) sitesWithProblems++
    for (const k of Object.keys(sl.counts)) counts[k] = (counts[k] || 0) + (sl.counts as Record<string, number>)[k]
    alerts += sl.alerts.active
    critical += sl.alerts.critical
    warning += sl.alerts.warning
  }
  const nodes = Object.values(counts).reduce((a, b) => a + b, 0)
  const healthPct = nodes ? ((counts.up || 0) / nodes) * 100 : null
  const hotV = hot as MapStats['hot']
  return {
    counts, nodes, healthPct, totalBps, hot: hotV && hotV.bps > 0 ? hotV : null, alerts, critical, warning,
    faulted, saturated, linkCount, bound, sites, sitesWithProblems, siteDevices,
  }
}

/* ── Summary tiles ──────────────────────────────────────────────────────────
 * Always-on operations summary across the top of the live canvas: health,
 * device states, links, traffic, the busiest link, alerts and sites, plus the
 * live clock and data age. `size="wall"` draws the same tiles larger for a
 * NOC video wall; on a narrow desk screen the secondary tiles drop out. */
export function NocStatusBar({ detail, nodesLive, liveData, sitesLive, updatedAt, onOpenAlerts, size = 'desk' }: {
  detail: ManualMapDetail
  nodesLive: Record<string, NodeLiveData>
  liveData: Record<string, LiveLinkData>
  sitesLive?: Record<string, SiteLive>
  updatedAt: number
  onOpenAlerts?: () => void
  size?: 'desk' | 'wall'
}) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  const stats = useMemo(() => computeMapStats(detail, nodesLive, liveData, sitesLive), [detail, nodesLive, liveData, sitesLive])
  const age = Math.max(0, Math.round((now - updatedAt) / 1000))
  const clock = regionalDate(new Date(now), 'time', [], { hour12: false })
  const stale = age > 60
  const wall = size === 'wall'
  const health = stats.healthPct
  const healthTone = health == null ? 'text-muted' : (stats.counts.down || 0) > 0 ? 'text-danger' : (stats.counts.degraded || 0) > 0 ? 'text-warning' : 'text-success'

  return (
    <div className={cn('flex flex-wrap items-stretch', wall ? 'gap-2.5' : 'gap-1.5')}>
      {/* LIVE beacon + clock */}
      <Tile wall={wall} className="min-w-[6.5rem]">
        <div className="flex items-center gap-1.5">
          <span className="relative flex h-2 w-2">
            <span className={cn('absolute inline-flex h-full w-full animate-ping rounded-full opacity-60', stale ? 'bg-warning' : 'bg-success')} />
            <span className={cn('relative inline-flex h-2 w-2 rounded-full', stale ? 'bg-warning' : 'bg-success')} />
          </span>
          <span className={cn('text-2xs font-bold tracking-[0.18em]', stale ? 'text-warning' : 'text-success')}>{stale ? 'STALE' : 'LIVE'}</span>
        </div>
        <div className={cn('font-mono font-semibold leading-tight text-text', wall ? 'text-2xl' : 'text-base')}>{clock}</div>
        <div className="text-2xs leading-none text-muted">data {age < 2 ? 'now' : `${age}s ago`}</div>
      </Tile>

      {/* Health */}
      <Tile wall={wall} label="Health" icon={<HeartPulse className="h-3 w-3" />}>
        <div className={cn('font-mono font-bold leading-tight', healthTone, wall ? 'text-3xl' : 'text-xl')}>{health == null ? '—' : `${health >= 99.95 ? 100 : health.toFixed(1)}%`}</div>
        <StatusBar counts={stats.counts} total={stats.nodes} />
      </Tile>

      {/* Device states */}
      <Tile wall={wall} label="Devices" icon={<ServerCrash className="h-3 w-3" />}>
        <div className="flex items-baseline gap-2.5">
          <Count n={stats.counts.down || 0} label="down" tone="danger" wall={wall} />
          <Count n={stats.counts.degraded || 0} label="warn" tone="warning" wall={wall} />
          <Count n={stats.counts.up || 0} label="up" tone="success" wall={wall} />
        </div>
        <div className="text-2xs leading-none text-muted">
          {stats.nodes} total{(stats.counts.maintenance || 0) > 0 ? ` · ${stats.counts.maintenance} maint` : ''}{(stats.counts.unknown || 0) > 0 ? ` · ${stats.counts.unknown} unknown` : ''}
        </div>
      </Tile>

      {/* Links */}
      <Tile wall={wall} label="Links" icon={<Cable className="h-3 w-3" />} className="hidden md:flex">
        <div className="flex items-baseline gap-2.5">
          <Count n={stats.faulted} label="down" tone="danger" wall={wall} />
          <Count n={stats.saturated} label="≥85%" tone="warning" wall={wall} />
        </div>
        <div className="text-2xs leading-none text-muted">{stats.bound}/{stats.linkCount} with live data</div>
      </Tile>

      {/* Aggregate traffic + busiest link */}
      <Tile wall={wall} label="Traffic" icon={<Activity className="h-3 w-3" />} className="hidden lg:flex">
        <div className={cn('font-mono font-bold leading-tight text-text', wall ? 'text-2xl' : 'text-base')}>{formatBps(stats.totalBps)}</div>
        {stats.hot ? (
          <div className={cn('flex items-center gap-1 truncate text-2xs leading-none', wall ? 'max-w-[18rem]' : 'max-w-[12rem]')} title={`Busiest: ${stats.hot.label}`}>
            <Flame className="h-2.5 w-2.5 shrink-0 text-muted" />
            <span className="truncate text-text2">{stats.hot.label}</span>
            <span className="shrink-0 font-mono font-bold" style={{ color: utilHex(stats.hot.util) }}>{stats.hot.util != null ? `${stats.hot.util.toFixed(0)}%` : formatBps(stats.hot.bps)}</span>
          </div>
        ) : <div className="text-2xs leading-none text-muted">across all cables</div>}
      </Tile>

      {/* Sites */}
      {stats.sites > 0 && (
        <Tile wall={wall} label="Sites" icon={<Building2 className="h-3 w-3" />} className="hidden xl:flex">
          <div className="flex items-baseline gap-2.5">
            <Count n={stats.sitesWithProblems} label="impacted" tone="danger" wall={wall} />
            <Count n={stats.sites} label="sites" tone="muted" wall={wall} />
          </div>
          <div className="text-2xs leading-none text-muted">{stats.siteDevices} devices inside sites</div>
        </Tile>
      )}

      {/* Alerts (click → alert list) */}
      <button type="button" onClick={onOpenAlerts} className="flex text-left" title="Open active alerts">
        <Tile wall={wall} label="Alerts" icon={<AlertTriangle className="h-3 w-3" />} className={cn(onOpenAlerts && 'transition hover:border-primary/50', stats.critical > 0 && 'border-danger/60 bg-danger/10')}>
          <div className="flex items-baseline gap-2.5">
            <Count n={stats.critical} label="crit" tone="danger" wall={wall} />
            <Count n={stats.warning} label="warn" tone="warning" wall={wall} />
          </div>
          <div className="text-2xs leading-none text-muted">{stats.alerts} active</div>
        </Tile>
      </button>
    </div>
  )
}

function Tile({ label, icon, children, wall, className }: { label?: string; icon?: React.ReactNode; children: React.ReactNode; wall?: boolean; className?: string }) {
  return (
    <div className={cn(
      'flex flex-col justify-center gap-1 rounded-xl border border-border bg-surface/92 shadow-lg backdrop-blur',
      wall ? 'min-w-[9rem] px-4 py-2.5' : 'min-w-[5.5rem] px-3 py-1.5',
      className,
    )}>
      {label && <div className="flex items-center gap-1 text-2xs font-bold uppercase tracking-wider text-muted">{icon}{label}</div>}
      {children}
    </div>
  )
}

const TONE_TEXT: Record<string, string> = { danger: 'text-danger', warning: 'text-warning', success: 'text-success', muted: 'text-text2' }

function Count({ n, label, tone, wall }: { n: number; label: string; tone: 'danger' | 'warning' | 'success' | 'muted'; wall?: boolean }) {
  return (
    <span className="flex items-baseline gap-0.5">
      <span className={cn('font-mono font-bold leading-tight', wall ? 'text-2xl' : 'text-base', n > 0 || tone === 'muted' ? TONE_TEXT[tone] : 'text-muted')}>{n}</span>
      <span className="text-2xs text-muted">{label}</span>
    </span>
  )
}

/** One bar split by status (down → up), the at-a-glance mix. */
export function StatusBar({ counts, total, className }: { counts: Record<string, number>; total: number; className?: string }) {
  return (
    <div className={cn('flex h-1.5 w-full min-w-[4rem] overflow-hidden rounded-full bg-surface3', className)}>
      {total > 0 && (['down', 'degraded', 'maintenance', 'unknown', 'up'] as const).map((k) => {
        const n = counts[k] || 0
        return n ? <div key={k} style={{ width: `${(n / total) * 100}%`, background: STATUS_HEX[k] }} title={`${n} ${STATUS_LABEL[k].toLowerCase()}`} /> : null
      })}
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

export function computeProblems(detail: ManualMapDetail, nodesLive: Record<string, NodeLiveData>, liveData: Record<string, LiveLinkData>, sitesLive: Record<string, SiteLive> = {}): Problem[] {
  const out: Problem[] = []
  for (const sh of detail.shapes || []) {
    const sl = sh.metadata?.widget === 'site' ? sitesLive[sh.id] : undefined
    if (!sl) continue
    const name = shapeName(sh) === 'rectangle' ? (sh.metadata?.site?.location || 'Site') : shapeName(sh)
    const down = sl.counts.down || 0, warn = sl.counts.degraded || 0
    if (down) out.push({ id: `site-${sh.id}`, kind: 'device', severity: 'critical', title: name, detail: `Site · ${down} of ${sl.total} devices down${warn ? ` · ${warn} warning` : ''}`, targetId: sh.id })
    else if (warn) out.push({ id: `site-${sh.id}`, kind: 'device', severity: 'warning', title: name, detail: `Site · ${warn} of ${sl.total} devices warning`, targetId: sh.id })
  }
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
  const consider = (id: string, label: string, intentionallyUnbound = false) => {
    const ld = liveData[id]
    if (!ld) return
    // A cable to an annotation with no port typed on either end is drawn for
    // context (an iLO, a cloud) — it has no live data by design, not a problem.
    if (intentionallyUnbound && !ld.source.matched && !ld.target.matched) return
    const f = linkFlow(ld)
    if (f?.ifaceDown) out.push({ id: `lnk-${id}`, kind: 'link', severity: 'critical', title: label, detail: `Interface down (${f.srcDown ? ld.source.if_name || 'A-end' : ld.target.if_name || 'B-end'})`, targetId: id })
    else if (f?.utilPct != null && f.utilPct >= 85) out.push({ id: `hot-${id}`, kind: 'link', severity: 'critical', title: label, detail: `Saturated · ${f.utilPct.toFixed(0)}% · ${formatBps(f.total)}`, targetId: id })
    else if (f?.utilPct != null && f.utilPct >= 60) out.push({ id: `hot-${id}`, kind: 'link', severity: 'warning', title: label, detail: `High load · ${f.utilPct.toFixed(0)}% · ${formatBps(f.total)}`, targetId: id })
    else if (!ld.source.matched && !ld.target.matched) out.push({ id: `unb-${id}`, kind: 'link', severity: 'info', title: label, detail: 'No interface bound — no live data', targetId: id })
  }
  for (const l of detail.links) consider(l.id, linkLabel(l.source_node_id, l.target_node_id))
  const shapeIds = new Set((detail.shapes || []).map((sh) => sh.id))
  for (const al of annLinks) {
    // Skip cables whose endpoint was deleted (older maps kept them in metadata).
    if (!(nodeById.has(al.source) || shapeIds.has(al.source)) || !(nodeById.has(al.target) || shapeIds.has(al.target))) continue
    const noPorts = !al.metadata?.src_interface && !al.metadata?.dst_interface
    consider(al.id, linkLabel(al.source, al.target, al.source_type, al.target_type), noPorts)
  }
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
        className="flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-2xs font-bold uppercase tracking-wider text-muted transition hover:text-text"
      >
        <span className="flex items-center gap-1.5">
          {allClear ? <ShieldCheck className="h-3.5 w-3.5 text-success" /> : <AlertTriangle className={cn('h-3.5 w-3.5', crit ? 'text-danger' : 'text-warning')} />}
          {allClear ? 'All clear' : 'Attention'}
        </span>
        <span className="flex items-center gap-1.5 font-mono text-2xs">
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
                <span className="block truncate text-xs font-semibold text-text">{p.title}</span>
                <span className="block truncate text-2xs text-muted">{p.detail}</span>
              </span>
            </button>
          ))}
        </div>
      )}
      {open && allClear && (
        <div className="border-t border-border/60 px-2.5 py-2 text-2xs text-muted">Every device is up, no link is faulted or saturated.</div>
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
        className="flex w-full items-center justify-between gap-3 px-2.5 py-1 text-2xs font-bold uppercase tracking-wider text-muted transition hover:text-text"
      >
        Legend {open ? <ChevronDown className="h-3 w-3" /> : <ChevronUp className="h-3 w-3" />}
      </button>
      {open && (
        <div className="flex flex-wrap items-center gap-4 border-t border-border/60 px-3 py-1.5">
          <div className="flex items-center gap-2.5">
            {(['up', 'degraded', 'down', 'maintenance', 'unknown'] as const).map((s) => (
              <span key={s} className="flex items-center gap-1 text-2xs capitalize text-text2">
                <span className={cn('h-2 w-2 rounded-full', STATUS_DOT[s])} /> {s === 'maintenance' ? 'maint' : s}
              </span>
            ))}
          </div>
          <div className="h-4 w-px bg-border/70" />
          <div className="flex items-center gap-1.5">
            <span className="text-2xs text-muted">Load</span>
            <div className="h-2 w-24 rounded-full" style={{ background: 'linear-gradient(90deg, #22c55e 0%, #a3e635 35%, #f59e0b 65%, #ef4444 90%)' }} />
            <span className="font-mono text-[8.5px] text-muted">0→100%</span>
          </div>
          <div className="h-4 w-px bg-border/70" />
          <span className="flex items-center gap-1.5 text-2xs text-text2">
            <svg width="26" height="8" className="overflow-visible">
              <line x1="0" y1="4" x2="26" y2="4" className="stroke-success/60" strokeWidth="2" />
              <circle cx="6" cy="4" r="2.4" fill="#22c55e"><animate attributeName="cx" values="2;24" dur="1.6s" repeatCount="indefinite" /></circle>
            </svg>
            traffic
          </span>
          <span className="flex items-center gap-1.5 text-2xs text-text2">
            <svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" className="stroke-danger" strokeWidth="2" strokeDasharray="5 3" /></svg>
            iface down
          </span>
          <span className="flex items-center gap-1.5 text-2xs text-text2">
            <svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" className="stroke-muted/70" strokeWidth="2" strokeDasharray="2 4" /></svg>
            unbound
          </span>
        </div>
      )}
    </div>
  )
}
