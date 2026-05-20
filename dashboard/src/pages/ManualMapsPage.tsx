import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent as ReactDragEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type WheelEvent as ReactWheelEvent,
} from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowRight,
  Cable,
  ChevronLeft,
  ChevronRight,
  Circle,
  Copy,
  Download,
  GitBranch,
  Hexagon,
  Image as ImageIcon,
  Layers,
  Loader2,
  Lock,
  Maximize2,
  Minus,
  Minus as LineIcon,
  MousePointer2,
  Palette,
  Pencil,
  Plus,
  Radio,
  RefreshCw,
  Save,
  Search,
  Spline,
  Square,
  StickyNote,
  Trash2,
  Triangle,
  Type,
  Unlock,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { NetworkIcon, iconLabel, networkIcons, type IconKey } from '@/components/network-icons'

/* ── Types ────────────────────────────────────────────────────── */

type NodeStatus = 'up' | 'down' | 'degraded' | 'unknown' | 'maintenance' | string

type ManualMapListItem = {
  id: string
  name: string
  description?: string | null
  created_at?: string | null
  updated_at?: string | null
  node_count: number
  link_count: number
  status_counts: Record<string, number>
}

// All NodeMetadata fields except size_scale are visual sugar — they reshape
// or recolor the rendered node without touching the underlying device data.
type NodeShapeVariant = 'disc' | 'square' | 'rounded' | 'hex' | 'diamond' | 'cloud'
type NodeLabelPos = 'bottom' | 'top' | 'right' | 'left' | 'hidden'

type NodeMetadata = {
  // 1.0 renders the historical 64×64 px disc. Clamp to NODE_SCALE_RANGE
  // so a runaway value can't make a node fill the whole canvas.
  size_scale?: number
  // Optional CSS color (hex or rgba). When set, overrides the status ring
  // and icon tint — used to colour-code by site, role, or team.
  color?: string | null
  // Frame shape around the icon. Defaults to 'disc' for back-compat.
  shape_variant?: NodeShapeVariant | null
  // Where the hostname/IP label sits relative to the icon.
  label_pos?: NodeLabelPos | null
  // Optional secondary line under the main label (eg. role, location, tag).
  sub_label?: string | null
  // True locks the node in place — drag/resize/delete suppressed in design mode.
  locked?: boolean | null
}

type ManualMapNode = {
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

const NODE_SCALE_MIN = 0.6
const NODE_SCALE_MAX = 2.5
const NODE_SCALE_DEFAULT = 1.0
const NODE_BASE_PX = 64  // historical disc size

function clampScale(s: number): number {
  if (!isFinite(s)) return NODE_SCALE_DEFAULT
  return Math.max(NODE_SCALE_MIN, Math.min(NODE_SCALE_MAX, s))
}

function nodeScale(node: ManualMapNode): number {
  return clampScale(node.metadata?.size_scale ?? NODE_SCALE_DEFAULT)
}

type LinkKind = 'ethernet' | 'fiber' | 'wireless' | 'vpn' | 'trunk' | 'serial' | 'manual'
type LinkSpeed = '10M' | '100M' | '1G' | '2.5G' | '10G' | '25G' | '40G' | '100G' | string
type LinkShape = 'curve' | 'straight' | 'orthogonal'

type Waypoint = { x_pct: number; y_pct: number }

type LinkArrow = 'none' | 'forward' | 'backward' | 'both'

type LinkMetadata = {
  src_interface?: string | null
  dst_interface?: string | null
  speed?: LinkSpeed | null
  kind?: LinkKind | null
  shape?: LinkShape | null
  waypoints?: Waypoint[] | null   // user-placed bend points (orthogonal only)
  notes?: string | null
  // Visual overrides. When unset, the renderer falls back to defaults derived
  // from kind + endpoint health (the historical behaviour).
  color?: string | null            // CSS color — overrides health colour
  arrow?: LinkArrow | null         // direction marker(s)
  thickness?: number | null        // multiplier, 0.5 – 3.0
  animate?: boolean | null         // explicit on/off; null = auto by mode
  opacity?: number | null          // 0.1 – 1.0
}

type LiveInterface = {
  matched: boolean
  if_index?: number | null
  if_name?: string | null
  if_descr?: string | null
  if_alias?: string | null
  if_speed?: number | null            // bps
  admin_status?: string | null
  oper_status?: string | null
  in_bps?: number | null
  out_bps?: number | null
  in_packets?: number | null
  out_packets?: number | null
  util_pct?: number | null
}

type LiveLinkData = {
  source: LiveInterface
  target: LiveInterface
  window_seconds: number
  generated_at: string
}

type ManualMapLink = {
  id: string
  map_id: string
  source_node_id: string
  target_node_id: string
  label?: string | null
  link_type: string
  metadata?: LinkMetadata | null
}

type ShapeKind =
  | 'rectangle' | 'circle' | 'text'
  | 'line' | 'arrow' | 'diamond' | 'hexagon' | 'sticky'

type ShapeMetadata = {
  font_size?: number | null         // px-equivalent (1..6 viewBox units)
  font_weight?: 'normal' | 'semibold' | 'bold' | null
  font_color?: string | null
  rotation?: number | null          // degrees, applied around shape center
  opacity?: number | null           // 0.1..1.0
  stroke_width?: number | null      // viewBox units, default 0.35
  stroke_dash?: string | null       // SVG dasharray, e.g. "2 1"
}

type ManualMapShape = {
  id: string
  map_id: string
  kind: ShapeKind
  x_pct: number   // center
  y_pct: number   // center
  w_pct: number
  h_pct: number
  text?: string | null
  fill?: string | null
  stroke?: string | null
  z_index: number
  metadata?: ShapeMetadata | null
}

type ShapeRect = { x_pct: number; y_pct: number; w_pct: number; h_pct: number }

// MapMetadata is the per-map UI state we want to persist across browsers:
// background image, theme, and snap settings live here.
type MapTheme = 'default' | 'dark' | 'blueprint' | 'light' | 'graph'

type MapBackground = {
  url?: string | null              // image URL (http(s) or data:image/*;base64,…)
  opacity?: number | null          // 0..1, default 0.35
  fit?: 'cover' | 'contain' | 'stretch' | null
}

type MapMetadata = {
  theme?: MapTheme | null
  background?: MapBackground | null
  snap_enabled?: boolean | null
  snap_size?: number | null        // grid spacing in percent (1..10)
  default_link_shape?: LinkShape | null
}

type ManualMapDetail = ManualMapListItem & {
  summary: {
    nodes: number
    links: number
    shapes?: number
    status_counts: Record<string, number>
    generated_at: string
  }
  metadata?: MapMetadata | null
  nodes: ManualMapNode[]
  links: ManualMapLink[]
  shapes?: ManualMapShape[]
}

const SHAPE_DEFAULTS: Record<ShapeKind, { w: number; h: number; text: string; fill: string; stroke: string }> = {
  rectangle: { w: 22, h: 14, text: 'Zone',     fill: 'rgba(59,130,246,0.10)', stroke: '#3b82f6' },
  circle:    { w: 16, h: 16, text: '',         fill: 'rgba(168,85,247,0.10)', stroke: '#a855f7' },
  text:      { w: 14, h: 5,  text: 'Label',    fill: 'transparent',          stroke: 'transparent' },
  line:      { w: 22, h: 0.5, text: '',        fill: 'transparent',          stroke: '#64748b' },
  arrow:     { w: 22, h: 0.5, text: '',        fill: 'transparent',          stroke: '#0ea5e9' },
  diamond:   { w: 18, h: 14,  text: 'Decision', fill: 'rgba(244,114,182,0.10)', stroke: '#ec4899' },
  hexagon:   { w: 18, h: 16,  text: 'Region',  fill: 'rgba(16,185,129,0.10)', stroke: '#10b981' },
  sticky:    { w: 14, h: 9,   text: 'Note',    fill: '#fef3c7',              stroke: '#f59e0b' },
}

// Curated swatch palette — 12 well-spaced hues chosen to read on both light
// and dark backgrounds. The first entry ('') means "use default" and is
// rendered as a slash through the chip.
const COLOR_SWATCHES: { value: string; label: string }[] = [
  { value: '',         label: 'Default' },
  { value: '#ef4444',  label: 'Red' },
  { value: '#f97316',  label: 'Orange' },
  { value: '#f59e0b',  label: 'Amber' },
  { value: '#84cc16',  label: 'Lime' },
  { value: '#10b981',  label: 'Emerald' },
  { value: '#14b8a6',  label: 'Teal' },
  { value: '#06b6d4',  label: 'Cyan' },
  { value: '#3b82f6',  label: 'Blue' },
  { value: '#6366f1',  label: 'Indigo' },
  { value: '#8b5cf6',  label: 'Violet' },
  { value: '#ec4899',  label: 'Pink' },
  { value: '#64748b',  label: 'Slate' },
]

const NODE_SHAPE_VARIANTS: { value: NodeShapeVariant; label: string }[] = [
  { value: 'disc',    label: 'Disc' },
  { value: 'square',  label: 'Square' },
  { value: 'rounded', label: 'Rounded' },
  { value: 'hex',     label: 'Hex' },
  { value: 'diamond', label: 'Diamond' },
  { value: 'cloud',   label: 'Cloud' },
]

const MAP_THEMES: { value: MapTheme; label: string; hint: string }[] = [
  { value: 'default',   label: 'Default',   hint: 'Soft grid on surface' },
  { value: 'dark',      label: 'Dark',      hint: 'Inky backdrop · high contrast' },
  { value: 'light',     label: 'Light',     hint: 'Pure white · presentation mode' },
  { value: 'blueprint', label: 'Blueprint', hint: 'Engineering schematic' },
  { value: 'graph',     label: 'Graph',     hint: 'Subtle bold grid' },
]

const THEME_STYLES: Record<MapTheme, { bg: string; gridFine: string; gridMajor: string; text: string }> = {
  default:   { bg: 'transparent',            gridFine: 'rgba(148,163,184,0.10)', gridMajor: 'rgba(148,163,184,0.18)', text: 'inherit' },
  dark:      { bg: 'rgb(9 13 22)',           gridFine: 'rgba(148,163,184,0.07)', gridMajor: 'rgba(148,163,184,0.14)', text: 'rgb(228 231 240)' },
  light:     { bg: 'rgb(252 252 253)',       gridFine: 'rgba(15,23,42,0.06)',    gridMajor: 'rgba(15,23,42,0.10)',    text: 'rgb(15 23 42)' },
  blueprint: { bg: 'rgb(15 50 110)',         gridFine: 'rgba(186,230,253,0.18)', gridMajor: 'rgba(186,230,253,0.30)', text: 'rgb(224 242 254)' },
  graph:     { bg: 'rgb(250 245 235)',       gridFine: 'rgba(45,55,72,0.10)',    gridMajor: 'rgba(45,55,72,0.22)',    text: 'rgb(45 55 72)' },
}

// Snap helper — used both during drag and on pointer-up so the in-flight
// preview already matches where the node will land.
function snapPct(v: number, step: number, enabled: boolean): number {
  if (!enabled || step <= 0) return v
  return Math.round(v / step) * step
}

type Device = {
  id: string
  hostname: string
  ip_address: string
  device_type: string
  status: NodeStatus
  location?: string | null
  vendor?: string | null
  model?: string | null
}

/* ── Constants ────────────────────────────────────────────────── */

// Icon palette displayed in the left rail (in this order). The "auto"
// option means "infer from device_type" and is selectable in the inspector.
const PALETTE_ICONS: IconKey[] = [
  'router', 'switch', 'firewall', 'server', 'database',
  'load_balancer', 'access_point', 'storage', 'workstation',
  'printer', 'cloud', 'internet', 'camera', 'other',
]

// Map device_type string from backend → icon key used in our palette.
const TYPE_TO_ICON: Record<string, IconKey> = {
  router: 'router',
  switch: 'switch',
  firewall: 'firewall',
  server: 'server',
  database: 'database',
  load_balancer: 'load_balancer',
  access_point: 'access_point',
  storage: 'storage',
  workstation: 'workstation',
  printer: 'printer',
  cloud: 'cloud',
  internet: 'internet',
  camera: 'camera',
  other: 'other',
}

const STATUS_ORDER: NodeStatus[] = ['down', 'degraded', 'maintenance', 'unknown', 'up']

// Link kinds — each gets a distinct visual treatment on the canvas.
const LINK_KINDS: { value: LinkKind; label: string; hint: string }[] = [
  { value: 'ethernet', label: 'Ethernet', hint: 'Copper RJ45 / generic L2 link' },
  { value: 'fiber',    label: 'Fiber',    hint: 'Optical (SFP/SFP+) link' },
  { value: 'trunk',    label: 'Trunk',    hint: '802.1Q tagged or LAG bundle' },
  { value: 'wireless', label: 'Wireless', hint: 'Point-to-point WLAN bridge' },
  { value: 'vpn',      label: 'VPN',      hint: 'IPsec / WireGuard / GRE tunnel' },
  { value: 'serial',   label: 'Serial',   hint: 'PPP, console, or out-of-band' },
]

const LINK_SPEEDS: LinkSpeed[] = ['10M', '100M', '1G', '2.5G', '10G', '25G', '40G', '100G']

const LINK_SHAPES: { value: LinkShape; label: string; hint: string }[] = [
  { value: 'curve',      label: 'Curve',      hint: 'Soft Bezier — best when many parallel links' },
  { value: 'straight',   label: 'Straight',   hint: 'Direct A-to-B line' },
  { value: 'orthogonal', label: 'Orthogonal', hint: 'Right-angle bends — engineering diagrams' },
]

// Stroke style per kind. Color comes from the link's *health* status
// (computed from endpoint nodes), but the kind controls the dash and
// extra accents (eg. fiber adds a warm glow, wireless is dashed, etc).
const LINK_KIND_STYLE: Record<LinkKind, { dash?: string; widthMul?: number; accent?: string; arrow?: boolean }> = {
  ethernet: { },
  fiber:    { accent: '#f59e0b' },   // amber outer glow
  trunk:    { widthMul: 1.4 },
  wireless: { dash: '2 4' },
  vpn:      { dash: '6 4', accent: '#8b5cf6' }, // violet
  serial:   { dash: '8 2' },
  manual:   { },
}

const STATUS_COLOR: Record<string, { ring: string; fill: string; line: string; dot: string; badge: any }> = {
  up: {
    ring: 'border-success/60 text-success',
    fill: 'bg-success/5',
    line: 'stroke-success/55',
    dot: 'bg-success',
    badge: 'success',
  },
  down: {
    ring: 'border-danger/70 text-danger',
    fill: 'bg-danger/10',
    line: 'stroke-danger/65',
    dot: 'bg-danger',
    badge: 'danger',
  },
  degraded: {
    ring: 'border-warning/70 text-warning',
    fill: 'bg-warning/10',
    line: 'stroke-warning/55',
    dot: 'bg-warning',
    badge: 'warning',
  },
  maintenance: {
    ring: 'border-info/60 text-info',
    fill: 'bg-info/10',
    line: 'stroke-info/55',
    dot: 'bg-info',
    badge: 'info',
  },
  unknown: {
    ring: 'border-border text-muted',
    fill: 'bg-surface2/40',
    line: 'stroke-muted/35',
    dot: 'bg-muted',
    badge: 'outline',
  },
}

/* ── Small helpers ────────────────────────────────────────────── */

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value))
}

function statusKey(status?: NodeStatus): keyof typeof STATUS_COLOR {
  const key = String(status || 'unknown').toLowerCase()
  return (STATUS_COLOR[key] ? key : 'unknown') as keyof typeof STATUS_COLOR
}

// Compute a sensible curve between two points on the percent canvas.
// We use a quadratic Bezier with the control offset perpendicular to
// the segment by a fraction of its length. The same input (a,b) always
// produces the same curve, so animation is stable.
function linkPath(
  ax: number, ay: number, bx: number, by: number,
  curve = 0.18,
) {
  const mx = (ax + bx) / 2
  const my = (ay + by) / 2
  const dx = bx - ax
  const dy = by - ay
  const len = Math.hypot(dx, dy) || 1
  // perpendicular unit vector
  const nx = -dy / len
  const ny = dx / len
  const cx = mx + nx * len * curve
  const cy = my + ny * len * curve
  return { d: `M ${ax} ${ay} Q ${cx} ${cy} ${bx} ${by}`, cx, cy, mx, my }
}

// Point at parameter t (0..1) on the quadratic Bezier.
function pointOnQuadratic(ax: number, ay: number, cx: number, cy: number, bx: number, by: number, t: number) {
  const it = 1 - t
  return {
    x: it * it * ax + 2 * it * t * cx + t * t * bx,
    y: it * it * ay + 2 * it * t * cy + t * t * by,
  }
}

type Segment = { ax: number; ay: number; bx: number; by: number; horizontal: boolean }
type EdgePathResult = {
  d: string
  mid: { x: number; y: number }
  near: { x: number; y: number }
  far: { x: number; y: number }
  segments: Segment[]
  vertices: { x: number; y: number }[]
}

function _pointAtT(segments: Segment[], t: number) {
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

function _buildOrthogonal(ax: number, ay: number, bx: number, by: number, waypoints: Waypoint[]): EdgePathResult {
  // Vertex sequence: src + user waypoints + dst
  const verts: { x: number; y: number }[] = [
    { x: ax, y: ay },
    ...waypoints.map((w) => ({ x: w.x_pct, y: w.y_pct })),
    { x: bx, y: by },
  ]
  // Build the polyline with implicit L-bends between adjacent verts whose
  // x and y both differ. Horizontal-first is the default routing.
  const poly: { x: number; y: number }[] = [verts[0]]
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
  return {
    d,
    segments,
    vertices: poly,
    mid: _pointAtT(segments, 0.5),
    near: _pointAtT(segments, 0.16),
    far: _pointAtT(segments, 0.84),
  }
}

// Build an SVG path + annotation anchors (near-source, mid, near-target)
// for any of the supported link shapes. Output is shape-agnostic so the
// rendering pipeline can swap shape without other code changes.
function edgePath(
  shape: LinkShape,
  ax: number, ay: number, bx: number, by: number,
  waypoints: Waypoint[] = [],
): EdgePathResult {
  if (shape === 'straight') {
    const mx = (ax + bx) / 2
    const my = (ay + by) / 2
    const segments: Segment[] = [{ ax, ay, bx, by, horizontal: Math.abs(ay - by) < 0.01 }]
    return {
      d: `M ${ax} ${ay} L ${bx} ${by}`,
      mid: { x: mx, y: my },
      near: { x: ax + (bx - ax) * 0.14, y: ay + (by - ay) * 0.14 },
      far:  { x: ax + (bx - ax) * 0.86, y: ay + (by - ay) * 0.86 },
      segments,
      vertices: [{ x: ax, y: ay }, { x: bx, y: by }],
    }
  }
  if (shape === 'orthogonal') {
    return _buildOrthogonal(ax, ay, bx, by, waypoints)
  }
  // Default: curve
  const c = linkPath(ax, ay, bx, by)
  const mid = pointOnQuadratic(ax, ay, c.cx, c.cy, bx, by, 0.5)
  const near = pointOnQuadratic(ax, ay, c.cx, c.cy, bx, by, 0.14)
  const far  = pointOnQuadratic(ax, ay, c.cx, c.cy, bx, by, 0.86)
  return {
    d: c.d,
    mid, near, far,
    segments: [{ ax, ay, bx, by, horizontal: Math.abs(ay - by) < 0.01 }],
    vertices: [{ x: ax, y: ay }, { x: bx, y: by }],
  }
}

function linkWaypoints(link: ManualMapLink): Waypoint[] {
  const w = link.metadata?.waypoints
  return Array.isArray(w) ? w : []
}

function linkShape(link: ManualMapLink): LinkShape {
  const s = link.metadata?.shape
  return s === 'straight' || s === 'orthogonal' ? s : 'curve'
}

function formatBps(bps: number | null | undefined): string {
  if (!bps || bps <= 0) return '0 bps'
  const units = ['bps', 'Kbps', 'Mbps', 'Gbps', 'Tbps']
  let i = 0
  let v = bps
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++ }
  return `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${units[i]}`
}

