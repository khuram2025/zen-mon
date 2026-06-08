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
}

export type LinkKind = 'ethernet' | 'fiber' | 'wireless' | 'vpn' | 'trunk' | 'serial' | 'manual'
export type LinkSpeed = '10M' | '100M' | '1G' | '2.5G' | '10G' | '25G' | '40G' | '100G' | string
export type LinkShape = 'curve' | 'straight' | 'orthogonal'

export type Waypoint = { x_pct: number; y_pct: number }

export type LinkMetadata = {
  src_interface?: string | null
  dst_interface?: string | null
  speed?: LinkSpeed | null
  kind?: LinkKind | null
  shape?: LinkShape | null
  waypoints?: Waypoint[] | null
  notes?: string | null
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

export type ManualMapDetail = ManualMapListItem & {
  summary: {
    nodes: number
    links: number
    status_counts: Record<string, number>
    generated_at: string
  }
  nodes: ManualMapNode[]
  links: ManualMapLink[]
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

function buildOrthogonal(ax: number, ay: number, bx: number, by: number, waypoints: Pt[]): EdgePathResult {
  const verts: Pt[] = [{ x: ax, y: ay }, ...waypoints, { x: bx, y: by }]
  const poly: Pt[] = [verts[0]]
  for (let i = 1; i < verts.length; i++) {
    const prev = verts[i - 1]
    const cur = verts[i]
    if (Math.abs(prev.x - cur.x) > 0.01 && Math.abs(prev.y - cur.y) > 0.01) {
      poly.push({ x: cur.x, y: prev.y })
    }
    poly.push(cur)
  }
  const d = poly.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x} ${p.y}`).join(' ')
  const segments: Segment[] = []
  for (let i = 0; i < poly.length - 1; i++) {
    const a = poly[i]
    const b = poly[i + 1]
    segments.push({ ax: a.x, ay: a.y, bx: b.x, by: b.y, horizontal: Math.abs(a.y - b.y) < 0.01 })
  }
  return { d, segments, vertices: poly, mid: pointAtT(segments, 0.5), near: pointAtT(segments, 0.16), far: pointAtT(segments, 0.84) }
}

/** Build an SVG path + annotation anchors for any supported link shape. Inputs
 *  and waypoints are plain {x,y} in the SAME coordinate space (logical px). */
export function edgePath(
  shape: LinkShape,
  ax: number, ay: number, bx: number, by: number,
  waypoints: Pt[] = [],
): EdgePathResult {
  if (shape === 'straight') {
    const mx = (ax + bx) / 2
    const my = (ay + by) / 2
    return {
      d: `M ${ax} ${ay} L ${bx} ${by}`,
      mid: { x: mx, y: my },
      near: { x: ax + (bx - ax) * 0.14, y: ay + (by - ay) * 0.14 },
      far: { x: ax + (bx - ax) * 0.86, y: ay + (by - ay) * 0.86 },
      segments: [{ ax, ay, bx, by, horizontal: Math.abs(ay - by) < 0.01 }],
      vertices: [{ x: ax, y: ay }, { x: bx, y: by }],
    }
  }
  if (shape === 'orthogonal') {
    return buildOrthogonal(ax, ay, bx, by, waypoints)
  }
  const c = linkCurve(ax, ay, bx, by)
  return {
    d: c.d,
    mid: pointOnQuadratic(ax, ay, c.cx, c.cy, bx, by, 0.5),
    near: pointOnQuadratic(ax, ay, c.cx, c.cy, bx, by, 0.14),
    far: pointOnQuadratic(ax, ay, c.cx, c.cy, bx, by, 0.86),
    segments: [{ ax, ay, bx, by, horizontal: Math.abs(ay - by) < 0.01 }],
    vertices: [{ x: ax, y: ay }, { x: bx, y: by }],
  }
}
