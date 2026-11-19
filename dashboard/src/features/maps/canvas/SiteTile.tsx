import { memo, useMemo, useState } from 'react'
import { Bell, Building2, ChevronDown, ChevronRight, ChevronUp, Layers } from 'lucide-react'
import { cn } from '@/lib/utils'
import { STATUS_HEX, STATUS_LABEL, statusKey, type MapShape, type SiteLive, type SiteMember } from '../core'

/* A site node: one tile that stands for many devices (a branch, a building, a
 * data-centre hall). It shows the aggregate health as a ring, the counts that
 * matter on a NOC wall (down / warning first) and the alert total; the member
 * grid opens under the tile (one cell per device, worst first) and the site's
 * own map opens with the drill-down button. Rendering stays cheap for sites
 * with thousands of devices: the tile draws one SVG ring, and the grid draws
 * plain cells only while it is open. */

const ORDER = ['down', 'degraded', 'maintenance', 'unknown', 'up'] as const
const GRID_CAP = 600

export function siteTitle(shape: MapShape, live?: SiteLive): string {
  const text = (shape.text || '').trim().split('\n')[0]
  if (text) return text
  const spec = shape.metadata?.site || {}
  return spec.location || live?.map_name || 'Site'
}

function sourceSummary(shape: MapShape, live?: SiteLive): string {
  const spec = shape.metadata?.site || {}
  const parts: string[] = []
  if (spec.location) parts.push(`Location: ${spec.location}`)
  if (spec.map_id) parts.push(live?.map_name ? `Map: ${live.map_name}` : 'Drill-down map')
  if (spec.device_ids?.length) parts.push(`${spec.device_ids.length} picked device${spec.device_ids.length > 1 ? 's' : ''}`)
  return parts.join(' · ') || 'No members yet — edit the site to choose them'
}

/** Donut ring of the status mix; the gap-free arcs read at any size. */
function HealthRing({ live, size }: { live?: SiteLive; size: number }) {
  const total = live?.total || 0
  const stroke = Math.max(4, size * 0.14)
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  let offset = 0
  const arcs = total
    ? ORDER.map((k) => {
        const n = live!.counts[k] || 0
        if (!n) return null
        const len = (n / total) * c
        const el = (
          <circle
            key={k}
            r={r}
            cx={size / 2}
            cy={size / 2}
            fill="none"
            stroke={STATUS_HEX[k]}
            strokeWidth={stroke}
            strokeDasharray={`${len} ${c - len}`}
            strokeDashoffset={-offset}
          />
        )
        offset += len
        return el
      })
    : null
  const upPct = total ? Math.round(((live!.counts.up || 0) / total) * 100) : null
  return (
    <svg width={size} height={size} className="shrink-0 -rotate-90" aria-hidden>
      <circle r={r} cx={size / 2} cy={size / 2} fill="none" stroke="rgb(var(--surface3))" strokeWidth={stroke} />
      {arcs}
      <g className="rotate-90" style={{ transformOrigin: 'center' }}>
        <text x="50%" y="47%" textAnchor="middle" dominantBaseline="middle" fill="rgb(var(--text))" fontWeight={700} fontSize={size * 0.26}>
          {upPct == null ? '—' : `${upPct}%`}
        </text>
        <text x="50%" y="70%" textAnchor="middle" dominantBaseline="middle" fill="rgb(var(--muted))" fontSize={size * 0.12} fontWeight={600}>
          UP
        </text>
      </g>
    </svg>
  )
}

