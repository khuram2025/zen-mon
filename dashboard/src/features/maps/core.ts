/* ──────────────────────────────────────────────────────────────────────────
 * ZenPlus Network Maps — v2 shared core
 *
 * Types, status palette, coordinate model and link-geometry helpers shared by
 * the React-Flow–based map editor (features/maps/*). These are faithful ports
 * of the equivalents in pages/ManualMapsPage.tsx (v1) so the v2 canvas renders
 * with the identical visual language while we migrate the editor mechanics
 * onto React Flow. v1 is intentionally left untouched.
 * ────────────────────────────────────────────────────────────────────────── */

import { networkIcons, type IconKey } from '@/components/network-icons'

/* ── Types (ported from v1) ─────────────────────────────────────────────── */

export type NodeStatus = 'up' | 'down' | 'degraded' | 'unknown' | 'maintenance' | string

export type ManualMapListItem = {
  id: string
  name: string
  description?: string | null
  created_at?: string | null
  updated_at?: string | null
  node_count: number
  link_count: number
  status_counts: Record<string, number>
}

export type NodeLabelStyle = {
  color?: string
  fontFamily?: string
  fontSize?: number
  bold?: boolean
  italic?: boolean
}

export type NodeMetadata = {
  label_offset?: { dx: number; dy: number }
  size_scale?: number
  /** Icon size as a fraction of the tile (0.35–1). */
  icon_fill?: number
  label_style?: NodeLabelStyle
  /** Outer frame shape of the device tile. */
  frame?: 'circle' | 'rounded'
  [k: string]: unknown
}

export type ManualMapNode = {
  id: string
  map_id: string
  device_id: string
  label: string
  icon: string
  x_pct: number
  y_pct: number
  hostname: string
  ip_address: string
  device_type: string
  status: NodeStatus
  location?: string | null
  vendor?: string | null
  model?: string | null
  last_seen?: string | null
  metadata?: NodeMetadata | null
}

export type LinkKind = 'ethernet' | 'fiber' | 'wireless' | 'vpn' | 'trunk' | 'serial' | 'manual'
export type LinkSpeed = '10M' | '100M' | '1G' | '2.5G' | '10G' | '25G' | '40G' | '100G' | string
export type LinkShape = 'curve' | 'straight' | 'orthogonal'

export type Waypoint = { x_pct: number; y_pct: number }

/** Per-endpoint port-label placement: offset (logical px) from the cable anchor
 *  + rotation (deg). Lets admins drag/rotate the interface chips off the line. */
export type IfaceLabelPos = { dx?: number; dy?: number; rot?: number }

export type LinkMetadata = {
  src_interface?: string | null
  dst_interface?: string | null
  speed?: LinkSpeed | null
  kind?: LinkKind | null
  shape?: LinkShape | null
  waypoints?: Waypoint[] | null
  notes?: string | null
  /** Cable thickness multiplier (1 = default). */
  width_scale?: number | null
  /** Manual placement for the source/target interface chips. */
  iface_pos?: { src?: IfaceLabelPos; dst?: IfaceLabelPos } | null
}

export type LiveInterface = {
  matched: boolean
  if_index?: number | null
  if_name?: string | null
  if_descr?: string | null
  if_alias?: string | null
  if_speed?: number | null
  admin_status?: string | null
  oper_status?: string | null
  in_bps?: number | null
  out_bps?: number | null
  in_packets?: number | null
  out_packets?: number | null
  util_pct?: number | null
}

export type LiveLinkData = {
  source: LiveInterface
  target: LiveInterface
  window_seconds: number
  generated_at: string
}

/** Per-device live health (from GET /maps/{id}/nodes-live). */
export type NodeLiveData = {
  device_id: string
  status: NodeStatus
  last_seen?: string | null
  rtt_ms?: number | null
  cpu_pct?: number | null
  mem_pct?: number | null
  uptime_seconds?: number | null
  temperature_c?: number | null
  alerts: { active: number; critical: number; warning: number }
}

export type ManualMapLink = {
  id: string
  map_id: string
  source_node_id: string
  target_node_id: string
  label?: string | null
  link_type: string
  metadata?: LinkMetadata | null
}

export type SuggestedLink = {
  source_node_id: string
  target_node_id: string
  source_hostname: string
  target_hostname: string
  src_interface: string | null
  dst_interface: string | null
  protocol: string | null
  confidence: number | null
  physical_links: number
}

/* ── Annotation shapes (standalone canvas objects, not tied to a device) ──────
 * Stored in manual_map_shapes. We reuse kind='image' for both uploaded images
 * (metadata.src) and built-in network/system/cloud icons (metadata.icon). */
