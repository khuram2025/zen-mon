import { memo, useRef, useState } from 'react'
import { EdgeLabelRenderer, useInternalNode, useReactFlow, useStore, type EdgeProps } from '@xyflow/react'
import { cn } from '@/lib/utils'
import {
  DISC,
  DISC_CX,
  DISC_CY,
  LINK_KIND_STYLE,
  STATUS_COLOR,
  anchorOnCircle,
  anchorOnRect,
  edgePath,
  formatBps,
  linkFlow,
  linkHealth,
  linkKindOf,
  linkShapeOf,
  linkWaypoints,
  nearestSegmentIndex,
  pctToPx,
  pruneOrthoWaypoints,
  pruneStraightWaypoints,
  routeOrthoEdge,
  utilHex,
  utilizationColor,
  particleSpec,
  type EdgePathResult,
  type EndGeom,
  type LiveLinkData,
  type LiveInterface,
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
  /** Drop a detached cable end at pt — MapCanvas hit-tests and reconnects. */
  reconnectEnd?: (which: 'src' | 'dst', pt: Pt) => void
}

/** Centre + shape info for an endpoint — devices are discs (radius follows the
 *  user's size scale), annotations are boxes (anchor on the rectangle border). */
function endpointGeom(n: ReturnType<typeof useInternalNode>): EndGeom | null {
  if (!n) return null
  const p = n.internals.positionAbsolute
  if (n.type === 'shape') {
    const w = n.measured?.width ?? (typeof n.width === 'number' ? n.width : 80)
    const h = n.measured?.height ?? (typeof n.height === 'number' ? n.height : 60)
    return { center: { x: p.x + w / 2, y: p.y + h / 2 }, rect: true, halfW: w / 2, halfH: h / 2 }
  }
  const scale = ((n.data as any)?.node?.metadata?.size_scale as number) || 1
  return { center: { x: p.x + DISC_CX, y: p.y + DISC_CY }, rect: false, halfW: 0, halfH: 0, r: (DISC * scale) / 2 + 2 }
}

/* One animated particle stream along the cable. `reverse` runs target→source.
 * SMIL animateMotion runs on the compositor — zero React re-renders per frame,
 * so dozens of busy links stay cheap. Negative begin offsets pre-fill the
 * stream so dots are spread along the cable from the first frame. */
function ParticleStream({ pathId, count, dur, color, r, reverse }: {
  pathId: string; count: number; dur: number; color: string; r: number; reverse?: boolean
}) {
  return (
    <g style={{ pointerEvents: 'none' }}>
      {Array.from({ length: count }, (_, i) => (
        <circle key={i} r={r} fill={color} opacity={0.9}>
          <animateMotion
            dur={`${dur}s`}
            begin={`${-(i * dur) / count}s`}
            repeatCount="indefinite"
            calcMode="linear"
            keyPoints={reverse ? '1;0' : '0;1'}
            keyTimes="0;1"
          >
            <mpath href={`#${pathId}`} />
          </animateMotion>
        </circle>
      ))}
    </g>
  )
}