function SiteTileImpl({ shape, live, w, h, selected, onDrill, onOpenMember, designMode }: {
  shape: MapShape
  live?: SiteLive
  w: number
  h: number
  selected?: boolean
  designMode: boolean
  onDrill?: (mapId: string) => void
  onOpenMember?: (m: SiteMember) => void
}) {
  const spec = shape.metadata?.site || {}
  const [open, setOpen] = useState<boolean>(!!spec.expanded && !designMode)
  const worst = statusKey(live?.worst)
  const hasProblems = !!live && ((live.counts.down || 0) + (live.counts.degraded || 0)) > 0
  const tone = !live || !live.total ? 'unknown' : worst
  const accent = STATUS_HEX[tone] || STATUS_HEX.unknown
  const title = siteTitle(shape, live)
  const drillId = live?.map_id || null
  const canDrill = !designMode && !!drillId && !!onDrill

  // Sizes follow the tile, so a resized site stays balanced.
  const base = Math.max(10, Math.min(w * 0.06, h * 0.11, 26))
  const ring = Math.max(36, Math.min(h * 0.62, w * 0.36))
  const compact = h < 90 || w < 170

  const problems = useMemo(() => (live?.members || []).filter((m) => m.status === 'down' || m.status === 'degraded').slice(0, 6), [live])

  return (
    <div className="relative h-full w-full" style={{ fontSize: base }}>
      <div
        className={cn(
          'flex h-full w-full flex-col overflow-hidden rounded-xl border-2 bg-surface/95 shadow-lg backdrop-blur',
          selected && 'ring-2 ring-primary/70 ring-offset-2 ring-offset-surface',
          canDrill && 'nodrag nopan cursor-pointer focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary',
        )}
        style={{ borderColor: `${accent}cc`, boxShadow: !designMode && hasProblems ? `0 0 0 3px ${accent}33, 0 0 24px ${accent}55` : undefined, pointerEvents: canDrill ? 'auto' : undefined }}
        role={canDrill ? 'link' : undefined}
        tabIndex={canDrill ? 0 : undefined}
        aria-label={canDrill ? `Open ${title} site map` : undefined}
        onClick={canDrill ? (e) => { e.stopPropagation(); onDrill!(drillId!) } : undefined}
        onKeyDown={canDrill ? (e) => {
          if (e.target !== e.currentTarget || (e.key !== 'Enter' && e.key !== ' ')) return
          e.preventDefault()
          e.stopPropagation()
          onDrill!(drillId!)
        } : undefined}
      >
        {/* Header */}
        <div className="flex items-center gap-[0.4em] px-[0.6em] py-[0.35em]" style={{ background: `linear-gradient(90deg, ${accent}2e, transparent 75%)` }}>
          <Building2 className="shrink-0 text-text2" style={{ width: '1.1em', height: '1.1em' }} />
          <span className="min-w-0 flex-1 truncate font-bold text-text" title={title}>{title}</span>
          {live && live.alerts.active > 0 && (
            <span
              className={cn('flex shrink-0 items-center gap-[0.2em] rounded-full px-[0.45em] font-mono font-bold leading-[1.5em] text-white', live.alerts.critical > 0 ? 'bg-danger' : 'bg-warning')}
              style={{ fontSize: '0.8em' }}
              title={`${live.alerts.active} active alerts${live.alerts.critical ? ` · ${live.alerts.critical} critical` : ''}`}
            >
              <Bell style={{ width: '0.9em', height: '0.9em' }} strokeWidth={3} />{live.alerts.active > 999 ? '999+' : live.alerts.active}
            </span>
          )}
          {drillId && onDrill && (
            <button
              type="button"
              className="nodrag nopan flex shrink-0 items-center gap-[0.15em] rounded-md border border-border bg-surface px-[0.4em] font-semibold leading-[1.6em] text-text2 hover:border-primary/60 hover:text-text"
              style={{ fontSize: '0.8em' }}
              title={`Open ${live?.map_name || 'the site map'}`}
              onClick={(e) => { e.stopPropagation(); onDrill(drillId) }}
            >
              Open <ChevronRight style={{ width: '1em', height: '1em' }} />
            </button>
          )}
        </div>

        {/* Body: health ring + counts */}
        <div className="flex min-h-0 flex-1 items-center gap-[0.7em] px-[0.6em] pb-[0.35em]">
          {!compact && <HealthRing live={live} size={ring} />}
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline gap-[0.3em]">
              <span className="font-mono font-bold text-text" style={{ fontSize: '1.45em', lineHeight: 1.1 }}>{live ? live.total : '…'}</span>
              <span className="text-muted" style={{ fontSize: '0.8em' }}>{live?.total === 1 ? 'device' : 'devices'}</span>
            </div>
            <div className="mt-[0.2em] grid grid-cols-2 gap-x-[0.6em] gap-y-[0.1em]" style={{ fontSize: '0.82em' }}>
              {ORDER.filter((k) => k !== 'up' || !compact).map((k) => {
                const n = live?.counts[k] || 0
                if (!n && k !== 'down' && k !== 'up') return null
                return (
                  <span key={k} className="flex items-center gap-[0.3em] whitespace-nowrap">
                    <span className="inline-block shrink-0 rounded-full" style={{ width: '0.6em', height: '0.6em', background: STATUS_HEX[k] }} />
                    <span className={cn('font-mono font-bold', n && k === 'down' ? 'text-danger' : n && k === 'degraded' ? 'text-warning' : 'text-text2')}>{n}</span>
                    <span className="truncate text-muted">{STATUS_LABEL[k]}</span>
                  </span>
                )
              })}
            </div>
          </div>
        </div>

        {/* Footer: membership + grid toggle */}
        {h >= 110 && (
          <div className="flex items-center gap-[0.4em] border-t border-border/70 px-[0.6em] py-[0.25em] text-muted" style={{ fontSize: '0.72em' }}>
            <Layers className="shrink-0" style={{ width: '1.1em', height: '1.1em' }} />
            <span className="min-w-0 flex-1 truncate" title={sourceSummary(shape, live)}>{sourceSummary(shape, live)}</span>
            {live && live.total > 0 && (
              <button
                type="button"
                className="nodrag nopan flex shrink-0 items-center gap-[0.15em] rounded px-[0.3em] font-semibold text-text2 hover:bg-primary/10 hover:text-text"
                onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
                title={open ? 'Hide the device grid' : 'Show every device in this site'}
              >
                {open ? 'Hide' : 'Devices'} {open ? <ChevronUp style={{ width: '1.1em', height: '1.1em' }} /> : <ChevronDown style={{ width: '1.1em', height: '1.1em' }} />}
              </button>
            )}
          </div>
        )}
      </div>

      {/* Expanded member grid (floats under the tile; never resizes the node) */}
      {open && live && live.total > 0 && (
        <div
          className="nodrag nopan nowheel absolute left-0 top-full z-50 mt-[0.4em] rounded-xl border border-border bg-surface/95 p-[0.5em] shadow-2xl backdrop-blur"
          style={{ width: Math.max(w, 260), fontSize: Math.max(10, base * 0.8) }}
          onClick={(e) => e.stopPropagation()}
        >
          {problems.length > 0 && (
            <div className="mb-[0.5em] space-y-[0.2em]">
              {problems.map((m) => (
                <button
                  key={`${m.kind}:${m.id}`}
                  type="button"
                  onClick={() => onOpenMember?.(m)}
                  className="flex w-full items-center gap-[0.4em] rounded px-[0.3em] text-left hover:bg-primary/10"
                >
                  <span className="inline-block shrink-0 rounded-full" style={{ width: '0.65em', height: '0.65em', background: STATUS_HEX[statusKey(m.status)] }} />
                  <span className="min-w-0 flex-1 truncate font-semibold text-text">{m.hostname || m.ip_address || m.id}</span>
                  <span className="shrink-0 font-mono text-muted">{m.ip_address}</span>
                  {m.alerts.active > 0 && <span className={cn('shrink-0 font-mono font-bold', m.alerts.critical ? 'text-danger' : 'text-warning')}>{m.alerts.active}</span>}
                </button>
              ))}
            </div>
          )}
          <div className="flex flex-wrap gap-[3px]" style={{ maxHeight: '16em', overflowY: 'auto' }}>
            {live.members.slice(0, GRID_CAP).map((m) => (
              <button
                key={`${m.kind}:${m.id}`}
                type="button"
                onClick={() => onOpenMember?.(m)}
                className="nm-site-cell rounded-[3px]"
                style={{ width: '1.05em', height: '1.05em', background: STATUS_HEX[statusKey(m.status)], outline: m.alerts.critical ? '2px solid rgb(var(--danger))' : undefined }}
                title={`${m.hostname || m.id}${m.ip_address ? ` · ${m.ip_address}` : ''} · ${STATUS_LABEL[statusKey(m.status)]}${m.alerts.active ? ` · ${m.alerts.active} alert${m.alerts.active > 1 ? 's' : ''}` : ''}`}
              />
            ))}
          </div>
          {(live.total > GRID_CAP || live.truncated) && (
            <div className="mt-[0.4em] text-muted">Showing the {Math.min(GRID_CAP, live.members.length)} most urgent of {live.total} devices{drillId ? ' — open the site map for all of them' : ''}.</div>
          )}
        </div>
      )}
    </div>
  )
}

export const SiteTile = memo(SiteTileImpl)