export type ShapeKind = 'rectangle' | 'circle' | 'text' | 'line' | 'arrow' | 'diamond' | 'hexagon' | 'image' | 'sticky'

export type ShapeStyle = {
  icon?: IconKey            // built-in icon (when kind='image' and no src)
  src?: string              // image URL or data URL (when kind='image')
  color?: string            // text / foreground colour
  fontFamily?: string
  fontSize?: number         // logical px
  bold?: boolean
  italic?: boolean
  align?: 'left' | 'center' | 'right'
  rounded?: boolean
  /* Live data widgets (rendered by ShapeNode instead of a plain shape). */
  widget?: 'conversations'
  limit?: number            // top-N rows for widgets
  hours?: number            // lookback window for widgets
  exporter?: string | null  // bind widget to one exporter/device IP
}

export type MapShape = {
  id: string
  map_id: string
  kind: ShapeKind
  x_pct: number
  y_pct: number
  w_pct: number
  h_pct: number
  text?: string | null
  fill?: string | null
  stroke?: string | null
  z_index: number
  metadata: ShapeStyle
}

/* A cable that touches at least one annotation (icon/image/shape). Stored in
 * the map's metadata (annotation_links) since the links table only accepts
 * device endpoints. Endpoints reference either a device node id or a shape id. */
export type AnnotationLink = {
  id: string
  source: string
  target: string
  source_type: 'node' | 'shape'
  target_type: 'node' | 'shape'
  label?: string | null
  link_type?: string
  metadata?: LinkMetadata
}

export type ManualMapDetail = ManualMapListItem & {
  /** Per-map UI state: background, theme, annotation_links, snap settings. */
  metadata?: Record<string, unknown> | null
  summary: {
    nodes: number
    links: number
    status_counts: Record<string, number>
    generated_at: string
  }
  nodes: ManualMapNode[]
  links: ManualMapLink[]
  shapes?: MapShape[]
}

export type Device = {
  id: string
  hostname: string
  ip_address: string
  device_type: string
  status: NodeStatus
  location?: string | null
  vendor?: string | null
  model?: string | null
}

/* ── Coordinate model ───────────────────────────────────────────────────────
 * v1 stores node positions as percentages (x_pct/y_pct). React Flow needs an
 * absolute pixel space. We map percent onto a fixed logical lab canvas so the
 * existing maps render in identical relative positions, and convert back to
 * percent on save (keeping x_pct/y_pct authoritative so v1 keeps working).
 * ────────────────────────────────────────────────────────────────────────── */

export const LOGICAL_W = 4000
export const LOGICAL_H = 2400

// Device node box: a 128px-wide column with a 64px icon disc centred at top.
// The disc centre is the connection/anchor point and the logical coordinate.
export const NODE_W = 128
export const DISC = 64
export const DISC_CX = NODE_W / 2 // 64
export const DISC_CY = DISC / 2 // 32
/** Default icon size relative to the device tile (was 0.56 — too much padding). */
export const DEFAULT_ICON_FILL = 0.78

export function iconFillFor(md?: NodeMetadata | null): number {
  const v = md?.icon_fill
  if (typeof v === 'number' && v > 0) return Math.min(1, Math.max(0.35, v))
  return DEFAULT_ICON_FILL
}

export function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value))
}

/** Percent position → logical pixel of the disc centre. */
export function pctToPx(p: { x_pct: number; y_pct: number }) {
  return { x: (p.x_pct / 100) * LOGICAL_W, y: (p.y_pct / 100) * LOGICAL_H }
}

/** Logical pixel of the disc centre → clamped percent position. */
export function pxToPct(x: number, y: number): Waypoint {
  return {
    x_pct: clamp((x / LOGICAL_W) * 100, 0, 100),
    y_pct: clamp((y / LOGICAL_H) * 100, 0, 100),
  }
}

/** Shape rect (x_pct/y_pct = top-left, w/h_pct = size) → logical px rect. */
export function shapeToPx(s: { x_pct: number; y_pct: number; w_pct: number; h_pct: number }) {
  return {
    x: (s.x_pct / 100) * LOGICAL_W,
    y: (s.y_pct / 100) * LOGICAL_H,
    w: (s.w_pct / 100) * LOGICAL_W,
    h: (s.h_pct / 100) * LOGICAL_H,
  }
}

