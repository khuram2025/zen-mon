/* Auto legend (F11): built from what this map actually uses — cable kinds,
 * bundles, device/icon types and statuses — plus the utilisation scale in
 * live mode. Cable samples mirror NetworkEdge: kind dash patterns, the VPN
 * accent halo and the fiber amber centre stripe on top of the status line. */

import { useMemo, useState } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { NetworkIcon, iconLabel, type IconKey } from '@/components/network-icons'
import { cn } from '@/lib/utils'
import {
  LINK_KIND_STYLE,
  iconForNode,
  statusKey,
  type AnnotationLink,
  type LinkKind,
  type ManualMapDetail,
} from '../core'

export type AutoLegendProps = {
  detail: ManualMapDetail
  live: boolean
  /** localStorage key for the collapsed state (omit = not persisted). */
  collapsedKey?: string
  /** Initial state when nothing is stored yet (design mode starts collapsed so
   *  it does not cover a hand-drawn legend). */
  defaultCollapsed?: boolean
}

const KIND_ORDER: LinkKind[] = ['ethernet', 'fiber', 'trunk', 'wireless', 'vpn', 'serial', 'manual']
const KIND_LABEL: Record<string, string> = {
  ethernet: 'Ethernet', fiber: 'Fiber', trunk: 'Trunk', wireless: 'Wireless', vpn: 'VPN', serial: 'Serial', manual: 'Manual',
}
const STATUS_ORDER = ['up', 'degraded', 'down', 'maintenance', 'unknown'] as const
const STATUS_DOT: Record<string, string> = {
  up: 'bg-success', down: 'bg-danger', degraded: 'bg-warning', maintenance: 'bg-info', unknown: 'bg-muted',
}
const STATUS_LABEL: Record<string, string> = {
  up: 'Up', degraded: 'Degraded', down: 'Down', maintenance: 'Maintenance', unknown: 'Unknown',
}

/** What the map uses, in display order (exported for tests / other panels). */
export function legendContents(detail: ManualMapDetail) {
  const annLinks: AnnotationLink[] = Array.isArray(detail.metadata?.annotation_links)
    ? (detail.metadata!.annotation_links as AnnotationLink[])
    : []
  const kinds = new Set<string>()
  let bundles = false
  const allLinks = [
    ...detail.links.map((l) => ({ kind: l.metadata?.kind || l.link_type, md: l.metadata })),
    ...annLinks.map((l) => ({ kind: l.metadata?.kind || l.link_type, md: l.metadata })),
  ]
  for (const l of allLinks) {
    const k = String(l.kind || 'ethernet').toLowerCase()
    kinds.add(LINK_KIND_STYLE[k as LinkKind] ? k : 'ethernet')
    if (Array.isArray(l.md?.members) && l.md!.members!.length > 1) bundles = true
  }

  const icons = new Set<IconKey>()
  const statuses = new Set<string>()
  for (const n of detail.nodes) {
    icons.add(iconForNode(n))
    statuses.add(statusKey(n.status))
  }
  let unmanaged = false
  for (const s of detail.shapes || []) {
    if (s.kind !== 'image') continue
    if (s.metadata?.unmanaged) unmanaged = true
    else if (s.metadata?.icon && !s.metadata?.src) icons.add(s.metadata.icon as IconKey)
  }

  return {
    kinds: KIND_ORDER.filter((k) => kinds.has(k)),
    bundles,
    icons: [...icons].filter((k) => iconLabel[k]).sort((a, b) => iconLabel[a].localeCompare(iconLabel[b])),
    unmanaged,
    statuses: STATUS_ORDER.filter((s) => statuses.has(s)),
  }
}

function readCollapsed(key?: string, fallback = false): boolean {
  if (!key) return fallback
  try {
    const v = window.localStorage.getItem(key)
    return v === null ? fallback : v === '1'
  } catch {
    return fallback
  }
}

function writeCollapsed(key: string | undefined, v: boolean) {
  if (!key) return
  try {
    window.localStorage.setItem(key, v ? '1' : '0')
  } catch {
    /* private mode / blocked storage — state just isn't remembered */
  }
}

/** Small cable sample matching NetworkEdge's rendering of a kind. */
function KindSample({ kind }: { kind: LinkKind }) {
  const ks = LINK_KIND_STYLE[kind] || {}
  const w = 2.2 * (ks.widthMul || 1)
  return (
    <svg width="30" height="10" className="shrink-0 overflow-visible">
      {ks.accent && kind !== 'fiber' && (
        <line x1="1" y1="5" x2="29" y2="5" stroke={ks.accent} strokeOpacity={0.45} strokeWidth={w + 2} strokeLinecap="round" />
      )}
      <line x1="1" y1="5" x2="29" y2="5" className="stroke-success/70" strokeWidth={w} strokeDasharray={ks.dash} strokeLinecap="round" />
      {kind === 'fiber' && (
        <line x1="1" y1="5" x2="29" y2="5" stroke={ks.accent} strokeOpacity={0.95} strokeWidth={Math.max(1, w * 0.4)} strokeLinecap="round" />
      )}
    </svg>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <div className="text-[8.5px] font-bold uppercase tracking-wider text-muted">{title}</div>
      {children}
    </div>
  )
}

