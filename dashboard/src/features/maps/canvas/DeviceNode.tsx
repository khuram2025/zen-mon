import { memo } from 'react'
import { Handle, Position, useStore, type NodeProps } from '@xyflow/react'
import { cn } from '@/lib/utils'
import { NetworkIcon } from '@/components/network-icons'
import {
  DISC, DISC_CX, DISC_CY, DISC_RADIUS, NODE_W, STATUS_COLOR,
  iconFillFor,
  formatAgo, formatUptime, iconForNode, statusKey, utilHex,
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
const LABEL_Y = DISC + 8

const STATUS_GLOW: Record<string, string> = {
  up: '0 0 14px rgb(34 197 94 / 0.35)',
  down: '0 0 18px rgb(239 68 68 / 0.55)',
  degraded: '0 0 16px rgb(245 158 11 / 0.45)',
  maintenance: '0 0 14px rgb(59 130 246 / 0.4)',
  unknown: 'none',
}

/* React Flow custom node: status-ringed icon disc + a separately MOVABLE label.
 * The label can be dragged to any offset (persisted in node metadata) so dense
 * maps stay readable; a faint leader line connects it back to the disc.
 * In live mode the disc glows with status, carries an alert badge, shows
 * CPU/MEM micro-gauges and reveals a full health card on hover. */
function DeviceNodeImpl({ data, selected }: NodeProps) {
  const { node, live, nodeLive, onLabelMove } = data as DeviceNodeData
  const { connectMode } = useMapMode()
  const zoom = useStore((s) => s.transform[2])
  const iconKey = iconForNode(node)
  // Live status feed (15s poll) wins over the map snapshot when present.
  const sk = statusKey(live ? (nodeLive?.status ?? node.status) : node.status)
  const color = STATUS_COLOR[sk]
  const pulsing = live && (sk === 'down' || sk === 'degraded')
  const alerts = live ? nodeLive?.alerts : undefined
  const alertCount = alerts?.active || 0

  const off = node.metadata?.label_offset || { dx: 0, dy: 0 }
  const scale = node.metadata?.size_scale || 1
  const ls = node.metadata?.label_style || {}
  const discSize = DISC * scale
  const iconFill = iconFillFor(node.metadata)
  // Outer frame: a full circle (default) or a rounded-corner square.
  const frameRadius = node.metadata?.frame === 'rounded' ? Math.round(discSize * 0.22) : discSize / 2
  const lx = LABEL_X + off.dx
  const ly = (DISC_CY + discSize / 2 + 8) + off.dy
  const moved = Math.hypot(off.dx, off.dy) > 6
  const editable = !live && !!onLabelMove
  const hasGauges = live && (nodeLive?.cpu_pct != null || nodeLive?.mem_pct != null)

  const startLabelDrag = (e: React.PointerEvent) => {
    if (!editable) return
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
  // dragged from/to anywhere on the device; otherwise they're a 1px anchor.
  const handleStyle = connectMode
    ? { left: DISC_CX, top: DISC_CY, width: DISC, height: DISC, borderRadius: '50%', background: 'transparent', border: 'none', transform: 'translate(-50%, -50%)', opacity: 0, cursor: 'crosshair', zIndex: 5 } as const
    : { left: DISC_CX, top: DISC_CY, width: 1, height: 1, minWidth: 1, minHeight: 1, background: 'transparent', border: 'none', transform: 'translate(-50%, -50%)', opacity: 0 } as const

  return (
    <div className="group relative" style={{ width: NODE_W, height: DISC }}>
      <Handle type="target" position={Position.Top} id="c" style={handleStyle} isConnectable={connectMode} />
      <Handle type="source" position={Position.Top} id="c" style={handleStyle} isConnectable={connectMode} />

      {/* Connect-mode affordance ring */}
      {connectMode && !live && (
        <span
          aria-hidden
          className="pointer-events-none absolute rounded-full ring-2 ring-primary/0 transition group-hover:ring-primary/70"
          style={{ left: DISC_CX, top: DISC_CY, width: DISC + 8, height: DISC + 8, transform: 'translate(-50%, -50%)' }}
        />
      )}

      {/* Leader line from disc to a moved label */}
      {moved && (
        <svg className="pointer-events-none absolute overflow-visible" style={{ left: 0, top: 0 }} width={1} height={1}>
          <line x1={DISC_CX} y1={DISC_CY + discSize / 2} x2={lx} y2={ly} className="stroke-border" strokeWidth={1} strokeDasharray="2 2" />
        </svg>
      )}

      {/* Icon disc (this is the draggable node body) */}
      <div className="absolute" style={{ left: DISC_CX, top: DISC_CY, transform: 'translate(-50%, -50%)' }}>
        {pulsing && <span aria-hidden className={cn('absolute inset-0', sk === 'down' ? 'bg-danger/40' : 'bg-warning/40', 'nm-ping')} style={{ borderRadius: frameRadius }} />}
        <div
          className={cn('relative flex items-center justify-center border-2 bg-surface shadow-md transition', color.ring, selected && 'ring-2 ring-primary ring-offset-2 ring-offset-surface')}
          style={{ width: discSize, height: discSize, borderRadius: frameRadius, boxShadow: live ? STATUS_GLOW[sk] : undefined }}
        >
          <span aria-hidden className={cn('absolute -right-0.5 -top-0.5 h-3.5 w-3.5 rounded-full border-2 border-surface', color.dot, live && 'animate-pulse-soft')} />
          <NetworkIcon name={iconKey} style={{ width: discSize * iconFill, height: discSize * iconFill }} />

          {/* Active-alert badge */}
          {alertCount > 0 && (
            <span
              className={cn(
                'absolute -left-1.5 -top-1.5 flex h-4 min-w-4 items-center justify-center rounded-full border border-surface px-0.5 font-mono text-[9px] font-bold leading-none text-white shadow',
                (alerts?.critical || 0) > 0 ? 'bg-danger' : 'bg-warning',
              )}
            >
              {alertCount > 99 ? '99+' : alertCount}
            </span>
          )}
        </div>
      </div>

      {/* Live CPU / MEM micro-gauges under the disc */}
      {hasGauges && (
        <div
          className="pointer-events-none absolute flex flex-col gap-[3px]"
          style={{ left: DISC_CX, top: DISC_CY + discSize / 2 + 3, width: Math.max(46, discSize * 0.8), transform: 'translateX(-50%)' }}
        >
          {nodeLive?.cpu_pct != null && <MicroGauge label="C" pct={nodeLive.cpu_pct} />}
          {nodeLive?.mem_pct != null && <MicroGauge label="M" pct={nodeLive.mem_pct} />}
        </div>
      )}

      {/* Movable, styleable label */}
      <div
        onPointerDown={startLabelDrag}
        className={cn(
          'nodrag absolute max-w-[10rem] rounded-md border border-border bg-surface/90 px-2 py-0.5 text-center leading-tight shadow-sm backdrop-blur',
          editable && 'cursor-move hover:border-primary/60',
        )}
        style={{
          left: lx, top: hasGauges ? ly + 14 : ly, transform: 'translateX(-50%)',
          fontFamily: ls.fontFamily || undefined,
          fontSize: ls.fontSize ? `${ls.fontSize}px` : '11px',
          fontWeight: ls.bold === false ? 400 : 600,
          fontStyle: ls.italic ? 'italic' : 'normal',
        }}
      >
        <div className="truncate" style={{ color: ls.color || 'rgb(var(--text))' }}>{node.label || node.hostname}</div>
        <div className="truncate font-normal text-muted" style={{ fontSize: ls.fontSize ? `${Math.max(9, ls.fontSize - 1)}px` : '10px' }}>{node.ip_address}</div>
      </div>

      {/* Hover health card (counter-scaled so it stays readable at any zoom) */}
      <div
        className="pointer-events-none absolute z-50 hidden group-hover:block"
        style={{ left: DISC_CX + discSize / 2 + 10, top: DISC_CY, transform: `translateY(-50%) scale(${1 / Math.max(0.5, Math.min(1.6, zoom))})`, transformOrigin: 'left center' }}
      >
        <DeviceHoverCard node={node} nodeLive={live ? nodeLive : undefined} sk={sk} />
      </div>
    </div>
  )
}

function MicroGauge({ label, pct }: { label: string; pct: number }) {
  const v = Math.max(0, Math.min(100, pct))
  return (
    <div className="flex items-center gap-1">
      <span className="w-2 font-mono text-[7px] font-bold leading-none text-muted">{label}</span>
      <div className="h-[4px] flex-1 overflow-hidden rounded-full bg-surface2/90 shadow-inner">
        <div className="h-full rounded-full transition-all duration-700" style={{ width: `${Math.max(3, v)}%`, background: utilHex(v) }} />
      </div>
      <span className="w-6 text-right font-mono text-[7.5px] font-semibold leading-none text-text2">{Math.round(v)}%</span>
    </div>
  )
}

function DeviceHoverCard({ node, nodeLive, sk }: { node: ManualMapNode; nodeLive?: NodeLiveData; sk: string }) {
  const badge =
    sk === 'up' ? 'bg-success/15 text-success' :
    sk === 'down' ? 'bg-danger/15 text-danger' :
    sk === 'degraded' ? 'bg-warning/15 text-warning' :
    sk === 'maintenance' ? 'bg-info/15 text-info' : 'bg-surface3 text-muted'
  return (
    <div className="w-60 rounded-lg border border-border bg-surface/95 p-2.5 text-left shadow-xl backdrop-blur animate-fade-in">
      <div className="mb-1 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-[11.5px] font-bold text-text">{node.label || node.hostname}</div>
          <div className="truncate font-mono text-[10px] text-muted">{node.ip_address}</div>
        </div>
        <span className={cn('rounded px-1.5 py-0.5 text-[9px] font-bold uppercase leading-none', badge)}>{sk}</span>
      </div>
      <div className="grid grid-cols-2 gap-x-2 gap-y-0.5 border-t border-border/60 pt-1.5 text-[9.5px] leading-snug">
        <Field k="Type" v={node.device_type} />
        {node.vendor ? <Field k="Vendor" v={node.vendor} /> : null}
        {node.model ? <Field k="Model" v={node.model} /> : null}
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
        <div className="mt-1.5 flex items-center gap-1.5 border-t border-border/60 pt-1.5 text-[9.5px] font-semibold">
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
      <span className="w-8 text-right font-mono text-[9px] font-semibold text-text2">{v.toFixed(0)}%</span>
    </div>
  )
}

export const DeviceNode = memo(DeviceNodeImpl)