/** Logical px rect → clamped shape percent rect. */
export function pxToShape(x: number, y: number, w: number, h: number) {
  return {
    x_pct: clamp((x / LOGICAL_W) * 100, 0, 100),
    y_pct: clamp((y / LOGICAL_H) * 100, 0, 100),
    w_pct: clamp((w / LOGICAL_W) * 100, 1, 100),
    h_pct: clamp((h / LOGICAL_H) * 100, 1, 100),
  }
}

/** React Flow node top-left position for a given disc-centre logical pixel. */
export function discCenterToNodeXY(cx: number, cy: number) {
  return { x: cx - DISC_CX, y: cy - DISC_CY }
}

/** React Flow node top-left position → disc-centre logical pixel. */
export function nodeXYToDiscCenter(x: number, y: number) {
  return { x: x + DISC_CX, y: y + DISC_CY }
}

/* ── Status palette (ported verbatim from v1) ───────────────────────────── */

export const STATUS_COLOR: Record<
  string,
  { ring: string; fill: string; line: string; dot: string; badge: any }
> = {
  up: { ring: 'border-success/60 text-success', fill: 'bg-success/5', line: 'stroke-success/55', dot: 'bg-success', badge: 'success' },
  down: { ring: 'border-danger/70 text-danger', fill: 'bg-danger/10', line: 'stroke-danger/65', dot: 'bg-danger', badge: 'danger' },
  degraded: { ring: 'border-warning/70 text-warning', fill: 'bg-warning/10', line: 'stroke-warning/55', dot: 'bg-warning', badge: 'warning' },
  maintenance: { ring: 'border-info/60 text-info', fill: 'bg-info/10', line: 'stroke-info/55', dot: 'bg-info', badge: 'info' },
  unknown: { ring: 'border-border text-muted', fill: 'bg-surface2/40', line: 'stroke-muted/35', dot: 'bg-muted', badge: 'outline' },
}

export const STATUS_ORDER: NodeStatus[] = ['down', 'degraded', 'maintenance', 'unknown', 'up']

export const PALETTE_ICONS: IconKey[] = [
  'router', 'switch', 'firewall', 'server', 'database',
  'load_balancer', 'access_point', 'storage', 'workstation',
  'printer', 'cloud', 'internet', 'camera', 'other',
]

export const TYPE_TO_ICON: Record<string, IconKey> = {
  router: 'router', switch: 'switch', firewall: 'firewall', server: 'server',
  database: 'database', load_balancer: 'load_balancer', access_point: 'access_point',
  storage: 'storage', workstation: 'workstation', printer: 'printer',
  cloud: 'cloud', internet: 'internet', camera: 'camera', other: 'other',
}

export const LINK_KIND_STYLE: Record<LinkKind, { dash?: string; widthMul?: number; accent?: string; arrow?: boolean }> = {
  ethernet: {},
  fiber: { accent: '#f59e0b' },
  trunk: { widthMul: 1.4 },
  wireless: { dash: '2 4' },
  vpn: { dash: '6 4', accent: '#8b5cf6' },
  serial: { dash: '8 2' },
  manual: {},
}

/* ── Helpers (ported from v1) ───────────────────────────────────────────── */

export function statusKey(status?: NodeStatus): keyof typeof STATUS_COLOR {
  const key = String(status || 'unknown').toLowerCase()
  return (STATUS_COLOR[key] ? key : 'unknown') as keyof typeof STATUS_COLOR
}

export function utilizationColor(pct: number | null | undefined): string {
  if (pct == null) return 'stroke-muted/50'
  if (pct >= 85) return 'stroke-danger'
  if (pct >= 60) return 'stroke-warning'
  if (pct >= 30) return 'stroke-success'
  return 'stroke-success/60'
}

export function linkHealth(srcStatus: NodeStatus, dstStatus: NodeStatus): keyof typeof STATUS_COLOR {
  const s = String(srcStatus || '').toLowerCase()
  const d = String(dstStatus || '').toLowerCase()
  if (s === 'down' || d === 'down') return 'down'
  if (s === 'degraded' || d === 'degraded') return 'degraded'
  if (s === 'maintenance' || d === 'maintenance') return 'maintenance'
  if (s === 'unknown' || d === 'unknown') return 'unknown'
  return 'up'
}

export function formatBps(bps: number | null | undefined): string {
  if (!bps || bps <= 0) return '0 bps'
  const units = ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps']
  let i = 0
  let v = bps
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++ }
  return `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${units[i]}`
}

export function iconForNode(node: { icon?: string; device_type?: string }): IconKey {
  if (node.icon && node.icon !== 'auto' && networkIcons[node.icon as IconKey]) return node.icon as IconKey
  return TYPE_TO_ICON[node.device_type || 'other'] || 'other'
}

