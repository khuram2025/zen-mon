import { memo } from 'react'
import { Handle, Position, useStore, type NodeProps } from '@xyflow/react'
import { cn } from '@/lib/utils'
import { NetworkIcon } from '@/components/network-icons'
import { DISC, DISC_CX, DISC_CY, NODE_W, STATUS_COLOR, iconForNode, statusKey, type ManualMapNode } from '../core'
import { useMapMode } from './MapModeContext'

export type DeviceNodeData = {
  node: ManualMapNode
  live: boolean
  /** Move this node's label by an offset (logical px); commit=false live, true persist. */
  onLabelMove?: (dx: number, dy: number, commit: boolean) => void
}

// Default label anchor within the node box (centred just below the disc).
const LABEL_X = DISC_CX
const LABEL_Y = DISC + 8

/* React Flow custom node: status-ringed icon disc + a separately MOVABLE label.
 * The label can be dragged to any offset (persisted in node metadata) so dense
 * maps stay readable; a faint leader line connects it back to the disc. */
function DeviceNodeImpl({ data, selected }: NodeProps) {
  const { node, live, onLabelMove } = data as DeviceNodeData
  const { connectMode } = useMapMode()
  const zoom = useStore((s) => s.transform[2])
  const iconKey = iconForNode(node)
  const sk = statusKey(node.status)
  const color = STATUS_COLOR[sk]
  const pulsing = live && (sk === 'down' || sk === 'degraded')

  const off = node.metadata?.label_offset || { dx: 0, dy: 0 }
  const lx = LABEL_X + off.dx
  const ly = LABEL_Y + off.dy
  const moved = Math.hypot(off.dx, off.dy) > 6
  const editable = !live && !!onLabelMove

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
    <div className="group relative" style={{ width: NODE_W, height: DISC }} title={`${node.hostname} · ${node.ip_address}`}>
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
          <line x1={DISC_CX} y1={DISC} x2={lx} y2={ly} className="stroke-border" strokeWidth={1} strokeDasharray="2 2" />
        </svg>
      )}

      {/* Icon disc (this is the draggable node body) */}
      <div className="absolute" style={{ left: DISC_CX, top: DISC_CY, transform: 'translate(-50%, -50%)' }}>
        {pulsing && <span aria-hidden className={cn('absolute inset-0 rounded-full', sk === 'down' ? 'bg-danger/40' : 'bg-warning/40', 'nm-ping')} />}
        <div className={cn('relative flex h-16 w-16 items-center justify-center rounded-full border-2 bg-surface shadow-md transition', color.ring, selected && 'ring-2 ring-primary ring-offset-2 ring-offset-surface')}>
          <span aria-hidden className={cn('absolute -right-0.5 -top-0.5 h-3.5 w-3.5 rounded-full border-2 border-surface', color.dot, live && 'animate-pulse-soft')} />
          <NetworkIcon name={iconKey} className="h-9 w-9" />
        </div>
      </div>

      {/* Movable label */}
      <div
        onPointerDown={startLabelDrag}
        className={cn(
          'nodrag absolute max-w-[8rem] rounded-md border border-border bg-surface/90 px-2 py-0.5 text-center text-[11px] font-semibold leading-tight shadow-sm backdrop-blur',
          editable && 'cursor-move hover:border-primary/60',
        )}
        style={{ left: lx, top: ly, transform: 'translateX(-50%)' }}
      >
        <div className="truncate text-text">{node.label || node.hostname}</div>
        <div className="truncate text-[10px] font-normal text-muted">{node.ip_address}</div>
      </div>
    </div>
  )
}

export const DeviceNode = memo(DeviceNodeImpl)