function NetworkEdgeImpl({ source, target, sourceX, sourceY, targetX, targetY, data, selected }: EdgeProps) {
  const d = data as NetworkEdgeData
  const { link, sourceStatus, targetStatus, live, liveMode, showThroughput, parallelOffset = 0, setWaypoints, setIfacePos, reconnectEnd } = d
  const rf = useReactFlow()
  const zoom = useStore((s) => s.transform[2])
  const [hover, setHover] = useState(false)
  /** While dragging a bend, freeze anchor positions so the port doesn't chase the cursor. */
  const [anchorFreeze, setAnchorFreeze] = useState<{ sa: Pt; ta: Pt } | null>(null)
  /** While dragging a cable end off its device/shape, the detached tip follows the cursor. */
  const [endDrag, setEndDrag] = useState<{ which: 'src' | 'dst'; pt: Pt } | null>(null)

  // Floating endpoints: anchor each link on the node's outer circle, pointing
  // toward its first/last bend (or the other node). Many cables fan out.
  const srcNode = useInternalNode(source)
  const tgtNode = useInternalNode(target)
  const sg = endpointGeom(srcNode)
  const tg = endpointGeom(tgtNode)
  const sc = sg?.center ?? { x: sourceX, y: sourceY }
  const tc = tg?.center ?? { x: targetX, y: targetY }

  const storedWps = linkWaypoints(link).map((w) => pctToPx(w))
  // Perpendicular shift so multiple cables between the same two devices run as
  // parallel lines with a visible gap instead of stacking on one line.
  const dx = tc.x - sc.x, dy = tc.y - sc.y
  const dlen = Math.hypot(dx, dy) || 1
  const perp = { x: -dy / dlen, y: dx / dlen }
  const shift = (p: Pt): Pt => (parallelOffset ? { x: p.x + perp.x * parallelOffset, y: p.y + perp.y * parallelOffset } : p)
  const shape = linkShapeOf(link)
  const isOrtho = shape === 'orthogonal'

  let path: EdgePathResult
  /** Final rendered polyline (ortho only) — its interior points are the visual corners. */
  let clipped: Pt[] = []
  let srcAnchor: Pt
  let tgtAnchor: Pt
  if (isOrtho) {
    // draw.io-style: route centre-to-centre through the waypoints with right
    // angles, then clip at the shape borders — the cable always exits a
    // device/shape axis-aligned, and the anchor glides along the border.
    const srcPt = endDrag?.which === 'src' ? endDrag.pt : shift(sc)
    const tgtPt = endDrag?.which === 'dst' ? endDrag.pt : shift(tc)
    const res = routeOrthoEdge(
      srcPt, tgtPt, storedWps,
      endDrag?.which === 'src' ? null : sg,
      endDrag?.which === 'dst' ? null : tg,
    )
    path = res
    clipped = res.clipped
    srcAnchor = clipped[0]
    tgtAnchor = clipped[clipped.length - 1]
  } else {
    const srcToward = storedWps[0] ?? (endDrag?.which === 'dst' ? endDrag.pt : tc)
    const tgtToward = storedWps[storedWps.length - 1] ?? (endDrag?.which === 'src' ? endDrag.pt : sc)
    const liveSrcAnchor = shift(sg?.rect ? anchorOnRect(sc, srcToward, sg.halfW, sg.halfH) : anchorOnCircle(sc, srcToward, sg?.r))
    const liveTgtAnchor = shift(tg?.rect ? anchorOnRect(tc, tgtToward, tg.halfW, tg.halfH) : anchorOnCircle(tc, tgtToward, tg?.r))
    srcAnchor = endDrag?.which === 'src' ? endDrag.pt : (anchorFreeze?.sa ?? liveSrcAnchor)
    tgtAnchor = endDrag?.which === 'dst' ? endDrag.pt : (anchorFreeze?.ta ?? liveTgtAnchor)
    path = edgePath(shape, srcAnchor.x, srcAnchor.y, tgtAnchor.x, tgtAnchor.y, storedWps)
  }
  const pathId = `nme-${link.id}`

  const health = linkHealth(sourceStatus, targetStatus)
  const color = STATUS_COLOR[health].line
  const kind = linkKindOf(link)
  const kindStyle = LINK_KIND_STYLE[kind] || {}
  const widthScale = Math.max(0.4, Math.min(4, Number(link.metadata?.width_scale) || 1))
  const baseWidth = (kindStyle.widthMul || 1) * 3 * widthScale

  const flow = liveMode ? linkFlow(live) : null
  const utilPct = flow?.utilPct ?? null
  const utilStroke = flow ? utilizationColor(utilPct) : null
  // A matched-but-down interface overrides everything: the cable is in fault.
  const faulted = !!flow?.ifaceDown || health === 'down'
  const idle = !!flow && !faulted && flow.total < 1000

  const srcIf = link.metadata?.src_interface || live?.source.if_name
  const dstIf = link.metadata?.dst_interface || live?.target.if_name

  // Particle streams: forward (src→tgt) rides source-out, reverse rides source-in.
  const fwdSpec = flow && !faulted ? particleSpec(flow.fwd, utilPct) : { count: 0, dur: 0 }
  const revSpec = flow && !faulted ? particleSpec(flow.rev, utilPct) : { count: 0, dur: 0 }
  const particleColor = utilHex(utilPct)
  const particleR = 3.2 / Math.max(0.55, Math.min(1.4, zoom))
  const hot = utilPct != null && utilPct >= 60

  const editable = !!selected && !liveMode && !!setWaypoints
  // Port labels can be dragged (and rotated when the link is selected) in design mode.
  const editChip = !liveMode && !!setIfacePos
  const ipos = link.metadata?.iface_pos || {}

  // On commit, drop bends that no longer affect the route (e.g. dragged onto a
  // straight segment). Orthogonal pruning compares centre-routed polylines, so
  // it matches exactly what is rendered. Tolerances are in screen px (scaled
  // to flow units) so "drop the dot on the line to remove it" works by hand.
  const commitWps = (pts: Pt[], commit: boolean) => {
    if (!commit) {
      setWaypoints!(pts, false)
      return
    }
    if (isOrtho) setWaypoints!(pruneOrthoWaypoints(shift(sc), shift(tc), pts, 5 / zoom), true)
    else setWaypoints!(pruneStraightWaypoints(srcAnchor, tgtAnchor, pts, 9 / zoom), true)
  }

  const handleHit = 16 / Math.max(0.55, Math.min(1.4, zoom))

  /** Shared pointer-drag runner: `apply` gets the flow position every frame
   *  and once more with commit=true on release. */
  const runDrag = (apply: (fp: Pt, commit: boolean) => void, onDone?: () => void) => {
    const move = (ev: PointerEvent) => apply(rf.screenToFlowPosition({ x: ev.clientX, y: ev.clientY }), false)
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      apply(rf.screenToFlowPosition({ x: ev.clientX, y: ev.clientY }), true)
      onDone?.()
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  /* ── Straight / curve editing ── stored waypoints are the drag handles; a
   * translucent mid-segment handle inserts a new bend (draw.io style). */
  const dragStoredWp = (idx: number, base: Pt[]) => {
    setAnchorFreeze({ sa: srcAnchor, ta: tgtAnchor })
    runDrag(
      (fp, commit) => commitWps(base.map((w, i) => (i === idx ? fp : w)), commit),
      () => setAnchorFreeze(null),
    )
  }

  const insertWpDrag = (segIdx: number, at: Pt) => {
    const insertAt = Math.min(segIdx, storedWps.length)
    const base = [...storedWps.slice(0, insertAt), at, ...storedWps.slice(insertAt)]
    commitWps(base, false)
    dragStoredWp(insertAt, base)
  }

  /* ── Orthogonal editing ── handles live on the RENDERED polyline: corner
   * dots move a bend freely; mid-segment handles slide the whole segment
   * perpendicular (the draw.io interaction). Both materialise the current
   * route into waypoints first so the rest of the cable stays put. */
  const orthoCorners = isOrtho ? clipped.slice(1, -1) : []

  const dragOrthoCorner = (idx: number) => {
    const base = orthoCorners.map((p) => ({ ...p }))
    runDrag((fp, commit) => commitWps(base.map((w, i) => (i === idx ? fp : w)), commit))
  }

  const removeOrthoCorner = (idx: number) => {
    commitWps(orthoCorners.filter((_, i) => i !== idx), true)
  }

  const dragOrthoSegment = (s: number) => {
    const P = clipped.map((p) => ({ ...p }))
    if (P.length < 2) return
    let W = P.slice(1, -1)
    let a = s - 1
    let b = s
    // End segments are bounded by the anchor itself — materialise a copy of
    // the anchor point so sliding the segment grows a right-angle jog there.
    if (s === 0) { W = [{ ...P[0] }, ...W]; a = 0; b = 1 }
    if (s === P.length - 2) { W = [...W, { ...P[P.length - 1] }]; b = W.length - 1 }
    const horizontal = Math.abs(P[s].y - P[s + 1].y) < 0.5
    runDrag((fp, commit) => {
      const next = W.map((w, i) => (
        i === a || i === b ? (horizontal ? { x: w.x, y: fp.y } : { x: fp.x, y: w.y }) : w
      ))
      commitWps(next, commit)
    })
  }

  // Detach a cable end: the tip follows the cursor; on drop MapCanvas hit-tests
  // the target device/shape and reconnects (or snaps back if dropped on nothing).
  const startEndDrag = (which: 'src' | 'dst') => (e: React.PointerEvent) => {
    if (e.button !== 0) return
    e.stopPropagation()
    e.preventDefault()
    const move = (ev: PointerEvent) => {
      const fp = rf.screenToFlowPosition({ x: ev.clientX, y: ev.clientY })
      setEndDrag({ which, pt: fp })
    }
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      const fp = rf.screenToFlowPosition({ x: ev.clientX, y: ev.clientY })
      setEndDrag(null)
      reconnectEnd?.(which, fp)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  // Grab the selected link to reshape it: ortho slides the grabbed segment,
  // straight/curve drops a new bend right under the cursor and drags it.
  const onPathPointerDown = (e: React.PointerEvent) => {
    if (!editable || e.button !== 0) return // not selected: let the click select the edge
    e.stopPropagation()
    e.preventDefault()
    const fp0 = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY })
    if (isOrtho) {
      dragOrthoSegment(nearestSegmentIndex(clipped, fp0))
      return
    }
    insertWpDrag(nearestSegmentIndex(path.vertices, fp0), fp0)
  }

  // Double-click the cable → drop a bend point at that spot.
  const onPathDoubleClick = (e: React.MouseEvent) => {
    if (!editable) return
    e.stopPropagation()
    e.preventDefault()
    const fp = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY })
    if (isOrtho) {
      const s = nearestSegmentIndex(clipped, fp)
      const W = clipped.slice(1, -1)
      const insertAt = Math.max(0, Math.min(s, W.length))
      commitWps([...W.slice(0, insertAt), fp, ...W.slice(insertAt)], true)
      return
    }
    const segIdx = nearestSegmentIndex(path.vertices, fp)
    const insertAt = Math.min(segIdx, storedWps.length)
    commitWps([...storedWps.slice(0, insertAt), fp, ...storedWps.slice(insertAt)], true)
  }

  const startWpDrag = (i: number) => (e: React.PointerEvent) => {
    if (e.button !== 0) return
    e.stopPropagation()
    e.preventDefault()
    if (isOrtho) dragOrthoCorner(i)
    else dragStoredWp(i, storedWps.slice())
  }

  const removeWp = (i: number) => (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (isOrtho) removeOrthoCorner(i)
    else commitWps(storedWps.filter((_, idx) => idx !== i), true)
  }

  const startMidDrag = (m: { index: number; x: number; y: number; horizontal: boolean }) => (e: React.PointerEvent) => {
    if (e.button !== 0) return
    e.stopPropagation()
    e.preventDefault()
    if (isOrtho) dragOrthoSegment(m.index)
    else insertWpDrag(m.index, { x: m.x, y: m.y })
  }

  // Mid-segment handles: ortho gets one per rendered segment (slide), the
  // others get one per user segment (insert a bend). Tiny segments skipped.
  // The smooth curve passes exactly through each segment midpoint, so the
  // handles sit ON the cable — except the no-waypoint arc, which uses the
  // arc's own midpoint.
  const editVerts = isOrtho ? clipped : path.vertices
  const midHandles = !editable
    ? []
    : shape === 'curve' && storedWps.length === 0
      ? [{ index: 0, x: path.mid.x, y: path.mid.y, horizontal: false, len: dlen }].filter((m) => m.len > 30)
      : editVerts.slice(0, -1).map((p, i) => {
          const q = editVerts[i + 1]
          return {
            index: i,
            x: (p.x + q.x) / 2,
            y: (p.y + q.y) / 2,
            horizontal: Math.abs(p.y - q.y) < 0.5,
            len: Math.hypot(q.x - p.x, q.y - p.y),
          }
        }).filter((m) => m.len > 30)

  // Real bend handles: ortho corners on the rendered route, otherwise the
  // stored waypoints themselves.
  const bendHandles = isOrtho ? orthoCorners : storedWps

  const r = 9 / zoom

  return (
    <>
      {/* Wide invisible hit area — selects when unselected, grabs-to-bend when
          selected, and reveals the live inspection card on hover. */}
      <path
        d={path.d}
        fill="none"
        stroke="transparent"
        strokeWidth={18}
        vectorEffect="non-scaling-stroke"
        className={cn('react-flow__edge-interaction', editable && 'cursor-grab')}
        style={editable || liveMode ? { pointerEvents: 'stroke' } : undefined}
        onPointerDown={onPathPointerDown}
        onDoubleClick={onPathDoubleClick}
        onPointerEnter={liveMode ? () => setHover(true) : undefined}
        onPointerLeave={liveMode ? () => setHover(false) : undefined}
      >
        {editable && <title>{isOrtho ? 'Drag a segment to slide it · drag a corner to move it · right-click corner to remove' : 'Drag to bend · drag a mid-point handle to add a bend · drag onto line to remove'}</title>}
      </path>

      {/* Selection / kind accent halo */}
      {(kindStyle.accent || selected) && (
        <path d={path.d} fill="none" stroke={selected ? 'rgb(var(--primary))' : (kindStyle.accent as string)} strokeOpacity={selected ? 0.6 : 0.45} strokeWidth={selected ? baseWidth + 3 : baseWidth + 1.5} vectorEffect="non-scaling-stroke" style={{ pointerEvents: 'none' }} />
      )}

      {/* Hot-link glow underlay — saturated cables radiate */}
      {hot && !faulted && (
        <path d={path.d} fill="none" stroke={particleColor} strokeOpacity={0.32} strokeWidth={baseWidth + 6} vectorEffect="non-scaling-stroke" className="nm-hot" style={{ pointerEvents: 'none' }} />
      )}

      {/* Base cable. In live mode the colour IS the utilisation; a faulted
          interface renders the cable red-dashed regardless of traffic. */}
      <path
        id={pathId}
        d={path.d}
        fill="none"
        vectorEffect="non-scaling-stroke"
        strokeWidth={faulted ? baseWidth : hover && liveMode ? baseWidth + 1 : baseWidth}
        strokeDasharray={faulted && liveMode ? '7 5' : kindStyle.dash}
        strokeLinecap="round"
        className={cn(
          faulted && liveMode ? 'stroke-danger' : utilStroke || color,
          idle ? 'opacity-35' : 'opacity-80',
          'transition-opacity',
        )}
        style={{ pointerEvents: 'none' }}
      />

      {/* Directional traffic particles (live mode) */}
      {liveMode && fwdSpec.count > 0 && (
        <ParticleStream pathId={pathId} count={fwdSpec.count} dur={fwdSpec.dur} color={particleColor} r={particleR} />
      )}
      {liveMode && revSpec.count > 0 && (
        <ParticleStream pathId={pathId} count={revSpec.count} dur={revSpec.dur} color={particleColor} r={particleR * 0.78} reverse />
      )}

      {/* Faulted endpoint markers — a red ✕ on the side whose interface is down */}
      {liveMode && flow?.srcDown && <FaultMark x={path.near.x} y={path.near.y} zoom={zoom} />}
      {liveMode && flow?.dstDown && <FaultMark x={path.far.x} y={path.far.y} zoom={zoom} />}

      <EdgeLabelRenderer>
        {/* Bend handles render in the HTML overlay so they stay above device
            nodes — SVG handles near a device disc were buried and felt stuck. */}
        {/* Mid-segment handles (draw.io): ortho slides the segment, others
            insert a bend. Rendered under the corner dots. */}
        {midHandles.map((m) => (
          <MidHandle
            key={`mid-${m.index}`}
            x={m.x}
            y={m.y}
            r={r}
            hit={handleHit}
            cursor={isOrtho ? (m.horizontal ? 'ns-resize' : 'ew-resize') : 'copy'}
            title={isOrtho ? 'Drag to slide this segment' : 'Drag to add a bend here'}
            onPointerDown={startMidDrag(m)}
          />
        ))}
        {editable && bendHandles.map((w, i) => (
          <BendHandle
            key={`wp-${i}`}
            x={w.x}
            y={w.y}
            r={r}
            hit={handleHit}
            onPointerDown={startWpDrag(i)}
            onRemove={removeWp(i)}
          />
        ))}
        {/* Cable-end plugs — drag one off its device/shape and drop it on
            another item to reconnect the link. */}
        {editable && !!reconnectEnd && (
          <>
            <EndpointHandle x={srcAnchor.x} y={srcAnchor.y} r={r} hit={handleHit} active={endDrag?.which === 'src'} onPointerDown={startEndDrag('src')} />
            <EndpointHandle x={tgtAnchor.x} y={tgtAnchor.y} r={r} hit={handleHit} active={endDrag?.which === 'dst'} onPointerDown={startEndDrag('dst')} />
          </>
        )}
        {srcIf && (
          <IfaceChip x={path.near.x} y={path.near.y} value={srcIf} pos={ipos.src} editable={editChip} showRotate={editChip && !!selected} zoom={zoom}
            down={liveMode && !!flow?.srcDown}
            onMove={(p, commit) => setIfacePos!('src', p, commit)} onRotate={(rot, commit) => setIfacePos!('src', { rot }, commit)} />
        )}
        {dstIf && (
          <IfaceChip x={path.far.x} y={path.far.y} value={dstIf} pos={ipos.dst} editable={editChip} showRotate={editChip && !!selected} zoom={zoom}
            down={liveMode && !!flow?.dstDown}
            onMove={(p, commit) => setIfacePos!('dst', p, commit)} onRotate={(rot, commit) => setIfacePos!('dst', { rot }, commit)} />
        )}
        {showThroughput && liveMode && flow && (flow.total > 0 || faulted) && (
          <TrafficChip x={path.mid.x} y={path.mid.y} flow={flow} faulted={faulted} />
        )}
        {liveMode && hover && live && (
          <LinkHoverCard x={path.mid.x} y={path.mid.y} link={link} live={live} flow={flow} />
        )}
      </EdgeLabelRenderer>
    </>
  )
}

