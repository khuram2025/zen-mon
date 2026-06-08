import { memo } from 'react'
import { EdgeLabelRenderer, useReactFlow, useStore, type EdgeProps } from '@xyflow/react'
import { cn } from '@/lib/utils'
import {
  LINK_KIND_STYLE,
  STATUS_COLOR,
  edgePath,
  formatBps,
  linkHealth,
  linkKindOf,
  linkShapeOf,
  linkWaypoints,
  pctToPx,
  segmentMidpoints,
  utilizationColor,
  type LiveLinkData,
  type ManualMapLink,
  type NodeStatus,
  type Pt,
} from '../core'

export type NetworkEdgeData = {
  link: ManualMapLink
  sourceStatus: NodeStatus
  targetStatus: NodeStatus
  live?: LiveLinkData
  liveMode: boolean
  showThroughput: boolean
  /** Update this link's waypoints (logical px). commit=false for live drag,
   *  true to persist. */
  setWaypoints?: (wpsPx: Pt[], commit: boolean) => void
}

function NetworkEdgeImpl({ sourceX, sourceY, targetX, targetY, data, selected }: EdgeProps) {
  const d = data as NetworkEdgeData
  const { link, sourceStatus, targetStatus, live, liveMode, showThroughput, setWaypoints } = d
  const rf = useReactFlow()
  const zoom = useStore((s) => s.transform[2])

  const health = linkHealth(sourceStatus, targetStatus)
  const color = STATUS_COLOR[health].line
  const kind = linkKindOf(link)
  const shape = linkShapeOf(link)
  const kindStyle = LINK_KIND_STYLE[kind] || {}
  const wpsPx = linkWaypoints(link).map((w) => pctToPx(w))
  const path = edgePath(shape, sourceX, sourceY, targetX, targetY, wpsPx)

  const animate = liveMode && (health === 'up' || health === 'degraded')
  const baseWidth = (kindStyle.widthMul || 1) * 3
  const flowWidth = (kindStyle.widthMul || 1) * 1.5

  const utilPct = live ? Math.max(live.source.util_pct || 0, live.target.util_pct || 0) : null
  const utilStroke = live && utilPct != null ? utilizationColor(utilPct) : null

  const srcIf = link.metadata?.src_interface || live?.source.if_name
  const dstIf = link.metadata?.dst_interface || live?.target.if_name
  const bps = live ? Math.max(live.source.in_bps || 0, live.source.out_bps || 0, live.target.in_bps || 0, live.target.out_bps || 0) : 0

  const editable = !!selected && !liveMode && !!setWaypoints
  const interiorWps = wpsPx // already interior (source/target excluded)

  // Drag an existing waypoint. Uses window listeners + a base snapshot so the
  // drag survives the re-renders that live updates trigger.
  const startDrag = (i: number) => (e: React.PointerEvent) => {
    e.stopPropagation()
    e.preventDefault()
    const base = interiorWps.slice()
    const move = (ev: PointerEvent) => {
      const fp = rf.screenToFlowPosition({ x: ev.clientX, y: ev.clientY })
      setWaypoints!(base.map((w, idx) => (idx === i ? fp : w)), false)
    }
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      const fp = rf.screenToFlowPosition({ x: ev.clientX, y: ev.clientY })
      setWaypoints!(base.map((w, idx) => (idx === i ? fp : w)), true)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const addWaypoint = (segIndex: number, at: Pt) => (e: React.MouseEvent) => {
    e.stopPropagation()
    setWaypoints!([...interiorWps.slice(0, segIndex), at, ...interiorWps.slice(segIndex)], true)
  }

  const removeWaypoint = (i: number) => (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setWaypoints!(interiorWps.filter((_, idx) => idx !== i), true)
  }

  const r = 9 / zoom
  const rAdd = 5.5 / zoom
  const sw = 1.6 / zoom

  return (
    <>
      <path d={path.d} fill="none" stroke="transparent" strokeWidth={14} vectorEffect="non-scaling-stroke" className="react-flow__edge-interaction" />
      {(kindStyle.accent || selected) && (
        <path
          d={path.d}
          fill="none"
          stroke={selected ? 'rgb(var(--primary))' : (kindStyle.accent as string)}
          strokeOpacity={selected ? 0.6 : 0.45}
          strokeWidth={selected ? baseWidth + 3 : baseWidth + 1.5}
          vectorEffect="non-scaling-stroke"
        />
      )}
      <path
        d={path.d}
        fill="none"
        vectorEffect="non-scaling-stroke"
        strokeWidth={baseWidth}
        strokeDasharray={kindStyle.dash}
        className={cn(utilStroke || color, 'opacity-70')}
      />
      {animate && (
        <path
          d={path.d}
          fill="none"
          vectorEffect="non-scaling-stroke"
          strokeWidth={flowWidth}
          className={cn(utilStroke || color, utilPct != null && utilPct >= 60 ? 'nm-flow' : 'nm-flow-slow')}
        />
      )}

      {/* Bend editing — handles appear when the link is selected in design mode.
          pointerEvents must be re-enabled: the parent .react-flow__edges layer
          is pointer-events:none and RF only re-enables it for edge paths. */}
      {editable && (
        <g className="nodrag nopan" style={{ pointerEvents: 'all' }}>
          {/* Add-waypoint affordances at each segment midpoint */}
          {segmentMidpoints(path.vertices).map((m) => (
            <circle
              key={`add-${m.index}`}
              cx={m.x}
              cy={m.y}
              r={rAdd}
              className="cursor-copy fill-primary/30 stroke-primary"
              strokeWidth={sw}
              onClick={addWaypoint(m.index, { x: m.x, y: m.y })}
            />
          ))}
          {/* Draggable waypoints (right-click / dbl-click to remove) */}
          {interiorWps.map((w, i) => (
            <circle
              key={`wp-${i}`}
              cx={w.x}
              cy={w.y}
              r={r}
              className="cursor-grab fill-primary stroke-surface"
              strokeWidth={sw}
              onPointerDown={startDrag(i)}
              onContextMenu={removeWaypoint(i)}
              onDoubleClick={removeWaypoint(i)}
            />
          ))}
        </g>
      )}

      <EdgeLabelRenderer>
        {srcIf && <EdgeChip x={path.near.x} y={path.near.y} variant="iface">{srcIf}</EdgeChip>}
        {dstIf && <EdgeChip x={path.far.x} y={path.far.y} variant="iface">{dstIf}</EdgeChip>}
        {showThroughput && live && bps > 0 && (
          <EdgeChip
            x={path.mid.x}
            y={path.mid.y}
            variant="live"
            tone={utilPct != null && utilPct >= 85 ? 'danger' : utilPct != null && utilPct >= 60 ? 'warning' : 'success'}
          >
            {formatBps(bps)}{utilPct != null && utilPct > 0 ? ` · ${utilPct.toFixed(0)}%` : ''}
          </EdgeChip>
        )}
      </EdgeLabelRenderer>
    </>
  )
}

function EdgeChip({ x, y, variant, tone, children }: {
  x: number; y: number; variant: 'iface' | 'live'; tone?: 'success' | 'warning' | 'danger'; children: React.ReactNode
}) {
  const cls =
    variant === 'iface'
      ? 'bg-surface/95 text-text2 border-border'
      : tone === 'danger' ? 'bg-danger/15 text-danger border-danger/40'
        : tone === 'warning' ? 'bg-warning/15 text-warning border-warning/40'
          : 'bg-success/15 text-success border-success/40'
  return (
    <div className="nodrag nopan pointer-events-none absolute" style={{ transform: `translate(-50%, -50%) translate(${x}px, ${y}px)` }}>
      <div className={cn('rounded border px-1 py-px font-mono text-[9px] font-semibold leading-none tracking-tight shadow-sm backdrop-blur', cls)}>
        {children}
      </div>
    </div>
  )
}

export const NetworkEdge = memo(NetworkEdgeImpl)
