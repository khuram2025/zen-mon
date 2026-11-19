import { memo } from 'react'
import { Handle, Position, useStore, type NodeProps } from '@xyflow/react'
import { Bell, Lock } from 'lucide-react'
import { cn } from '@/lib/utils'
import { NetworkIcon } from '@/components/network-icons'
import {
  DISC, DISC_CX, DISC_CY, NODE_W, STATUS_COLOR, STATUS_LABEL,
  iconFillFor,
  formatAgo, formatUptime, iconForNode, nodeKindOf, statusKey, textScaleFor, utilHex,
  type ManualMapNode, type NodeLiveData,
} from '../core'
import { useMapMode } from './MapModeContext'

export type DeviceNodeData = {
  node: ManualMapNode
  live: boolean
  /** Per-device live health (status, cpu/mem, alerts) — live mode only. */
  nodeLive?: NodeLiveData
  /** Move this node's label by an offset (logical px); commit=false live, true persist. */
  onLabelMove?: (dx: number, dy: number, commit: boolean) => void
}

// Default label anchor within the node box (centred just below the disc).
const LABEL_X = DISC_CX

const STATUS_GLOW: Record<string, string> = {
  up: '0 0 0 3px rgb(34 197 94 / 0.10), 0 4px 14px rgb(34 197 94 / 0.22)',
  down: '0 0 0 4px rgb(239 68 68 / 0.22), 0 0 22px rgb(239 68 68 / 0.6)',
  degraded: '0 0 0 3px rgb(245 158 11 / 0.18), 0 0 18px rgb(245 158 11 / 0.45)',
  maintenance: '0 0 0 3px rgb(59 130 246 / 0.14), 0 0 14px rgb(59 130 246 / 0.35)',
  unknown: '0 2px 8px rgb(0 0 0 / 0.25)',
}

/* React Flow custom node: status-ringed icon disc + a separately MOVABLE label.
 * The label can be dragged to any offset (persisted in node metadata) so dense
 * maps stay readable; a faint leader line connects it back to the disc.
 * In live mode the disc glows with status, carries an alert badge, shows
 * CPU/MEM micro-gauges and reveals a full health card on hover.
 *
 * Text (label, gauges) counter-scales when the map is zoomed out so a
 * wall-sized map stays readable — the disc itself keeps true scale so the
 * topology geometry never lies. */
