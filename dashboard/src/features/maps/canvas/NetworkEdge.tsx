import { memo } from 'react'
import { EdgeLabelRenderer, useInternalNode, useReactFlow, useStore, type EdgeProps } from '@xyflow/react'
import { cn } from '@/lib/utils'
import {
  DISC_CX,
  DISC_CY,
  LINK_KIND_STYLE,
  STATUS_COLOR,
  anchorOnCircle,
  edgePath,
  formatBps,
  linkHealth,
  linkKindOf,
  linkShapeOf,
  linkWaypoints,
  nearestSegmentIndex,
  pctToPx,
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
  setWaypoints?: (wpsPx: Pt[], commit: boolean) => void
}

function nodeCenter(n: ReturnType<typeof useInternalNode>): Pt | null {
  if (!n) return null
  const p = n.internals.positionAbsolute
  return { x: p.x + DISC_CX, y: p.y + DISC_CY }
}

function NetworkEdgeImpl({ source, target, sourceX, sourceY, targetX, targetY, data, selected }: EdgeProps) {
  const d = data as NetworkEdgeData
  const { link, sourceStatus, targetStatus, live, liveMode, showThroughput, setWaypoints } = d
  const rf = useReactFlow()
  const zoom = useStore((s) => s.transform[2])

  // Floating endpoints: anchor each link on the node's outer circle, pointing
  // toward its first/last bend (or the other node). Many cables fan out.
  const srcNode = useInternalNode(source)
  const tgtNode = useInternalNode(target)
  const sc = nodeCenter(srcNode) ?? { x: sourceX, y: sourceY }
  const tc = nodeCenter(tgtNode) ?? { x: targetX, y: targetY }

  const wps = linkWaypoints(link).map((w) => pctToPx(w))
  const srcAnchor = anchorOnCircle(sc, wps[0] ?? tc)
  const tgtAnchor = anchorOnCircle(tc, wps[wps.length - 1] ?? sc)

  const shape = linkShapeOf(link)
  const path = edgePath(shape, srcAnchor.x, srcAnchor.y, tgtAnchor.x, tgtAnchor.y, wps)

  const health = linkHealth(sourceStatus, targetStatus)
  const color = STATUS_COLOR[health].line
  const kind = linkKindOf(link)
  const kindStyle = LINK_KIND_STYLE[kind] || {}
  const animate = liveMode && (health === 'up' || health === 'degraded')
  const baseWidth = (kindStyle.widthMul || 1) * 3
  const flowWidth = (kindStyle.widthMul || 1) * 1.5

  const utilPct = live ? Math.max(live.source.util_pct || 0, live.target.util_pct || 0) : null
  const utilStroke = live && utilPct != null ? utilizationColor(utilPct) : null

  const srcIf = link.metadata?.src_interface || live?.source.if_name
  const dstIf = link.metadata?.dst_interface || live?.target.if_name
  const bps = live ? Math.max(live.source.in_bps || 0, live.source.out_bps || 0, live.target.in_bps || 0, live.target.out_bps || 0) : 0

  const editable = !!selected && !liveMode && !!setWaypoints

  // Drag a bend: shared by existing-waypoint handles and grab-anywhere. `base`
  // is the waypoint list snapshot, `idx` the index being moved.
  const dragBend = (base: Pt[], idx: number) => {
    const move = (ev: PointerEvent) => {
      const fp = rf.screenToFlowPosition({ x: ev.clientX, y: ev.clientY })
      setWaypoints!(base.map((w, i) => (i === idx ? fp : w)), false)
    }
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      const fp = rf.screenToFlowPosition({ x: ev.clientX, y: ev.clientY })
      setWaypoints!(base.map((w, i) => (i === idx ? fp : w)), true)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  // Grab anywhere on the selected link → insert a bend there and drag it.
  const onPathPointerDown = (e: React.PointerEvent) => {
    if (!editable) return // not selected: let the click select the edge
    e.stopPropagation()
    e.preventDefault()
    const fp = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY })
    const idx = nearestSegmentIndex(path.vertices, fp)
    const base = [...wps.slice(0, idx), fp, ...wps.slice(idx)]
    setWaypoints!(base, false)
    dragBend(base, idx)
  }

  const startWpDrag = (i: number) => (e: React.PointerEvent) => {
    e.stopPropagation()
    e.preventDefault()
    dragBend(wps.slice(), i)
  }

  const removeWp = (i: number) => (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setWaypoints!(wps.filter((_, idx) => idx !== i), true)
  }

  const r = 9 / zoom
  const sw = 1.6 / zoom

  return (
    <>
      {/* Wide invisible hit area — selects when unselected, grabs-to-bend when selected. */}
      <path
        d={path.d}
        fill="none"
        stroke="transparent"
        strokeWidth={16}
        vectorEffect="non-scaling-stroke"
        className={cn('react-flow__edge-interaction', editable && 'cursor-grab')}
        style={editable ? { pointerEvents: 'stroke' } : undefined}
        onPointerDown={onPathPointerDown}
      />
      {(kindStyle.accent || selected) && (
        <path d={path.d} fill="none" stroke={selected ? 'rgb(var(--primary))' : (kindStyle.accent as string)} strokeOpacity={selected ? 0.6 : 0.45} strokeWidth={selected ? baseWidth + 3 : baseWidth + 1.5} vectorEffect="non-scaling-stroke" style={{ pointerEvents: 'none' }} />
      )}
      <path d={path.d} fill="none" vectorEffect="non-scaling-stroke" strokeWidth={baseWidth} strokeDasharray={kindStyle.dash} className={cn(utilStroke || color, 'opacity-70')} style={{ pointerEvents: 'none' }} />
      {animate && (
        <path d={path.d} fill="none" vectorEffect="non-scaling-stroke" strokeWidth={flowWidth} className={cn(utilStroke || color, utilPct != null && utilPct >= 60 ? 'nm-flow' : 'nm-flow-slow')} style={{ pointerEvents: 'none' }} />
      )}

      {/* Existing bend handles (drag to move, right-click/dbl-click to remove). */}
      {editable && (
        <g style={{ pointerEvents: 'all' }}>
          {wps.map((w, i) => (
            <circle
              key={`wp-${i}`}
              cx={w.x}
              cy={w.y}
              r={r}
              className="cursor-grab fill-primary stroke-surface"
              strokeWidth={sw}
              onPointerDown={startWpDrag(i)}
              onContextMenu={removeWp(i)}
              onDoubleClick={removeWp(i)}
            />
          ))}
        </g>
      )}

      <EdgeLabelRenderer>
        {srcIf && <EdgeChip x={path.near.x} y={path.near.y} variant="iface">{srcIf}</EdgeChip>}
        {dstIf && <EdgeChip x={path.far.x} y={path.far.y} variant="iface">{dstIf}</EdgeChip>}
        {showThroughput && live && bps > 0 && (
          <EdgeChip x={path.mid.x} y={path.mid.y} variant="live" tone={utilPct != null && utilPct >= 85 ? 'danger' : utilPct != null && utilPct >= 60 ? 'warning' : 'success'}>
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
