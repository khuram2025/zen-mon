import { memo, useRef } from 'react'
import { EdgeLabelRenderer, useInternalNode, useReactFlow, useStore, type EdgeProps } from '@xyflow/react'
import { cn } from '@/lib/utils'
import {
  DISC_CX,
  DISC_CY,
  LINK_KIND_STYLE,
  STATUS_COLOR,
  anchorOnCircle,
  anchorOnRect,
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
  /** Perpendicular px offset so parallel links between the same pair don't overlap. */
  parallelOffset?: number
  setWaypoints?: (wpsPx: Pt[], commit: boolean) => void
  setIfacePos?: (which: 'src' | 'dst', pos: { dx?: number; dy?: number; rot?: number }, commit: boolean) => void
}

/** Centre + shape info for an endpoint — devices are discs, annotations are
 *  boxes (anchor on the rectangle border instead of a circle). */
function endpointGeom(n: ReturnType<typeof useInternalNode>) {
  if (!n) return null
  const p = n.internals.positionAbsolute
  if (n.type === 'shape') {
    const w = n.measured?.width ?? (typeof n.width === 'number' ? n.width : 80)
    const h = n.measured?.height ?? (typeof n.height === 'number' ? n.height : 60)
    return { center: { x: p.x + w / 2, y: p.y + h / 2 }, rect: true as const, halfW: w / 2, halfH: h / 2 }
  }
  return { center: { x: p.x + DISC_CX, y: p.y + DISC_CY }, rect: false as const, halfW: 0, halfH: 0 }
}

function NetworkEdgeImpl({ source, target, sourceX, sourceY, targetX, targetY, data, selected }: EdgeProps) {
  const d = data as NetworkEdgeData
  const { link, sourceStatus, targetStatus, live, liveMode, showThroughput, parallelOffset = 0, setWaypoints, setIfacePos } = d
  const rf = useReactFlow()
  const zoom = useStore((s) => s.transform[2])

  // Floating endpoints: anchor each link on the node's outer circle, pointing
  // toward its first/last bend (or the other node). Many cables fan out.
  const srcNode = useInternalNode(source)
  const tgtNode = useInternalNode(target)
  const sg = endpointGeom(srcNode)
  const tg = endpointGeom(tgtNode)
  const sc = sg?.center ?? { x: sourceX, y: sourceY }
  const tc = tg?.center ?? { x: targetX, y: targetY }

  const wps = linkWaypoints(link).map((w) => pctToPx(w))
  // Perpendicular shift so multiple cables between the same two devices run as
  // parallel lines with a visible gap instead of stacking on one line.
  const dx = tc.x - sc.x, dy = tc.y - sc.y
  const dlen = Math.hypot(dx, dy) || 1
  const perp = { x: -dy / dlen, y: dx / dlen }
  const shift = (p: Pt): Pt => (parallelOffset ? { x: p.x + perp.x * parallelOffset, y: p.y + perp.y * parallelOffset } : p)
  const srcToward = wps[0] ?? tc
  const tgtToward = wps[wps.length - 1] ?? sc
  const srcAnchor = shift(sg?.rect ? anchorOnRect(sc, srcToward, sg.halfW, sg.halfH) : anchorOnCircle(sc, srcToward))
  const tgtAnchor = shift(tg?.rect ? anchorOnRect(tc, tgtToward, tg.halfW, tg.halfH) : anchorOnCircle(tc, tgtToward))

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
  // Port labels can be dragged (and rotated when the link is selected) in design mode.
  const editChip = !liveMode && !!setIfacePos
  const ipos = link.metadata?.iface_pos || {}

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

  // Drag a whole orthogonal segment perpendicular (move left/right or up/down),
  // keeping right angles — instead of kinking a bend at the grab point.
  const dragOrthoSegment = (e: React.PointerEvent) => {
    // Routed vertices (corners included): first anchor + each segment end.
    const rv: Pt[] = [{ x: path.segments[0].ax, y: path.segments[0].ay }, ...path.segments.map((s) => ({ x: s.bx, y: s.by }))]
    if (rv.length < 2) return
    const fp0 = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY })
    const k = nearestSegmentIndex(rv, fp0) // segment between rv[k] and rv[k+1]
    const a = rv[k], b = rv[k + 1]
    const horizontal = Math.abs(a.y - b.y) <= Math.abs(a.x - b.x)
    const last = rv.length - 1
    const apply = (ev: PointerEvent, commit: boolean) => {
      const fp = rf.screenToFlowPosition({ x: ev.clientX, y: ev.clientY })
      const nrv = rv.map((p) => ({ ...p }))
      if (horizontal) { nrv[k].y = fp.y; nrv[k + 1].y = fp.y } else { nrv[k].x = fp.x; nrv[k + 1].x = fp.x }
      // Interior corners become waypoints; if we moved an end that sits on a
      // node anchor, keep that offset point as a waypoint so the stub bends.
      let wpts = nrv.slice(1, last)
      if (k === 0) wpts = [nrv[0], ...wpts]
      if (k + 1 === last) wpts = [...wpts, nrv[last]]
      setWaypoints!(wpts, commit)
    }
    const move = (ev: PointerEvent) => apply(ev, false)
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      apply(ev, true)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  // Grab the selected link to reshape it. Orthogonal = move the segment;
  // curve/straight = insert a bend at the grab point and drag it.
  const onPathPointerDown = (e: React.PointerEvent) => {
    if (!editable) return // not selected: let the click select the edge
    e.stopPropagation()
    e.preventDefault()
    if (shape === 'orthogonal') { dragOrthoSegment(e); return }
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
        {srcIf && (
          <IfaceChip x={path.near.x} y={path.near.y} value={srcIf} pos={ipos.src} editable={editChip} showRotate={editChip && !!selected} zoom={zoom}
            onMove={(p, commit) => setIfacePos!('src', p, commit)} onRotate={(rot, commit) => setIfacePos!('src', { rot }, commit)} />
        )}
        {dstIf && (
          <IfaceChip x={path.far.x} y={path.far.y} value={dstIf} pos={ipos.dst} editable={editChip} showRotate={editChip && !!selected} zoom={zoom}
            onMove={(p, commit) => setIfacePos!('dst', p, commit)} onRotate={(rot, commit) => setIfacePos!('dst', { rot }, commit)} />
        )}
        {showThroughput && live && bps > 0 && (
          <EdgeChip x={path.mid.x} y={path.mid.y} variant="live" tone={utilPct != null && utilPct >= 85 ? 'danger' : utilPct != null && utilPct >= 60 ? 'warning' : 'success'}>
            {formatBps(bps)}{utilPct != null && utilPct > 0 ? ` · ${utilPct.toFixed(0)}%` : ''}
          </EdgeChip>
        )}
      </EdgeLabelRenderer>
    </>
  )
}