// Pick a color class for a utilization percentage (0..100).
function utilizationColor(pct: number | null | undefined): string {
  if (pct == null) return 'stroke-muted/50'
  if (pct >= 85) return 'stroke-danger'
  if (pct >= 60) return 'stroke-warning'
  if (pct >= 30) return 'stroke-success'
  return 'stroke-success/60'
}

// Compute link health from endpoint statuses. If either endpoint is
// 'down', the link is 'down'. Degraded propagates. Maintenance on
// either side keeps the link 'maintenance' (informative only).
function linkHealth(srcStatus: NodeStatus, dstStatus: NodeStatus): keyof typeof STATUS_COLOR {
  const s = String(srcStatus || '').toLowerCase()
  const d = String(dstStatus || '').toLowerCase()
  if (s === 'down' || d === 'down') return 'down'
  if (s === 'degraded' || d === 'degraded') return 'degraded'
  if (s === 'maintenance' || d === 'maintenance') return 'maintenance'
  if (s === 'unknown' || d === 'unknown') return 'unknown'
  return 'up'
}

function linkKind(link: ManualMapLink): LinkKind {
  return ((link.metadata?.kind || link.link_type || 'ethernet') as LinkKind)
}

function iconForNode(node: { icon?: string; device_type?: string }): IconKey {
  if (node.icon && node.icon !== 'auto' && networkIcons[node.icon as IconKey]) return node.icon as IconKey
  return TYPE_TO_ICON[node.device_type || 'other'] || 'other'
}

// Return the user-supplied node color or null if none. Validates lightly so
// a bogus string can't leak into inline styles.
function nodeColor(node: ManualMapNode): string | null {
  const c = (node.metadata?.color || '').trim()
  if (!c) return null
  // Allow #abc, #aabbcc, #aabbccdd, rgb(...), rgba(...). Reject anything else.
  if (/^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(c)) return c
  if (/^rgba?\([\d.\s,/%]+\)$/i.test(c)) return c
  return null
}

function nodeShapeVariant(node: ManualMapNode): NodeShapeVariant {
  const v = node.metadata?.shape_variant
  if (v && ['disc','square','rounded','hex','diamond','cloud'].includes(v)) return v
  return 'disc'
}

function nodeLabelPos(node: ManualMapNode): NodeLabelPos {
  const v = node.metadata?.label_pos
  if (v && ['bottom','top','right','left','hidden'].includes(v)) return v
  return 'bottom'
}

function isNodeLocked(node: ManualMapNode): boolean {
  return Boolean(node.metadata?.locked)
}

function linkColor(link: ManualMapLink): string | null {
  const c = (link.metadata?.color || '').trim()
  if (!c) return null
  if (/^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(c)) return c
  if (/^rgba?\([\d.\s,/%]+\)$/i.test(c)) return c
  return null
}

function linkArrow(link: ManualMapLink): LinkArrow {
  const a = link.metadata?.arrow
  if (a === 'forward' || a === 'backward' || a === 'both' || a === 'none') return a
  return 'none'
}

function linkThickness(link: ManualMapLink): number {
  const t = link.metadata?.thickness
  if (typeof t === 'number' && isFinite(t)) return Math.max(0.4, Math.min(t, 3.5))
  return 1
}

function linkOpacity(link: ManualMapLink): number {
  const o = link.metadata?.opacity
  if (typeof o === 'number' && isFinite(o)) return Math.max(0.1, Math.min(o, 1))
  return 0.7
}

/* ── Page ─────────────────────────────────────────────────────── */