function BendHandle({ x, y, r, hit, onPointerDown, onRemove }: {
  x: number; y: number; r: number; hit: number
  onPointerDown: (e: React.PointerEvent) => void
  onRemove: (e: React.MouseEvent) => void
}) {
  return (
    <div
      className="nodrag nopan pointer-events-auto absolute z-[1000] cursor-grab"
      style={{
        transform: `translate(-50%, -50%) translate(${x}px, ${y}px)`,
        width: hit * 2,
        height: hit * 2,
      }}
      onPointerDown={onPointerDown}
      onContextMenu={onRemove}
      onDoubleClick={onRemove}
      title="Drag corner · right-click to remove"
    >
      <div
        className="pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full border border-surface bg-primary shadow-md"
        style={{ width: r * 2, height: r * 2 }}
      />
    </div>
  )
}

/* Mid-segment handle (draw.io's translucent dot): on orthogonal cables it
 * slides the whole segment perpendicular; on straight/curved cables it
 * inserts a new bend right where it sits. */
function MidHandle({ x, y, r, hit, cursor, title, onPointerDown }: {
  x: number; y: number; r: number; hit: number; cursor: string; title: string
  onPointerDown: (e: React.PointerEvent) => void
}) {
  return (
    <div
      className="group nodrag nopan pointer-events-auto absolute z-[999]"
      style={{
        transform: `translate(-50%, -50%) translate(${x}px, ${y}px)`,
        width: hit * 2,
        height: hit * 2,
        cursor,
      }}
      onPointerDown={onPointerDown}
      title={title}
    >
      <div
        className="pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full border border-primary/70 bg-primary/30 shadow-sm transition-colors group-hover:bg-primary/60"
        style={{ width: r * 1.4, height: r * 1.4 }}
      />
    </div>
  )
}