/* Port (interface) label that the admin can drag along/around the cable and
 * rotate. Position = cable anchor (x,y) + persisted offset; a rotate grip
 * appears above it when the link is selected. */
function IfaceChip({ x, y, value, pos, editable, showRotate, zoom, onMove, onRotate }: {
  x: number; y: number; value: string
  pos?: { dx?: number; dy?: number; rot?: number }
  editable: boolean; showRotate: boolean; zoom: number
  onMove: (p: { dx: number; dy: number }, commit: boolean) => void
  onRotate: (rot: number, commit: boolean) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const dx = pos?.dx || 0, dy = pos?.dy || 0, rot = pos?.rot || 0

  const startMove = (e: React.PointerEvent) => {
    if (!editable) return
    e.stopPropagation(); e.preventDefault()
    const sx = e.clientX, sy = e.clientY
    const base = { dx, dy }
    const move = (ev: PointerEvent) => onMove({ dx: base.dx + (ev.clientX - sx) / zoom, dy: base.dy + (ev.clientY - sy) / zoom }, false)
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up)
      onMove({ dx: base.dx + (ev.clientX - sx) / zoom, dy: base.dy + (ev.clientY - sy) / zoom }, true)
    }
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up)
  }

  const startRotate = (e: React.PointerEvent) => {
    e.stopPropagation(); e.preventDefault()
    const rect = ref.current!.getBoundingClientRect()
    const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2
    const ang = (ev: PointerEvent) => Math.round(Math.atan2(ev.clientY - cy, ev.clientX - cx) * 180 / Math.PI + 90)
    const move = (ev: PointerEvent) => onRotate(ang(ev), false)
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up)
      onRotate(ang(ev), true)
    }
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up)
  }

  return (
    <div
      ref={ref}
      className={cn('nodrag nopan absolute', editable ? 'pointer-events-auto' : 'pointer-events-none')}
      style={{ transform: `translate(-50%, -50%) translate(${x + dx}px, ${y + dy}px) rotate(${rot}deg)` }}
    >
      <div
        onPointerDown={startMove}
        className={cn('relative rounded border border-border bg-surface/95 px-1 py-px font-mono text-[9px] font-semibold leading-none tracking-tight text-text2 shadow-sm backdrop-blur', editable && 'cursor-move hover:border-primary/60')}
      >
        {value}
        {showRotate && (
          <span
            onPointerDown={startRotate}
            title="Drag to rotate"
            className="absolute -top-3 left-1/2 h-2.5 w-2.5 -translate-x-1/2 cursor-grab rounded-full border border-surface bg-primary shadow"
          />
        )}
      </div>
    </div>
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