export function linkWaypoints(link: ManualMapLink): Waypoint[] {
  const w = link.metadata?.waypoints
  return Array.isArray(w) ? w : []
}

export function linkShapeOf(link: ManualMapLink): LinkShape {
  const s = link.metadata?.shape
  return s === 'straight' || s === 'orthogonal' ? s : 'curve'
}

export function linkKindOf(link: ManualMapLink): LinkKind {
  return (link.metadata?.kind || link.link_type || 'ethernet') as LinkKind
}

/* ── Link geometry (ported from v1, generalised to plain {x,y}) ──────────── */

export type Pt = { x: number; y: number }
type Segment = { ax: number; ay: number; bx: number; by: number; horizontal: boolean }
export type EdgePathResult = {
  d: string
  mid: Pt
  near: Pt
  far: Pt
  segments: Segment[]
  vertices: Pt[]
}

function linkCurve(ax: number, ay: number, bx: number, by: number, curve = 0.18) {
  const mx = (ax + bx) / 2
  const my = (ay + by) / 2
  const dx = bx - ax
  const dy = by - ay
  const len = Math.hypot(dx, dy) || 1
  const nx = -dy / len
  const ny = dx / len
  const cx = mx + nx * len * curve
  const cy = my + ny * len * curve
  return { d: `M ${ax} ${ay} Q ${cx} ${cy} ${bx} ${by}`, cx, cy }
}

function pointOnQuadratic(ax: number, ay: number, cx: number, cy: number, bx: number, by: number, t: number): Pt {
  const it = 1 - t
  return {
    x: it * it * ax + 2 * it * t * cx + t * t * bx,
    y: it * it * ay + 2 * it * t * cy + t * t * by,
  }
}

function pointAtT(segments: Segment[], t: number): Pt {
  const lens = segments.map((s) => Math.hypot(s.bx - s.ax, s.by - s.ay))
  const total = lens.reduce((s, l) => s + l, 0) || 1
  const target = t * total
  let acc = 0
  for (let i = 0; i < segments.length; i++) {
    const s = segments[i]
    if (acc + lens[i] >= target) {
      const local = (target - acc) / (lens[i] || 1)
      return { x: s.ax + (s.bx - s.ax) * local, y: s.ay + (s.by - s.ay) * local }
    }
    acc += lens[i]
  }
  const last = segments[segments.length - 1]
  return { x: last.bx, y: last.by }
}

function segsFrom(poly: Pt[]): Segment[] {
  const out: Segment[] = []
  for (let i = 0; i < poly.length - 1; i++) {
    const a = poly[i]
    const b = poly[i + 1]
    out.push({ ax: a.x, ay: a.y, bx: b.x, by: b.y, horizontal: Math.abs(a.y - b.y) < 0.01 })
  }
  return out
}

/** When an orthogonal link has no explicit waypoints, `orthoPoly` inserts this
 *  single bend so the cable stays axis-aligned. Exposed so the editor can show
 *  a draggable handle even before the user has added waypoints. */
export function orthoImplicitCorner(src: Pt, tgt: Pt): Pt | null {
  if (Math.abs(src.x - tgt.x) <= 0.01 || Math.abs(src.y - tgt.y) <= 0.01) return null
  return { x: tgt.x, y: src.y }
}

/** Waypoints used for orthogonal editing — materialises the implicit corner so
 *  the turning point can be dragged on both axes. */
export function orthoEffectiveWaypoints(src: Pt, tgt: Pt, waypoints: Pt[]): Pt[] {
  if (waypoints.length > 0) return waypoints
  const c = orthoImplicitCorner(src, tgt)
  return c ? [c] : []
}

/** Interior right-angle corners on the rendered cable. */
export function orthoRoutedInterior(src: Pt, tgt: Pt, waypoints: Pt[], eps = 1.5): Pt[] {
  if (Math.abs(src.x - tgt.x) <= eps || Math.abs(src.y - tgt.y) <= eps) return []
  const routed = orthoPoly([src, ...waypoints, tgt])
  if (routed.length <= 2) return []
  const out: Pt[] = []
  for (let i = 1; i < routed.length - 1; i++) {
    const p = routed[i]
    if (!out.some((q) => Math.abs(q.x - p.x) <= eps && Math.abs(q.y - p.y) <= eps)) out.push(p)
  }
  return out
}