export function ManualMapsPage() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const selectedMapId = params.get('map')

  // UI state
  const [mode, setMode] = useState<'design' | 'live'>('design')
  const [tool, setTool] = useState<
    'select' | 'connect' |
    'shape-rectangle' | 'shape-circle' | 'shape-text' |
    'shape-line' | 'shape-arrow' | 'shape-diamond' | 'shape-hexagon' | 'shape-sticky'
  >('select')
  const [paletteOpen, setPaletteOpen] = useState(true)
  const [inspectorOpen, setInspectorOpen] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)
  const [deleteMapOpen, setDeleteMapOpen] = useState(false)
  const [newMap, setNewMap] = useState({ name: '', description: '' })
  const [paletteSearch, setPaletteSearch] = useState('')
  const [paletteStatus, setPaletteStatus] = useState<'all' | NodeStatus>('all')
  // Multi-selection. While `selectedNodeId` is the "primary" node (the one
  // whose properties show in the inspector), `multiSelectedNodeIds`
  // contains every node currently in the selection set — including the
  // primary. Shift-click toggles a node in the set; clicking empty canvas
  // clears it. Drag/delete operations target the whole set.
  const [multiSelectedNodeIds, setMultiSelectedNodeIds] = useState<Set<string>>(new Set())

  // Canvas state
  const [view, setView] = useState({ x: 0, y: 0, zoom: 1 })
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [selectedLinkId, setSelectedLinkId] = useState<string | null>(null)
  const [labelDraft, setLabelDraft] = useState('')
  const [draggingNodeId, setDraggingNodeId] = useState<string | null>(null)
  const [draftPositions, setDraftPositions] = useState<Record<string, { x_pct: number; y_pct: number }>>({})
  // Node resize via corner-handle drag. We track the in-flight scale as a
  // draft and only PATCH on pointer-up — same approach as draftPositions.
  const [draggingResize, setDraggingResize] = useState<
    { id: string; startClientX: number; startClientY: number; startScale: number } | null
  >(null)
  const [draftScales, setDraftScales] = useState<Record<string, number>>({})
  const [connectFrom, setConnectFrom] = useState<string | null>(null)
  const [connectCursor, setConnectCursor] = useState<{ x_pct: number; y_pct: number } | null>(null)
  const [linkWizard, setLinkWizard] = useState<{ source: string; target: string } | null>(null)
  const [panFrom, setPanFrom] = useState<{ x: number; y: number; vx: number; vy: number } | null>(null)
  const [dragOverCanvas, setDragOverCanvas] = useState(false)
  const [defaultShape, setDefaultShape] = useState<LinkShape>('curve')
  // Live, in-flight waypoint edits per link. Mirrors `draftPositions` for nodes:
  // we don't hit the network on every pointer-move; we commit on pointer-up.
  const [draftWaypoints, setDraftWaypoints] = useState<Record<string, Waypoint[]>>({})
  const [draggingWaypoint, setDraggingWaypoint] = useState<{ linkId: string; index: number } | null>(null)
  // Shape (rectangle/circle/text annotation) editing state. Same draft pattern:
  // mid-drag rects live here; backend PATCH only happens on pointer-up.
  const [selectedShapeId, setSelectedShapeId] = useState<string | null>(null)
  const [draftShapes, setDraftShapes] = useState<Record<string, ShapeRect>>({})
  const [draggingShape, setDraggingShape] = useState<
    | { id: string; mode: 'move'; startClientX: number; startClientY: number; startRect: ShapeRect }
    | { id: string; mode: 'resize'; startClientX: number; startClientY: number; startRect: ShapeRect }
    | null
  >(null)
  const canvasRef = useRef<HTMLDivElement>(null)

  /* ── Data ───────────────────────────────────────────────────── */

  const mapsQuery = useQuery<{ data: ManualMapListItem[] }>({
    queryKey: ['manual-maps'],
    queryFn: async () => (await api.get('/maps')).data,
    refetchInterval: 30_000,
  })
  const maps = mapsQuery.data?.data || []

  const mapQuery = useQuery<ManualMapDetail>({
    queryKey: ['manual-map', selectedMapId],
    enabled: !!selectedMapId,
    queryFn: async () => (await api.get(`/maps/${selectedMapId}`)).data,
    refetchInterval: mode === 'live' ? 5_000 : 20_000,
  })

  const devicesQuery = useQuery<{ data: Device[] }>({
    queryKey: ['devices', 'manual-map-picker'],
    queryFn: async () => (await api.get('/devices?limit=200')).data,
    refetchInterval: mode === 'live' ? 5_000 : 30_000,
  })

  // Live link statistics — match typed interface names against
  // device_interfaces and overlay NetFlow throughput. Cheap to fetch
  // (one round-trip, server-side joins everything) and only when a
  // map is selected.
  const liveLinksQuery = useQuery<{ data: Record<string, LiveLinkData> }>({
    queryKey: ['manual-map-live', selectedMapId],
    enabled: !!selectedMapId,
    queryFn: async () => (await api.get(`/maps/${selectedMapId}/links-live`)).data,
    refetchInterval: mode === 'live' ? 10_000 : 30_000,
  })
  const liveById = liveLinksQuery.data?.data || {}

  const detail = mapQuery.data || null
  const nodes = detail?.nodes || []
  const links = detail?.links || []
  const mapMeta: MapMetadata = (detail?.metadata as MapMetadata | null) || {}
  const theme: MapTheme = (mapMeta.theme || 'default') as MapTheme
  const themeStyle = THEME_STYLES[theme] || THEME_STYLES.default
  const snapEnabled = Boolean(mapMeta.snap_enabled)
  const snapSize = clamp(mapMeta.snap_size || 2, 0.5, 10)
  const mapBackground = mapMeta.background || null

  const nodeMap = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes])
  const selectedNode = selectedNodeId ? nodeMap.get(selectedNodeId) || null : null
  const currentMap = selectedMapId ? maps.find((map) => map.id === selectedMapId) || detail : null
  const allDevices = devicesQuery.data?.data || []
  const usedDeviceIds = useMemo(() => new Set(nodes.map((n) => n.device_id)), [nodes])

  const filteredDevices = useMemo(() => {
    const q = paletteSearch.trim().toLowerCase()
    return allDevices.filter((d) => {
      if (paletteStatus !== 'all' && d.status !== paletteStatus) return false
      if (!q) return true
      return (
        d.hostname.toLowerCase().includes(q) ||
        d.ip_address.toLowerCase().includes(q) ||
        d.device_type.toLowerCase().includes(q)
      )
    })
  }, [allDevices, paletteSearch, paletteStatus])

  const statusTotals = useMemo(() => {
    const t: Record<string, number> = { up: 0, down: 0, degraded: 0, unknown: 0, maintenance: 0 }
    for (const n of nodes) t[n.status] = (t[n.status] || 0) + 1
    return t
  }, [nodes])

  /* ── Effects ─────────────────────────────────────────────── */

  // Auto-select first map
  useEffect(() => {
    if (mapsQuery.isLoading) return
    if (maps.length === 0 && selectedMapId) {
      selectMap(null)
      return
    }
    if (maps.length > 0 && (!selectedMapId || !maps.some((m) => m.id === selectedMapId))) {
      selectMap(maps[0].id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapsQuery.isLoading, maps, selectedMapId])

  useEffect(() => {
    setLabelDraft(selectedNode?.label || '')
  }, [selectedNode?.id, selectedNode?.label])

  useEffect(() => {
    if (selectedNodeId && !nodeMap.has(selectedNodeId)) setSelectedNodeId(null)
  }, [nodeMap, selectedNodeId])

  // Reset view & clear selection when switching maps
  useEffect(() => {
    setView({ x: 0, y: 0, zoom: 1 })
    setSelectedNodeId(null)
    setSelectedLinkId(null)
    setConnectFrom(null)
    setDraftPositions({})
  }, [selectedMapId])

  // Clear link selection if the selected link disappears (eg. after delete).
  useEffect(() => {
    if (selectedLinkId && !links.some((l) => l.id === selectedLinkId)) setSelectedLinkId(null)
  }, [links, selectedLinkId])

  // Keyboard shortcuts
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.target as HTMLElement)?.matches?.('input,textarea,select')) return
      if (e.key === 'Escape') {
        setConnectFrom(null)
        setSelectedNodeId(null)
        setSelectedLinkId(null)
        setLinkWizard(null)
        setTool('select')
      }
      if (e.key === 'Delete' || e.key === 'Backspace') {
        if (mode === 'design') {
          if (selectedShapeId) deleteShape.mutate(selectedShapeId)
          else if (selectedLinkId) deleteLink.mutate(selectedLinkId)
          else if (multiSelectedNodeIds.size > 1) {
            // Bulk delete — each PATCH is independent.
            for (const id of multiSelectedNodeIds) {
              if (!isNodeLocked(nodeMap.get(id) || ({} as ManualMapNode))) deleteNode.mutate(id)
            }
            setMultiSelectedNodeIds(new Set())
          }
          else if (selectedNode && !isNodeLocked(selectedNode)) deleteNode.mutate(selectedNode.id)
        }
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === 'd' || e.key === 'D')) {
        e.preventDefault()
        if (mode === 'design' && selectedNode) {
          // Duplicate: place a copy 4% to the right of the original, same icon
          // and metadata. The user can pick a different device after.
          // Since each manual_map_node row is tied to a single device, we
          // can't literally clone — instead, we just nudge the user to add
          // another device from the palette. So for now, toast and skip.
          toast.info('Duplicate', 'Each map node is tied to one device — drop another device from the palette to duplicate.')
        }
      }
      if (e.key === 'c' || e.key === 'C') setTool('connect')
      if (e.key === 'v' || e.key === 'V') setTool('select')
      if (e.key === 'l' || e.key === 'L') setMode((m) => (m === 'design' ? 'live' : 'design'))
      if (e.key === '0') setView({ x: 0, y: 0, zoom: 1 })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedNode, selectedLinkId, selectedShapeId, multiSelectedNodeIds, mode, nodeMap])

  /* ── Mutations ────────────────────────────────────────────── */

  function selectMap(id: string | null) {
    const next = new URLSearchParams(params)
    if (id) next.set('map', id)
    else next.delete('map')
    setParams(next, { replace: true })
  }

  function invalidateMap(id: string | null) {
    qc.invalidateQueries({ queryKey: ['manual-maps'] })
    if (id) qc.invalidateQueries({ queryKey: ['manual-map', id] })
  }

  const createMap = useMutation({
    mutationFn: async () => (await api.post('/maps', {
      name: newMap.name.trim(),
      description: newMap.description.trim() || null,
    })).data as ManualMapListItem,
    onSuccess: (created) => {
      toast.success('Map created')
      setCreateOpen(false)
      setNewMap({ name: '', description: '' })
      qc.invalidateQueries({ queryKey: ['manual-maps'] })
      selectMap(created.id)
    },
    onError: (e: any) => toast.error('Create failed', apiErrorMessage(e)),
  })

  const addNode = useMutation({
    mutationFn: async ({ device_id, x_pct, y_pct, icon }: { device_id: string; x_pct: number; y_pct: number; icon?: string }) => {
      if (!selectedMapId) throw new Error('No map selected')
      return (await api.post(`/maps/${selectedMapId}/nodes`, {
        device_id,
        icon: icon || 'auto',
        x_pct,
        y_pct,
      })).data
    },
    onSuccess: (created) => {
      if (created?.id) setSelectedNodeId(created.id)
      invalidateMap(selectedMapId)
    },
    onError: (e: any) => toast.error('Add device failed', apiErrorMessage(e)),
  })

  const updateNode = useMutation({
    mutationFn: async ({ id, patch }: {
      id: string
      patch: Partial<Pick<ManualMapNode, 'label' | 'icon' | 'x_pct' | 'y_pct'>> & {
        metadata?: NodeMetadata | null
      }
    }) => {
      if (!selectedMapId) throw new Error('No map selected')
      return (await api.put(`/maps/${selectedMapId}/nodes/${id}`, patch)).data
    },
    onSuccess: (_, vars) => {
      setDraftPositions((prev) => {
        const next = { ...prev }
        delete next[vars.id]
        return next
      })
      invalidateMap(selectedMapId)
    },
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })

  const deleteNode = useMutation({
    mutationFn: async (id: string) => {
      if (!selectedMapId) throw new Error('No map selected')
      await api.delete(`/maps/${selectedMapId}/nodes/${id}`)
    },
    onSuccess: () => {
      toast.success('Device removed')
      setSelectedNodeId(null)
      invalidateMap(selectedMapId)
    },
    onError: (e: any) => toast.error('Remove failed', apiErrorMessage(e)),
  })

  const createLink = useMutation({
    mutationFn: async ({ source, target, kind, metadata }: {
      source: string
      target: string
      kind?: LinkKind
      metadata?: LinkMetadata
    }) => {
      if (!selectedMapId) throw new Error('No map selected')
      return (await api.post(`/maps/${selectedMapId}/links`, {
        source_node_id: source,
        target_node_id: target,
        link_type: kind || 'ethernet',
        metadata: { kind: kind || 'ethernet', ...(metadata || {}) },
      })).data
    },
    onSuccess: () => {
      toast.success('Link created')
      setLinkWizard(null)
      invalidateMap(selectedMapId)
    },
    onError: (e: any) => toast.error('Link failed', apiErrorMessage(e)),
  })

  const updateLink = useMutation({
    mutationFn: async ({ id, patch }: { id: string; patch: Partial<Pick<ManualMapLink, 'label' | 'link_type'>> & { metadata?: LinkMetadata } }) => {
      if (!selectedMapId) throw new Error('No map selected')
      return (await api.put(`/maps/${selectedMapId}/links/${id}`, patch)).data
    },
    onSuccess: () => invalidateMap(selectedMapId),
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })

  const deleteLink = useMutation({
    mutationFn: async (id: string) => {
      if (!selectedMapId) throw new Error('No map selected')
      await api.delete(`/maps/${selectedMapId}/links/${id}`)
    },
    onSuccess: () => {
      setSelectedLinkId(null)
      invalidateMap(selectedMapId)
    },
    onError: (e: any) => toast.error('Remove failed', apiErrorMessage(e)),
  })

  /* ── Shape mutations + helpers ─────────────────────────────── */

  const createShape = useMutation({
    mutationFn: async (data: Partial<ManualMapShape> & { kind: ShapeKind; x_pct: number; y_pct: number }) => {
      if (!selectedMapId) throw new Error('No map selected')
      return (await api.post<ManualMapShape>(`/maps/${selectedMapId}/shapes`, data)).data
    },
    onSuccess: (s) => {
      setSelectedShapeId(s.id)
      setSelectedNodeId(null)
      setSelectedLinkId(null)
      setTool('select')
      invalidateMap(selectedMapId)
    },
    onError: (e: any) => toast.error('Add shape failed', apiErrorMessage(e)),
  })

  const updateShape = useMutation({
    mutationFn: async ({ id, patch }: { id: string; patch: Partial<ManualMapShape> }) => {
      if (!selectedMapId) throw new Error('No map selected')
      return (await api.put<ManualMapShape>(`/maps/${selectedMapId}/shapes/${id}`, patch)).data
    },
    onSuccess: (_, vars) => {
      setDraftShapes((prev) => { const out = { ...prev }; delete out[vars.id]; return out })
      invalidateMap(selectedMapId)
    },
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })

  const deleteShape = useMutation({
    mutationFn: async (id: string) => {
      if (!selectedMapId) throw new Error('No map selected')
      await api.delete(`/maps/${selectedMapId}/shapes/${id}`)
    },
    onSuccess: () => { setSelectedShapeId(null); invalidateMap(selectedMapId) },
    onError: (e: any) => toast.error('Remove failed', apiErrorMessage(e)),
  })

  const shapes = (detail?.shapes ?? []) as ManualMapShape[]
  const shapeMap = useMemo(() => new Map(shapes.map((s) => [s.id, s])), [shapes])

  function effectiveShapeRect(s: ManualMapShape): ShapeRect {
    return draftShapes[s.id] ?? { x_pct: s.x_pct, y_pct: s.y_pct, w_pct: s.w_pct, h_pct: s.h_pct }
  }

  function beginShapeMove(e: ReactPointerEvent, s: ManualMapShape) {
    if (mode === 'live') return
    e.stopPropagation()
    setSelectedShapeId(s.id)
    setSelectedNodeId(null)
    setSelectedLinkId(null)
    setDraggingShape({
      id: s.id, mode: 'move',
      startClientX: e.clientX, startClientY: e.clientY,
      startRect: effectiveShapeRect(s),
    })
    ;(e.currentTarget as Element & { setPointerCapture?: (id: number) => void }).setPointerCapture?.(e.pointerId)
  }

  function beginShapeResize(e: ReactPointerEvent, s: ManualMapShape) {
    if (mode === 'live') return
    e.stopPropagation()
    setSelectedShapeId(s.id)
    setDraggingShape({
      id: s.id, mode: 'resize',
      startClientX: e.clientX, startClientY: e.clientY,
      startRect: effectiveShapeRect(s),
    })
    ;(e.currentTarget as Element & { setPointerCapture?: (id: number) => void }).setPointerCapture?.(e.pointerId)
  }

  const updateMap = useMutation({
    mutationFn: async (patch: Partial<{ name: string; description: string | null; metadata: MapMetadata }>) => {
      if (!selectedMapId) throw new Error('No map selected')
      return (await api.put(`/maps/${selectedMapId}`, patch)).data
    },
    onSuccess: () => invalidateMap(selectedMapId),
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })

  const patchMapMeta = useCallback((next: Partial<MapMetadata>) => {
    const merged: MapMetadata = { ...mapMeta, ...next }
    updateMap.mutate({ metadata: merged })
  }, [mapMeta, updateMap])

  const deleteMap = useMutation({
    mutationFn: async () => {
      if (!selectedMapId) throw new Error('No map selected')
      await api.delete(`/maps/${selectedMapId}`)
    },
    onSuccess: () => {
      toast.success('Map deleted')
      setDeleteMapOpen(false)
      selectMap(null)
      qc.invalidateQueries({ queryKey: ['manual-maps'] })
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  /* ── Coords ───────────────────────────────────────────────── */

  // Convert client coords to canvas-percent (taking pan/zoom into account)
  const clientToCanvasPct = useCallback((clientX: number, clientY: number) => {
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return null
    const localX = clientX - rect.left
    const localY = clientY - rect.top
    const unzX = (localX - view.x) / view.zoom
    const unzY = (localY - view.y) / view.zoom
    return {
      x_pct: clamp((unzX / rect.width) * 100, 2, 98),
      y_pct: clamp((unzY / rect.height) * 100, 2, 98),
    }
  }, [view])

  function positionFor(node: ManualMapNode) {
    return draftPositions[node.id] || { x_pct: node.x_pct, y_pct: node.y_pct }
  }

  // Get the effective waypoints for a link — in-flight drag state takes
  // precedence over the persisted metadata.
  function waypointsFor(link: ManualMapLink): Waypoint[] {
    return draftWaypoints[link.id] || linkWaypoints(link)
  }

  function persistWaypoints(link: ManualMapLink, next: Waypoint[]) {
    const md = link.metadata || {}
    updateLink.mutate({
      id: link.id,
      patch: { metadata: { ...md, waypoints: next.length ? next : null } },
    })
  }

  /* ── Node drag (move on canvas) ───────────────────────────── */

  function beginNodeDrag(e: ReactPointerEvent<HTMLButtonElement>, node: ManualMapNode) {
    if (mode === 'live' || tool === 'connect') return
    if (isNodeLocked(node)) return
    e.stopPropagation()
    setSelectedNodeId(node.id)
    setSelectedLinkId(null)
    setDraggingNodeId(node.id)
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  // Effective node scale: draft (mid-drag) takes precedence over the
  // persisted metadata.size_scale.
  function effectiveScale(node: ManualMapNode): number {
    return clampScale(draftScales[node.id] ?? nodeScale(node))
  }

  function beginNodeResize(e: ReactPointerEvent, node: ManualMapNode) {
    if (mode === 'live') return
    e.stopPropagation()
    setSelectedNodeId(node.id)
    setSelectedLinkId(null)
    setDraggingResize({
      id: node.id,
      startClientX: e.clientX,
      startClientY: e.clientY,
      startScale: effectiveScale(node),
    })
    ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
  }

  function bumpNodeScale(node: ManualMapNode, delta: number) {
    const next = clampScale(effectiveScale(node) + delta)
    const md = node.metadata || {}
    updateNode.mutate({ id: node.id, patch: { metadata: { ...md, size_scale: next } } })
  }

  function moveCanvas(e: ReactPointerEvent) {
    // Shape drag (move or resize). We compute the new rect from start values
    // plus the cursor delta so the in-flight visual matches the pointer
    // pixel-for-pixel.
    if (draggingShape) {
      const rect = canvasRef.current?.getBoundingClientRect()
      if (rect) {
        const dxPct = ((e.clientX - draggingShape.startClientX) / rect.width) * 100 / view.zoom
        const dyPct = ((e.clientY - draggingShape.startClientY) / rect.height) * 100 / view.zoom
        const sr = draggingShape.startRect
        if (draggingShape.mode === 'move') {
          setDraftShapes((prev) => ({
            ...prev,
            [draggingShape.id]: {
              x_pct: clamp(sr.x_pct + dxPct, 0, 100),
              y_pct: clamp(sr.y_pct + dyPct, 0, 100),
              w_pct: sr.w_pct,
              h_pct: sr.h_pct,
            },
          }))
        } else {
          setDraftShapes((prev) => ({
            ...prev,
            [draggingShape.id]: {
              x_pct: sr.x_pct,
              y_pct: sr.y_pct,
              w_pct: clamp(sr.w_pct + dxPct, 2, 100),
              h_pct: clamp(sr.h_pct + dyPct, 2, 100),
            },
          }))
        }
      }
      return
    }
    if (draggingResize) {
      // Convert pointer drag distance to a scale delta. The diagonal
      // (dx+dy)/2 keeps the handle "going outward" feel even if the user
      // pulls mostly vertically or mostly horizontally. 60 px of drag
      // changes scale by ~1.0 — gives a sensible feel at default zoom.
      const dx = e.clientX - draggingResize.startClientX
      const dy = e.clientY - draggingResize.startClientY
      const delta = (dx + dy) / 120
      const next = clampScale(draggingResize.startScale + delta)
      setDraftScales((prev) => ({ ...prev, [draggingResize.id]: next }))
      return
    }
    if (draggingNodeId) {
      const p = clientToCanvasPct(e.clientX, e.clientY)
      if (!p) return
      // Apply optional grid snap. We snap the leader (the dragged node); the
      // delta for the rest of the multi-selection is the same so the cluster
      // moves together.
      const snapped = {
        x_pct: snapPct(p.x_pct, snapSize, snapEnabled),
        y_pct: snapPct(p.y_pct, snapSize, snapEnabled),
      }
      const original = nodeMap.get(draggingNodeId)
      const groupIds = multiSelectedNodeIds.has(draggingNodeId)
        ? Array.from(multiSelectedNodeIds)
        : [draggingNodeId]
      if (!original || groupIds.length === 1) {
        setDraftPositions((prev) => ({ ...prev, [draggingNodeId]: snapped }))
      } else {
        const dx = snapped.x_pct - original.x_pct
        const dy = snapped.y_pct - original.y_pct
        setDraftPositions((prev) => {
          const next = { ...prev }
          for (const id of groupIds) {
            const n = nodeMap.get(id)
            if (!n) continue
            next[id] = {
              x_pct: clamp(n.x_pct + dx, 2, 98),
              y_pct: clamp(n.y_pct + dy, 2, 98),
            }
          }
          return next
        })
      }
      return
    }
    if (draggingWaypoint) {
      const p = clientToCanvasPct(e.clientX, e.clientY)
      if (!p) return
      const { linkId, index } = draggingWaypoint
      const link = links.find((l) => l.id === linkId)
      if (!link) return
      const current = waypointsFor(link)
      const next = current.slice()
      next[index] = { x_pct: p.x_pct, y_pct: p.y_pct }
      setDraftWaypoints((prev) => ({ ...prev, [linkId]: next }))
      return
    }
    if (connectFrom) {
      const p = clientToCanvasPct(e.clientX, e.clientY)
      if (p) setConnectCursor(p)
      return
    }
    if (panFrom) {
      const dx = e.clientX - panFrom.x
      const dy = e.clientY - panFrom.y
      setView((v) => ({ ...v, x: panFrom.vx + dx, y: panFrom.vy + dy }))
      return
    }
  }

  function endCanvasPointer() {
    if (draggingShape) {
      const next = draftShapes[draggingShape.id]
      const original = shapeMap.get(draggingShape.id)
      setDraggingShape(null)
      if (next && original) {
        const same = (
          Math.abs(next.x_pct - original.x_pct) < 0.1 &&
          Math.abs(next.y_pct - original.y_pct) < 0.1 &&
          Math.abs(next.w_pct - original.w_pct) < 0.1 &&
          Math.abs(next.h_pct - original.h_pct) < 0.1
        )
        if (!same) {
          updateShape.mutate({ id: draggingShape.id, patch: next })
        } else {
          // Drop the draft if it's a no-op so the rendered geometry doesn't
          // get stuck waiting for an unnecessary refetch.
          setDraftShapes((prev) => { const out = { ...prev }; delete out[draggingShape.id]; return out })
        }
      }
    }
    if (draggingResize) {
      const next = draftScales[draggingResize.id]
      const original = nodeMap.get(draggingResize.id)
      setDraggingResize(null)
      if (next != null && original && Math.abs(next - draggingResize.startScale) > 0.02) {
        const md = original.metadata || {}
        updateNode.mutate({
          id: draggingResize.id,
          patch: { metadata: { ...md, size_scale: next } },
        })
      }
      // Drop the draft so the persisted value re-takes effect on refetch.
      setDraftScales((prev) => {
        const out = { ...prev }
        delete out[draggingResize.id]
        return out
      })
    }
    if (draggingNodeId) {
      // Commit every dirty draft position. With multi-select, that may include
      // siblings of the dragged node, not just the leader. Each PATCH is
      // independent so a failure on one node doesn't block the others.
      const draftIds = Object.keys(draftPositions)
      setDraggingNodeId(null)
      for (const id of draftIds) {
        const next = draftPositions[id]
        const original = nodeMap.get(id)
        if (next && original && (Math.abs(next.x_pct - original.x_pct) > 0.2 || Math.abs(next.y_pct - original.y_pct) > 0.2)) {
          updateNode.mutate({ id, patch: next })
        } else {
          // Drop the no-op draft so it doesn't linger across refetches.
          setDraftPositions((prev) => { const out = { ...prev }; delete out[id]; return out })
        }
      }
    }
    if (draggingWaypoint) {
      const { linkId } = draggingWaypoint
      const link = links.find((l) => l.id === linkId)
      const next = draftWaypoints[linkId]
      setDraggingWaypoint(null)
      // Drop the draft after persisting; on success the query refetch
      // brings the persisted state back.
      if (link && next) {
        persistWaypoints(link, next)
        setDraftWaypoints((prev) => {
          const out = { ...prev }
          delete out[linkId]
          return out
        })
      }
    }
    if (panFrom) setPanFrom(null)
  }

  /* ── Canvas: pan & wheel zoom ─────────────────────────────── */

  function onCanvasPointerDown(e: ReactPointerEvent) {
    if (mode !== 'design') {
      setPanFrom({ x: e.clientX, y: e.clientY, vx: view.x, vy: view.y })
      ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
      return
    }
    if (e.target !== e.currentTarget) return
    // If a shape tool is active, an empty-canvas click drops a new shape at
    // that point. We only fire on a fresh-down (not while dragging) and only
    // on the canvas surface — not on top of an existing element.
    const shapeToolToKind: Record<string, ShapeKind> = {
      'shape-rectangle': 'rectangle',
      'shape-circle': 'circle',
      'shape-text': 'text',
      'shape-line': 'line',
      'shape-arrow': 'arrow',
      'shape-diamond': 'diamond',
      'shape-hexagon': 'hexagon',
      'shape-sticky': 'sticky',
    }
    if (shapeToolToKind[tool]) {
      const kind = shapeToolToKind[tool]
      const p = clientToCanvasPct(e.clientX, e.clientY)
      if (!p) return
      const d = SHAPE_DEFAULTS[kind]
      createShape.mutate({
        kind,
        x_pct: clamp(p.x_pct, d.w / 2, 100 - d.w / 2),
        y_pct: clamp(p.y_pct, Math.max(d.h, 1) / 2, 100 - Math.max(d.h, 1) / 2),
        w_pct: d.w,
        h_pct: Math.max(d.h, 1.2),
        text: d.text,
        fill: d.fill,
        stroke: d.stroke,
      })
      return
    }
    setSelectedNodeId(null)
    setSelectedLinkId(null)
    setSelectedShapeId(null)
    setMultiSelectedNodeIds(new Set())
    if (e.button === 1 || e.altKey) {
      setPanFrom({ x: e.clientX, y: e.clientY, vx: view.x, vy: view.y })
      ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    }
  }

  function onCanvasWheel(e: ReactWheelEvent) {
    if (!e.ctrlKey && !e.metaKey && Math.abs(e.deltaY) < 30) return
    e.preventDefault()
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return
    const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1
    const next = clamp(view.zoom * factor, 0.4, 3)
    const cx = e.clientX - rect.left
    const cy = e.clientY - rect.top
    // Keep cursor anchored
    const nx = cx - (cx - view.x) * (next / view.zoom)
    const ny = cy - (cy - view.y) * (next / view.zoom)
    setView({ x: nx, y: ny, zoom: next })
  }

  /* ── Drop from palette ───────────────────────────────────── */

  function onCanvasDragOver(e: ReactDragEvent<HTMLDivElement>) {
    if (!selectedMapId) return
    if (e.dataTransfer.types.includes('application/x-zenplus-device')) {
      e.preventDefault()
      e.dataTransfer.dropEffect = 'copy'
      setDragOverCanvas(true)
    }
  }

  function onCanvasDragLeave() {
    setDragOverCanvas(false)
  }

  function onCanvasDrop(e: ReactDragEvent<HTMLDivElement>) {
    e.preventDefault()
    setDragOverCanvas(false)
    if (!selectedMapId) return
    const deviceId = e.dataTransfer.getData('application/x-zenplus-device')
    if (!deviceId || usedDeviceIds.has(deviceId)) {
      if (usedDeviceIds.has(deviceId)) toast.info('Already on this map', 'That device is already placed on the canvas.')
      return
    }
    const pos = clientToCanvasPct(e.clientX, e.clientY)
    if (!pos) return
    addNode.mutate({ device_id: deviceId, x_pct: pos.x_pct, y_pct: pos.y_pct })
  }

  /* ── Connect mode ─────────────────────────────────────────── */

  function startConnect(node: ManualMapNode, e: ReactPointerEvent) {
    e.stopPropagation()
    setConnectFrom(node.id)
    const p = clientToCanvasPct(e.clientX, e.clientY)
    if (p) setConnectCursor(p)
  }

  function finishConnect(target: ManualMapNode) {
    if (!connectFrom || connectFrom === target.id) {
      setConnectFrom(null)
      return
    }
    // dedup against existing
    const exists = links.some((l) =>
      (l.source_node_id === connectFrom && l.target_node_id === target.id) ||
      (l.source_node_id === target.id && l.target_node_id === connectFrom),
    )
    if (exists) {
      toast.info('Already linked', 'A link already exists between these nodes.')
    } else {
      // Open the link wizard so the user can specify interfaces, speed, kind.
      setLinkWizard({ source: connectFrom, target: target.id })
    }
    setConnectFrom(null)
    setConnectCursor(null)
  }

  function cancelConnect() {
    setConnectFrom(null)
    setConnectCursor(null)
  }

  /* ── Export ────────────────────────────────────────────────
     Serializes the canvas SVG and downloads it. Native browsers can
     render the result directly; tools like Figma / Inkscape import it.
     The serializer scans the live SVG inside the canvas wrapper rather
     than re-rendering off-screen, so what you see is what you export
     (selection chrome aside — that gets stripped via a clone).
  */
  function exportSvg() {
    const root = canvasRef.current?.querySelector('svg') as SVGSVGElement | null
    if (!root) {
      toast.error('Nothing to export', 'The canvas has no rendered SVG yet.')
      return
    }
    const clone = root.cloneNode(true) as SVGSVGElement
    // Drop selection-only artifacts (dashed halos, resize handles, waypoint dots).
    clone.querySelectorAll('[data-export-strip]').forEach((el) => el.remove())
    // Inline xmlns so the file is standalone.
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
    clone.setAttribute('width', '1600')
    clone.setAttribute('height', '900')
    const xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(clone)
    const blob = new Blob([xml], { type: 'image/svg+xml' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${(currentMap?.name || 'network-map').replace(/[^a-z0-9_-]+/gi, '_')}.svg`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    toast.success('Exported', 'Map saved as SVG.')
  }

  /* ── Alignment guides during drag ─────────────────────────
     When a single node is being moved, find any other node that shares
     (within 0.6%) its X or Y. We surface up to a couple of guide lines
     so the user can see they're aligning. The list is recomputed on
     every render while dragging — cheap because nodes are typically <100.
  */
  const alignmentGuides = useMemo<{ x: number[]; y: number[] }>(() => {
    if (!draggingNodeId) return { x: [], y: [] }
    const leader = draftPositions[draggingNodeId]
    if (!leader) return { x: [], y: [] }
    const xs: number[] = []
    const ys: number[] = []
    for (const n of nodes) {
      if (n.id === draggingNodeId) continue
      if (multiSelectedNodeIds.has(n.id)) continue
      const px = positionFor(n)
      if (Math.abs(px.x_pct - leader.x_pct) < 0.6) xs.push(px.x_pct)
      if (Math.abs(px.y_pct - leader.y_pct) < 0.6) ys.push(px.y_pct)
    }
    return { x: xs.slice(0, 3), y: ys.slice(0, 3) }
  }, [draggingNodeId, draftPositions, nodes, multiSelectedNodeIds])

  /* ── Inspector save label ────────────────────────────────── */

  function saveLabel() {
    if (!selectedNode) return
    const next = labelDraft.trim()
    if (next && next !== selectedNode.label) {
      updateNode.mutate({ id: selectedNode.id, patch: { label: next } })
    }
  }

  /* ── Render ──────────────────────────────────────────────── */

  if (mapsQuery.error) {
    return (
      <div className="rounded-lg border border-danger/30 bg-danger/10 p-4 text-sm text-danger">
        Failed to load maps: {apiErrorMessage(mapsQuery.error as any)}
      </div>
    )
  }

  const downCount = statusTotals.down || 0
  const degradedCount = statusTotals.degraded || 0
  const inConnectFlow = tool === 'connect' || connectFrom !== null
  const showCanvasCursor = mode === 'live' ? 'cursor-grab' : panFrom ? 'cursor-grabbing' : inConnectFlow ? 'cursor-crosshair' : 'cursor-default'

  return (
    <div className="-m-5 flex h-[calc(100vh-2.75rem)] flex-col overflow-hidden bg-surface2/30">
      {/* ── Top bar ───────────────────────────────────────── */}
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border bg-surface px-3">
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary/15 text-primary">
            <Spline className="h-3.5 w-3.5" />
          </div>
          <div className="hidden flex-col leading-tight md:flex">
            <span className="text-xs font-semibold">Network Studio</span>
            <span className="text-[10px] text-muted">Live device-backed maps</span>
          </div>
        </div>

        <div className="mx-2 h-6 w-px bg-border/60" />

        <MapSwitcher
          maps={maps}
          selectedId={selectedMapId}
          onSelect={selectMap}
          onCreate={() => setCreateOpen(true)}
        />

        <div className="mx-2 h-6 w-px bg-border/60" />

        <div className="hidden items-center gap-1 rounded-md border border-border bg-surface2/40 p-0.5 text-xs lg:flex">
          <button
            type="button"
            onClick={() => setMode('design')}
            className={cn(
              'flex items-center gap-1 rounded px-2.5 py-1 transition',
              mode === 'design' ? 'bg-primary/15 text-primary' : 'text-muted hover:text-text',
            )}
            title="Design mode (V) — edit topology"
          >
            <Pencil className="h-3.5 w-3.5" /> Design
          </button>
          <button
            type="button"
            onClick={() => setMode('live')}
            className={cn(
              'flex items-center gap-1 rounded px-2.5 py-1 transition',
              mode === 'live' ? 'bg-success/15 text-success' : 'text-muted hover:text-text',
            )}
            title="Live mode (L) — read-only, 5s refresh"
          >
            <Radio className={cn('h-3.5 w-3.5', mode === 'live' && 'animate-pulse-soft')} /> Live
          </button>
        </div>

        <div className="flex-1" />

        <div className="hidden items-center gap-1.5 text-xs md:flex">
          <StatusChip label="Up" count={statusTotals.up || 0} tone="success" />
          <StatusChip label="Degraded" count={degradedCount} tone="warning" />
          <StatusChip label="Down" count={downCount} tone="danger" />
          <StatusChip label="Unknown" count={statusTotals.unknown || 0} tone="default" />
        </div>

        <div className="mx-2 h-6 w-px bg-border/60" />

        <Button variant="ghost" size="icon" onClick={() => invalidateMap(selectedMapId)} disabled={mapQuery.isFetching} title="Refresh">
          {mapQuery.isFetching ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
        <Button variant="ghost" size="icon" asChild title="Switch to automated topology">
          <Link to="/maps/automated">
            <GitBranch className="h-4 w-4" />
          </Link>
        </Button>
      </div>

      {/* ── Main: 3-col grid ─────────────────────────────── */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left palette */}
        <PaletteRail
          open={paletteOpen}
          toggle={() => setPaletteOpen((v) => !v)}
          search={paletteSearch}
          onSearch={setPaletteSearch}
          status={paletteStatus}
          onStatus={setPaletteStatus}
          devices={filteredDevices}
          usedIds={usedDeviceIds}
          loading={devicesQuery.isLoading}
          disabled={!selectedMapId || mode === 'live'}
        />

        {/* Canvas */}
        <div className="relative flex-1 overflow-hidden">
          {/* Toolbar (top-left) */}
          <div className="pointer-events-none absolute inset-0 z-30">
            <div className="pointer-events-auto absolute left-3 top-3 flex items-center gap-1.5 rounded-lg border border-border bg-surface/95 p-1 shadow-md backdrop-blur">
              <ToolBtn
                active={tool === 'select'}
                onClick={() => setTool('select')}
                disabled={mode === 'live'}
                icon={<MousePointer2 className="h-3.5 w-3.5" />}
                label="Select (V)"
              />
              <ToolBtn
                active={tool === 'connect'}
                onClick={() => setTool(tool === 'connect' ? 'select' : 'connect')}
                disabled={mode === 'live' || nodes.length < 2}
                icon={<Cable className="h-3.5 w-3.5" />}
                label="Connect (C)"
              />
              <div className="mx-0.5 h-5 w-px bg-border/60" />
              <ToolBtn
                active={tool === 'shape-rectangle'}
                onClick={() => setTool(tool === 'shape-rectangle' ? 'select' : 'shape-rectangle')}
                disabled={mode === 'live'}
                icon={<Square className="h-3.5 w-3.5" />}
                label="Rectangle"
              />
              <ToolBtn
                active={tool === 'shape-circle'}
                onClick={() => setTool(tool === 'shape-circle' ? 'select' : 'shape-circle')}
                disabled={mode === 'live'}
                icon={<Circle className="h-3.5 w-3.5" />}
                label="Circle"
              />
              <ToolBtn
                active={tool === 'shape-diamond'}
                onClick={() => setTool(tool === 'shape-diamond' ? 'select' : 'shape-diamond')}
                disabled={mode === 'live'}
                icon={<svg viewBox="0 0 14 14" className="h-3.5 w-3.5"><path d="M7 1 L13 7 L7 13 L1 7 Z" stroke="currentColor" strokeWidth="1.4" fill="none" /></svg>}
                label="Diamond"
              />
              <ToolBtn
                active={tool === 'shape-hexagon'}
                onClick={() => setTool(tool === 'shape-hexagon' ? 'select' : 'shape-hexagon')}
                disabled={mode === 'live'}
                icon={<Hexagon className="h-3.5 w-3.5" />}
                label="Hexagon"
              />
              <ToolBtn
                active={tool === 'shape-line'}
                onClick={() => setTool(tool === 'shape-line' ? 'select' : 'shape-line')}
                disabled={mode === 'live'}
                icon={<LineIcon className="h-3.5 w-3.5" />}
                label="Line"
              />
              <ToolBtn
                active={tool === 'shape-arrow'}
                onClick={() => setTool(tool === 'shape-arrow' ? 'select' : 'shape-arrow')}
                disabled={mode === 'live'}
                icon={<ArrowRight className="h-3.5 w-3.5" />}
                label="Arrow"
              />
              <ToolBtn
                active={tool === 'shape-text'}
                onClick={() => setTool(tool === 'shape-text' ? 'select' : 'shape-text')}
                disabled={mode === 'live'}
                icon={<Type className="h-3.5 w-3.5" />}
                label="Text"
              />
              <ToolBtn
                active={tool === 'shape-sticky'}
                onClick={() => setTool(tool === 'shape-sticky' ? 'select' : 'shape-sticky')}
                disabled={mode === 'live'}
                icon={<StickyNote className="h-3.5 w-3.5" />}
                label="Sticky note"
              />
              <div className="mx-0.5 h-5 w-px bg-border/60" />
              <ToolBtn
                active={snapEnabled}
                onClick={() => patchMapMeta({ snap_enabled: !snapEnabled })}
                disabled={mode === 'live' || !selectedMapId}
                icon={<svg viewBox="0 0 14 14" className="h-3.5 w-3.5"><path d="M2 2h2M6 2h2M10 2h2M2 6h2M10 6h2M2 10h2M6 10h2M10 10h2M5 5h4v4h-4z" stroke="currentColor" strokeWidth="1" fill="none" /></svg>}
                label={`Snap to grid${snapEnabled ? ' · ON' : ' · OFF'}`}
              />
              <ToolBtn
                onClick={() => exportSvg()}
                disabled={!selectedMapId || nodes.length === 0}
                icon={<Download className="h-3.5 w-3.5" />}
                label="Export SVG"
              />
              <div className="mx-0.5 h-5 w-px bg-border/60" />
              <ToolBtn onClick={() => setView({ x: 0, y: 0, zoom: 1 })} icon={<Maximize2 className="h-3.5 w-3.5" />} label="Fit (0)" />
              <ToolBtn onClick={() => setView((v) => ({ ...v, zoom: clamp(v.zoom * 1.15, 0.4, 3) }))} icon={<Plus className="h-3.5 w-3.5" />} label="Zoom in" />
              <ToolBtn onClick={() => setView((v) => ({ ...v, zoom: clamp(v.zoom / 1.15, 0.4, 3) }))} icon={<Minus className="h-3.5 w-3.5" />} label="Zoom out" />
              <div className="px-2 text-[11px] tabular-nums text-muted">{Math.round(view.zoom * 100)}%</div>
            </div>

            {/* Map label (top-center) */}
            {currentMap && (
              <div className="pointer-events-none absolute left-1/2 top-3 -translate-x-1/2 rounded-md border border-border/70 bg-surface/85 px-3 py-1 text-xs backdrop-blur">
                <span className="font-semibold text-text">{currentMap.name}</span>
                <span className="ml-2 text-muted">· {nodes.length} devices · {links.length} links</span>
                {detail?.summary.generated_at && (
                  <span className="ml-2 text-muted">· {relativeTime(detail.summary.generated_at)}</span>
                )}
              </div>
            )}

            {/* Hint banner when in connect mode */}
            {mode === 'design' && (tool === 'connect' || connectFrom) && (
              <div className="pointer-events-none absolute right-3 top-3 rounded-md border border-info/40 bg-info/10 px-3 py-1.5 text-[11px] text-info">
                {connectFrom ? 'Click a target node to create the link · Esc to cancel' : 'Drag from one node to another to link them'}
              </div>
            )}
          </div>

          {/* Canvas grid + nodes */}
          <div
            ref={canvasRef}
            data-testid="map-canvas"
            className={cn(
              'absolute inset-0 select-none overflow-hidden touch-none',
              showCanvasCursor,
            )}
            style={{ background: themeStyle.bg }}
            onPointerDown={onCanvasPointerDown}
            onPointerMove={moveCanvas}
            onPointerUp={endCanvasPointer}
            onPointerCancel={endCanvasPointer}
            onWheel={onCanvasWheel}
            onDragOver={onCanvasDragOver}
            onDragLeave={onCanvasDragLeave}
            onDrop={onCanvasDrop}
            onContextMenu={(e) => { if (connectFrom) { e.preventDefault(); cancelConnect() } }}
          >
            {/* User-supplied background image — sits behind everything. Falls back
                gracefully if the URL is bad (broken-image icon, but nothing else
                breaks). */}
            {mapBackground?.url && (
              <div
                className="pointer-events-none absolute inset-0"
                style={{
                  backgroundImage: `url("${mapBackground.url.replace(/"/g, '\\"')}")`,
                  backgroundSize: mapBackground.fit === 'contain' ? 'contain' : mapBackground.fit === 'stretch' ? '100% 100%' : 'cover',
                  backgroundPosition: 'center',
                  backgroundRepeat: 'no-repeat',
                  opacity: mapBackground.opacity != null ? mapBackground.opacity : 0.35,
                }}
              />
            )}
            {/* Grid background — only on default theme keeps the soft glow accent. */}
            {theme === 'default' && (
              <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_20%_30%,rgba(59,130,246,0.08),transparent_40%),radial-gradient(circle_at_80%_70%,rgba(168,85,247,0.05),transparent_45%)]" />
            )}
            <GridBackground view={view} fine={themeStyle.gridFine} major={themeStyle.gridMajor} />

            {/* Drop hint */}
            {dragOverCanvas && (
              <div className="pointer-events-none absolute inset-4 z-10 rounded-lg border-2 border-dashed border-primary/55 bg-primary/5" />
            )}

            {/* Scene transform wrapper */}
            <div
              className="absolute inset-0"
              style={{
                transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`,
                transformOrigin: '0 0',
              }}
            >
              {/* Links SVG — two layers: paths (in scene), then labels (HTML, also in scene) */}
              <svg className="absolute inset-0 h-full w-full overflow-visible" viewBox="0 0 100 100" preserveAspectRatio="none" role="img">
                {/* Shapes (annotations) — rendered first so they sit behind
                    links and nodes. Each kind has its own primitive but they
                    all share the same hit area, selection halo, and resize
                    handle behaviour. */}
                {shapes.map((s) => {
                  const rect = effectiveShapeRect(s)
                  const isSelected = selectedShapeId === s.id && mode === 'design'
                  const x = rect.x_pct - rect.w_pct / 2
                  const y = rect.y_pct - rect.h_pct / 2
                  const cx = rect.x_pct
                  const cy = rect.y_pct
                  const sm: ShapeMetadata = (s.metadata as ShapeMetadata | null) || {}
                  const rotation = sm.rotation || 0
                  const opacity = sm.opacity != null ? Math.max(0.1, Math.min(sm.opacity, 1)) : 1
                  const strokeW = sm.stroke_width != null ? sm.stroke_width : (s.kind === 'line' || s.kind === 'arrow' ? 0.5 : 0.35)
                  const dash = sm.stroke_dash || undefined
                  const stroke = s.stroke || (s.kind === 'text' ? 'transparent' : '#3b82f6')
                  const fill = s.fill || (s.kind === 'text' ? 'transparent' : 'rgba(59,130,246,0.10)')
                  const hitProps = mode === 'design' ? {
                    onPointerDown: (e: ReactPointerEvent) => beginShapeMove(e, s),
                    onClick: (e: React.MouseEvent) => {
                      e.stopPropagation()
                      setSelectedShapeId(s.id); setSelectedNodeId(null); setSelectedLinkId(null)
                      setMultiSelectedNodeIds(new Set())
                    },
                    style: { cursor: 'move' } as const,
                  } : {}
                  const transform = rotation ? `rotate(${rotation} ${cx} ${cy})` : undefined
                  const renderTextLabel = (forceText = false) => (s.text || forceText) && (
                    <foreignObject x={x} y={y} width={rect.w_pct} height={rect.h_pct} pointerEvents="none">
                      <div
                        className="flex h-full w-full items-center justify-center text-center leading-tight"
                        style={{
                          fontSize: `${sm.font_size != null ? Math.max(0.8, Math.min(sm.font_size, 8)) : Math.max(1.2, Math.min(rect.h_pct * 0.35, 3.5))}px`,
                          fontWeight: sm.font_weight === 'bold' ? 700 : sm.font_weight === 'semibold' ? 600 : 500,
                          color: sm.font_color || (s.kind === 'sticky' ? '#7c2d12' : undefined),
                        }}
                      >
                        {s.text || (s.kind === 'text' ? 'Label' : '')}
                      </div>
                    </foreignObject>
                  )

                  return (
                    <g key={`shape-${s.id}`} transform={transform} opacity={opacity}>
                      {s.kind === 'rectangle' && (
                        <rect
                          x={x} y={y} width={rect.w_pct} height={rect.h_pct}
                          rx={1.2} ry={1.2}
                          fill={fill} stroke={stroke}
                          strokeWidth={isSelected ? Math.max(strokeW, 0.55) : strokeW}
                          strokeDasharray={dash}
                          vectorEffect="non-scaling-stroke"
                          {...hitProps}
                        />
                      )}
                      {s.kind === 'sticky' && (
                        <g {...hitProps}>
                          <rect
                            x={x} y={y} width={rect.w_pct} height={rect.h_pct}
                            rx={0.4} ry={0.4}
                            fill={fill} stroke={stroke}
                            strokeWidth={isSelected ? Math.max(strokeW, 0.55) : strokeW}
                            strokeDasharray={dash}
                            vectorEffect="non-scaling-stroke"
                          />
                          {/* Corner fold to suggest a peeled note */}
                          <path
                            d={`M ${x + rect.w_pct - 1.6} ${y + rect.h_pct} L ${x + rect.w_pct} ${y + rect.h_pct} L ${x + rect.w_pct} ${y + rect.h_pct - 1.6} Z`}
                            fill="rgba(0,0,0,0.10)"
                            stroke={stroke}
                            strokeWidth={strokeW}
                            vectorEffect="non-scaling-stroke"
                          />
                        </g>
                      )}
                      {s.kind === 'circle' && (
                        <ellipse
                          cx={cx} cy={cy}
                          rx={rect.w_pct / 2} ry={rect.h_pct / 2}
                          fill={fill} stroke={stroke}
                          strokeWidth={isSelected ? Math.max(strokeW, 0.55) : strokeW}
                          strokeDasharray={dash}
                          vectorEffect="non-scaling-stroke"
                          {...hitProps}
                        />
                      )}
                      {s.kind === 'diamond' && (
                        <polygon
                          points={`${cx} ${y} ${x + rect.w_pct} ${cy} ${cx} ${y + rect.h_pct} ${x} ${cy}`}
                          fill={fill} stroke={stroke}
                          strokeWidth={isSelected ? Math.max(strokeW, 0.55) : strokeW}
                          strokeDasharray={dash}
                          vectorEffect="non-scaling-stroke"
                          {...hitProps}
                        />
                      )}
                      {s.kind === 'hexagon' && (() => {
                        const w = rect.w_pct
                        const h = rect.h_pct
                        const inset = w * 0.25
                        const pts = [
                          [x + inset, y],
                          [x + w - inset, y],
                          [x + w, cy],
                          [x + w - inset, y + h],
                          [x + inset, y + h],
                          [x, cy],
                        ].map((p) => p.join(' ')).join(' ')
                        return (
                          <polygon
                            points={pts}
                            fill={fill} stroke={stroke}
                            strokeWidth={isSelected ? Math.max(strokeW, 0.55) : strokeW}
                            strokeDasharray={dash}
                            vectorEffect="non-scaling-stroke"
                            {...hitProps}
                          />
                        )
                      })()}
                      {(s.kind === 'line' || s.kind === 'arrow') && (
                        <g {...hitProps}>
                          <line
                            x1={x} y1={cy} x2={x + rect.w_pct} y2={cy}
                            stroke={stroke}
                            strokeWidth={isSelected ? Math.max(strokeW, 0.55) : strokeW}
                            strokeDasharray={dash}
                            strokeLinecap="round"
                            vectorEffect="non-scaling-stroke"
                            markerEnd={s.kind === 'arrow' ? `url(#nm-shape-arrow)` : undefined}
                          />
                          {/* Wider invisible hit area for thin lines. */}
                          <line
                            x1={x} y1={cy} x2={x + rect.w_pct} y2={cy}
                            stroke="transparent" strokeWidth={Math.max(strokeW * 4, 2)}
                            vectorEffect="non-scaling-stroke"
                          />
                        </g>
                      )}
                      {(s.kind === 'rectangle' || s.kind === 'circle' || s.kind === 'diamond' || s.kind === 'hexagon' || s.kind === 'sticky' || s.kind === 'text') && renderTextLabel(s.kind === 'text')}
                      {/* Selection halo + resize handle */}
                      {isSelected && (
                        <>
                          <rect
                            x={x - 0.4} y={y - 0.4}
                            width={rect.w_pct + 0.8} height={Math.max(rect.h_pct + 0.8, 1.2)}
                            fill="none" stroke="rgb(var(--primary))" strokeWidth={0.4}
                            strokeDasharray="0.8 0.6"
                            vectorEffect="non-scaling-stroke"
                            pointerEvents="none"
                          />
                          <rect
                            x={x + rect.w_pct - 1.4} y={y + Math.max(rect.h_pct, 1.2) - 1.4}
                            width={1.8} height={1.8} rx={0.4}
                            fill="rgb(var(--primary))" stroke="white"
                            strokeWidth={0.3}
                            vectorEffect="non-scaling-stroke"
                            style={{ cursor: 'nwse-resize' }}
                            onPointerDown={(e) => beginShapeResize(e, s)}
                          />
                        </>
                      )}
                    </g>
                  )
                })}
                <defs>
                  {/* Arrowheads for fiber/wireless direction */}
                  {(Object.keys(STATUS_COLOR) as Array<keyof typeof STATUS_COLOR>).map((k) => (
                    <marker
                      key={`arr-${k}`}
                      id={`nm-arr-${k}`}
                      viewBox="0 0 10 10"
                      refX="9"
                      refY="5"
                      markerWidth="5"
                      markerHeight="5"
                      orient="auto"
                    >
                      <path d="M0 0 L10 5 L0 10 z" className={cn(STATUS_COLOR[k].line, 'opacity-80')} fill="currentColor" />
                    </marker>
                  ))}
                  {/* Generic arrow markers — used by link arrow style and the
                      arrow shape annotation. Forward / backward variants flip
                      orient so SVG handles direction at render time. */}
                  <marker id="nm-shape-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
                    <path d="M0 0 L10 5 L0 10 z" fill="currentColor" />
                  </marker>
                  <marker id="nm-link-arrow-fwd" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="4" markerHeight="4" orient="auto">
                    <path d="M0 0 L10 5 L0 10 z" fill="currentColor" />
                  </marker>
                  <marker id="nm-link-arrow-bwd" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse">
                    <path d="M0 0 L10 5 L0 10 z" fill="currentColor" />
                  </marker>
                </defs>

                {/* Existing links — rendered with the per-link shape (curve/straight/orthogonal).
                    Visual layers, bottom-up: hit area → accent halo → base stroke → animated flow. */}
                {links.map((link) => {
                  const source = nodeMap.get(link.source_node_id)
                  const target = nodeMap.get(link.target_node_id)
                  if (!source || !target) return null
                  const a = positionFor(source)
                  const b = positionFor(target)
                  const health = linkHealth(source.status, target.status)
                  const color = STATUS_COLOR[health].line
                  const kind = linkKind(link)
                  const shape = linkShape(link)
                  const kindStyle = LINK_KIND_STYLE[kind] || {}
                  const wps = waypointsFor(link)
                  const path = edgePath(shape, a.x_pct, a.y_pct, b.x_pct, b.y_pct, wps)
                  const isSelected = selectedLinkId === link.id
                  const thickness = linkThickness(link)
                  const baseWidth = (kindStyle.widthMul || 1) * 3 * thickness
                  const flowWidth = (kindStyle.widthMul || 1) * 1.5 * thickness
                  const opacity = linkOpacity(link)
                  const custom = linkColor(link)
                  const arrow = linkArrow(link)
                  const animateOverride = link.metadata?.animate
                  const live = liveById[link.id]
                  const utilPct = live ? Math.max(live.source.util_pct || 0, live.target.util_pct || 0) : null
                  const utilStroke = live && utilPct != null ? utilizationColor(utilPct) : null
                  const animate = animateOverride === false
                    ? false
                    : animateOverride === true
                      ? true
                      : mode === 'live' && (health === 'up' || health === 'degraded')

                  // When the user supplies a custom color we apply it inline via `stroke`
                  // and skip the status class. The arrow markers inherit `currentColor`,
                  // so set `color` too.
                  const inlineStroke = custom || undefined
                  const baseClass = custom ? undefined : (utilStroke || color)
                  const markerFwd = (arrow === 'forward' || arrow === 'both') ? 'url(#nm-link-arrow-fwd)' : undefined
                  const markerBwd = (arrow === 'backward' || arrow === 'both') ? 'url(#nm-link-arrow-bwd)' : undefined

                  return (
                    <g
                      key={link.id}
                      className="cursor-pointer"
                      style={{ color: inlineStroke }}
                      onClick={(e) => {
                        e.stopPropagation()
                        setSelectedLinkId(link.id)
                        setSelectedNodeId(null)
                        setSelectedShapeId(null)
                        setMultiSelectedNodeIds(new Set())
                      }}
                    >
                      <path d={path.d} fill="none" stroke="transparent" strokeWidth={8} vectorEffect="non-scaling-stroke" />
                      {(kindStyle.accent || isSelected) && (
                        <path
                          d={path.d}
                          fill="none"
                          stroke={isSelected ? 'rgb(var(--primary))' : (kindStyle.accent as string)}
                          strokeOpacity={isSelected ? 0.6 : 0.45}
                          strokeWidth={isSelected ? baseWidth + 3 : baseWidth + 1.5}
                          vectorEffect="non-scaling-stroke"
                        />
                      )}
                      <path
                        d={path.d}
                        fill="none"
                        stroke={inlineStroke}
                        vectorEffect="non-scaling-stroke"
                        strokeWidth={baseWidth}
                        strokeDasharray={kindStyle.dash}
                        opacity={opacity}
                        className={baseClass || undefined}
                        markerStart={markerBwd}
                        markerEnd={markerFwd}
                      />
                      {animate && (
                        <path
                          d={path.d}
                          fill="none"
                          stroke={inlineStroke}
                          vectorEffect="non-scaling-stroke"
                          strokeWidth={flowWidth}
                          className={cn(
                            baseClass || undefined,
                            utilPct != null && utilPct >= 60 ? 'nm-flow' : 'nm-flow-slow',
                          )}
                        />
                      )}
                    </g>
                  )
                })}

                {/* Alignment guides — shown only while dragging. Faint vertical
                    and horizontal hairlines through any other node that shares
                    the leader's X or Y. */}
                {draggingNodeId && (
                  <g data-export-strip>
                    {alignmentGuides.x.map((xv, i) => (
                      <line key={`gx-${i}`} x1={xv} y1={0} x2={xv} y2={100}
                        stroke="rgb(var(--primary))" strokeOpacity={0.45}
                        strokeWidth={0.3} strokeDasharray="0.6 0.6"
                        vectorEffect="non-scaling-stroke" pointerEvents="none" />
                    ))}
                    {alignmentGuides.y.map((yv, i) => (
                      <line key={`gy-${i}`} x1={0} y1={yv} x2={100} y2={yv}
                        stroke="rgb(var(--primary))" strokeOpacity={0.45}
                        strokeWidth={0.3} strokeDasharray="0.6 0.6"
                        vectorEffect="non-scaling-stroke" pointerEvents="none" />
                    ))}
                  </g>
                )}

                {/* In-flight connect line — uses the default shape so the
                    user sees what the new link will look like before they
                    drop it. */}
                {connectFrom && connectCursor && nodeMap.has(connectFrom) && (() => {
                  const src = positionFor(nodeMap.get(connectFrom)!)
                  const p = edgePath(defaultShape, src.x_pct, src.y_pct, connectCursor.x_pct, connectCursor.y_pct)
                  return (
                    <path
                      d={p.d}
                      fill="none"
                      vectorEffect="non-scaling-stroke"
                      strokeWidth={2.4}
                      strokeDasharray="4 3"
                      className="stroke-primary"
                    />
                  )
                })()}

                {/* Waypoint editing layer — only for the selected
                    orthogonal link. Drag a filled dot to move a bend,
                    click a hollow dot in a segment to add a new bend. */}
                <g data-export-strip>
                {mode === 'design' && selectedLinkId && (() => {
                  const sel = links.find((l) => l.id === selectedLinkId)
                  if (!sel || linkShape(sel) !== 'orthogonal') return null
                  const src = nodeMap.get(sel.source_node_id)
                  const dst = nodeMap.get(sel.target_node_id)
                  if (!src || !dst) return null
                  const a = positionFor(src)
                  const b = positionFor(dst)
                  const wps = waypointsFor(sel)
                  const path = edgePath('orthogonal', a.x_pct, a.y_pct, b.x_pct, b.y_pct, wps)
                  return (
                    <g>
                      {/* "Add bend" affordances: one hollow dot per segment midpoint */}
                      {path.segments.map((s, i) => {
                        const mx = (s.ax + s.bx) / 2
                        const my = (s.ay + s.by) / 2
                        return (
                          <circle
                            key={`add-${i}`}
                            cx={mx}
                            cy={my}
                            r={1.1}
                            className="cursor-pointer fill-surface stroke-primary hover:fill-primary/30"
                            strokeWidth={0.4}
                            vectorEffect="non-scaling-stroke"
                            onClick={(e) => {
                              e.stopPropagation()
                              // The expanded polyline interleaves user
                              // waypoints with implicit L-corners — each
                              // original vertex pair contributes up to 2
                              // segments. Floor-by-2 maps a clicked segment
                              // back to the waypoint-array slot to insert at.
                              const wpIdx = Math.min(Math.floor(i / 2), wps.length)
                              const next = [...wps]
                              next.splice(wpIdx, 0, { x_pct: mx, y_pct: my })
                              persistWaypoints(sel, next)
                            }}
                          >
                            <title>Click to add bend, drag to position</title>
                          </circle>
                        )
                      })}
                      {/* Waypoint handles — filled dots. Drag to reposition.
                          Right-click to delete. */}
                      {wps.map((w, i) => (
                        <circle
                          key={`wp-${i}`}
                          cx={w.x_pct}
                          cy={w.y_pct}
                          r={1.7}
                          className="cursor-move fill-primary stroke-surface hover:r-2"
                          strokeWidth={0.55}
                          vectorEffect="non-scaling-stroke"
                          onPointerDown={(e) => {
                            e.stopPropagation()
                            setDraggingWaypoint({ linkId: sel.id, index: i })
                            ;(canvasRef.current as any)?.setPointerCapture?.(e.pointerId)
                          }}
                          onContextMenu={(e) => {
                            e.preventDefault()
                            e.stopPropagation()
                            const next = wps.filter((_, j) => j !== i)
                            persistWaypoints(sel, next)
                          }}
                        >
                          <title>Drag to move · Right-click to remove</title>
                        </circle>
                      ))}
                    </g>
                  )
                })()}
                </g>
              </svg>

              {/* Link annotations — interface labels at endpoints + speed/throughput badge mid-link */}
              {links.map((link) => {
                const source = nodeMap.get(link.source_node_id)
                const target = nodeMap.get(link.target_node_id)
                if (!source || !target) return null
                const a = positionFor(source)
                const b = positionFor(target)
                const shape = linkShape(link)
                const wps = waypointsFor(link)
                const path = edgePath(shape, a.x_pct, a.y_pct, b.x_pct, b.y_pct, wps)
                const md = link.metadata || {}
                const live = liveById[link.id]
                // Live takes precedence over configured speed if both src+dst have throughput data.
                const bps = live ? Math.max(
                  (live.source.in_bps || 0) + (live.source.out_bps || 0),
                  (live.target.in_bps || 0) + (live.target.out_bps || 0),
                ) : 0
                const utilPct = live ? Math.max(live.source.util_pct || 0, live.target.util_pct || 0) : null
                const showThroughput = live && bps > 0
                const midText = showThroughput
                  ? `${formatBps(bps)}${utilPct != null && utilPct > 0 ? ` · ${utilPct.toFixed(0)}%` : ''}`
                  : (md.speed || link.label || '')
                const showLabels = view.zoom >= 0.7
                if (!showLabels && !midText) return null
                return (
                  <div key={`anno-${link.id}`} className="pointer-events-none">
                    {md.src_interface && (
                      <LinkChip x={path.near.x} y={path.near.y} variant="iface">
                        {md.src_interface}
                      </LinkChip>
                    )}
                    {midText && (
                      <LinkChip
                        x={path.mid.x}
                        y={path.mid.y}
                        variant={showThroughput ? 'live' : 'speed'}
                        tone={showThroughput && utilPct != null
                          ? (utilPct >= 85 ? 'danger' : utilPct >= 60 ? 'warning' : 'success')
                          : undefined}
                      >
                        {midText}
                      </LinkChip>
                    )}
                    {md.dst_interface && (
                      <LinkChip x={path.far.x} y={path.far.y} variant="iface">
                        {md.dst_interface}
                      </LinkChip>
                    )}
                  </div>
                )
              })}

              {/* Nodes */}
              {nodes.map((node) => (
                <NodeCard
                  key={node.id}
                  node={node}
                  position={positionFor(node)}
                  selected={selectedNodeId === node.id}
                  multiSelected={multiSelectedNodeIds.has(node.id)}
                  live={mode === 'live'}
                  connectMode={inConnectFlow}
                  isConnectSource={connectFrom === node.id}
                  scale={effectiveScale(node)}
                  themeText={themeStyle.text}
                  onPointerDown={(e) => beginNodeDrag(e, node)}
                  onResizeStart={(e) => beginNodeResize(e, node)}
                  onClick={(e) => {
                    if (connectFrom) {
                      finishConnect(node)
                    } else if (tool === 'connect') {
                      // In connect mode, clicking any node starts the connect.
                      setSelectedNodeId(node.id)
                      setSelectedLinkId(null)
                      setConnectFrom(node.id)
                    } else if (e.shiftKey) {
                      // Shift-click toggles the node into the multi-selection
                      // set; the most-recently-toggled node becomes primary.
                      setMultiSelectedNodeIds((prev) => {
                        const next = new Set(prev)
                        if (next.has(node.id)) next.delete(node.id)
                        else next.add(node.id)
                        return next
                      })
                      setSelectedNodeId(node.id)
                      setSelectedLinkId(null)
                      setSelectedShapeId(null)
                    } else {
                      setSelectedNodeId(node.id)
                      setSelectedLinkId(null)
                      setSelectedShapeId(null)
                      setMultiSelectedNodeIds(new Set([node.id]))
                    }
                  }}
                  onConnectHandle={(e) => startConnect(node, e)}
                />
              ))}
            </div>

            {/* Empty / loading overlays */}
            {mapQuery.isLoading && selectedMapId ? (
              <CanvasCenter>
                <Loader2 className="mr-2 inline h-4 w-4 animate-spin" /> Loading map …
              </CanvasCenter>
            ) : !selectedMapId || maps.length === 0 ? (
              <CanvasCenter>
                <div className="text-center">
                  <Layers className="mx-auto h-8 w-8 text-primary" />
                  <div className="mt-3 text-sm font-semibold">No maps yet</div>
                  <div className="mt-1 text-xs text-muted">Create your first map to start designing.</div>
                  <Button className="mt-4" onClick={() => setCreateOpen(true)}>
                    <Plus className="h-4 w-4" /> New Map
                  </Button>
                </div>
              </CanvasCenter>
            ) : nodes.length === 0 ? (
              <CanvasCenter>
                <div className="text-center">
                  <div className="mx-auto inline-flex h-9 w-9 items-center justify-center rounded-full border-2 border-dashed border-primary/50 text-primary">
                    <Plus className="h-4 w-4" />
                  </div>
                  <div className="mt-3 text-sm font-semibold">Empty canvas</div>
                  <div className="mt-1 max-w-xs text-xs text-muted">
                    Drag a device from the left palette onto the canvas. Connect them with the Cable tool, or press C.
                  </div>
                </div>
              </CanvasCenter>
            ) : null}

            {/* Mini-map (bottom-right) */}
            {nodes.length > 0 && (
              <MiniMap
                nodes={nodes.map((n) => ({ id: n.id, ...positionFor(n), status: n.status }))}
                links={links.map((l) => ({ source: l.source_node_id, target: l.target_node_id }))}
              />
            )}
          </div>
        </div>

        {/* Right inspector */}
        <InspectorRail
          open={inspectorOpen}
          toggle={() => setInspectorOpen((v) => !v)}
          selectedNode={selectedNode}
          selectedLink={selectedLinkId ? links.find((l) => l.id === selectedLinkId) || null : null}
          selectedShape={selectedShapeId ? shapeMap.get(selectedShapeId) || null : null}
          selectedLinkLive={selectedLinkId ? liveById[selectedLinkId] || null : null}
          nodeMap={nodeMap}
          labelDraft={labelDraft}
          setLabelDraft={setLabelDraft}
          onSaveLabel={saveLabel}
          onChangeIcon={(icon) => selectedNode && updateNode.mutate({ id: selectedNode.id, patch: { icon } })}
          onChangeNodeMetadata={(metadata) => selectedNode && updateNode.mutate({ id: selectedNode.id, patch: { metadata } })}
          onDeleteNode={() => selectedNode && deleteNode.mutate(selectedNode.id)}
          onUpdateLink={(id, patch) => updateLink.mutate({ id, patch })}
          onDeleteLink={(id) => deleteLink.mutate(id)}
          onUpdateShape={(id, patch) => updateShape.mutate({ id, patch })}
          onDeleteShape={(id) => deleteShape.mutate(id)}
          onDeselectLink={() => setSelectedLinkId(null)}
          onDeselectShape={() => setSelectedShapeId(null)}
          onPatchMapMeta={patchMapMeta}
          mapMeta={mapMeta}
          currentMap={currentMap}
          totals={statusTotals}
          links={links}
          nodes={nodes}
          onDeleteMap={() => setDeleteMapOpen(true)}
          mode={mode}
        />
      </div>

      {/* ── Link Wizard (after click-to-connect target) ─────── */}
      {linkWizard && nodeMap.get(linkWizard.source) && nodeMap.get(linkWizard.target) && (
        <LinkWizard
          source={nodeMap.get(linkWizard.source)!}
          target={nodeMap.get(linkWizard.target)!}
          defaultShape={defaultShape}
          pending={createLink.isPending}
          onCancel={() => setLinkWizard(null)}
          onSubmit={(kind, metadata) => {
            if (metadata.shape) setDefaultShape(metadata.shape)
            createLink.mutate({ source: linkWizard.source, target: linkWizard.target, kind, metadata })
          }}
        />
      )}

      {/* ── Create Map Dialog ────────────────────────────── */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New Network Map</DialogTitle>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault()
              if (!newMap.name.trim()) return
              createMap.mutate()
            }}
          >
            <FormField label="Name" required>
              <Input
                autoFocus
                value={newMap.name}
                placeholder="e.g. Datacenter A — Core"
                onChange={(e) => setNewMap((p) => ({ ...p, name: e.target.value }))}
              />
            </FormField>
            <FormField label="Description">
              <Textarea
                rows={3}
                value={newMap.description}
                placeholder="Optional notes about this topology"
                onChange={(e) => setNewMap((p) => ({ ...p, description: e.target.value }))}
              />
            </FormField>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setCreateOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={createMap.isPending || !newMap.name.trim()}>
                {createMap.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Create
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={deleteMapOpen}
        onOpenChange={setDeleteMapOpen}
        title="Delete map?"
        description={currentMap ? `This removes "${currentMap.name}" and all manual links on it.` : undefined}
        confirmText="Delete"
        destructive
        loading={deleteMap.isPending}
        onConfirm={() => deleteMap.mutate()}
      />
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════ */
/* ── Sub-components ─────────────────────────────────────────── */
/* ═══════════════════════════════════════════════════════════════ */

function MapSwitcher({
  maps,
  selectedId,
  onSelect,
  onCreate,
}: {
  maps: ManualMapListItem[]
  selectedId: string | null
  onSelect: (id: string | null) => void
  onCreate: () => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    if (open) document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])
  const current = maps.find((m) => m.id === selectedId)
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 rounded-md border border-border bg-surface2/40 px-2.5 py-1 text-xs hover:bg-surface2/70"
      >
        <Layers className="h-3.5 w-3.5 text-primary" />
        <span className="max-w-[180px] truncate font-medium">{current?.name || 'Select map'}</span>
        <ChevronRight className={cn('h-3 w-3 text-muted transition-transform', open && 'rotate-90')} />
      </button>
      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-64 overflow-hidden rounded-md border border-border bg-surface shadow-xl animate-fade-in">
          <div className="max-h-72 overflow-y-auto p-1">
            {maps.length === 0 && (
              <div className="px-3 py-4 text-center text-xs text-muted">No maps yet</div>
            )}
            {maps.map((m) => (
              <button
                key={m.id}
                type="button"
                onClick={() => { onSelect(m.id); setOpen(false) }}
                className={cn(
                  'flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left transition hover:bg-surface2/70',
                  selectedId === m.id && 'bg-primary/10',
                )}
              >
                <div className={cn('mt-0.5 h-2 w-2 shrink-0 rounded-full', m.status_counts.down ? 'bg-danger' : m.status_counts.degraded ? 'bg-warning' : m.status_counts.up ? 'bg-success' : 'bg-muted')} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium">{m.name}</div>
                  <div className="truncate text-[10px] text-muted">{m.node_count} devices · {m.link_count} links</div>
                </div>
              </button>
            ))}
          </div>
          <div className="border-t border-border p-1">
            <button
              type="button"
              onClick={() => { onCreate(); setOpen(false) }}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-primary hover:bg-primary/10"
            >
              <Plus className="h-3.5 w-3.5" /> New map
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function StatusChip({ label, count, tone }: { label: string; count: number; tone: 'success' | 'warning' | 'danger' | 'default' }) {
  const cls = tone === 'success' ? 'bg-success/10 text-success' :
              tone === 'warning' ? 'bg-warning/10 text-warning' :
              tone === 'danger' ? 'bg-danger/10 text-danger' :
              'bg-surface2 text-muted'
  return (
    <div className={cn('flex items-center gap-1 rounded px-2 py-0.5', cls)}>
      <span className="font-semibold tabular-nums">{count}</span>
      <span className="text-[10px] uppercase tracking-wide">{label}</span>
    </div>
  )
}

function ToolBtn({
  active,
  disabled,
  onClick,
  icon,
  label,
}: {
  active?: boolean
  disabled?: boolean
  onClick: () => void
  icon: ReactNode
  label: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={label}
      className={cn(
        'flex h-7 w-7 items-center justify-center rounded transition',
        active ? 'bg-primary/15 text-primary' : 'text-muted hover:bg-surface2 hover:text-text',
        disabled && 'cursor-not-allowed opacity-40 hover:bg-transparent',
      )}
    >
      {icon}
    </button>
  )
}

function GridBackground({
  view,
  fine = 'rgba(148,163,184,0.10)',
  major = 'rgba(148,163,184,0.18)',
}: {
  view: { x: number; y: number; zoom: number }
  fine?: string
  major?: string
}) {
  const fineSize = 24 * view.zoom
  const majorSize = fineSize * 4
  return (
    <div
      className="pointer-events-none absolute inset-0 opacity-90"
      style={{
        backgroundImage:
          `linear-gradient(to right, ${fine} 1px, transparent 1px),` +
          `linear-gradient(to bottom, ${fine} 1px, transparent 1px),` +
          `linear-gradient(to right, ${major} 1px, transparent 1px),` +
          `linear-gradient(to bottom, ${major} 1px, transparent 1px)`,
        backgroundSize: `${fineSize}px ${fineSize}px, ${fineSize}px ${fineSize}px, ${majorSize}px ${majorSize}px, ${majorSize}px ${majorSize}px`,
        backgroundPosition: `${view.x}px ${view.y}px, ${view.x}px ${view.y}px, ${view.x}px ${view.y}px, ${view.x}px ${view.y}px`,
      }}
    />
  )
}

function CanvasCenter({ children }: { children: ReactNode }) {
  return (
    <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center">
      <div className="pointer-events-auto rounded-lg border border-border bg-surface/95 px-5 py-4 shadow-lg backdrop-blur">
        {children}
      </div>
    </div>
  )
}

/* ── Node card ────────────────────────────────────────────────── */
// Renders the icon "disc" with the user-chosen shape, color override, label
// position, and lock badge. The pure-CSS clip-path approach keeps a single
// DOM element no matter the variant — the only thing that changes is the
// shape and (occasionally) border-radius. Drawback: the focus ring follows
// the bounding box, not the clipped shape. Tradeoff is worth it because
// per-shape SVG would explode the markup.

function nodeShapeStyle(variant: NodeShapeVariant): {
  borderRadius?: string
  clipPath?: string
} {
  switch (variant) {
    case 'disc':    return { borderRadius: '50%' }
    case 'square':  return { borderRadius: '6%' }
    case 'rounded': return { borderRadius: '22%' }
    case 'hex':     return { clipPath: 'polygon(25% 0, 75% 0, 100% 50%, 75% 100%, 25% 100%, 0 50%)' }
    case 'diamond': return { clipPath: 'polygon(50% 0, 100% 50%, 50% 100%, 0 50%)' }
    case 'cloud':   return { clipPath: 'polygon(15% 30%, 0 60%, 15% 90%, 50% 100%, 85% 90%, 100% 60%, 85% 30%, 65% 15%, 35% 15%)' }
    default:        return { borderRadius: '50%' }
  }
}

function NodeCard({
  node,
  position,
  selected,
  multiSelected,
  live,
  connectMode,
  isConnectSource,
  scale,
  themeText,
  onPointerDown,
  onClick,
  onConnectHandle,
  onResizeStart,
}: {
  node: ManualMapNode
  position: { x_pct: number; y_pct: number }
  selected: boolean
  multiSelected: boolean
  live: boolean
  connectMode: boolean
  isConnectSource: boolean
  scale: number
  themeText: string
  onPointerDown: (e: ReactPointerEvent<HTMLButtonElement>) => void
  onClick: (e: React.MouseEvent) => void
  onConnectHandle: (e: ReactPointerEvent) => void
  onResizeStart?: (e: ReactPointerEvent) => void
}) {
  const iconKey = iconForNode(node)
  const sk = statusKey(node.status)
  const statusCol = STATUS_COLOR[sk]
  const customCol = nodeColor(node)
  const variant = nodeShapeVariant(node)
  const labelPos = nodeLabelPos(node)
  const locked = isNodeLocked(node)
  const pulsing = live && (sk === 'down' || sk === 'degraded') && variant === 'disc'
  const dim = connectMode && !isConnectSource

  const discSize = Math.round(NODE_BASE_PX * scale)
  const iconSize = Math.round(36 * scale)
  const statusDot = Math.round(14 * scale)
  const shapeStyle = nodeShapeStyle(variant)

  // When a custom color is set, we put it on the border via inline style.
  // The status-class ring still applies (mostly for the icon tint).
  const customBorder = customCol ? { borderColor: customCol, color: customCol } : undefined

  const isLabelStacked = labelPos === 'top' || labelPos === 'bottom'
  const labelBox = labelPos === 'hidden' ? null : (
    <div
      className={cn(
        'rounded-md border px-2 py-0.5 text-center text-[11px] font-semibold leading-tight shadow-sm backdrop-blur',
        'bg-surface/90 border-border',
        labelPos === 'top' && 'mb-1.5',
        labelPos === 'bottom' && 'mt-1.5',
        (labelPos === 'left' || labelPos === 'right') && 'mx-1.5',
      )}
      style={{ color: themeText !== 'inherit' ? themeText : undefined }}
    >
      <div className="truncate text-text">{node.label || node.hostname}</div>
      <div className="truncate text-[10px] font-normal text-muted">{node.ip_address}</div>
      {node.metadata?.sub_label && (
        <div className="mt-0.5 truncate text-[9px] font-medium uppercase tracking-wider text-primary/80">
          {node.metadata.sub_label}
        </div>
      )}
    </div>
  )

  return (
    <button
      type="button"
      onPointerDown={onPointerDown}
      onClick={onClick}
      title={`${node.hostname} · ${node.ip_address}${locked ? ' · locked' : ''}`}
      data-node-id={node.id}
      className={cn(
        'group absolute z-20 -translate-x-1/2 -translate-y-1/2 transition-opacity',
        isLabelStacked ? 'flex flex-col items-center' : 'flex items-center',
        dim && 'opacity-60',
      )}
      style={{
        left: `${position.x_pct}%`,
        top: `${position.y_pct}%`,
        // Container width depends on whether labels stack or sit beside.
        minWidth: isLabelStacked ? Math.max(discSize, 128) : undefined,
      }}
    >
      {labelPos === 'top' && labelBox}
      {labelPos === 'left' && labelBox}
      {/* Icon container */}
      <div className="relative" style={{ width: discSize, height: discSize }}>
        {/* Pulse halo for problems in live mode — only on round shapes. */}
        {pulsing && (
          <span
            aria-hidden
            className={cn(
              'absolute inset-0 rounded-full',
              sk === 'down' ? 'bg-danger/40' : 'bg-warning/40',
              'nm-ping',
            )}
          />
        )}
        <div
          className={cn(
            'relative flex h-full w-full items-center justify-center border-2 shadow-md transition',
            'bg-surface',
            !customCol && statusCol.ring,
            selected && 'ring-2 ring-primary ring-offset-2 ring-offset-surface',
            multiSelected && !selected && 'ring-2 ring-primary/55 ring-offset-1 ring-offset-surface',
          )}
          style={{ ...shapeStyle, ...customBorder }}
        >
          {/* status dot */}
          <span
            aria-hidden
            className={cn(
              'absolute -right-0.5 -top-0.5 rounded-full border-2 border-surface',
              statusCol.dot,
              live && 'animate-pulse-soft',
            )}
            style={{ width: statusDot, height: statusDot }}
          />
          <NetworkIcon name={iconKey} style={{ width: iconSize, height: iconSize }} />
          {locked && (
            <span
              aria-hidden
              className="absolute -bottom-0.5 -left-0.5 flex h-4 w-4 items-center justify-center rounded-full border border-border bg-surface text-muted shadow-sm"
              title="Locked"
            >
              <Lock className="h-2.5 w-2.5" />
            </span>
          )}
        </div>
        {/* Resize handle */}
        {selected && !live && onResizeStart && !locked && (
          <span
            onPointerDown={(e) => { e.stopPropagation(); onResizeStart(e) }}
            className="absolute -bottom-1 -right-1 z-40 flex h-4 w-4 cursor-nwse-resize items-center justify-center rounded-sm border border-primary/70 bg-surface text-primary shadow-sm hover:bg-primary/10"
            title="Drag to resize"
          >
            <svg viewBox="0 0 10 10" className="h-2.5 w-2.5">
              <path d="M1 9 L9 1 M5 9 L9 5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
            </svg>
          </span>
        )}
        {!live && (
          <span
            aria-hidden
            className={cn(
              'pointer-events-none absolute -bottom-1 left-1/2 z-30 flex h-5 w-5 -translate-x-1/2 items-center justify-center rounded-full border border-primary/60 bg-surface text-primary shadow-sm transition',
              (selected || isConnectSource || connectMode) ? 'opacity-100' : 'opacity-0 group-hover:opacity-100',
              isConnectSource && 'ring-2 ring-primary/60',
            )}
          >
            <Cable className="h-2.5 w-2.5" />
          </span>
        )}
      </div>
      {labelPos === 'bottom' && labelBox}
      {labelPos === 'right' && labelBox}
    </button>
  )
}

/* ── Palette (left rail) ─────────────────────────────────────── */

function PaletteRail({
  open,
  toggle,
  search,
  onSearch,
  status,
  onStatus,
  devices,
  usedIds,
  loading,
  disabled,
}: {
  open: boolean
  toggle: () => void
  search: string
  onSearch: (v: string) => void
  status: 'all' | NodeStatus
  onStatus: (s: 'all' | NodeStatus) => void
  devices: Device[]
  usedIds: Set<string>
  loading: boolean
  disabled: boolean
}) {
  return (
    <aside
      className={cn(
        'relative flex shrink-0 flex-col border-r border-border bg-surface transition-all',
        open ? 'w-72' : 'w-9',
      )}
    >
      {!open ? (
        <button
          type="button"
          onClick={toggle}
          className="flex h-full w-full items-center justify-center text-muted hover:text-text"
          title="Show device palette"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      ) : (
        <>
          <div className="flex items-center justify-between border-b border-border px-3 py-2">
            <div>
              <div className="text-xs font-semibold">Device Palette</div>
              <div className="text-[10px] text-muted">Drag onto canvas to place</div>
            </div>
            <button
              type="button"
              onClick={toggle}
              className="flex h-6 w-6 items-center justify-center rounded text-muted hover:bg-surface2 hover:text-text"
              title="Collapse"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
          </div>
          <div className="space-y-2 border-b border-border p-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <Input
                value={search}
                onChange={(e) => onSearch(e.target.value)}
                placeholder="Search hostname, IP, type…"
                className="pl-7 text-xs"
              />
            </div>
            <div className="flex flex-wrap gap-1">
              {(['all', ...STATUS_ORDER] as const).map((s) => {
                const activeCls =
                  s === 'all' ? 'bg-primary/15 text-primary' :
                  s === 'up' ? 'bg-success/15 text-success' :
                  s === 'down' ? 'bg-danger/15 text-danger' :
                  s === 'degraded' ? 'bg-warning/15 text-warning' :
                  s === 'maintenance' ? 'bg-info/15 text-info' :
                  'bg-surface2 text-text2'
                return (
                  <button
                    key={s}
                    type="button"
                    onClick={() => onStatus(s)}
                    className={cn(
                      'rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide transition',
                      status === s
                        ? activeCls
                        : 'text-muted hover:bg-surface2 hover:text-text',
                    )}
                  >
                    {s === 'all' ? 'All' : String(s)}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Icon legend */}
          <div className="border-b border-border px-3 py-2">
            <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Icon styles</div>
            <div className="grid grid-cols-7 gap-1">
              {PALETTE_ICONS.map((key) => (
                <div
                  key={key}
                  title={iconLabel[key]}
                  className="flex aspect-square items-center justify-center rounded border border-border bg-surface2/40 p-1 text-text2"
                >
                  <NetworkIcon name={key} className="h-4 w-4" />
                </div>
              ))}
            </div>
          </div>

          {/* Device list */}
          <div className="flex-1 overflow-y-auto p-2">
            {loading ? (
              <div className="space-y-2 p-2">
                {[0, 1, 2, 3].map((i) => (
                  <div key={i} className="h-12 animate-pulse rounded-md bg-surface2/50" />
                ))}
              </div>
            ) : devices.length === 0 ? (
              <div className="rounded-md border border-dashed border-border p-4 text-center text-[11px] text-muted">
                No devices match these filters
              </div>
            ) : (
              <div className="space-y-1.5">
                {devices.map((d) => {
                  const used = usedIds.has(d.id)
                  const iconKey = TYPE_TO_ICON[d.device_type] || 'other'
                  const sk = statusKey(d.status)
                  const color = STATUS_COLOR[sk]
                  return (
                    <div
                      key={d.id}
                      data-device-id={d.id}
                      data-testid="palette-device"
                      draggable={!used && !disabled}
                      onDragStart={(e) => {
                        e.dataTransfer.setData('application/x-zenplus-device', d.id)
                        e.dataTransfer.effectAllowed = 'copy'
                      }}
                      className={cn(
                        'group flex items-center gap-2 rounded-md border border-border bg-surface px-2 py-1.5 text-xs transition',
                        used || disabled
                          ? 'opacity-50'
                          : 'cursor-grab hover:border-primary/45 hover:bg-primary/5 active:cursor-grabbing',
                      )}
                      title={used ? 'Already on this map' : disabled ? 'Select or create a map first' : `Drag onto canvas`}
                    >
                      <div className={cn('flex h-7 w-7 shrink-0 items-center justify-center rounded-md border', color.ring, 'bg-surface')}>
                        <NetworkIcon name={iconKey} className="h-4 w-4" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1">
                          <span className="truncate font-medium">{d.hostname}</span>
                          {used && <span className="rounded bg-primary/15 px-1 text-[9px] font-semibold text-primary">ON MAP</span>}
                        </div>
                        <div className="flex items-center gap-1.5 text-[10px] text-muted">
                          <span className={cn('h-1.5 w-1.5 rounded-full', color.dot)} />
                          <span className="truncate">{d.ip_address}</span>
                          <span className="text-muted/70">·</span>
                          <span className="capitalize">{d.device_type.replace('_', ' ')}</span>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </>
      )}
    </aside>
  )
}

/* ── Inspector (right rail) ──────────────────────────────────── */

function InspectorRail({
  open,
  toggle,
  selectedNode,
  selectedLink,
  selectedShape,
  selectedLinkLive,
  nodeMap,
  labelDraft,
  setLabelDraft,
  onSaveLabel,
  onChangeIcon,
  onChangeNodeMetadata,
  onDeleteNode,
  onUpdateLink,
  onDeleteLink,
  onUpdateShape,
  onDeleteShape,
  onDeselectLink,
  onDeselectShape,
  onPatchMapMeta,
  mapMeta,
  currentMap,
  totals,
  links,
  nodes,
  onDeleteMap,
  mode,
}: {
  open: boolean
  toggle: () => void
  selectedNode: ManualMapNode | null
  selectedLink: ManualMapLink | null
  selectedShape: ManualMapShape | null
  selectedLinkLive: LiveLinkData | null
  nodeMap: Map<string, ManualMapNode>
  labelDraft: string
  setLabelDraft: (v: string) => void
  onSaveLabel: () => void
  onChangeIcon: (icon: string) => void
  onChangeNodeMetadata: (metadata: NodeMetadata) => void
  onDeleteNode: () => void
  onUpdateLink: (id: string, patch: Partial<Pick<ManualMapLink, 'label' | 'link_type'>> & { metadata?: LinkMetadata }) => void
  onDeleteLink: (id: string) => void
  onUpdateShape: (id: string, patch: Partial<ManualMapShape>) => void
  onDeleteShape: (id: string) => void
  onDeselectLink: () => void
  onDeselectShape: () => void
  onPatchMapMeta: (next: Partial<MapMetadata>) => void
  mapMeta: MapMetadata
  currentMap: ManualMapListItem | ManualMapDetail | null
  totals: Record<string, number>
  links: ManualMapLink[]
  nodes: ManualMapNode[]
  onDeleteMap: () => void
  mode: 'design' | 'live'
}) {
  const header = selectedNode
    ? { title: 'Device', subtitle: selectedNode.hostname }
    : selectedLink
      ? { title: 'Link', subtitle: `${nodeMap.get(selectedLink.source_node_id)?.hostname || '?'} ↔ ${nodeMap.get(selectedLink.target_node_id)?.hostname || '?'}` }
      : selectedShape
        ? { title: 'Annotation', subtitle: selectedShape.kind }
        : { title: 'Map Summary', subtitle: currentMap?.name || '—' }
  return (
    <aside
      className={cn(
        'relative flex shrink-0 flex-col border-l border-border bg-surface transition-all',
        open ? 'w-80' : 'w-9',
      )}
    >
      {!open ? (
        <button
          type="button"
          onClick={toggle}
          className="flex h-full w-full items-center justify-center text-muted hover:text-text"
          title="Show inspector"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
      ) : (
        <>
          <div className="flex items-center justify-between border-b border-border px-3 py-2">
            <div className="min-w-0">
              <div className="text-xs font-semibold">{header.title}</div>
              <div className="truncate text-[10px] text-muted">{header.subtitle}</div>
            </div>
            {selectedLink && (
              <button
                type="button"
                onClick={onDeselectLink}
                className="rounded px-1.5 py-0.5 text-[10px] text-muted hover:bg-surface2 hover:text-text"
                title="Clear link selection"
              >Clear</button>
            )}
            {selectedShape && (
              <button
                type="button"
                onClick={onDeselectShape}
                className="rounded px-1.5 py-0.5 text-[10px] text-muted hover:bg-surface2 hover:text-text"
                title="Clear annotation selection"
              >Clear</button>
            )}
            <button
              type="button"
              onClick={toggle}
              className="flex h-6 w-6 items-center justify-center rounded text-muted hover:bg-surface2 hover:text-text"
              title="Collapse"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto p-3">
            {selectedNode ? (
              <DeviceInspector
                node={selectedNode}
                labelDraft={labelDraft}
                setLabelDraft={setLabelDraft}
                onSaveLabel={onSaveLabel}
                onChangeIcon={onChangeIcon}
                onChangeMetadata={onChangeNodeMetadata}
                onDeleteNode={onDeleteNode}
                mode={mode}
              />
            ) : selectedLink ? (
              <LinkInspector
                link={selectedLink}
                source={nodeMap.get(selectedLink.source_node_id) || null}
                target={nodeMap.get(selectedLink.target_node_id) || null}
                live={selectedLinkLive}
                onChange={(patch) => onUpdateLink(selectedLink.id, patch)}
                onDelete={() => onDeleteLink(selectedLink.id)}
                mode={mode}
              />
            ) : selectedShape ? (
              <ShapeInspector
                shape={selectedShape}
                onChange={(patch) => onUpdateShape(selectedShape.id, patch)}
                onDelete={() => onDeleteShape(selectedShape.id)}
                mode={mode}
              />
            ) : (
              <MapInspector
                currentMap={currentMap}
                totals={totals}
                links={links}
                nodes={nodes}
                mapMeta={mapMeta}
                onPatchMapMeta={onPatchMapMeta}
                onDeleteLink={onDeleteLink}
                onDeleteMap={onDeleteMap}
                mode={mode}
              />
            )}
          </div>
        </>
      )}
    </aside>
  )
}

function ColorSwatchRow({
  value,
  onChange,
  disabled,
  allowDefault = true,
}: {
  value: string | null | undefined
  onChange: (next: string | null) => void
  disabled?: boolean
  allowDefault?: boolean
}) {
  const swatches = allowDefault ? COLOR_SWATCHES : COLOR_SWATCHES.filter((c) => c.value !== '')
  const current = value || ''
  return (
    <div className="flex flex-wrap items-center gap-1">
      {swatches.map((c) => {
        const isActive = current === c.value || (c.value === '' && !current)
        return (
          <button
            key={c.value || 'default'}
            type="button"
            disabled={disabled}
            onClick={() => onChange(c.value || null)}
            title={c.label}
            className={cn(
              'relative h-5 w-5 shrink-0 rounded-full border transition',
              isActive ? 'ring-2 ring-primary ring-offset-1 ring-offset-surface' : 'border-border hover:border-border-strong',
              disabled && 'cursor-not-allowed opacity-50',
            )}
            style={{ background: c.value || 'transparent' }}
          >
            {c.value === '' && (
              <svg viewBox="0 0 10 10" className="absolute inset-0 h-full w-full text-muted">
                <line x1="1" y1="9" x2="9" y2="1" stroke="currentColor" strokeWidth="1.2" />
              </svg>
            )}
          </button>
        )
      })}
      <input
        type="color"
        value={(current && /^#[0-9a-f]{6}$/i.test(current)) ? current : '#3b82f6'}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        title="Custom color"
        className="h-5 w-5 cursor-pointer rounded border border-border bg-transparent disabled:cursor-not-allowed disabled:opacity-50"
      />
    </div>
  )
}

function DeviceInspector({
  node,
  labelDraft,
  setLabelDraft,
  onSaveLabel,
  onChangeIcon,
  onChangeMetadata,
  onDeleteNode,
  mode,
}: {
  node: ManualMapNode
  labelDraft: string
  setLabelDraft: (v: string) => void
  onSaveLabel: () => void
  onChangeIcon: (icon: string) => void
  onChangeMetadata: (next: NodeMetadata) => void
  onDeleteNode: () => void
  mode: 'design' | 'live'
}) {
  const sk = statusKey(node.status)
  const color = STATUS_COLOR[sk]
  const currentIcon = iconForNode(node)
  const customCol = nodeColor(node)
  const variant = nodeShapeVariant(node)
  const labelPos = nodeLabelPos(node)
  const locked = isNodeLocked(node)
  const md = node.metadata || {}
  const ro = mode === 'live'
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <div
          className={cn('flex h-12 w-12 items-center justify-center border-2 bg-surface', !customCol && color.ring)}
          style={{ ...nodeShapeStyle(variant), borderColor: customCol || undefined, color: customCol || undefined }}
        >
          <NetworkIcon name={currentIcon} className="h-7 w-7" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold">{node.label || node.hostname}</div>
          <div className="truncate text-[11px] text-muted">{node.hostname}</div>
        </div>
        <Badge variant={color.badge}>{String(node.status).replace('_', ' ')}</Badge>
      </div>

      {/* Label & icon */}
      <FormField label="Display label">
        <Input
          value={labelDraft}
          onChange={(e) => setLabelDraft(e.target.value)}
          onBlur={onSaveLabel}
          onKeyDown={(e) => {
            if (e.key === 'Enter') (e.currentTarget as HTMLInputElement).blur()
          }}
          disabled={mode === 'live'}
        />
      </FormField>

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Icon</div>
        <div className="grid grid-cols-7 gap-1.5">
          <button
            type="button"
            onClick={() => onChangeIcon('auto')}
            disabled={mode === 'live'}
            title="Auto (by device type)"
            className={cn(
              'flex aspect-square items-center justify-center rounded-md border text-text2 transition',
              node.icon === 'auto'
                ? 'border-primary/55 bg-primary/10 text-primary'
                : 'border-border bg-surface hover:border-border-strong',
              mode === 'live' && 'cursor-not-allowed opacity-50',
            )}
          >
            <span className="text-[10px] font-semibold">A</span>
          </button>
          {PALETTE_ICONS.map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => onChangeIcon(key)}
              disabled={mode === 'live'}
              title={iconLabel[key]}
              className={cn(
                'flex aspect-square items-center justify-center rounded-md border text-text2 transition',
                node.icon === key
                  ? 'border-primary/55 bg-primary/10 text-primary'
                  : 'border-border bg-surface hover:border-border-strong',
                mode === 'live' && 'cursor-not-allowed opacity-50',
              )}
            >
              <NetworkIcon name={key} className="h-4 w-4" />
            </button>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Color</div>
        <ColorSwatchRow
          value={md.color}
          disabled={ro}
          onChange={(next) => onChangeMetadata({ ...md, color: next })}
        />
      </div>

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Shape</div>
        <div className="grid grid-cols-6 gap-1">
          {NODE_SHAPE_VARIANTS.map((v) => (
            <button
              key={v.value}
              type="button"
              disabled={ro}
              onClick={() => onChangeMetadata({ ...md, shape_variant: v.value })}
              title={v.label}
              className={cn(
                'flex aspect-square items-center justify-center rounded-md border text-text2 transition',
                variant === v.value ? 'border-primary/55 bg-primary/10 text-primary' : 'border-border bg-surface hover:border-border-strong',
                ro && 'cursor-not-allowed opacity-50',
              )}
            >
              <span
                aria-hidden
                className="h-4 w-4 border-[1.5px] border-current"
                style={nodeShapeStyle(v.value)}
              />
            </button>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Label position</div>
        <div className="grid grid-cols-5 gap-1 text-[10px]">
          {(['top','left','bottom','right','hidden'] as NodeLabelPos[]).map((p) => (
            <button
              key={p}
              type="button"
              disabled={ro}
              onClick={() => onChangeMetadata({ ...md, label_pos: p })}
              className={cn(
                'rounded-md border px-1 py-1 text-center capitalize transition',
                labelPos === p ? 'border-primary/55 bg-primary/10 text-primary' : 'border-border bg-surface hover:border-border-strong',
                ro && 'cursor-not-allowed opacity-50',
              )}
            >{p}</button>
          ))}
        </div>
      </div>

      <FormField label="Sub-label">
        <Input
          value={md.sub_label || ''}
          placeholder="Optional — role, location, tag"
          disabled={ro}
          onChange={(e) => onChangeMetadata({ ...md, sub_label: e.target.value })}
          onBlur={(e) => onChangeMetadata({ ...md, sub_label: e.target.value.trim() || null })}
        />
      </FormField>

      <div className="flex items-center justify-between rounded-md border border-border bg-surface2/40 px-2.5 py-1.5">
        <div className="flex items-center gap-2 text-xs">
          {locked ? <Lock className="h-3.5 w-3.5 text-warning" /> : <Unlock className="h-3.5 w-3.5 text-muted" />}
          <span className="font-medium">Lock position</span>
        </div>
        <button
          type="button"
          disabled={ro}
          onClick={() => onChangeMetadata({ ...md, locked: !locked })}
          className={cn(
            'rounded-md px-2 py-0.5 text-[10px] font-semibold transition',
            locked ? 'bg-warning/15 text-warning' : 'bg-surface2 text-muted hover:text-text',
            ro && 'cursor-not-allowed opacity-50',
          )}
        >{locked ? 'Locked' : 'Unlocked'}</button>
      </div>

      <div>
        <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Properties</div>
        <div className="grid grid-cols-2 gap-1.5 text-xs">
          <InfoTile label="IP" value={node.ip_address} />
          <InfoTile label="Type" value={node.device_type.replace('_', ' ')} />
          <InfoTile label="Vendor" value={node.vendor || '—'} />
          <InfoTile label="Model" value={node.model || '—'} />
          <InfoTile label="Last seen" value={node.last_seen ? relativeTime(node.last_seen) : '—'} />
          <InfoTile label="Position" value={`${node.x_pct.toFixed(1)} · ${node.y_pct.toFixed(1)}`} />
        </div>
      </div>

      <div className="flex gap-2">
        <Button variant="outline" size="sm" asChild className="flex-1">
          <Link to={`/devices/${node.device_id}`}>Open device</Link>
        </Button>
        {mode === 'design' && (
          <Button variant="destructive" size="sm" onClick={onDeleteNode} title="Remove from map (Del)">
            <Trash2 className="h-4 w-4" />
          </Button>
        )}
      </div>
    </div>
  )
}

function MapInspector({
  currentMap,
  totals,
  links,
  nodes,
  mapMeta,
  onPatchMapMeta,
  onDeleteLink,
  onDeleteMap,
  mode,
}: {
  currentMap: ManualMapListItem | ManualMapDetail | null
  totals: Record<string, number>
  links: ManualMapLink[]
  nodes: ManualMapNode[]
  mapMeta: MapMetadata
  onPatchMapMeta: (next: Partial<MapMetadata>) => void
  onDeleteLink: (id: string) => void
  onDeleteMap: () => void
  mode: 'design' | 'live'
}) {
  const nodeMap = new Map(nodes.map((n) => [n.id, n]))
  const theme = (mapMeta.theme || 'default') as MapTheme
  const bg = mapMeta.background || {}
  const ro = mode === 'live'
  return (
    <div className="space-y-4">
      {currentMap && (currentMap as any).description && (
        <div className="rounded-md border border-border bg-surface2/40 p-2.5 text-xs text-text2">
          {(currentMap as any).description}
        </div>
      )}

      {/* Theme picker */}
      <div>
        <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
          <Palette className="h-3 w-3" /> Canvas theme
        </div>
        <div className="grid grid-cols-2 gap-1.5">
          {MAP_THEMES.map((t) => (
            <button
              key={t.value}
              type="button"
              disabled={ro || !currentMap}
              onClick={() => onPatchMapMeta({ theme: t.value })}
              title={t.hint}
              className={cn(
                'flex items-center gap-2 rounded-md border px-2 py-1.5 text-left transition',
                theme === t.value ? 'border-primary/55 bg-primary/10 text-primary' : 'border-border bg-surface hover:border-border-strong',
                (ro || !currentMap) && 'cursor-not-allowed opacity-50',
              )}
            >
              <span
                className="h-6 w-6 rounded border border-border"
                style={{ background: THEME_STYLES[t.value].bg }}
              />
              <div className="min-w-0 flex-1">
                <div className="text-xs font-semibold">{t.label}</div>
                <div className="truncate text-[9px] text-muted">{t.hint}</div>
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Background image */}
      <div>
        <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
          <ImageIcon className="h-3 w-3" /> Background image
        </div>
        <Input
          placeholder="Image URL (https:// or data:image/…)"
          value={bg.url || ''}
          disabled={ro || !currentMap}
          onChange={(e) => onPatchMapMeta({ background: { ...bg, url: e.target.value } })}
          onBlur={(e) => onPatchMapMeta({ background: { ...bg, url: e.target.value.trim() || null } })}
        />
        <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-muted">
          <span>Fit</span>
          <div className="flex gap-1">
            {(['cover','contain','stretch'] as const).map((f) => (
              <button
                key={f}
                type="button"
                disabled={ro || !currentMap}
                onClick={() => onPatchMapMeta({ background: { ...bg, fit: f } })}
                className={cn(
                  'rounded border px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide transition',
                  (bg.fit || 'cover') === f ? 'border-primary/55 bg-primary/10 text-primary' : 'border-border bg-surface text-muted hover:text-text',
                  (ro || !currentMap) && 'cursor-not-allowed opacity-50',
                )}
              >{f}</button>
            ))}
          </div>
        </div>
        <div className="mt-2 flex items-center gap-2 text-[10px] text-muted">
          <span className="w-12">Opacity</span>
          <input
            type="range"
            min={0.05}
            max={1}
            step={0.05}
            value={bg.opacity != null ? bg.opacity : 0.35}
            disabled={ro || !currentMap}
            onChange={(e) => onPatchMapMeta({ background: { ...bg, opacity: parseFloat(e.target.value) } })}
            className="flex-1"
          />
          <span className="w-9 text-right tabular-nums">{Math.round((bg.opacity != null ? bg.opacity : 0.35) * 100)}%</span>
        </div>
        {bg.url && (
          <button
            type="button"
            disabled={ro}
            onClick={() => onPatchMapMeta({ background: { url: null, opacity: bg.opacity, fit: bg.fit } })}
            className="mt-1 w-full rounded border border-dashed border-border px-2 py-1 text-[10px] text-muted hover:bg-surface2 hover:text-text"
          >Remove background</button>
        )}
      </div>

      {/* Snap to grid */}
      <div className="flex items-center justify-between rounded-md border border-border bg-surface2/40 px-2.5 py-1.5">
        <div className="text-xs font-medium">Snap to grid</div>
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={0.5}
            max={10}
            step={0.5}
            value={mapMeta.snap_size || 2}
            disabled={ro || !mapMeta.snap_enabled || !currentMap}
            onChange={(e) => onPatchMapMeta({ snap_size: parseFloat(e.target.value) || 2 })}
            className="h-6 w-12 rounded border border-border bg-surface px-1 text-[10px] tabular-nums"
          />
          <button
            type="button"
            disabled={ro || !currentMap}
            onClick={() => onPatchMapMeta({ snap_enabled: !mapMeta.snap_enabled })}
            className={cn(
              'rounded-md px-2 py-0.5 text-[10px] font-semibold transition',
              mapMeta.snap_enabled ? 'bg-success/15 text-success' : 'bg-surface2 text-muted hover:text-text',
            )}
          >{mapMeta.snap_enabled ? 'ON' : 'OFF'}</button>
        </div>
      </div>

      <div>
        <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Status breakdown</div>
        <div className="grid grid-cols-2 gap-1.5 text-xs">
          <InfoTile label="Up" value={String(totals.up || 0)} />
          <InfoTile label="Down" value={String(totals.down || 0)} accent={totals.down ? 'danger' : undefined} />
          <InfoTile label="Degraded" value={String(totals.degraded || 0)} accent={totals.degraded ? 'warning' : undefined} />
          <InfoTile label="Maintenance" value={String(totals.maintenance || 0)} />
          <InfoTile label="Unknown" value={String(totals.unknown || 0)} />
          <InfoTile label="Total" value={String(nodes.length)} />
        </div>
      </div>

      <div>
        <div className="mb-1.5 flex items-center justify-between">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Links ({links.length})</div>
        </div>
        {links.length === 0 ? (
          <div className="rounded-md border border-dashed border-border p-3 text-center text-[11px] text-muted">
            No links yet
          </div>
        ) : (
          <div className="max-h-64 space-y-1.5 overflow-y-auto pr-1">
            {links.map((l) => {
              const s = nodeMap.get(l.source_node_id)
              const t = nodeMap.get(l.target_node_id)
              return (
                <div key={l.id} className="flex items-center gap-2 rounded-md border border-border bg-surface2/40 px-2 py-1.5 text-xs">
                  <Cable className="h-3.5 w-3.5 shrink-0 text-muted" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[11px] font-medium">
                      {s?.hostname || '—'} <span className="text-muted">↔</span> {t?.hostname || '—'}
                    </div>
                    {l.label && <div className="truncate text-[10px] text-muted">{l.label}</div>}
                  </div>
                  {mode === 'design' && (
                    <button
                      type="button"
                      onClick={() => onDeleteLink(l.id)}
                      className="rounded p-1 text-muted hover:bg-danger/10 hover:text-danger"
                      title="Remove link"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      {currentMap && mode === 'design' && (
        <button
          type="button"
          onClick={onDeleteMap}
          className="flex w-full items-center justify-center gap-1.5 rounded-md border border-danger/30 bg-danger/5 px-2 py-1.5 text-[11px] font-medium text-danger transition hover:bg-danger/10"
        >
          <Trash2 className="h-3.5 w-3.5" /> Delete this map
        </button>
      )}
    </div>
  )
}

function InfoTile({ label, value, accent }: { label: string; value: string; accent?: 'danger' | 'warning' }) {
  const cls = accent === 'danger' ? 'text-danger' : accent === 'warning' ? 'text-warning' : 'text-text'
  return (
    <div className="rounded-md border border-border bg-surface2/40 p-1.5">
      <div className="text-[9px] uppercase tracking-[0.08em] text-muted">{label}</div>
      <div className={cn('mt-0.5 truncate text-xs font-semibold', cls)}>{value}</div>
    </div>
  )
}

/* ── Mini-map ──────────────────────────────────────────────── */

function MiniMap({
  nodes,
  links,
}: {
  nodes: { id: string; x_pct: number; y_pct: number; status: NodeStatus }[]
  links: { source: string; target: string }[]
}) {
  const map = new Map(nodes.map((n) => [n.id, n]))
  return (
    <div className="pointer-events-none absolute bottom-3 right-3 z-30 h-32 w-44 overflow-hidden rounded-lg border border-border bg-surface/90 p-1.5 shadow-lg backdrop-blur">
      <svg viewBox="0 0 100 100" className="h-full w-full" preserveAspectRatio="none">
        {links.map((l, i) => {
          const a = map.get(l.source)
          const b = map.get(l.target)
          if (!a || !b) return null
          return <line key={i} x1={a.x_pct} y1={a.y_pct} x2={b.x_pct} y2={b.y_pct} className="stroke-muted/40 stroke-[0.5]" />
        })}
        {nodes.map((n) => {
          const sk = statusKey(n.status)
          const cls = sk === 'down' ? 'fill-danger' : sk === 'degraded' ? 'fill-warning' : sk === 'up' ? 'fill-success' : 'fill-muted'
          return <circle key={n.id} cx={n.x_pct} cy={n.y_pct} r={1.6} className={cls} />
        })}
      </svg>
    </div>
  )
}

/* ── Tiny SVG preview glyph used in the shape picker ─────────── */

function ShapeGlyph({ shape }: { shape: LinkShape }) {
  return (
    <svg width="32" height="14" viewBox="0 0 32 14" fill="none">
      <circle cx="3" cy="7" r="2" className="fill-current" />
      <circle cx="29" cy="7" r="2" className="fill-current" />
      {shape === 'curve' && (
        <path d="M3 7 Q 16 -3 29 7" stroke="currentColor" strokeWidth="1.2" fill="none" />
      )}
      {shape === 'straight' && (
        <path d="M3 7 L 29 7" stroke="currentColor" strokeWidth="1.2" />
      )}
      {shape === 'orthogonal' && (
        <path d="M3 7 L 16 7 L 16 13 L 29 13" stroke="currentColor" strokeWidth="1.2" fill="none" />
      )}
    </svg>
  )
}

/* ── Live link data panel (interface up/down + throughput) ──── */

function LiveLinkPanel({
  live,
  source,
  target,
}: {
  live: LiveLinkData
  source: ManualMapNode | null
  target: ManualMapNode | null
}) {
  return (
    <div className="rounded-lg border border-border bg-surface2/40 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">Live interface data</span>
        <span className="text-[9px] text-muted">{live.window_seconds}s window</span>
      </div>
      <div className="grid grid-cols-1 gap-2">
        <LiveInterfaceCard label={source?.hostname || 'Source'} iface={live.source} />
        <LiveInterfaceCard label={target?.hostname || 'Target'} iface={live.target} />
      </div>
    </div>
  )
}

function LiveInterfaceCard({ label, iface }: { label: string; iface: LiveInterface }) {
  if (!iface.matched) {
    return (
      <div className="rounded border border-dashed border-border bg-surface px-2.5 py-2">
        <div className="text-[10px] font-semibold text-text2">{label}</div>
        <div className="mt-0.5 text-[10px] text-muted">No matching interface discovered yet.</div>
      </div>
    )
  }
  const tput = (iface.in_bps || 0) + (iface.out_bps || 0)
  const speedLabel = iface.if_speed ? formatBps(iface.if_speed) : '—'
  const utilCls =
    (iface.util_pct ?? 0) >= 85 ? 'text-danger' :
    (iface.util_pct ?? 0) >= 60 ? 'text-warning' :
    'text-success'
  const oper = (iface.oper_status || 'unknown').toLowerCase()
  const operCls =
    oper === 'up' ? 'bg-success/15 text-success border-success/30' :
    oper === 'down' ? 'bg-danger/15 text-danger border-danger/30' :
    'bg-surface2 text-muted border-border'
  return (
    <div className="rounded border border-border bg-surface px-2.5 py-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-[10px] font-semibold text-text2">{label}</div>
          <div className="truncate font-mono text-[10px] text-muted">
            {iface.if_name || iface.if_descr || `ifIndex ${iface.if_index}`}
          </div>
        </div>
        <span className={cn('rounded border px-1 py-px text-[9px] font-semibold uppercase tracking-wider', operCls)}>
          {iface.oper_status || 'unknown'}
        </span>
      </div>
      <div className="mt-1.5 grid grid-cols-3 gap-1.5">
        <div>
          <div className="text-[9px] uppercase tracking-wider text-muted">Speed</div>
          <div className="font-mono text-[11px] font-semibold">{speedLabel}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider text-muted">Throughput</div>
          <div className="font-mono text-[11px] font-semibold">{formatBps(tput)}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider text-muted">Util</div>
          <div className={cn('font-mono text-[11px] font-semibold', utilCls)}>
            {iface.util_pct != null ? `${iface.util_pct.toFixed(1)}%` : '—'}
          </div>
        </div>
      </div>
      {(iface.in_bps != null || iface.out_bps != null) && (
        <div className="mt-1.5 flex items-center gap-3 text-[10px] text-muted">
          <span>↓ {formatBps(iface.in_bps)}</span>
          <span>↑ {formatBps(iface.out_bps)}</span>
          {iface.if_alias && <span className="ml-auto truncate font-mono">{iface.if_alias}</span>}
        </div>
      )}
    </div>
  )
}

/* ── Link annotation chip ────────────────────────────────────── */
// Anchored at canvas percent (x,y). Pointer-events-none so it doesn't
// steal clicks from the underlying path.
function LinkChip({
  x, y, variant, children, tone,
}: {
  x: number
  y: number
  variant: 'iface' | 'speed' | 'live'
  children: ReactNode
  tone?: 'success' | 'warning' | 'danger'
}) {
  let cls: string
  if (variant === 'iface') {
    cls = 'bg-surface/95 text-text2 border-border'
  } else if (variant === 'speed') {
    cls = 'bg-primary/15 text-primary border-primary/30'
  } else {
    cls =
      tone === 'danger' ? 'bg-danger/15 text-danger border-danger/40' :
      tone === 'warning' ? 'bg-warning/15 text-warning border-warning/40' :
      'bg-success/15 text-success border-success/40'
  }
  return (
    <div
      className="pointer-events-none absolute z-[15]"
      style={{
        left: `${x}%`,
        top: `${y}%`,
        transform: 'translate(-50%, -50%)',
      }}
    >
      <div className={cn(
        'rounded border px-1 py-px font-mono text-[9px] font-semibold leading-none tracking-tight shadow-sm backdrop-blur',
        cls,
      )}>
        {children}
      </div>
    </div>
  )
}

/* ── Link inspector (replaces MapInspector when a link is selected) ── */

function LinkInspector({
  link,
  source,
  target,
  live,
  onChange,
  onDelete,
  mode,
}: {
  link: ManualMapLink
  source: ManualMapNode | null
  target: ManualMapNode | null
  live: LiveLinkData | null
  onChange: (patch: Partial<Pick<ManualMapLink, 'label' | 'link_type'>> & { metadata?: LinkMetadata }) => void
  onDelete: () => void
  mode: 'design' | 'live'
}) {
  const md: LinkMetadata = link.metadata || {}
  const kind = (md.kind || link.link_type || 'ethernet') as LinkKind
  const shape: LinkShape = linkShape(link)
  const health = source && target ? linkHealth(source.status, target.status) : 'unknown'
  const color = STATUS_COLOR[health]
  const ro = mode === 'live'

  const patchMeta = (next: Partial<LinkMetadata>) => {
    onChange({ metadata: { ...md, ...next } })
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="rounded-lg border border-border bg-surface2/40 p-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">Link</span>
          <Badge variant={color.badge}>{health}</Badge>
        </div>
        <div className="space-y-1.5 text-xs">
          <div className="flex items-center gap-2">
            <span className={cn('h-2 w-2 rounded-full', STATUS_COLOR[statusKey(source?.status)].dot)} />
            <span className="min-w-0 flex-1 truncate font-medium">{source?.hostname || '—'}</span>
            <span className="font-mono text-[10px] text-muted">{md.src_interface || '—'}</span>
          </div>
          <div className="ml-1 h-3 w-px bg-border" />
          <div className="flex items-center gap-2">
            <span className={cn('h-2 w-2 rounded-full', STATUS_COLOR[statusKey(target?.status)].dot)} />
            <span className="min-w-0 flex-1 truncate font-medium">{target?.hostname || '—'}</span>
            <span className="font-mono text-[10px] text-muted">{md.dst_interface || '—'}</span>
          </div>
        </div>
      </div>

      {/* Live interface data — shown only when at least one side resolved */}
      {live && (live.source.matched || live.target.matched) && (
        <LiveLinkPanel live={live} source={source} target={target} />
      )}

      {/* Shape picker — affects only the visual rendering */}
      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">Shape</span>
          {shape === 'orthogonal' && (md.waypoints?.length || 0) > 0 && !ro && (
            <button
              type="button"
              onClick={() => patchMeta({ waypoints: null })}
              className="rounded px-1.5 py-0.5 text-[10px] text-muted hover:bg-surface2 hover:text-text"
              title="Remove all custom bends"
            >
              Reset bends
            </button>
          )}
        </div>
        <div className="grid grid-cols-3 gap-1.5">
          {LINK_SHAPES.map((s) => (
            <button
              key={s.value}
              type="button"
              disabled={ro}
              onClick={() => patchMeta({ shape: s.value })}
              title={s.hint}
              className={cn(
                'flex flex-col items-center gap-1 rounded-md border p-2 transition',
                shape === s.value
                  ? 'border-primary/55 bg-primary/10 text-primary'
                  : 'border-border bg-surface hover:border-border-strong',
                ro && 'cursor-not-allowed opacity-50',
              )}
            >
              <ShapeGlyph shape={s.value} />
              <span className="text-[10px] font-medium">{s.label}</span>
            </button>
          ))}
        </div>
        {shape === 'orthogonal' && (
          <div className="mt-1 text-[10px] leading-tight text-muted">
            Click a hollow dot on the line to add a bend · drag a bend to reshape · right-click to remove.
          </div>
        )}
      </div>

      <FormField label="Source interface" hint={source?.hostname || ''}>
        <Input
          value={md.src_interface || ''}
          placeholder="e.g. Gi0/1, eth0, Te1/0/24"
          disabled={ro}
          onChange={(e) => patchMeta({ src_interface: e.target.value })}
          onBlur={(e) => patchMeta({ src_interface: e.target.value.trim() || null })}
        />
      </FormField>

      <FormField label="Destination interface" hint={target?.hostname || ''}>
        <Input
          value={md.dst_interface || ''}
          placeholder="e.g. eth0, ens33, Gi0/24"
          disabled={ro}
          onChange={(e) => patchMeta({ dst_interface: e.target.value })}
          onBlur={(e) => patchMeta({ dst_interface: e.target.value.trim() || null })}
        />
      </FormField>

      <div className="grid grid-cols-2 gap-2">
        <FormField label="Speed">
          <select
            value={md.speed || ''}
            disabled={ro}
            onChange={(e) => patchMeta({ speed: e.target.value || null })}
            className="h-8 w-full rounded-md border border-border bg-surface px-2 text-xs disabled:opacity-50"
          >
            <option value="">—</option>
            {LINK_SPEEDS.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </FormField>
        <FormField label="Kind">
          <select
            value={kind}
            disabled={ro}
            onChange={(e) => onChange({ link_type: e.target.value, metadata: { ...md, kind: e.target.value as LinkKind } })}
            className="h-8 w-full rounded-md border border-border bg-surface px-2 text-xs disabled:opacity-50"
          >
            {LINK_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </select>
        </FormField>
      </div>

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Color override</div>
        <ColorSwatchRow
          value={md.color}
          disabled={ro}
          onChange={(next) => patchMeta({ color: next })}
        />
      </div>

      <div className="grid grid-cols-2 gap-2">
        <FormField label="Arrow style">
          <select
            value={md.arrow || 'none'}
            disabled={ro}
            onChange={(e) => patchMeta({ arrow: e.target.value as LinkArrow })}
            className="h-8 w-full rounded-md border border-border bg-surface px-2 text-xs disabled:opacity-50"
          >
            <option value="none">None</option>
            <option value="forward">A → B</option>
            <option value="backward">A ← B</option>
            <option value="both">A ↔ B</option>
          </select>
        </FormField>
        <FormField label="Animate flow">
          <select
            value={md.animate == null ? 'auto' : md.animate ? 'on' : 'off'}
            disabled={ro}
            onChange={(e) => patchMeta({ animate: e.target.value === 'auto' ? null : e.target.value === 'on' })}
            className="h-8 w-full rounded-md border border-border bg-surface px-2 text-xs disabled:opacity-50"
          >
            <option value="auto">Auto (live mode)</option>
            <option value="on">Always</option>
            <option value="off">Never</option>
          </select>
        </FormField>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <FormField label="Thickness">
          <input
            type="range" min={0.4} max={3.5} step={0.1}
            value={md.thickness ?? 1}
            disabled={ro}
            onChange={(e) => patchMeta({ thickness: parseFloat(e.target.value) })}
            className="w-full"
          />
          <div className="text-[10px] text-muted">{(md.thickness ?? 1).toFixed(1)}×</div>
        </FormField>
        <FormField label="Opacity">
          <input
            type="range" min={0.1} max={1} step={0.05}
            value={md.opacity ?? 0.7}
            disabled={ro}
            onChange={(e) => patchMeta({ opacity: parseFloat(e.target.value) })}
            className="w-full"
          />
          <div className="text-[10px] text-muted">{Math.round((md.opacity ?? 0.7) * 100)}%</div>
        </FormField>
      </div>

      <FormField label="Notes">
        <Textarea
          rows={2}
          value={md.notes || ''}
          placeholder="Optional — patch panel position, VLANs, etc."
          disabled={ro}
          onChange={(e) => patchMeta({ notes: e.target.value })}
          onBlur={(e) => patchMeta({ notes: e.target.value.trim() || null })}
        />
      </FormField>

      {mode === 'design' && (
        <button
          type="button"
          onClick={onDelete}
          className="flex w-full items-center justify-center gap-1.5 rounded-md border border-danger/30 bg-danger/5 px-2 py-1.5 text-[11px] font-medium text-danger transition hover:bg-danger/10"
        >
          <Trash2 className="h-3.5 w-3.5" /> Delete link
        </button>
      )}
    </div>
  )
}

/* ── Shape (annotation) inspector ─────────────────────────────── */

function ShapeInspector({
  shape,
  onChange,
  onDelete,
  mode,
}: {
  shape: ManualMapShape
  onChange: (patch: Partial<ManualMapShape>) => void
  onDelete: () => void
  mode: 'design' | 'live'
}) {
  const md: ShapeMetadata = (shape.metadata as ShapeMetadata | null) || {}
  const ro = mode === 'live'
  const isTextual = shape.kind === 'text' || shape.kind === 'rectangle' || shape.kind === 'circle' ||
    shape.kind === 'diamond' || shape.kind === 'hexagon' || shape.kind === 'sticky'
  const isLine = shape.kind === 'line' || shape.kind === 'arrow'

  const patchMeta = (next: Partial<ShapeMetadata>) =>
    onChange({ metadata: { ...md, ...next } })

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-surface2/40 p-3">
        <div className="mb-1 flex items-center justify-between gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">Annotation</span>
          <Badge variant="outline">{shape.kind}</Badge>
        </div>
        <div className="text-[10px] text-muted">
          Position {shape.x_pct.toFixed(1)} · {shape.y_pct.toFixed(1)} · Size {shape.w_pct.toFixed(1)} × {shape.h_pct.toFixed(1)}
        </div>
      </div>

      {isTextual && (
        <FormField label="Text">
          <Textarea
            rows={2}
            value={shape.text || ''}
            disabled={ro}
            onChange={(e) => onChange({ text: e.target.value })}
            onBlur={(e) => onChange({ text: e.target.value.trim() || null })}
          />
        </FormField>
      )}

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Fill color</div>
        <ColorSwatchRow
          value={shape.fill}
          disabled={ro || isLine}
          onChange={(next) => onChange({ fill: next })}
        />
      </div>

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Stroke color</div>
        <ColorSwatchRow
          value={shape.stroke}
          disabled={ro}
          onChange={(next) => onChange({ stroke: next })}
        />
      </div>

      <div className="grid grid-cols-2 gap-2">
        <FormField label="Stroke width">
          <input
            type="range" min={0.1} max={2.0} step={0.1}
            value={md.stroke_width ?? (isLine ? 0.5 : 0.35)}
            disabled={ro}
            onChange={(e) => patchMeta({ stroke_width: parseFloat(e.target.value) })}
            className="w-full"
          />
          <div className="text-[10px] text-muted">{(md.stroke_width ?? (isLine ? 0.5 : 0.35)).toFixed(2)}</div>
        </FormField>
        <FormField label="Opacity">
          <input
            type="range" min={0.1} max={1} step={0.05}
            value={md.opacity ?? 1}
            disabled={ro}
            onChange={(e) => patchMeta({ opacity: parseFloat(e.target.value) })}
            className="w-full"
          />
          <div className="text-[10px] text-muted">{Math.round((md.opacity ?? 1) * 100)}%</div>
        </FormField>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <FormField label="Dashed">
          <select
            value={md.stroke_dash || ''}
            disabled={ro}
            onChange={(e) => patchMeta({ stroke_dash: e.target.value || null })}
            className="h-8 w-full rounded-md border border-border bg-surface px-2 text-xs disabled:opacity-50"
          >
            <option value="">Solid</option>
            <option value="2 1">Dashed</option>
            <option value="1 1">Dotted</option>
            <option value="4 2">Long dash</option>
            <option value="6 2 1 2">Dash-dot</option>
          </select>
        </FormField>
        <FormField label="Rotation">
          <input
            type="range" min={-180} max={180} step={5}
            value={md.rotation ?? 0}
            disabled={ro}
            onChange={(e) => patchMeta({ rotation: parseInt(e.target.value, 10) })}
            className="w-full"
          />
          <div className="text-[10px] text-muted">{(md.rotation ?? 0)}°</div>
        </FormField>
      </div>

      {isTextual && (
        <>
          <div className="grid grid-cols-2 gap-2">
            <FormField label="Font size">
              <input
                type="range" min={0.8} max={8} step={0.1}
                value={md.font_size ?? Math.max(1.2, Math.min(shape.h_pct * 0.35, 3.5))}
                disabled={ro}
                onChange={(e) => patchMeta({ font_size: parseFloat(e.target.value) })}
                className="w-full"
              />
              <div className="text-[10px] text-muted">{(md.font_size ?? 2).toFixed(1)}</div>
            </FormField>
            <FormField label="Weight">
              <select
                value={md.font_weight || 'normal'}
                disabled={ro}
                onChange={(e) => patchMeta({ font_weight: e.target.value as ShapeMetadata['font_weight'] })}
                className="h-8 w-full rounded-md border border-border bg-surface px-2 text-xs disabled:opacity-50"
              >
                <option value="normal">Normal</option>
                <option value="semibold">Semibold</option>
                <option value="bold">Bold</option>
              </select>
            </FormField>
          </div>
          <div>
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Text color</div>
            <ColorSwatchRow
              value={md.font_color}
              disabled={ro}
              onChange={(next) => patchMeta({ font_color: next })}
            />
          </div>
        </>
      )}

      <div className="grid grid-cols-2 gap-2">
        <button
          type="button"
          disabled={ro}
          onClick={() => onChange({ z_index: shape.z_index + 1 })}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-[11px] font-medium hover:bg-surface2/50 disabled:opacity-50"
        >Bring forward</button>
        <button
          type="button"
          disabled={ro}
          onClick={() => onChange({ z_index: Math.max(0, shape.z_index - 1) })}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-[11px] font-medium hover:bg-surface2/50 disabled:opacity-50"
        >Send back</button>
      </div>

      {mode === 'design' && (
        <button
          type="button"
          onClick={onDelete}
          className="flex w-full items-center justify-center gap-1.5 rounded-md border border-danger/30 bg-danger/5 px-2 py-1.5 text-[11px] font-medium text-danger transition hover:bg-danger/10"
        >
          <Trash2 className="h-3.5 w-3.5" /> Delete annotation
        </button>
      )}
    </div>
  )
}

/* ── Link wizard popover (after click-to-connect target) ────────── */

function LinkWizard({
  source,
  target,
  defaultShape,
  onCancel,
  onSubmit,
  pending,
}: {
  source: ManualMapNode
  target: ManualMapNode
  defaultShape: LinkShape
  onCancel: () => void
  onSubmit: (kind: LinkKind, metadata: LinkMetadata) => void
  pending: boolean
}) {
  const [kind, setKind] = useState<LinkKind>('ethernet')
  const [shape, setShape] = useState<LinkShape>(defaultShape)
  const [src, setSrc] = useState('')
  const [dst, setDst] = useState('')
  const [speed, setSpeed] = useState<LinkSpeed | ''>('1G')
  return (
    <Dialog open onOpenChange={(o) => !o && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New link</DialogTitle>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            onSubmit(kind, {
              kind,
              shape,
              src_interface: src.trim() || null,
              dst_interface: dst.trim() || null,
              speed: speed || null,
            })
          }}
        >
          <div className="rounded-lg border border-border bg-surface2/40 p-3 text-xs">
            <div className="mb-1 flex items-center justify-between">
              <span className="font-semibold text-text">{source.hostname}</span>
              <span className="text-[10px] text-muted">{source.ip_address}</span>
            </div>
            <div className="flex items-center justify-between border-t border-border/60 pt-1">
              <span className="font-semibold text-text">{target.hostname}</span>
              <span className="text-[10px] text-muted">{target.ip_address}</span>
            </div>
          </div>

          <div>
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Link kind</div>
            <div className="grid grid-cols-3 gap-1.5">
              {LINK_KINDS.map((k) => (
                <button
                  key={k.value}
                  type="button"
                  onClick={() => setKind(k.value)}
                  title={k.hint}
                  className={cn(
                    'rounded-md border px-2 py-1.5 text-left transition',
                    kind === k.value
                      ? 'border-primary/55 bg-primary/10 text-primary'
                      : 'border-border bg-surface hover:border-border-strong',
                  )}
                >
                  <div className="text-xs font-semibold">{k.label}</div>
                  <div className="text-[9px] leading-tight text-muted">{k.hint}</div>
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <FormField label={`${source.hostname} interface`}>
              <Input value={src} onChange={(e) => setSrc(e.target.value)} placeholder="Gi0/1" autoFocus />
            </FormField>
            <FormField label={`${target.hostname} interface`}>
              <Input value={dst} onChange={(e) => setDst(e.target.value)} placeholder="eth0" />
            </FormField>
          </div>

          <FormField label="Speed">
            <div className="flex flex-wrap gap-1">
              <button
                type="button"
                onClick={() => setSpeed('')}
                className={cn(
                  'rounded border px-2 py-0.5 text-[10px] font-medium transition',
                  !speed ? 'border-primary/55 bg-primary/10 text-primary' : 'border-border bg-surface hover:border-border-strong',
                )}
              >—</button>
              {LINK_SPEEDS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setSpeed(s)}
                  className={cn(
                    'rounded border px-2 py-0.5 text-[10px] font-mono font-semibold transition',
                    speed === s
                      ? 'border-primary/55 bg-primary/10 text-primary'
                      : 'border-border bg-surface hover:border-border-strong',
                  )}
                >{s}</button>
              ))}
            </div>
          </FormField>

          <FormField label="Shape">
            <div className="grid grid-cols-3 gap-1.5">
              {LINK_SHAPES.map((s) => (
                <button
                  key={s.value}
                  type="button"
                  onClick={() => setShape(s.value)}
                  title={s.hint}
                  className={cn(
                    'flex flex-col items-center gap-0.5 rounded-md border p-1.5 transition',
                    shape === s.value
                      ? 'border-primary/55 bg-primary/10 text-primary'
                      : 'border-border bg-surface hover:border-border-strong',
                  )}
                >
                  <ShapeGlyph shape={s.value} />
                  <span className="text-[10px] font-medium">{s.label}</span>
                </button>
              ))}
            </div>
          </FormField>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
            <Button type="submit" disabled={pending}>
              {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Cable className="h-4 w-4" />}
              Connect
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