function DeviceNodeImpl({ data, selected }: NodeProps) {
  const { node, live, nodeLive, onLabelMove } = data as DeviceNodeData
  const { connectMode, textBoost = 1, pinnedId } = useMapMode()
  const zoom = useStore((s) => s.transform[2])
  const ts = textScaleFor(zoom) * textBoost
  const iconKey = iconForNode(node)
  const kind = nodeKindOf(node)
  // Live status feed (15s poll) wins over the map snapshot when present.
  const sk = statusKey(live ? (nodeLive?.status ?? node.status) : node.status)
  const color = STATUS_COLOR[sk]
  const pulsing = live && (sk === 'down' || sk === 'degraded')
  const alerts = live ? nodeLive?.alerts : undefined
  const alertCount = alerts?.active || 0
  const critical = (alerts?.critical || 0) > 0

  const off = node.metadata?.label_offset || { dx: 0, dy: 0 }
  const scale = node.metadata?.size_scale || 1
  const ls = node.metadata?.label_style || {}
  const locked = !!node.metadata?.locked
  const imageSrc = typeof node.metadata?.image_src === 'string' && node.metadata.image_src ? node.metadata.image_src : null
  const discSize = DISC * scale
  const iconFill = iconFillFor(node.metadata)
  // Outer frame: a rounded square (default for new maps) or a full circle.
  const frameRadius = node.metadata?.frame === 'rounded' ? Math.round(discSize * 0.24) : discSize / 2
  const lx = LABEL_X + off.dx
  const ly = (DISC_CY + discSize / 2 + 7) + off.dy
  const moved = Math.hypot(off.dx, off.dy) > 6
  const editable = !live && !!onLabelMove && !locked
  const hasStats = live && (nodeLive?.cpu_pct != null || nodeLive?.mem_pct != null)
  const name = node.label || node.hostname
  const badgeScale = Math.min(2.4, ts)

  const startLabelDrag = (e: React.PointerEvent) => {
    if (!editable || e.button !== 0) return
    e.stopPropagation()
    e.preventDefault()
    const sx = e.clientX
    const sy = e.clientY
    const base = { dx: off.dx, dy: off.dy }
    const move = (ev: PointerEvent) => onLabelMove!(base.dx + (ev.clientX - sx) / zoom, base.dy + (ev.clientY - sy) / zoom, false)
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      onLabelMove!(base.dx + (ev.clientX - sx) / zoom, base.dy + (ev.clientY - sy) / zoom, true)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  // In connect mode the handles grow to cover the whole disc so a link can be
  // dragged from/to anywhere on the device; otherwise they're a 1px anchor at
  // the disc centre (the drop target — React Flow snaps within connectionRadius).
  const handleStyle = connectMode
    ? { left: DISC_CX, top: DISC_CY, width: discSize, height: discSize, borderRadius: '50%', background: 'transparent', border: 'none', transform: 'translate(-50%, -50%)', opacity: 0, cursor: 'crosshair', zIndex: 5 } as const
    : { left: DISC_CX, top: DISC_CY, width: 1, height: 1, minWidth: 1, minHeight: 1, background: 'transparent', border: 'none', transform: 'translate(-50%, -50%)', opacity: 0 } as const

  // draw.io-style hover connector: a small blue plug on the disc rim that
  // starts a cable when dragged, without leaving Select mode.
  // Sits on the LEFT rim (the hover card opens on the right) and grows as the
  // map is zoomed out so it stays grabbable on a wall-sized layout.
  const quickHandleVisible = !live && !connectMode && !locked
  const plug = Math.round(14 * Math.min(2.2, Math.max(1, 0.9 / Math.max(0.15, zoom))))
  const quickHandleStyle = {
    left: DISC_CX - discSize / 2 - plug * 0.55,
    top: DISC_CY,
    width: plug,
    height: plug,
    borderRadius: '50%',
    transform: 'translate(-50%, -50%)',
    background: 'rgb(var(--primary))',
    border: '2px solid rgb(var(--surface))',
    cursor: 'crosshair',
    zIndex: 6,
    opacity: 0,
  } as const

  return (
    <div className="group relative" style={{ width: NODE_W, height: DISC }} data-status={sk}>
      <Handle type="target" position={Position.Top} id="c" style={handleStyle} isConnectable={!live} />
      <Handle type="source" position={Position.Top} id="c" style={handleStyle} isConnectable={!live} />
      {quickHandleVisible && (
        <Handle
          type="source"
          position={Position.Left}
          id="q"
          title="Drag to connect"
          className="nm-quick-handle"
          style={quickHandleStyle}
          isConnectable
        />
      )}

      {/* Connect-mode affordance ring */}
      {connectMode && !live && (
        <span
          aria-hidden
          className="pointer-events-none absolute rounded-full ring-2 ring-primary/0 transition group-hover:ring-primary/70"
          style={{ left: DISC_CX, top: DISC_CY, width: discSize + 8, height: discSize + 8, transform: 'translate(-50%, -50%)' }}
        />
      )}

      {/* Leader line from disc to a moved label */}
      {moved && (
        <svg className="pointer-events-none absolute overflow-visible" style={{ left: 0, top: 0 }} width={1} height={1}>
          <line x1={DISC_CX} y1={DISC_CY + discSize / 2} x2={lx} y2={ly} className="stroke-border" strokeWidth={1} strokeDasharray="2 2" />
        </svg>
      )}

      {/* Icon tile (this is the draggable node body) */}
      <div className="absolute" style={{ left: DISC_CX, top: DISC_CY, transform: 'translate(-50%, -50%)' }}>
        {pulsing && <span aria-hidden className={cn('absolute inset-0', sk === 'down' ? 'bg-danger/45' : 'bg-warning/40', 'nm-ping')} style={{ borderRadius: frameRadius }} />}
        <div
          className={cn(
            'nm-tile relative flex items-center justify-center border-2 transition',
            color.ring,
            sk === 'down' && live && 'nm-tile-down',
            selected && 'ring-2 ring-primary ring-offset-2 ring-offset-surface',
          )}
          style={{ width: discSize, height: discSize, borderRadius: frameRadius, boxShadow: live ? STATUS_GLOW[sk] : undefined }}
        >
          {imageSrc ? (
            <img
              src={imageSrc}
              alt=""
              draggable={false}
              className="pointer-events-none select-none object-contain"
              style={{ width: discSize * iconFill, height: discSize * iconFill }}
            />
          ) : (
            <NetworkIcon name={iconKey} style={{ width: discSize * iconFill, height: discSize * iconFill }} />
          )}

          {/* Status pip (bottom-right) — the colour reads even when the ring is subtle */}
          <span
            aria-hidden
            className={cn('absolute rounded-full border-2 border-surface', color.dot, live && sk !== 'up' && 'animate-pulse-soft')}
            style={{ right: -3, bottom: -3, width: 14, height: 14, transform: `scale(${Math.min(1.8, badgeScale)})`, transformOrigin: 'bottom right' }}
            title={STATUS_LABEL[sk] || sk}
          />

          {/* Active-alert badge (top-right) */}
          {alertCount > 0 && (
            <span
              className={cn(
                'absolute flex h-[18px] min-w-[18px] items-center justify-center gap-0.5 rounded-full border-2 border-surface px-1 font-mono text-2xs font-bold leading-none text-white shadow-md',
                critical ? 'bg-danger' : 'bg-warning',
              )}
              style={{ right: -8, top: -8, transform: `scale(${badgeScale})`, transformOrigin: 'bottom left' }}
              title={`${alertCount} active alert${alertCount > 1 ? 's' : ''}${critical ? ` · ${alerts!.critical} critical` : ''}`}
            >
              <Bell className="h-2.5 w-2.5" strokeWidth={3} />
              {alertCount > 99 ? '99+' : alertCount}
            </span>
          )}

          {/* Kind badge: server / service-check nodes are not network devices */}
          {kind !== 'device' && (
            <span
              className="absolute -bottom-2 left-1/2 rounded-full border border-surface bg-surface2 px-1.5 font-mono text-2xs font-bold uppercase leading-4 text-muted shadow"
              style={{ transform: `translateX(-50%) scale(${Math.min(1.6, ts) * 0.85})`, transformOrigin: 'top center' }}
              title={kind === 'server' ? 'Server (Servers module)' : 'Service check'}
            >
              {kind === 'server' ? 'SRV' : 'CHK'}
            </span>
          )}

          {/* Lock badge (design mode) */}
          {locked && !live && (
            <span className="absolute -bottom-1 -left-1 flex h-4 w-4 items-center justify-center rounded-full border border-surface bg-surface2 text-muted shadow" title="Locked — unlock from the right-click menu">
              <Lock className="h-2.5 w-2.5" />
            </span>
          )}
        </div>
      </div>

      {/* Movable, styleable label card (counter-scaled when zoomed out). Live
       * CPU / memory sit inside the card so they never collide with cables. */}
      <div
        onPointerDown={startLabelDrag}
        className={cn(
          'nodrag absolute w-max max-w-[15rem] rounded-md border bg-surface/95 px-2 py-[3px] text-center leading-tight shadow-sm backdrop-blur',
          live && sk === 'down' ? 'border-danger/60' : live && sk === 'degraded' ? 'border-warning/50' : 'border-border',
          editable && 'cursor-move hover:border-primary/60',
        )}
        style={{
          left: lx, top: ly, transform: `translateX(-50%) scale(${ts})`, transformOrigin: 'top center',
          fontFamily: ls.fontFamily || undefined,
          fontSize: ls.fontSize ? `${ls.fontSize}px` : '0.75rem',
          fontWeight: ls.bold === false ? 400 : 600,
          fontStyle: ls.italic ? 'italic' : 'normal',
        }}
        title={`${name}${node.ip_address ? ` · ${node.ip_address}` : ''}`}
      >
        <div className="truncate" style={{ color: ls.color || 'rgb(var(--text))' }}>{name}</div>
        {!node.metadata?.hide_ip && node.ip_address && (
          <div className="truncate font-mono font-normal text-muted" style={{ fontSize: ls.fontSize ? `${Math.max(9, ls.fontSize - 1)}px` : '0.6875rem' }}>{node.ip_address}</div>
        )}
        {hasStats && (
          <div className="mt-[3px] flex items-center justify-center gap-2 border-t border-border/60 pt-[3px] font-mono font-semibold" style={{ fontSize: '0.625rem' }}>
            {nodeLive?.cpu_pct != null && <StatBar label="CPU" pct={nodeLive.cpu_pct} />}
            {nodeLive?.mem_pct != null && <StatBar label="MEM" pct={nodeLive.mem_pct} />}
          </div>
        )}
      </div>

      {/* Hover health card (counter-scaled so it stays readable at any zoom);
       * hidden while this node's inspector is pinned open. */}
      {pinnedId !== node.id && (
        <div
          className="pointer-events-none absolute z-50 hidden group-hover:block"
          style={{ left: DISC_CX + discSize / 2 + 12, top: DISC_CY, transform: `translateY(-50%) scale(${textBoost / Math.max(0.5, Math.min(1.6, zoom))})`, transformOrigin: 'left center' }}
        >
          <DeviceHoverCard node={node} nodeLive={live ? nodeLive : undefined} sk={sk} />
        </div>
      )}
    </div>
  )
}

function StatBar({ label, pct }: { label: string; pct: number }) {
  const v = Math.max(0, Math.min(100, pct))
  return (
    <span className="flex items-center gap-1" title={`${label} ${v.toFixed(1)}%`}>
      <span className="text-muted">{label}</span>
      <span className="relative h-[5px] w-6 overflow-hidden rounded-full bg-surface2">
        <span className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${Math.max(4, v)}%`, background: utilHex(v) }} />
      </span>
      <span style={{ color: v >= 60 ? utilHex(v) : 'rgb(var(--text2))' }}>{Math.round(v)}%</span>
    </span>
  )
}

export function DeviceHoverCard({ node, nodeLive, sk }: { node: ManualMapNode; nodeLive?: NodeLiveData; sk: string }) {
  const badge =
    sk === 'up' ? 'bg-success/15 text-success' :
    sk === 'down' ? 'bg-danger/15 text-danger' :
    sk === 'degraded' ? 'bg-warning/15 text-warning' :
    sk === 'maintenance' ? 'bg-info/15 text-info' : 'bg-surface3 text-muted'
  return (
    <div className="w-60 rounded-lg border border-border bg-surface/95 p-2.5 text-left shadow-xl backdrop-blur animate-fade-in">
      <div className="mb-1 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-xs font-bold text-text">{node.label || node.hostname}</div>
          <div className="truncate font-mono text-2xs text-muted">{node.ip_address}</div>
        </div>
        <span className={cn('rounded px-1.5 py-0.5 text-2xs font-bold uppercase leading-none', badge)}>{sk}</span>
      </div>
      <div className="grid grid-cols-2 gap-x-2 gap-y-0.5 border-t border-border/60 pt-1.5 text-2xs leading-snug">
        <Field k="Type" v={nodeKindOf(node) === 'service_check' ? `check · ${node.model || ''}` : node.device_type} />
        {node.vendor ? <Field k="Vendor" v={node.vendor} /> : null}
        {node.model && nodeKindOf(node) !== 'service_check' ? <Field k={nodeKindOf(node) === 'server' ? 'OS' : 'Model'} v={node.model} /> : null}
        {node.location ? <Field k="Location" v={node.location} /> : null}
        {nodeLive?.rtt_ms != null && <Field k="RTT" v={`${nodeLive.rtt_ms.toFixed(1)} ms`} mono />}
        {nodeLive?.uptime_seconds != null && <Field k="Uptime" v={formatUptime(nodeLive.uptime_seconds)} mono />}
        {nodeLive?.temperature_c != null && <Field k="Temp" v={`${nodeLive.temperature_c.toFixed(1)} °C`} mono />}
        <Field k="Seen" v={formatAgo(nodeLive?.last_seen ?? node.last_seen)} mono />
      </div>
      {(nodeLive?.cpu_pct != null || nodeLive?.mem_pct != null) && (
        <div className="mt-1.5 space-y-1 border-t border-border/60 pt-1.5">
          {nodeLive?.cpu_pct != null && <HoverGauge label="CPU" pct={nodeLive.cpu_pct} />}
          {nodeLive?.mem_pct != null && <HoverGauge label="MEM" pct={nodeLive.mem_pct} />}
        </div>
      )}
      {nodeLive && nodeLive.alerts.active > 0 && (
        <div className="mt-1.5 flex items-center gap-1.5 border-t border-border/60 pt-1.5 text-2xs font-semibold">
          <span className={cn('h-1.5 w-1.5 rounded-full', nodeLive.alerts.critical > 0 ? 'bg-danger' : 'bg-warning')} />
          <span className="text-text2">
            {nodeLive.alerts.active} active alert{nodeLive.alerts.active > 1 ? 's' : ''}
            {nodeLive.alerts.critical > 0 ? ` · ${nodeLive.alerts.critical} critical` : ''}
          </span>
        </div>
      )}
    </div>
  )
}

function Field({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex min-w-0 items-baseline gap-1">
      <span className="shrink-0 text-muted">{k}</span>
      <span className={cn('truncate font-medium text-text2', mono && 'font-mono')} title={v}>{v}</span>
    </div>
  )
}

function HoverGauge({ label, pct }: { label: string; pct: number }) {
  const v = Math.max(0, Math.min(100, pct))
  return (
    <div className="flex items-center gap-1.5">
      <span className="w-7 font-mono text-[8.5px] font-bold text-muted">{label}</span>
      <div className="h-[5px] flex-1 overflow-hidden rounded-full bg-surface2">
        <div className="h-full rounded-full" style={{ width: `${Math.max(3, v)}%`, background: utilHex(v) }} />
      </div>
      <span className="w-8 text-right font-mono text-2xs font-semibold text-text2">{v.toFixed(0)}%</span>
    </div>
  )
}

export const DeviceNode = memo(DeviceNodeImpl)