/** Draggable handles — skip auto elbows glued to the device/shape border. */
export function orthoEditableHandles(
  src: Pt,
  tgt: Pt,
  waypoints: Pt[],
  rimEps = 28,
): Pt[] {
  if (waypoints.length > 0) return waypoints.slice()
  const interior = orthoRoutedInterior(src, tgt, waypoints)
  const meaningful = interior.filter((p) =>
    Math.hypot(p.x - src.x, p.y - src.y) > rimEps &&
    Math.hypot(p.x - tgt.x, p.y - tgt.y) > rimEps,
  )
  if (meaningful.length > 0) return meaningful
  if (interior.length > 0) return [interior[Math.floor((interior.length - 1) / 2)]]
  const c = orthoImplicitCorner(src, tgt)
  return c ? [c] : []
}

function polylineEqual(a: Pt[], b: Pt[], eps = 1.5): boolean {
  if (a.length !== b.length) return false
  return a.every((p, i) => Math.abs(p.x - b[i].x) <= eps && Math.abs(p.y - b[i].y) <= eps)
}

/** Collapse near-duplicate and collinear bend points. */
export function simplifyOrthoWaypoints(pts: Pt[], eps = 1.5): Pt[] {
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
      if (colH || colV) continue
    }
    out.push(cur)
  }
  return out
}

/** Clear interface labels / bends on sides whose endpoint changed after a
 *  reconnect drag in the editor. */
export function reconcileLinkMetadataOnReconnect(
  meta: LinkMetadata,
  oldSourceId: string,
  oldTargetId: string,
  newSourceId: string,
  newTargetId: string,
  newSourceType: 'node' | 'shape',
  newTargetType: 'node' | 'shape',
): LinkMetadata {
  const next: LinkMetadata = { ...meta }
  const ip = { ...(next.iface_pos || {}) }
  let endpointsChanged = false
  if (oldSourceId !== newSourceId) {
    next.src_interface = newSourceType === 'node' ? null : null
    delete ip.src
    endpointsChanged = true
  }
  if (oldTargetId !== newTargetId) {
    next.dst_interface = newTargetType === 'node' ? null : null
    delete ip.dst
    endpointsChanged = true
  }
  if (endpointsChanged) next.waypoints = []
  next.iface_pos = Object.keys(ip).length ? ip : null
  return next
}

/** Drop bends that no longer change the routed cable — e.g. after dragging a
 *  handle onto a straight segment the path collapses and stored waypoints clear. */
export function pruneOrthoWaypoints(src: Pt, tgt: Pt, waypoints: Pt[], eps = 1.5): Pt[] {
  let w = simplifyOrthoWaypoints(waypoints, eps)
  if (w.length === 0) return []

  // Compare SIMPLIFIED routes so pass-through points sitting on a straight
  // run (invisible — no corner) count as redundant and get dropped too.
  const route = (pts: Pt[]) => simplifyOrthoWaypoints(orthoPoly([src, ...pts, tgt]), eps)
  const canonical = route(w)
  if (canonical.length <= 2) return []
  if (polylineEqual(canonical, route([]), eps)) return []

  let changed = true
  while (changed && w.length > 0) {
    changed = false
    for (let i = 0; i < w.length; i++) {
      const trial = w.filter((_, j) => j !== i)
      if (polylineEqual(route(trial), canonical, eps)) {
        w = trial
        changed = true
        break
      }
    }
  }
  return simplifyOrthoWaypoints(w, eps)
}

/** Straight/curved links: drop any bend that sits on the line between its
 *  neighbours — dragging a dot back onto the cable removes it (draw.io). */
export function pruneStraightWaypoints(src: Pt, tgt: Pt, waypoints: Pt[], eps = 1.5): Pt[] {
  const out = simplifyOrthoWaypoints(waypoints, Math.min(eps, 1.5)).slice()
  let changed = true
  while (changed) {
    changed = false
    for (let i = 0; i < out.length; i++) {
      const prev = i === 0 ? src : out[i - 1]
      const next = i === out.length - 1 ? tgt : out[i + 1]
      if (distToSegment(out[i], prev, next) <= eps) {
        out.splice(i, 1)
        changed = true
        break
      }
    }
  }
  return out
}

// Expand user vertices into a right-angle polyline (orthogonal routing).
function orthoPoly(verts: Pt[]): Pt[] {
  const poly: Pt[] = [verts[0]]
  for (let i = 1; i < verts.length; i++) {
    const prev = verts[i - 1]
    const cur = verts[i]
    if (Math.abs(prev.x - cur.x) > 0.01 && Math.abs(prev.y - cur.y) > 0.01) {
      poly.push({ x: cur.x, y: prev.y })
    }
    poly.push(cur)
  }
  return poly
}

