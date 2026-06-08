import { memo } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { cn } from '@/lib/utils'
import { NetworkIcon } from '@/components/network-icons'
import { DISC_CX, DISC_CY, NODE_W, STATUS_COLOR, iconForNode, statusKey, type ManualMapNode } from '../core'

export type DeviceNodeData = {
  node: ManualMapNode
  live: boolean
}

/* React Flow custom node — a faithful port of v1's NodeCard: a 64px status-
 * ringed icon disc with a status dot and (in live mode) a pulse halo for
 * problem nodes, plus a label card. Connection handles sit invisibly at the
 * disc centre so edges anchor exactly where v1 drew them (node centre). */
function DeviceNodeImpl({ data, selected }: NodeProps) {
  const { node, live } = data as DeviceNodeData
  const iconKey = iconForNode(node)
  const sk = statusKey(node.status)
  const color = STATUS_COLOR[sk]
  const pulsing = live && (sk === 'down' || sk === 'degraded')

  const handleStyle = {
    left: DISC_CX,
    top: DISC_CY,
    width: 1,
    height: 1,
    minWidth: 1,
    minHeight: 1,
    background: 'transparent',
    border: 'none',
    transform: 'translate(-50%, -50%)',
    opacity: 0,
  } as const

  return (
    <div
      className="group flex flex-col items-center"
      style={{ width: NODE_W }}
      title={`${node.hostname} · ${node.ip_address}`}
    >
      {/* Edge anchor handles — centred on the disc, invisible. */}
      <Handle type="target" position={Position.Top} id="c" style={handleStyle} isConnectable={false} />
      <Handle type="source" position={Position.Top} id="c" style={handleStyle} isConnectable={false} />

      {/* Icon disc */}
      <div className="relative">
        {pulsing && (
          <span
            aria-hidden
            className={cn('absolute inset-0 rounded-full', sk === 'down' ? 'bg-danger/40' : 'bg-warning/40', 'nm-ping')}
          />
        )}
        <div
          className={cn(
            'relative flex h-16 w-16 items-center justify-center rounded-full border-2 shadow-md transition',
            'bg-surface',
            color.ring,
            selected && 'ring-2 ring-primary ring-offset-2 ring-offset-surface',
          )}
        >
          <span
            aria-hidden
            className={cn(
              'absolute -right-0.5 -top-0.5 h-3.5 w-3.5 rounded-full border-2 border-surface',
              color.dot,
              live && 'animate-pulse-soft',
            )}
          />
          <NetworkIcon name={iconKey} className="h-9 w-9" />
        </div>
      </div>

      {/* Label */}
      <div className="mt-1.5 max-w-[8rem] rounded-md border border-border bg-surface/90 px-2 py-0.5 text-center text-[11px] font-semibold leading-tight shadow-sm backdrop-blur">
        <div className="truncate text-text">{node.label || node.hostname}</div>
        <div className="truncate text-[10px] font-normal text-muted">{node.ip_address}</div>
      </div>
    </div>
  )
}

export const DeviceNode = memo(DeviceNodeImpl)