/* Cable-end plug: hollow ring (vs the solid bend dot) that detaches the
 * endpoint when dragged, so a link can be re-plugged into another item. */
function EndpointHandle({ x, y, r, hit, active, onPointerDown }: {
  x: number; y: number; r: number; hit: number; active: boolean
  onPointerDown: (e: React.PointerEvent) => void
}) {
  return (
    <div
      className="nodrag nopan pointer-events-auto absolute z-[1001] cursor-grab"
      style={{
        transform: `translate(-50%, -50%) translate(${x}px, ${y}px)`,
        width: hit * 2,
        height: hit * 2,
      }}
      onPointerDown={onPointerDown}
      title="Drag to unplug · drop on another device or shape to reconnect"
    >
      <div
        className={cn(
          'pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 shadow-md',
          active ? 'border-warning bg-warning/30' : 'border-primary bg-surface',
        )}
        style={{ width: r * 2.2, height: r * 2.2 }}
      />
    </div>
  )
}

/* Red ✕ marker for an operationally-down interface end. */
function FaultMark({ x, y, zoom }: { x: number; y: number; zoom: number }) {
  const s = 7 / Math.max(0.55, Math.min(1.4, zoom))
  return (
    <g transform={`translate(${x}, ${y})`} style={{ pointerEvents: 'none' }} className="nm-fault">
      <circle r={s * 1.5} className="fill-danger/20 stroke-danger" strokeWidth={s * 0.22} />
      <path d={`M ${-s * 0.6} ${-s * 0.6} L ${s * 0.6} ${s * 0.6} M ${s * 0.6} ${-s * 0.6} L ${-s * 0.6} ${s * 0.6}`} className="stroke-danger" strokeWidth={s * 0.3} strokeLinecap="round" />
    </g>
  )
}

