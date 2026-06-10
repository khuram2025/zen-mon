import { memo, useRef, useState } from 'react'
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
  linkFlow,
  linkHealth,
  linkKindOf,
  linkShapeOf,
  linkWaypoints,
  nearestSegmentIndex,
  pctToPx,
  utilHex,
  utilizationColor,
  particleSpec,
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
}

/** Collapse near-duplicate and collinear bend points so repeated orthogonal
 *  segment drags don't accumulate redundant waypoints. */
function simplifyOrtho(pts: Pt[], eps = 1.5): Pt[] {
  const dedup: Pt[] = []
  for (const p of pts) {
    const l = dedup[dedup.length - 1]
    if (!l || Math.abs(l.x - p.x) > eps || Math.abs(l.y - p.y) > eps) dedup.push(p)
  }
  const out: Pt[] = []
  for (let i = 0; i < dedup.length; i++) {
    const prev = out[out.length - 1]
    const cur = dedup[i]
    const next = dedup[i + 1]
    if (prev && next) {
      const colH = Math.abs(prev.y - cur.y) <= eps && Math.abs(cur.y - next.y) <= eps
      const colV = Math.abs(prev.x - cur.x) <= eps && Math.abs(cur.x - next.x) <= eps
      if (colH || colV) continue // cur lies on the straight run prev→next
    }
    out.push(cur)
  }
  return out
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
  const { link, sourceStatus, targetStatus, live, liveMode, showThroughput, parallelOffset = 0, setWaypoints, setIfacePos } = d
  const rf = useReactFlow()
  const zoom = useStore((s) => s.transform[2])
  const [hover, setHover] = useState(false)

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
  const pathId = `nme-${link.id}`

  const health = linkHealth(sourceStatus, targetStatus)
  const color = STATUS_COLOR[health].line
  const kind = linkKindOf(link)
  const kindStyle = LINK_KIND_STYLE[kind] || {}
  const baseWidth = (kindStyle.widthMul || 1) * 3

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
    const sx = e.clientX, sy = e.clientY
    let moved = false
    const apply = (ev: PointerEvent, commit: boolean) => {
      const fp = rf.screenToFlowPosition({ x: ev.clientX, y: ev.clientY })
      const v = horizontal ? fp.y : fp.x // new perpendicular coordinate of the segment
      // An anchor end stays pinned to the node — instead of moving it, add an
      // elbow that joins the moved segment back to the anchor's fixed axis.
      const elbow = (anchor: Pt): Pt => (horizontal ? { x: anchor.x, y: v } : { x: v, y: anchor.y })
      const wpts: Pt[] = []
      if (k === 0) wpts.push(elbow(rv[0]))
      for (let i = 1; i <= last - 1; i++) {
        const p = { ...rv[i] }
        if (i === k || i === k + 1) { if (horizontal) p.y = v; else p.x = v }
        wpts.push(p)
      }
      if (k + 1 === last) wpts.push(elbow(rv[last]))
      setWaypoints!(simplifyOrtho(wpts), commit)
    }
    const move = (ev: PointerEvent) => {
      if (!moved && Math.hypot(ev.clientX - sx, ev.clientY - sy) < 3) return // ignore jitter / clicks
      moved = true
      apply(ev, false)
    }
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      if (moved) apply(ev, true) // a plain click leaves the route untouched
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

  // Double-click the cable → drop a free bend point you can drag anywhere.
  // This is the explicit "add a bend" action; works for every shape and is
  // the way to bend an orthogonal cable (single-drag moves a whole segment).
  const onPathDoubleClick = (e: React.MouseEvent) => {
    if (!editable) return
    e.stopPropagation()
    e.preventDefault()
    const fp = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY })
    const idx = nearestSegmentIndex(path.vertices, fp)
    setWaypoints!([...wps.slice(0, idx), fp, ...wps.slice(idx)], true)
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
        {editable && <title>{shape === 'orthogonal' ? 'Drag to move segment · double-click to add a bend' : 'Drag to bend · double-click to add a point'}</title>}
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