// Smooth curve passing through interior points (quadratic-through-midpoints).
function smoothPath(pts: Pt[]): string {
  if (pts.length < 3) return `M ${pts[0].x} ${pts[0].y} L ${pts[1].x} ${pts[1].y}`
  let d = `M ${pts[0].x} ${pts[0].y} L ${(pts[0].x + pts[1].x) / 2} ${(pts[0].y + pts[1].y) / 2}`
  for (let i = 1; i < pts.length - 1; i++) {
    const mx = (pts[i].x + pts[i + 1].x) / 2
    const my = (pts[i].y + pts[i + 1].y) / 2
    d += ` Q ${pts[i].x} ${pts[i].y} ${mx} ${my}`
  }
  const last = pts[pts.length - 1]
  d += ` L ${last.x} ${last.y}`
  return d
}

/** Build an SVG path + annotation anchors for any supported link shape. Every
 *  shape now routes through the user waypoints, so links can be bent into any
 *  shape. `vertices` is the user point list [source, ...waypoints, target] —
 *  the editor renders drag handles on the interior ones. Inputs and waypoints
 *  are plain {x,y} in the SAME coordinate space (logical px). */
export function edgePath(
  shape: LinkShape,
  ax: number, ay: number, bx: number, by: number,
  waypoints: Pt[] = [],
): EdgePathResult {
  const verts: Pt[] = [{ x: ax, y: ay }, ...waypoints, { x: bx, y: by }]

  // Curve with no waypoints keeps the pretty perpendicular-offset arc.
  if (shape === 'curve' && waypoints.length === 0) {
    const c = linkCurve(ax, ay, bx, by)
    return {
      d: c.d,
      vertices: verts,
      mid: pointOnQuadratic(ax, ay, c.cx, c.cy, bx, by, 0.5),
      near: pointOnQuadratic(ax, ay, c.cx, c.cy, bx, by, 0.14),
      far: pointOnQuadratic(ax, ay, c.cx, c.cy, bx, by, 0.86),
      segments: [{ ax, ay, bx, by, horizontal: Math.abs(ay - by) < 0.01 }],
    }
  }

  // Render polyline differs per shape, but anchors sample the routed polyline.
  const routed = shape === 'orthogonal' ? orthoPoly(verts) : verts
  const segments = segsFrom(routed)
  let d: string
  if (shape === 'curve') d = smoothPath(verts)
  else d = routed.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x} ${p.y}`).join(' ')

  return {
    d,
    vertices: verts,
    segments,
    mid: pointAtT(segments, 0.5),
    near: pointAtT(segments, 0.16),
    far: pointAtT(segments, 0.84),
  }
}

/* ── Orthogonal routing, draw.io style ────────────────────────────────────
 * The cable is routed CENTre-to-centre through the user waypoints with right
 * angles, then clipped where it crosses each endpoint's border. That way the
 * cable always leaves a device/shape axis-aligned (no diagonal stubs), and
 * dragging a segment or corner re-anchors the ends smoothly along the border. */

export type EndGeom = {
  center: Pt
  rect: boolean
  halfW: number
  halfH: number
  /** Disc radius for circular (device) endpoints. */
  r?: number
}

function insideEndGeom(p: Pt, g: EndGeom): boolean {
  if (g.rect) return Math.abs(p.x - g.center.x) <= g.halfW && Math.abs(p.y - g.center.y) <= g.halfH
  return Math.hypot(p.x - g.center.x, p.y - g.center.y) <= (g.r ?? DISC_RADIUS)
}

/** Border crossing on the segment inside→outside (bisection — works for any shape). */
function borderCross(inside: Pt, outside: Pt, g: EndGeom): Pt {
  let a = inside
  let b = outside
  for (let i = 0; i < 24; i++) {
    const m = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
    if (insideEndGeom(m, g)) a = m
    else b = m
  }
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }
}

function clipStart(pts: Pt[], g: EndGeom): Pt[] {
  if (!insideEndGeom(pts[0], g)) return pts
  let k = 0
  while (k + 1 < pts.length && insideEndGeom(pts[k + 1], g)) k++
  if (k + 1 >= pts.length) return pts // fully inside the shape — give up
  return [borderCross(pts[k], pts[k + 1], g), ...pts.slice(k + 1)]
}

/** Trim a polyline so it starts/ends on the endpoint shapes' borders. */
export function clipRouteEnds(poly: Pt[], sg: EndGeom | null, tg: EndGeom | null): Pt[] {
  let pts = poly.slice()
  if (sg) pts = clipStart(pts, sg)
  if (tg) pts = clipStart(pts.slice().reverse(), tg).reverse()
  return pts
}

/** Full orthogonal edge: centre-routed through waypoints, border-clipped.
 *  `clipped` is the final polyline — its interior points are the visual
 *  corners the editor exposes as drag handles. */
export function routeOrthoEdge(
  srcCenter: Pt,
  tgtCenter: Pt,
  waypoints: Pt[],
  sg: EndGeom | null,
  tg: EndGeom | null,
): EdgePathResult & { clipped: Pt[] } {
  const routed = orthoPoly([srcCenter, ...waypoints, tgtCenter])
  const clipped = simplifyOrthoWaypoints(clipRouteEnds(routed, sg, tg), 0.25)
  const safe = clipped.length >= 2 ? clipped : [srcCenter, tgtCenter]
  const segments = segsFrom(safe)
  return {
    d: safe.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x} ${p.y}`).join(' '),
    vertices: safe,
    segments,
    mid: pointAtT(segments, 0.5),
    near: pointAtT(segments, 0.16),
    far: pointAtT(segments, 0.84),
    clipped: safe,
  }
}