/* Directional throughput pill: ▲ fwd ▼ rev with a utilisation bar underneath.
 * This is the at-a-glance "weathermap" reading for NOC walls. */
function TrafficChip({ x, y, flow, faulted }: { x: number; y: number; flow: ReturnType<typeof linkFlow>; faulted: boolean }) {
  if (!flow) return null
  const u = flow.utilPct
  const tone = faulted ? 'danger' : u != null && u >= 85 ? 'danger' : u != null && u >= 60 ? 'warning' : 'success'
  const cls = tone === 'danger' ? 'border-danger/50 text-danger' : tone === 'warning' ? 'border-warning/50 text-warning' : 'border-success/40 text-success'
  return (
    <div className="nodrag nopan pointer-events-none absolute" style={{ transform: `translate(-50%, -50%) translate(${x}px, ${y}px)` }}>
      <div className={cn('overflow-hidden rounded-md border bg-surface/95 shadow-md backdrop-blur', cls)}>
        <div className="flex items-center gap-1.5 px-1.5 py-0.5 font-mono text-[9.5px] font-semibold leading-none tracking-tight">
          {faulted ? (
            <span>LINK DOWN</span>
          ) : (
            <>
              <span className="flex items-center gap-0.5"><span className="text-[8px]">▲</span>{formatBps(flow.fwd)}</span>
              <span className="flex items-center gap-0.5 opacity-80"><span className="text-[8px]">▼</span>{formatBps(flow.rev)}</span>
              {u != null && u > 0 && <span className="opacity-90">{u >= 10 ? u.toFixed(0) : u.toFixed(1)}%</span>}
            </>
          )}
        </div>
        {!faulted && u != null && (
          <div className="h-[3px] w-full bg-surface2">
            <div className="h-full transition-all duration-700" style={{ width: `${Math.max(2, Math.min(100, u))}%`, background: utilHex(u) }} />
          </div>
        )}
      </div>
    </div>
  )
}