export function AutoLegend({ detail, live, collapsedKey, defaultCollapsed = false }: AutoLegendProps) {
  const [collapsed, setCollapsed] = useState(() => readCollapsed(collapsedKey, defaultCollapsed))
  const c = useMemo(() => legendContents(detail), [detail])

  const toggle = () => {
    setCollapsed((v) => {
      writeCollapsed(collapsedKey, !v)
      return !v
    })
  }

  const empty = !c.kinds.length && !c.icons.length && !c.statuses.length && !c.unmanaged

  return (
    <div className="w-64 max-w-[80vw] rounded-lg border border-border bg-surface/90 shadow-lg backdrop-blur">
      <button
        type="button"
        onClick={toggle}
        className="flex w-full items-center justify-between gap-3 px-2.5 py-1 text-2xs font-bold uppercase tracking-wider text-muted transition hover:text-text"
        aria-expanded={!collapsed}
      >
        Legend {collapsed ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
      </button>
      {!collapsed && (
        <div className="flex max-h-[45vh] flex-col gap-2.5 overflow-y-auto border-t border-border/60 px-2.5 py-2">
          {empty && <div className="text-2xs text-muted">Nothing on the map yet.</div>}

          {c.statuses.length > 0 && (
            <Section title="Status">
              <div className="flex flex-wrap gap-x-2.5 gap-y-1">
                {c.statuses.map((s) => (
                  <span key={s} className="flex items-center gap-1 text-2xs text-text2">
                    <span className={cn('h-2 w-2 rounded-full', STATUS_DOT[s])} /> {STATUS_LABEL[s]}
                  </span>
                ))}
              </div>
            </Section>
          )}

          {(c.kinds.length > 0 || c.bundles) && (
            <Section title="Cables">
              <div className="grid grid-cols-2 gap-x-2 gap-y-1">
                {c.kinds.map((k) => (
                  <span key={k} className="flex items-center gap-1.5 text-2xs text-text2">
                    <KindSample kind={k} /> {KIND_LABEL[k] || k}
                  </span>
                ))}
                {c.bundles && (
                  <span className="flex items-center gap-1.5 text-2xs text-text2">
                    <span className="flex w-[30px] shrink-0 justify-center">
                      <span className="rounded-full border border-border bg-surface px-1.5 py-px font-mono text-[8.5px] font-bold leading-none text-text2">×N</span>
                    </span>
                    Bundle
                  </span>
                )}
              </div>
            </Section>
          )}

          {(c.icons.length > 0 || c.unmanaged) && (
            <Section title="Devices">
              <div className="grid grid-cols-2 gap-x-2 gap-y-1">
                {c.icons.map((k) => (
                  <span key={k} className="flex min-w-0 items-center gap-1.5 text-2xs text-text2">
                    <NetworkIcon name={k} className="h-4 w-4 shrink-0 text-text2" />
                    <span className="truncate">{iconLabel[k]}</span>
                  </span>
                ))}
                {c.unmanaged && (
                  <span className="flex min-w-0 items-center gap-1.5 text-2xs text-text2">
                    <NetworkIcon name="switch" className="h-4 w-4 shrink-0 text-muted opacity-60" />
                    <span className="truncate">Unmanaged</span>
                  </span>
                )}
              </div>
            </Section>
          )}

          {live && (
            <Section title="Live">
              <div className="flex items-center gap-1.5">
                <span className="text-2xs text-muted">Load</span>
                <div className="h-2 flex-1 rounded-full" style={{ background: 'linear-gradient(90deg, #22c55e 0%, #a3e635 35%, #f59e0b 65%, #ef4444 90%)' }} />
                <span className="font-mono text-[8.5px] text-muted">0→100%</span>
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                <span className="flex items-center gap-1.5 text-2xs text-text2">
                  <svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" className="stroke-danger" strokeWidth="2" strokeDasharray="5 3" /></svg>
                  iface down
                </span>
                <span className="flex items-center gap-1.5 text-2xs text-text2">
                  <svg width="26" height="8"><line x1="0" y1="4" x2="26" y2="4" className="stroke-muted/70" strokeWidth="2" strokeDasharray="2 4" /></svg>
                  unbound
                </span>
              </div>
            </Section>
          )}
        </div>
      )}
    </div>
  )
}