/** Midpoints of each user segment — candidate spots to insert a new waypoint. */
export function segmentMidpoints(verts: Pt[]): { x: number; y: number; index: number }[] {
  const out: { x: number; y: number; index: number }[] = []
  for (let i = 0; i < verts.length - 1; i++) {
    out.push({ x: (verts[i].x + verts[i + 1].x) / 2, y: (verts[i].y + verts[i + 1].y) / 2, index: i })
  }
  return out
}

// Device disc radius (the 64px icon disc; +2px ring) used to anchor links on
// the node's outer circle instead of its centre, so many cables fan out.
export const DISC_RADIUS = DISC / 2 + 2

/** Point on a circle of radius r around `center`, in the direction of `toward`. */
export function anchorOnCircle(center: Pt, toward: Pt, r = DISC_RADIUS): Pt {
  const dx = toward.x - center.x
  const dy = toward.y - center.y
  const d = Math.hypot(dx, dy) || 1
  return { x: center.x + (dx / d) * r, y: center.y + (dy / d) * r }
}

/** Where a ray from a rectangle's centre toward `toward` exits the rect border.
 *  Used to anchor cables on icon/image/shape annotations (which are boxes). */
export function anchorOnRect(center: Pt, toward: Pt, halfW: number, halfH: number): Pt {
  const dx = toward.x - center.x
  const dy = toward.y - center.y
  if (!dx && !dy) return { x: center.x + halfW, y: center.y }
  const sx = halfW / (Math.abs(dx) || 1e-6)
  const sy = halfH / (Math.abs(dy) || 1e-6)
  const s = Math.min(sx, sy)
  return { x: center.x + dx * s, y: center.y + dy * s }
}

export type EndpointGeom = {
  center: Pt
  rect: boolean
  halfW: number
  halfH: number
  r: number
}

/** Flow-space centre + border for a canvas node (device disc or annotation box). */
export function endpointGeomFromNode(
  position: { x: number; y: number },
  type: string | undefined,
  data: unknown,
  measured?: { width?: number; height?: number },
  width?: number | null,
  height?: number | null,
): EndpointGeom | null {
  if (type === 'shape') {
    const w = measured?.width ?? (typeof width === 'number' ? width : 80)
    const h = measured?.height ?? (typeof height === 'number' ? height : 60)
    return { center: { x: position.x + w / 2, y: position.y + h / 2 }, rect: true, halfW: w / 2, halfH: h / 2, r: 0 }
  }
  if (type === 'device') {
    const scale = ((data as any)?.node?.metadata?.size_scale as number) || 1
    return {
      center: { x: position.x + DISC_CX, y: position.y + DISC_CY },
      rect: false,
      halfW: 0,
      halfH: 0,
      r: (DISC * scale) / 2 + 2,
    }
  }
  return null
}