/* Full link inspection card shown while hovering a cable in live mode. */
function LinkHoverCard({ x, y, link, live, flow }: { x: number; y: number; link: ManualMapLink; live: LiveLinkData; flow: ReturnType<typeof linkFlow> }) {
  return (
    <div className="nodrag nopan pointer-events-none absolute z-50" style={{ transform: `translate(-50%, -110%) translate(${x}px, ${y - 14}px)` }}>
      <div className="w-64 rounded-lg border border-border bg-surface/95 p-2.5 shadow-xl backdrop-blur animate-fade-in">
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <span className="truncate text-[11px] font-semibold text-text">{link.label || 'Link'}</span>
          <span className="rounded bg-surface2 px-1 py-px font-mono text-[9px] uppercase text-muted">{link.metadata?.kind || link.link_type}{link.metadata?.speed ? ` · ${link.metadata.speed}` : ''}</span>
        </div>
        {flow && flow.utilPct != null && (
          <div className="mb-2 flex items-center gap-2">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface2">
              <div className="h-full rounded-full" style={{ width: `${Math.max(2, Math.min(100, flow.utilPct))}%`, background: utilHex(flow.utilPct) }} />
            </div>
            <span className="font-mono text-[10px] font-semibold" style={{ color: utilHex(flow.utilPct) }}>{flow.utilPct.toFixed(1)}%</span>
          </div>
        )}
        <div className="grid grid-cols-2 gap-2">
          <HoverIface title="A-END" iface={live.source} />
          <HoverIface title="B-END" iface={live.target} />
        </div>
        <div className="mt-1.5 text-right text-[9px] text-muted">window {Math.round(live.window_seconds / 60)}m</div>
      </div>
    </div>
  )
}

