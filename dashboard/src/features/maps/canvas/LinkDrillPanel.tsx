/* Live link drill-down (F7): side panel for one cable in NOC mode — both
 * ends (port, oper/admin, speed, in/out, utilisation), the cable's kind /
 * speed / notes, bundle members, and a 24 h in/out sparkline per matched end
 * from the interface metrics endpoint. Ends without a bound interface say so
 * instead of showing zeros. */

import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, ExternalLink, Layers, Loader2, Unplug, X } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { formatBps, utilHex, type LinkMetadata, type LiveInterface, type LiveLinkData } from '../core'

export type LinkDrillPanelProps = {
  /** A device link or an annotation link — only id/label/link_type/metadata are read. */
  link: { id: string; label?: string | null; link_type?: string | null; metadata?: LinkMetadata | null }
  live?: LiveLinkData
  sourceName: string
  targetName: string
  /** Device ids of the ends (null/undefined for shapes, servers, checks). */
  sourceDeviceId?: string | null
  targetDeviceId?: string | null
  onClose: () => void
}

type TrafficPoint = { ts: number | string; in_bps: number; out_bps: number }

const IN_COLOR = '#22c55e'
const OUT_COLOR = '#6366f1'

function useIfaceTraffic(deviceId: string | null | undefined, ifIndex: number | null | undefined) {
  return useQuery<{ traffic: TrafficPoint[] }>({
    queryKey: ['map-link-drill-traffic', deviceId, ifIndex],
    enabled: !!deviceId && ifIndex != null,
    staleTime: 60_000,
    refetchInterval: 300_000,
    retry: false,
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces/${ifIndex}/metrics`, { params: { hours: 24 } })).data,
  })
}

/** Interface speed (bps) → "1G" / "100M" / "10G". */
function speedLabel(bps: number | null | undefined): string | null {
  if (!bps || bps <= 0) return null
  if (bps >= 1e9) return `${+(bps / 1e9).toFixed(bps % 1e9 ? 1 : 0)}G`
  if (bps >= 1e6) return `${+(bps / 1e6).toFixed(bps % 1e6 ? 1 : 0)}M`
  return `${Math.round(bps / 1e3)}K`
}

function StatusPill({ label, value }: { label: string; value?: string | null }) {
  const v = (value || '').toLowerCase()
  const cls = !v ? 'bg-surface2 text-muted' : v === 'up' ? 'bg-success/15 text-success' : v === 'down' ? 'bg-danger/15 text-danger' : 'bg-warning/15 text-warning'
  return (
    <span className={cn('rounded px-1.5 py-px text-2xs font-semibold', cls)}>
      {label} {value || '—'}
    </span>
  )
}

/** Two-series inline SVG sparkline (no chart library). */
function Sparkline({ points }: { points: TrafficPoint[] }) {
  const W = 260
  const H = 44
  const geom = useMemo(() => {
    if (points.length < 2) return null
    const max = Math.max(1, ...points.map((p) => Math.max(p.in_bps || 0, p.out_bps || 0)))
    const step = W / (points.length - 1)
    const line = (key: 'in_bps' | 'out_bps') =>
      points.map((p, i) => `${(i * step).toFixed(1)},${(H - 2 - ((p[key] || 0) / max) * (H - 4)).toFixed(1)}`).join(' ')
    const peakIn = Math.max(...points.map((p) => p.in_bps || 0))
    const peakOut = Math.max(...points.map((p) => p.out_bps || 0))
    return { inPts: line('in_bps'), outPts: line('out_bps'), peakIn, peakOut }
  }, [points])
  if (!geom) return <div className="py-2 text-center text-2xs text-muted">Not enough samples in the last 24 h</div>
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-11 w-full" preserveAspectRatio="none">
        <line x1="0" y1={H - 1} x2={W} y2={H - 1} className="stroke-border" strokeWidth="1" />
        <polyline points={geom.inPts} fill="none" stroke={IN_COLOR} strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
        <polyline points={geom.outPts} fill="none" stroke={OUT_COLOR} strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="flex justify-between font-mono text-2xs text-muted">
        <span>24 h ago</span>
        <span>
          peak <span style={{ color: IN_COLOR }}>in {formatBps(geom.peakIn)}</span> · <span style={{ color: OUT_COLOR }}>out {formatBps(geom.peakOut)}</span>
        </span>
        <span>now</span>
      </div>
    </div>
  )
}

function EndSection({ side, name, configuredPort, iface, deviceId, liveLoaded }: {
  side: 'A' | 'B'
  name: string
  configuredPort?: string | null
  iface?: LiveInterface
  deviceId?: string | null
  liveLoaded: boolean
}) {
  const matched = !!iface?.matched
  const traffic = useIfaceTraffic(matched ? deviceId : null, matched ? iface?.if_index : null)
  const port = (matched && (iface?.if_name || iface?.if_descr)) || configuredPort || null
  const util = iface?.util_pct ?? null

  return (
    <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-surface2/30 p-2.5">
      <div className="flex items-center gap-2">
        <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded bg-primary/15 text-2xs font-bold text-primary">{side}</span>
        <span className="min-w-0 flex-1 truncate text-sm font-semibold text-text" title={name}>{name}</span>
        {deviceId && (
          <a
            href={`/devices/${deviceId}?tab=interfaces`}
            target="_blank"
            rel="noopener noreferrer"
            className="flex shrink-0 items-center gap-1 rounded px-1 py-0.5 text-2xs text-muted transition hover:bg-primary/10 hover:text-text"
            title="Open the device's interfaces in a new tab"
          >
            Open interfaces <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </div>

      <div className="flex items-center gap-1.5 text-xs">
        <span className="text-muted">Port</span>
        <span className="truncate font-mono text-text" title={port || undefined}>{port || '—'}</span>
        {matched && iface?.if_alias && <span className="truncate text-2xs text-muted" title={iface.if_alias}>“{iface.if_alias}”</span>}
      </div>

      {!liveLoaded ? (
        <div className="flex items-center gap-1.5 text-2xs text-muted"><Loader2 className="h-3 w-3 animate-spin" /> Waiting for live data…</div>
      ) : !matched ? (
        <div className="flex items-start gap-1.5 rounded-md bg-surface2/60 px-2 py-1.5 text-2xs leading-snug text-muted">
          <Unplug className="mt-px h-3 w-3 shrink-0" />
          {!deviceId
            ? 'This end is not a monitored device — no interface data.'
            : configuredPort
              ? `Port “${configuredPort}” was not found on this device. Re-pick it in the link editor.`
              : 'No interface bound — set the port in the link editor to see live data.'}
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-1">
            <StatusPill label="oper" value={iface?.oper_status} />
            <StatusPill label="admin" value={iface?.admin_status} />
            {speedLabel(iface?.if_speed) && (
              <span className="rounded bg-surface2 px-1.5 py-px font-mono text-2xs text-text2">{speedLabel(iface?.if_speed)}</span>
            )}
          </div>
          <div className="grid grid-cols-2 gap-2 font-mono text-xs">
            <span className="flex items-center gap-1" style={{ color: IN_COLOR }}><ArrowDown className="h-3 w-3" /> {formatBps(iface?.in_bps)}</span>
            <span className="flex items-center gap-1" style={{ color: OUT_COLOR }}><ArrowUp className="h-3 w-3" /> {formatBps(iface?.out_bps)}</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface2">
              <div className="h-full rounded-full" style={{ width: `${Math.min(100, Math.max(util ?? 0, util ? 2 : 0))}%`, background: utilHex(util) }} />
            </div>
            <span className="w-12 text-right font-mono text-2xs" style={{ color: utilHex(util) }}>{util != null ? `${util.toFixed(1)}%` : 'n/a'}</span>
          </div>
          <div className="border-t border-border/50 pt-1.5">
            {traffic.isLoading ? (
              <div className="flex items-center justify-center gap-1.5 py-2 text-2xs text-muted"><Loader2 className="h-3 w-3 animate-spin" /> Loading 24 h traffic…</div>
            ) : traffic.isError ? (
              <div className="py-2 text-center text-2xs text-muted">24 h traffic unavailable</div>
            ) : (
              <Sparkline points={traffic.data?.traffic || []} />
            )}
          </div>
        </>
      )}
    </div>
  )
}

export function LinkDrillPanel({ link, live, sourceName, targetName, sourceDeviceId, targetDeviceId, onClose }: LinkDrillPanelProps) {
  const md = link.metadata || {}
  const kind = String(md.kind || link.link_type || 'ethernet')
  const members = Array.isArray(md.members) ? md.members : []
  const bundle = members.length > 1

  return (
    <div className="flex max-h-full w-80 max-w-[90vw] flex-col rounded-lg border border-border bg-surface/95 text-xs shadow-xl backdrop-blur">
      {/* Header */}
      <div className="flex items-start gap-2 border-b border-border/60 px-3 py-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold text-text" title={`${sourceName} ⇄ ${targetName}`}>
            {sourceName} <span className="text-muted">⇄</span> {targetName}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-1">
            <span className={cn('rounded px-1.5 py-px text-2xs font-semibold capitalize', kind === 'fiber' ? 'bg-warning/15 text-warning' : 'bg-surface2 text-text2')}>{kind}</span>
            {md.speed && <span className="rounded bg-surface2 px-1.5 py-px font-mono text-2xs text-text2">{md.speed}</span>}
            {bundle && <span className="rounded bg-primary/15 px-1.5 py-px font-mono text-2xs font-semibold text-primary">×{members.length}</span>}
            {md.discovered && <span className="rounded bg-surface2 px-1.5 py-px text-2xs text-muted">{md.protocol ? `${String(md.protocol).toUpperCase()} ` : ''}discovered</span>}
            {link.label && <span className="truncate text-2xs text-muted">{link.label}</span>}
          </div>
        </div>
        <button type="button" onClick={onClose} className="rounded p-1 text-muted transition hover:bg-primary/10 hover:text-text" title="Close (Esc)">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex flex-col gap-2 overflow-y-auto p-2.5">
        <EndSection side="A" name={sourceName} configuredPort={md.src_interface} iface={live?.source} deviceId={sourceDeviceId} liveLoaded={!!live} />
        <EndSection side="B" name={targetName} configuredPort={md.dst_interface} iface={live?.target} deviceId={targetDeviceId} liveLoaded={!!live} />

        {bundle && (
          <div className="rounded-lg border border-border p-2.5">
            <div className="mb-1 flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-muted">
              <Layers className="h-3 w-3" /> Bundle members ({members.length})
            </div>
            <ul className="flex flex-col gap-0.5 font-mono text-xs text-text2">
              {members.map((m, i) => (
                <li key={i} className="flex items-center gap-1.5">
                  <span className="truncate">{m.src_interface || '?'}</span>
                  <span className="text-muted">⇄</span>
                  <span className="truncate">{m.dst_interface || '?'}</span>
                </li>
              ))}
            </ul>
            <p className="mt-1 text-2xs text-muted">Live figures above are for the bound ports ({md.src_interface || '?'} ⇄ {md.dst_interface || '?'}).</p>
          </div>
        )}

        {md.notes && (
          <div className="rounded-lg border border-border p-2.5">
            <div className="mb-0.5 text-2xs font-bold uppercase tracking-wider text-muted">Notes</div>
            <p className="whitespace-pre-wrap text-xs text-text2">{md.notes}</p>
          </div>
        )}

        {live?.generated_at && (
          <div className="text-2xs text-muted">
            Live window {live.window_seconds ? `${Math.round(live.window_seconds / 60)} min` : '—'} · updated {new Date(live.generated_at).toLocaleTimeString()}
          </div>
        )}
      </div>
    </div>
  )
}