/** Bend just outside the node border so the cable anchors where the user dropped. */
export function waypointForEndpointReposition(
  dropPt: Pt,
  geom: EndpointGeom,
  towardFallback: Pt,
): Pt {
  let dx = dropPt.x - geom.center.x
  let dy = dropPt.y - geom.center.y
  if (Math.hypot(dx, dy) < 8) {
    dx = towardFallback.x - geom.center.x
    dy = towardFallback.y - geom.center.y
  }
  const d = Math.hypot(dx, dy) || 1
  const toward = { x: geom.center.x + dx, y: geom.center.y + dy }
  const anchor = geom.rect
    ? anchorOnRect(geom.center, toward, geom.halfW, geom.halfH)
    : anchorOnCircle(geom.center, toward, geom.r)
  const lead = Math.hypot(anchor.x - geom.center.x, anchor.y - geom.center.y) + 40
  return { x: geom.center.x + (dx / d) * lead, y: geom.center.y + (dy / d) * lead }
}

/** Update first/last waypoint so an endpoint can be re-anchored on the same node. */
export function repositionEndpointWaypoints(
  storedWpsPx: Pt[],
  which: 'src' | 'dst',
  dropPt: Pt,
  geom: EndpointGeom,
  towardFallback: Pt,
): Pt[] {
  const wp = waypointForEndpointReposition(dropPt, geom, towardFallback)
  if (which === 'src') {
    return storedWpsPx.length > 0 ? [wp, ...storedWpsPx.slice(1)] : [wp]
  }
  return storedWpsPx.length > 0 ? [...storedWpsPx.slice(0, -1), wp] : [wp]
}

function distToSegment(p: Pt, a: Pt, b: Pt): number {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const len2 = dx * dx + dy * dy || 1
  let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2
  t = Math.max(0, Math.min(1, t))
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))
}

/* ── Live traffic helpers (NOC view) ─────────────────────────────────────── */

/** Hex colour for a utilisation percentage — used for strokes, particles and
 *  chips where Tailwind classes can't reach (SVG attrs, gradients). */
export function utilHex(pct: number | null | undefined): string {
  if (pct == null) return '#64748b'
  if (pct >= 85) return '#ef4444'
  if (pct >= 60) return '#f59e0b'
  if (pct >= 30) return '#a3e635'
  return '#22c55e'
}

export type LinkFlow = {
  /** bps flowing source → target (max of src out / dst in). */
  fwd: number
  /** bps flowing target → source. */
  rev: number
  total: number
  utilPct: number | null
  /** Either matched interface is operationally down. */
  ifaceDown: boolean
  srcDown: boolean
  dstDown: boolean
}

/** Direction-aware traffic summary for a link's live data. */
export function linkFlow(live: LiveLinkData | undefined): LinkFlow | null {
  if (!live) return null
  const s = live.source, t = live.target
  const fwd = Math.max(s.out_bps || 0, t.in_bps || 0)
  const rev = Math.max(s.in_bps || 0, t.out_bps || 0)
  const utilPct = s.util_pct != null || t.util_pct != null
    ? Math.max(s.util_pct || 0, t.util_pct || 0)
    : null
  const srcDown = s.matched && s.oper_status != null && s.oper_status !== 'up'
  const dstDown = t.matched && t.oper_status != null && t.oper_status !== 'up'
  return { fwd, rev, total: fwd + rev, utilPct, ifaceDown: srcDown || dstDown, srcDown, dstDown }
}

/** Particle stream spec for an animated cable: how many dots and how fast.
 *  Density grows with absolute traffic, speed with line utilisation, so a
 *  saturated 1G link races while an idle 100G link drifts. */
export function particleSpec(bps: number, utilPct: number | null): { count: number; dur: number } {
  if (bps < 1000) return { count: 0, dur: 0 }
  // 1 Kbps → 1 dot, ~1 Mbps → 2, ~100 Mbps → 3, ≥10 Gbps → 5
  const count = Math.max(1, Math.min(5, Math.floor((Math.log10(bps) - 1) / 2) + 1))
  const u = utilPct ?? Math.min(100, (Math.log10(bps) - 3) * 12)
  const dur = Math.max(1.1, 5.5 - (Math.max(0, u) / 100) * 4.4) // 5.5s idle → 1.1s saturated
  return { count, dur }
}

export function formatUptime(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return '—'
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  if (d > 0) return `${d}d ${h}h`
  const m = Math.floor((seconds % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

export function formatAgo(iso: string | null | undefined): string {
  if (!iso) return '—'
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

/** Index of the segment in `vertices` nearest to point `p` — i.e. the waypoint
 *  insertion index when the user grabs the link at `p` to bend it. */
export function nearestSegmentIndex(vertices: Pt[], p: Pt): number {
  let best = 0
  let bestD = Infinity
  for (let i = 0; i < vertices.length - 1; i++) {
    const d = distToSegment(p, vertices[i], vertices[i + 1])
    if (d < bestD) { bestD = d; best = i }
  }
  return best
}