function HoverIface({ title, iface }: { title: string; iface: LiveInterface }) {
  const down = iface.matched && iface.oper_status != null && iface.oper_status !== 'up'
  return (
    <div className="rounded-md border border-border/70 bg-surface2/50 p-1.5">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[8.5px] font-bold tracking-wider text-muted">{title}</span>
        <span className={cn('rounded px-1 py-px text-[8.5px] font-bold uppercase leading-none', down ? 'bg-danger/15 text-danger' : iface.matched ? 'bg-success/15 text-success' : 'bg-surface3 text-muted')}>
          {iface.matched ? (iface.oper_status || '?') : 'n/a'}
        </span>
      </div>
      <div className="truncate font-mono text-[10px] font-semibold text-text">{iface.if_name || '—'}</div>
      {iface.if_alias ? <div className="truncate text-[8.5px] italic text-muted" title={iface.if_alias}>{iface.if_alias}</div> : null}
      <div className="mt-1 space-y-px font-mono text-[9px] leading-tight text-text2">
        <div>in&nbsp;&nbsp;{formatBps(iface.in_bps)}</div>
        <div>out&nbsp;{formatBps(iface.out_bps)}</div>
        {iface.if_speed ? <div className="text-muted">spd {formatBps(iface.if_speed)}</div> : null}
      </div>
    </div>
  )
}

/* Port (interface) label that the admin can drag along/around the cable and
 * rotate. Position = cable anchor (x,y) + persisted offset; a rotate grip
 * appears above it when the link is selected. */
function IfaceChip({ x, y, value, pos, editable, showRotate, zoom, down, onMove, onRotate }: {
  x: number; y: number; value: string
  pos?: { dx?: number; dy?: number; rot?: number }
  editable: boolean; showRotate: boolean; zoom: number
  down?: boolean
  onMove: (p: { dx: number; dy: number }, commit: boolean) => void
  onRotate: (rot: number, commit: boolean) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const dx = pos?.dx || 0, dy = pos?.dy || 0, rot = pos?.rot || 0

  const startMove = (e: React.PointerEvent) => {
    if (!editable || e.button !== 0) return
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
    if (e.button !== 0) return
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
        className={cn(
          'relative rounded border px-1 py-px font-mono text-[9px] font-semibold leading-none tracking-tight shadow-sm backdrop-blur',
          down ? 'border-danger/60 bg-danger/15 text-danger' : 'border-border bg-surface/95 text-text2',
          editable && 'cursor-move hover:border-primary/60',
        )}
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

export const NetworkEdge = memo(NetworkEdgeImpl)
