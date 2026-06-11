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
  Cable,
  ChevronLeft,
  ChevronRight,
  Layers,
  Loader2,
  Maximize2,
  Minimize2,
  Minus,
  Monitor,
  MousePointer2,
  Pencil,
  Plus,
  Radio,
  RefreshCw,
  Save,
  Search,
  Spline,
  Sparkles,
  Trash2,
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

type NodeMetadata = {
  label_offset?: { dx: number; dy: number }
  size_scale?: number
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

type LinkKind = 'ethernet' | 'fiber' | 'wireless' | 'vpn' | 'trunk' | 'serial' | 'manual'
type LinkSpeed = '10M' | '100M' | '1G' | '2.5G' | '10G' | '25G' | '40G' | '100G' | string
type LinkShape = 'curve' | 'straight' | 'orthogonal'

type Waypoint = { x_pct: number; y_pct: number }

/** Per-side port label overrides (stored in link metadata). */
type IfaceLabelStyle = {
  /** Distance from device along the cable, canvas %. */
  dist?: number | null
  /** Nudge along cable direction (+ = toward far end), canvas %. */
  along?: number | null
  /** Nudge perpendicular to cable (+ = right of cable direction), canvas %. */
  perp?: number | null
  /** Rotation in degrees; null = auto-align to cable. */
  angle?: number | null
  /** Label font size in SVG viewBox units (default 0.95). */
  fontSize?: number | null
  textColor?: string | null
  bgColor?: string | null
  borderColor?: string | null
}

type LinkMetadata = {
  src_interface?: string | null
  dst_interface?: string | null
  speed?: LinkSpeed | null
  kind?: LinkKind | null
  shape?: LinkShape | null
  waypoints?: Waypoint[] | null
  notes?: string | null
  src_label?: IfaceLabelStyle | null
  dst_label?: IfaceLabelStyle | null
}

const IFACE_LABEL_FONT_DEFAULT = 0.95

function hasIfaceLabelStyle(style?: IfaceLabelStyle | null): boolean {
  if (!style) return false
  return (
    style.dist != null || style.along != null || style.perp != null || style.angle != null
    || style.fontSize != null || !!style.textColor || !!style.bgColor || !!style.borderColor
  )
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

type DeviceInterface = {
  if_index: number
  if_name: string | null
  if_descr: string | null
  if_alias: string | null
  if_type?: number | null
  if_speed?: number | null
  admin_status?: string | null
  oper_status?: string | null
  monitored?: boolean | null
}

const IFACE_OTHER = '__other__'
const SPEED_OTHER = '__other__'

function ifaceLabel(i: DeviceInterface) {
  return i.if_name || i.if_descr || `ifIndex ${i.if_index}`
}

function findIface(list: DeviceInterface[] | undefined, name: string): DeviceInterface | null {
  if (!list || !name) return null
  return list.find((i) => ifaceLabel(i) === name) || null
}

function detectLinkKindFromIface(iface: DeviceInterface | null, name: string): LinkKind {
  const n = name.toLowerCase()
  const t = iface?.if_type

  if (/wlan|wifi|wireless|802\.11|radio/.test(n)) return 'wireless'
  if (/tunnel|ipsec|gre|wg-|vpn|vti|st0|ipip/.test(n)) return 'vpn'
  if (/port-channel|portchannel|po\d|lag|bond|ae\d|802\.1q|trunk|bundle/.test(n)) return 'trunk'
  if (/serial|console|tty|ppp|async|vty/.test(n)) return 'serial'
  if (/sfp|xfp|qsfp|fiber|fibre|optic|optical|ten[- ]?gig|tengig|xe-|et-\d/.test(n)) return 'fiber'

  if (t === 71) return 'wireless'
  if (t === 131 || t === 169 || t === 142) return 'vpn'
  if (t === 161) return 'trunk'
  if (t === 135 || t === 136) return 'trunk'
  if (t === 23 || t === 32) return 'serial'
  if (t === 250 || t === 219) return 'fiber'

  return 'ethernet'
}

function inferLinkKind(
  srcIf: DeviceInterface | null,
  dstIf: DeviceInterface | null,
  srcName: string,
  dstName: string,
): LinkKind {
  const kinds = [
    detectLinkKindFromIface(srcIf, srcName),
    detectLinkKindFromIface(dstIf, dstName),
  ]
  const priority: LinkKind[] = ['fiber', 'wireless', 'vpn', 'trunk', 'serial', 'ethernet']
  for (const k of priority) {
    if (kinds.includes(k)) return k
  }
  return 'ethernet'
}

function inferSpeedFromBps(bps: number | null | undefined): { preset: LinkSpeed | ''; custom: string } {
  if (!bps || bps <= 0) return { preset: '', custom: '' }

  const tiers: { bps: number; label: LinkSpeed }[] = [
    { bps: 100_000_000_000, label: '100G' },
    { bps: 40_000_000_000, label: '40G' },
    { bps: 25_000_000_000, label: '25G' },
    { bps: 10_000_000_000, label: '10G' },
    { bps: 2_500_000_000, label: '2.5G' },
    { bps: 1_000_000_000, label: '1G' },
    { bps: 100_000_000, label: '100M' },
    { bps: 10_000_000, label: '10M' },
  ]

  let best: { label: LinkSpeed; diff: number } | null = null
  for (const tier of tiers) {
    const diff = Math.abs(bps - tier.bps)
    if (!best || diff < best.diff) best = { label: tier.label, diff }
    if (diff / tier.bps <= 0.12) return { preset: tier.label, custom: '' }
  }
  if (best && best.diff / bps <= 0.12) return { preset: best.label, custom: '' }
  return { preset: SPEED_OTHER, custom: formatBps(bps) }
}

function inferLinkProps(
  srcIf: DeviceInterface | null,
  dstIf: DeviceInterface | null,
  srcName: string,
  dstName: string,
) {
  const kind = inferLinkKind(srcIf, dstIf, srcName, dstName)
  const speeds = [srcIf?.if_speed, dstIf?.if_speed].filter((v): v is number => !!v && v > 0)
  const bps = speeds.length ? Math.min(...speeds) : null
  const { preset, custom } = inferSpeedFromBps(bps)
  return { kind, speedPreset: preset, speedCustom: custom }
}

function resolveSpeedValue(preset: LinkSpeed | '', custom: string): LinkSpeed | '' {
  if (preset === SPEED_OTHER) return custom.trim() || ''
  return preset
}

function parseStoredSpeed(speed: LinkSpeed | null | undefined): { preset: LinkSpeed | ''; custom: string } {
  if (!speed) return { preset: '', custom: '' }
  if (LINK_SPEEDS.includes(speed as LinkSpeed)) return { preset: speed, custom: '' }
  return { preset: SPEED_OTHER, custom: speed }
}

function sortInterfaces(list: DeviceInterface[]) {
  return [...list].sort((a, b) => {
    const aUp = a.oper_status === 'up' ? 0 : 1
    const bUp = b.oper_status === 'up' ? 0 : 1
    if (aUp !== bUp) return aUp - bUp
    return ifaceLabel(a).localeCompare(ifaceLabel(b))
  })
}

function useDeviceInterfaces(deviceId: string | undefined) {
  return useQuery<DeviceInterface[]>({
    queryKey: ['device-interfaces', deviceId],
    enabled: !!deviceId,
    queryFn: async () => sortInterfaces((await api.get(`/devices/${deviceId}/interfaces`)).data),
    staleTime: 60_000,
  })
}

function DeviceInterfaceSelect({
  deviceId,
  hostname,
  value,
  disabled,
  onChange,
  autoFocus,
}: {
  deviceId: string
  hostname: string
  value: string
  disabled?: boolean
  onChange: (value: string) => void
  autoFocus?: boolean
}) {
  const q = useDeviceInterfaces(deviceId)
  const ifaces = q.data || []
  const listed = value ? ifaces.some((i) => ifaceLabel(i) === value) : false
  const [mode, setMode] = useState<'none' | 'snmp' | 'custom'>(() => {
    if (!value) return 'none'
    return listed ? 'snmp' : 'custom'
  })
  const [custom, setCustom] = useState(listed ? '' : value)

  useEffect(() => {
    if (!value) {
      setMode('none')
      setCustom('')
      return
    }
    if (ifaces.some((i) => ifaceLabel(i) === value)) {
      setMode('snmp')
      return
    }
    setMode('custom')
    setCustom(value)
  }, [value, ifaces])

  const selectValue = mode === 'custom' ? IFACE_OTHER : value

  return (
    <FormField label={`${hostname} interface`} hint={q.isLoading ? 'Loading SNMP interfaces…' : `${ifaces.length} interfaces`}>
      <select
        autoFocus={autoFocus && mode !== 'custom'}
        value={selectValue}
        disabled={disabled || q.isLoading}
        onChange={(e) => {
          const v = e.target.value
          if (v === IFACE_OTHER) {
            setMode('custom')
            onChange(custom)
            return
          }
          if (!v) {
            setMode('none')
            setCustom('')
            onChange('')
            return
          }
          setMode('snmp')
          onChange(v)
        }}
        className="h-9 w-full rounded-md border border-border bg-surface px-2 text-xs text-text outline-none focus:border-primary/60 disabled:opacity-50"
      >
        <option value="">{q.isLoading ? 'Loading interfaces…' : '— select interface —'}</option>
        {ifaces.map((i) => {
          const name = ifaceLabel(i)
          const alias = i.if_alias ? ` · ${i.if_alias}` : ''
          const status = i.oper_status && i.oper_status !== 'up' ? ` · ${i.oper_status}` : ''
          return (
            <option key={i.if_index} value={name}>
              {name}{alias}{status}
            </option>
          )
        })}
        <option value={IFACE_OTHER}>Other (custom…)</option>
      </select>
      {mode === 'custom' && (
        <Input
          autoFocus={autoFocus}
          value={custom}
          disabled={disabled}
          placeholder="Custom label — map-only, no SNMP match"
          className="mt-1.5 h-8 text-xs"
          onChange={(e) => {
            setCustom(e.target.value)
            onChange(e.target.value)
          }}
        />
      )}
    </FormField>
  )
}

function LinkSpeedSelect({
  preset,
  custom,
  autoDetected,
  disabled,
  onChange,
}: {
  preset: LinkSpeed | ''
  custom: string
  autoDetected?: boolean
  disabled?: boolean
  onChange: (preset: LinkSpeed | '', custom: string) => void
}) {
  const selectVal = preset === SPEED_OTHER || (preset && !LINK_SPEEDS.includes(preset as LinkSpeed))
    ? SPEED_OTHER
    : preset

  return (
    <FormField
      label="Speed"
      hint={autoDetected ? 'Auto-detected from interface speed' : undefined}
    >
      <select
        value={selectVal}
        disabled={disabled}
        onChange={(e) => {
          const v = e.target.value as LinkSpeed | ''
          if (v === SPEED_OTHER) onChange(SPEED_OTHER, custom)
          else onChange(v, '')
        }}
        className="h-9 w-full rounded-md border border-border bg-surface px-2 text-xs text-text outline-none focus:border-primary/60 disabled:opacity-50"
      >
        <option value="">— none —</option>
        {LINK_SPEEDS.map((s) => (
          <option key={s} value={s}>{s}</option>
        ))}
        <option value={SPEED_OTHER}>Other…</option>
      </select>
      {selectVal === SPEED_OTHER && (
        <Input
          value={custom}
          disabled={disabled}
          placeholder="e.g. 400G, 1 Tbps, unknown"
          className="mt-1.5 h-8 text-xs font-mono"
          onChange={(e) => onChange(SPEED_OTHER, e.target.value)}
        />
      )}
    </FormField>
  )
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

type SuggestedLink = {
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

type ManualMapDetail = ManualMapListItem & {
  summary: {
    nodes: number
    links: number
    status_counts: Record<string, number>
    generated_at: string
  }
  nodes: ManualMapNode[]
  links: ManualMapLink[]
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

// Default label anchor: centred just below the device icon box.
const NODE_LABEL_TOP = 38
const NODE_ICON_H = 56 // px — keep leader-line math in sync with h-14

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

function linkPerpUnit(ax: number, ay: number, bx: number, by: number) {
  const dx = bx - ax
  const dy = by - ay
  const len = Math.hypot(dx, dy) || 1
  return { nx: -dy / len, ny: dx / len }
}

function applyParallelOffset(
  ax: number, ay: number, bx: number, by: number,
  waypoints: Waypoint[],
  parallelOffset: number,
  perpRef?: { ax: number; ay: number; bx: number; by: number },
) {
  if (!parallelOffset) return { ax, ay, bx, by, waypoints }
  const ref = perpRef || { ax, ay, bx, by }
  const { nx, ny } = linkPerpUnit(ref.ax, ref.ay, ref.bx, ref.by)
  return {
    ax: ax + nx * parallelOffset,
    ay: ay + ny * parallelOffset,
    bx: bx + nx * parallelOffset,
    by: by + ny * parallelOffset,
    waypoints: waypoints.map((w) => ({
      x_pct: w.x_pct + nx * parallelOffset,
      y_pct: w.y_pct + ny * parallelOffset,
    })),
  }
}

function canonicalPerpRef(
  sourceId: string,
  targetId: string,
  ax: number, ay: number, bx: number, by: number,
) {
  if (sourceId < targetId) return { ax, ay, bx, by }
  return { ax: bx, ay: by, bx: ax, by: ay }
}

// Build an SVG path + annotation anchors (near-source, mid, near-target)
// for any of the supported link shapes. Output is shape-agnostic so the
// rendering pipeline can swap shape without other code changes.
function edgePath(
  shape: LinkShape,
  ax: number, ay: number, bx: number, by: number,
  waypoints: Waypoint[] = [],
  parallelOffset = 0,
  perpRef?: { ax: number; ay: number; bx: number; by: number },
): EdgePathResult {
  const shifted = applyParallelOffset(ax, ay, bx, by, waypoints, parallelOffset, perpRef)
  ax = shifted.ax
  ay = shifted.ay
  bx = shifted.bx
  by = shifted.by
  waypoints = shifted.waypoints

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

type PathPoint = { x: number; y: number }
type IfaceAnchor = { x: number; y: number; angle: number; textAnchor: 'start' | 'end' }

const IFACE_LABEL_BASE_OFFSET = 4.2   // canvas % along cable — small gap past device edge
const IFACE_LABEL_STAGGER = 1.6       // extra distance per link sharing a node

function samplePolyline(vertices: PathPoint[], steps: number): PathPoint[] {
  if (vertices.length < 2) return vertices.slice()
  const edgeLens = vertices.slice(1).map((v, i) => Math.hypot(v.x - vertices[i].x, v.y - vertices[i].y))
  const total = edgeLens.reduce((s, l) => s + l, 0) || 1
  const pts: PathPoint[] = []
  for (let i = 0; i <= steps; i++) {
    const target = (i / steps) * total
    let acc = 0
    for (let j = 0; j < edgeLens.length; j++) {
      if (acc + edgeLens[j] >= target || j === edgeLens.length - 1) {
        const local = edgeLens[j] > 0 ? (target - acc) / edgeLens[j] : 0
        const a = vertices[j]
        const b = vertices[j + 1]
        pts.push({ x: a.x + (b.x - a.x) * local, y: a.y + (b.y - a.y) * local })
        break
      }
      acc += edgeLens[j]
    }
  }
  return pts
}

function buildPathSamples(
  shape: LinkShape,
  ax: number, ay: number, bx: number, by: number,
  waypoints: Waypoint[],
  steps = 48,
  parallelOffset = 0,
  perpRef?: { ax: number; ay: number; bx: number; by: number },
): { points: PathPoint[]; total: number } {
  const shifted = applyParallelOffset(ax, ay, bx, by, waypoints, parallelOffset, perpRef)
  ax = shifted.ax
  ay = shifted.ay
  bx = shifted.bx
  by = shifted.by
  waypoints = shifted.waypoints

  let points: PathPoint[]
  if (shape === 'curve') {
    const c = linkPath(ax, ay, bx, by)
    points = []
    for (let i = 0; i <= steps; i++) {
      const t = i / steps
      points.push(pointOnQuadratic(ax, ay, c.cx, c.cy, bx, by, t))
    }
  } else if (shape === 'straight') {
    points = []
    for (let i = 0; i <= steps; i++) {
      const t = i / steps
      points.push({ x: ax + (bx - ax) * t, y: ay + (by - ay) * t })
    }
  } else {
    points = samplePolyline(_buildOrthogonal(ax, ay, bx, by, waypoints).vertices, steps)
  }
  let total = 0
  for (let i = 1; i < points.length; i++) {
    total += Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y)
  }
  return { points, total: total || 1 }
}

function cableLabelAngle(dx: number, dy: number, fromEnd: boolean): { angle: number; textAnchor: 'start' | 'end' } {
  let angle = Math.atan2(dy, dx) * (180 / Math.PI)
  if (fromEnd) angle += 180
  let textAnchor: 'start' | 'end' = 'start'
  while (angle > 180) angle -= 360
  while (angle <= -180) angle += 360
  if (angle > 90 || angle < -90) {
    angle += 180
    textAnchor = 'end'
  }
  return { angle, textAnchor }
}

function anchorAtPathDistance(
  points: PathPoint[],
  dist: number,
  fromEnd: boolean,
): IfaceAnchor {
  const cum: number[] = [0]
  for (let i = 1; i < points.length; i++) {
    cum.push(cum[i - 1] + Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y))
  }
  const total = cum[cum.length - 1] || 1
  const target = fromEnd ? Math.max(0, total - dist) : Math.min(total, dist)

  let seg = 1
  while (seg < cum.length && cum[seg] < target) seg++
  const i0 = seg - 1
  const segLen = cum[seg] - cum[i0] || 1
  const local = (target - cum[i0]) / segLen
  const p0 = points[i0]
  const p1 = points[Math.min(seg, points.length - 1)]
  const x = p0.x + (p1.x - p0.x) * local
  const y = p0.y + (p1.y - p0.y) * local

  let bestIdx = 0
  let bestD = Infinity
  for (let i = 0; i < points.length; i++) {
    const d = Math.hypot(points[i].x - x, points[i].y - y)
    if (d < bestD) { bestD = d; bestIdx = i }
  }
  const prev = Math.max(0, bestIdx - 1)
  const next = Math.min(points.length - 1, bestIdx + 1)
  const { angle, textAnchor } = cableLabelAngle(
    points[next].x - points[prev].x,
    points[next].y - points[prev].y,
    fromEnd,
  )
  return { x, y, angle, textAnchor }
}

function linkPairKey(a: string, b: string) {
  return a < b ? `${a}|${b}` : `${b}|${a}`
}

const PARALLEL_LINK_GAP = 2.2   // canvas % between parallel cables on the same node pair

function computeLinkParallelOffsets(links: ManualMapLink[]): Map<string, number> {
  const groups = new Map<string, ManualMapLink[]>()
  for (const link of links) {
    const key = linkPairKey(link.source_node_id, link.target_node_id)
    const g = groups.get(key) || []
    g.push(link)
    groups.set(key, g)
  }
  const out = new Map<string, number>()
  for (const g of groups.values()) {
    if (g.length < 2) continue
    g.sort((a, b) => a.id.localeCompare(b.id))
    g.forEach((link, i) => {
      out.set(link.id, (i - (g.length - 1) / 2) * PARALLEL_LINK_GAP)
    })
  }
  return out
}

function computeLinkIfaceLabelPositions(
  links: ManualMapLink[],
  positionOf: (nodeId: string) => { x_pct: number; y_pct: number } | null,
  waypointsOf: (link: ManualMapLink) => Waypoint[],
  parallelOffsets: Map<string, number>,
): Map<string, { src?: IfaceAnchor; dst?: IfaceAnchor }> {
  type EndRef = { linkId: string; side: 'src' | 'dst'; link: ManualMapLink }
  const pathByLink = new Map<string, { points: PathPoint[]; total: number }>()
  const endsByNode = new Map<string, EndRef[]>()
  const linkById = new Map(links.map((l) => [l.id, l]))

  for (const link of links) {
    const source = positionOf(link.source_node_id)
    const target = positionOf(link.target_node_id)
    if (!source || !target) continue

    const shape = linkShape(link)
    const perpRef = canonicalPerpRef(
      link.source_node_id, link.target_node_id,
      source.x_pct, source.y_pct, target.x_pct, target.y_pct,
    )
    const samples = buildPathSamples(
      shape, source.x_pct, source.y_pct, target.x_pct, target.y_pct,
      waypointsOf(link), 48, parallelOffsets.get(link.id) || 0, perpRef,
    )
    pathByLink.set(link.id, samples)

    const srcEnds = endsByNode.get(link.source_node_id) || []
    srcEnds.push({ linkId: link.id, side: 'src', link })
    endsByNode.set(link.source_node_id, srcEnds)

    const dstEnds = endsByNode.get(link.target_node_id) || []
    dstEnds.push({ linkId: link.id, side: 'dst', link })
    endsByNode.set(link.target_node_id, dstEnds)
  }

  const out = new Map<string, { src?: IfaceAnchor; dst?: IfaceAnchor }>()

  for (const ends of endsByNode.values()) {
    ends.sort((a, b) => {
      const la = linkById.get(a.linkId)
      const lb = linkById.get(b.linkId)
      if (!la || !lb) return 0
      const sa = positionOf(la.source_node_id)
      const ta = positionOf(la.target_node_id)
      const sb = positionOf(lb.source_node_id)
      const tb = positionOf(lb.target_node_id)
      if (!sa || !ta || !sb || !tb) return 0
      const angA = Math.atan2(ta.y_pct - sa.y_pct, ta.x_pct - sa.x_pct)
      const angB = Math.atan2(tb.y_pct - sb.y_pct, tb.x_pct - sb.x_pct)
      return angA - angB
    })
    ends.forEach((end, idx) => {
      const path = pathByLink.get(end.linkId)
      if (!path) return

      const labelStyle = end.side === 'src'
        ? end.link.metadata?.src_label
        : end.link.metadata?.dst_label

      const rawDist = IFACE_LABEL_BASE_OFFSET + idx * IFACE_LABEL_STAGGER
      const dist = labelStyle?.dist != null
        ? Math.max(1, Math.min(labelStyle.dist, path.total * 0.45))
        : Math.max(3, Math.min(rawDist, path.total * 0.32))
      const anchor = anchorAtPathDistance(path.points, dist, end.side === 'dst')

      const entry = out.get(end.linkId) || {}
      if (end.side === 'src') entry.src = anchor
      else entry.dst = anchor
      out.set(end.linkId, entry)
    })
  }

  return out
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

/* ── Page ─────────────────────────────────────────────────────── */

export function ManualMapsPage() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const selectedMapId = params.get('map')

  // UI state
  const [mode, setMode] = useState<'design' | 'live'>('design')
  const [tool, setTool] = useState<'select' | 'connect'>('select')
  const [paletteOpen, setPaletteOpen] = useState(true)
  const [inspectorOpen, setInspectorOpen] = useState(true)
  const [createOpen, setCreateOpen] = useState(false)
  const [deleteMapOpen, setDeleteMapOpen] = useState(false)
  const [newMap, setNewMap] = useState({ name: '', description: '' })
  const [paletteSearch, setPaletteSearch] = useState('')
  const [paletteStatus, setPaletteStatus] = useState<'all' | NodeStatus>('all')
  const [noc, setNoc] = useState(false)          // NOC fullscreen / video-wall mode
  const [rotate, setRotate] = useState(false)    // auto-rotate through maps in NOC mode

  // Canvas state
  const [view, setView] = useState({ x: 0, y: 0, zoom: 1 })
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [selectedLinkId, setSelectedLinkId] = useState<string | null>(null)
  const [labelDraft, setLabelDraft] = useState('')
  const [draggingNodeId, setDraggingNodeId] = useState<string | null>(null)
  const [draftPositions, setDraftPositions] = useState<Record<string, { x_pct: number; y_pct: number }>>({})
  const [connectFrom, setConnectFrom] = useState<string | null>(null)
  const [connectCursor, setConnectCursor] = useState<{ x_pct: number; y_pct: number } | null>(null)
  const [linkWizard, setLinkWizard] = useState<{ source: string; target: string } | null>(null)
  const [panFrom, setPanFrom] = useState<{ x: number; y: number; vx: number; vy: number } | null>(null)
  const [dragOverCanvas, setDragOverCanvas] = useState(false)
  const [defaultShape, setDefaultShape] = useState<LinkShape>('curve')
  // Live, in-flight waypoint edits per link. Mirrors `draftPositions` for nodes:
  // we don't hit the network on every pointer-move; we commit on pointer-up.
  const [draftWaypoints, setDraftWaypoints] = useState<Record<string, Waypoint[]>>({})
  const [draftLabelOffsets, setDraftLabelOffsets] = useState<Record<string, { dx: number; dy: number }>>({})
  const [draggingWaypoint, setDraggingWaypoint] = useState<{ linkId: string; index: number } | null>(null)
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

  // LLDP/CDP link assistance — discovered adjacencies among placed devices
  // that aren't manually linked yet. Surfaced as "ghost" links + one-click add.
  const suggestedLinksQuery = useQuery<{ data: SuggestedLink[]; count: number }>({
    queryKey: ['manual-map-suggested', selectedMapId],
    enabled: !!selectedMapId && mode === 'design',
    queryFn: async () => (await api.get(`/maps/${selectedMapId}/suggested-links`)).data,
    refetchInterval: 20_000,
  })
  const suggestedLinks = mode === 'design' ? (suggestedLinksQuery.data?.data || []) : []

  const detail = mapQuery.data || null
  const nodes = detail?.nodes || []
  const links = detail?.links || []
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

  // NOC mode forces live (weathermap), hides the app sidebar for a clean wall
  // display, and is exited with Esc.
  useEffect(() => {
    if (!noc) { setRotate(false); return }
    setMode('live')
    const aside = document.querySelector('aside') as HTMLElement | null
    const prev = aside?.style.display ?? ''
    if (aside) aside.style.display = 'none'
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') setNoc(false) }
    window.addEventListener('keydown', h)
    return () => { window.removeEventListener('keydown', h); if (aside) aside.style.display = prev }
  }, [noc])

  // Multi-map rotation/carousel: cycle through maps while NOC + rotate are on.
  useEffect(() => {
    if (!noc || !rotate || maps.length < 2) return
    const id = setInterval(() => {
      const idx = maps.findIndex((m) => m.id === selectedMapId)
      const next = maps[(idx + 1) % maps.length]
      if (next) selectMap(next.id)
    }, 15_000)
    return () => clearInterval(id)
  }, [noc, rotate, maps, selectedMapId])

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
          if (selectedLinkId) deleteLink.mutate(selectedLinkId)
          else if (selectedNode) deleteNode.mutate(selectedNode.id)
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
  }, [selectedNode, selectedLinkId, mode])

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
    mutationFn: async ({ id, patch }: { id: string; patch: Partial<Pick<ManualMapNode, 'label' | 'icon' | 'x_pct' | 'y_pct'>> & { metadata?: NodeMetadata } }) => {
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

  // LLDP/CDP link assistance — add a single discovered link, or all at once.
  const invalidateSuggested = () =>
    qc.invalidateQueries({ queryKey: ['manual-map-suggested', selectedMapId] })

  const addDiscoveredLink = useMutation({
    mutationFn: async (s: SuggestedLink) => {
      if (!selectedMapId) throw new Error('No map selected')
      return (await api.post(`/maps/${selectedMapId}/links`, {
        source_node_id: s.source_node_id,
        target_node_id: s.target_node_id,
        link_type: 'ethernet',
        label: (s.protocol || 'lldp').toUpperCase(),
        metadata: {
          kind: 'ethernet', discovered: true, protocol: s.protocol,
          src_interface: s.src_interface, dst_interface: s.dst_interface,
        },
      })).data
    },
    onSuccess: () => { toast.success('Discovered link added'); invalidateMap(selectedMapId); invalidateSuggested() },
    onError: (e: any) => toast.error('Add failed', apiErrorMessage(e)),
  })

  const autoConnect = useMutation({
    mutationFn: async () => {
      if (!selectedMapId) throw new Error('No map selected')
      return (await api.post(`/maps/${selectedMapId}/auto-connect`)).data
    },
    onSuccess: (r: any) => {
      toast.success(`Connected ${r.created} discovered link${r.created === 1 ? '' : 's'}`)
      invalidateMap(selectedMapId); invalidateSuggested()
    },
    onError: (e: any) => toast.error('Auto-connect failed', apiErrorMessage(e)),
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

  function labelOffsetFor(node: ManualMapNode) {
    return draftLabelOffsets[node.id] || node.metadata?.label_offset || { dx: 0, dy: 0 }
  }

  function setNodeLabelOffset(nodeId: string, dx: number, dy: number, commit: boolean) {
    setDraftLabelOffsets((prev) => ({ ...prev, [nodeId]: { dx, dy } }))
    if (!commit) return
    const node = nodeMap.get(nodeId)
    if (!node) return
    updateNode.mutate({
      id: nodeId,
      patch: { metadata: { ...(node.metadata || {}), label_offset: { dx, dy } } },
    })
    setDraftLabelOffsets((prev) => {
      const next = { ...prev }
      delete next[nodeId]
      return next
    })
  }

  // Get the effective waypoints for a link — in-flight drag state takes
  // precedence over the persisted metadata.
  function waypointsFor(link: ManualMapLink): Waypoint[] {
    return draftWaypoints[link.id] || linkWaypoints(link)
  }

  const linkParallelOffsets = useMemo(() => computeLinkParallelOffsets(links), [links])

  const linkIfaceAnchors = useMemo(
    () => computeLinkIfaceLabelPositions(
      links,
      (nodeId) => {
        const node = nodeMap.get(nodeId)
        if (!node) return null
        return draftPositions[nodeId] || { x_pct: node.x_pct, y_pct: node.y_pct }
      },
      (link) => draftWaypoints[link.id] || linkWaypoints(link),
      linkParallelOffsets,
    ),
    [links, nodeMap, draftPositions, draftWaypoints, linkParallelOffsets],
  )

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
    e.stopPropagation()
    setSelectedNodeId(node.id)
    setSelectedLinkId(null)
    setDraggingNodeId(node.id)
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  function moveCanvas(e: ReactPointerEvent) {
    if (draggingNodeId) {
      const p = clientToCanvasPct(e.clientX, e.clientY)
      if (p) setDraftPositions((prev) => ({ ...prev, [draggingNodeId]: p }))
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
    if (draggingNodeId) {
      const next = draftPositions[draggingNodeId]
      const original = nodeMap.get(draggingNodeId)
      setDraggingNodeId(null)
      if (next && original && (Math.abs(next.x_pct - original.x_pct) > 0.2 || Math.abs(next.y_pct - original.y_pct) > 0.2)) {
        updateNode.mutate({ id: draggingNodeId, patch: next })
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
    setSelectedNodeId(null)
    setSelectedLinkId(null)
    if (e.button === 1 || e.shiftKey || e.altKey) {
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
    setLinkWizard({ source: connectFrom, target: target.id })
    setConnectFrom(null)
    setConnectCursor(null)
  }

  function cancelConnect() {
    setConnectFrom(null)
    setConnectCursor(null)
  }

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
    <div className={cn(
      'flex flex-col overflow-hidden bg-surface2/30',
      noc ? 'fixed inset-0 z-[60] h-screen' : '-m-5 h-[calc(100vh-2.75rem)]',
    )}>
      {/* ── Top bar ───────────────────────────────────────── */}
      {!noc && (
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
        <Button variant="ghost" size="icon" onClick={() => setNoc(true)} disabled={!selectedMapId} title="NOC fullscreen (video wall)">
          <Monitor className="h-4 w-4" />
        </Button>
      </div>
      )}

      {/* ── Slim NOC bar (video-wall mode) ───────────────── */}
      {noc && (
        <div className="flex h-11 shrink-0 items-center gap-3 border-b border-border bg-surface px-4">
          <span className="text-sm font-semibold text-text">{currentMap?.name || 'Map'}</span>
          <span className="text-xs text-muted">{nodes.length} devices · {links.length} links</span>
          <div className="flex items-center gap-1.5">
            <StatusChip label="Up" count={statusTotals.up || 0} tone="success" />
            <StatusChip label="Degraded" count={degradedCount} tone="warning" />
            <StatusChip label="Down" count={downCount} tone="danger" />
          </div>
          <div className="flex-1" />
          {maps.length > 1 && (
            <>
              <button
                type="button"
                onClick={() => setRotate((r) => !r)}
                className={cn('flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors',
                  rotate ? 'border-success/40 bg-success/15 text-success' : 'border-border text-muted hover:text-text')}
                title="Auto-rotate through maps every 15s"
              >
                <Radio className={cn('h-3.5 w-3.5', rotate && 'animate-pulse-soft')} /> {rotate ? 'Rotating' : 'Rotate maps'}
              </button>
              <Button variant="ghost" size="icon" title="Previous map" onClick={() => {
                const i = maps.findIndex((m) => m.id === selectedMapId); const p = maps[(i - 1 + maps.length) % maps.length]; if (p) selectMap(p.id)
              }}><ChevronLeft className="h-4 w-4" /></Button>
              <Button variant="ghost" size="icon" title="Next map" onClick={() => {
                const i = maps.findIndex((m) => m.id === selectedMapId); const n = maps[(i + 1) % maps.length]; if (n) selectMap(n.id)
              }}><ChevronRight className="h-4 w-4" /></Button>
            </>
          )}
          <div className="mx-1 h-6 w-px bg-border/60" />
          <Button variant="ghost" size="icon" onClick={() => setNoc(false)} title="Exit NOC mode (Esc)"><Minimize2 className="h-4 w-4" /></Button>
        </div>
      )}

      {/* ── Main: 3-col grid ─────────────────────────────── */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left palette (hidden in NOC mode) */}
        {!noc && (
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
        )}

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

            {/* LLDP/CDP link assistance — auto-connect all discovered links */}
            {mode === 'design' && suggestedLinks.length > 0 && (
              <button
                type="button"
                onClick={() => autoConnect.mutate()}
                disabled={autoConnect.isPending}
                className="pointer-events-auto absolute left-3 top-16 flex items-center gap-1.5 rounded-lg border border-teal-400/40 bg-teal-500/15 px-2.5 py-1.5 text-[11px] font-medium text-teal-200 shadow-md backdrop-blur transition-colors hover:bg-teal-500/25 disabled:opacity-60"
                title="Draw all real LLDP/CDP links between devices placed on this map"
              >
                {autoConnect.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                Auto-connect {suggestedLinks.length} discovered link{suggestedLinks.length === 1 ? '' : 's'}
              </button>
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
            {/* Grid background — dual-layer (fine + major) */}
            <div className="absolute inset-0 bg-[radial-gradient(circle_at_20%_30%,rgba(59,130,246,0.08),transparent_40%),radial-gradient(circle_at_80%_70%,rgba(168,85,247,0.05),transparent_45%)]" />
            <GridBackground view={view} />

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
                </defs>

                {/* Existing links — rendered with the per-link shape (curve/straight/orthogonal). */}
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
                  const perpRef = canonicalPerpRef(
                    link.source_node_id, link.target_node_id,
                    a.x_pct, a.y_pct, b.x_pct, b.y_pct,
                  )
                  const path = edgePath(shape, a.x_pct, a.y_pct, b.x_pct, b.y_pct, wps, linkParallelOffsets.get(link.id) || 0, perpRef)
                  const animate = mode === 'live' && (health === 'up' || health === 'degraded')
                  const isSelected = selectedLinkId === link.id
                  const baseWidth = (kindStyle.widthMul || 1) * 3
                  const flowWidth = (kindStyle.widthMul || 1) * 1.5

                  // If live data is loaded for this link and util_pct is known,
                  // override the base color with a utilization color and speed
                  // up the flow animation proportionally.
                  const live = liveById[link.id]
                  const utilPct = live ? Math.max(live.source.util_pct || 0, live.target.util_pct || 0) : null
                  const utilStroke = live && utilPct != null ? utilizationColor(utilPct) : null

                  return (
                    <g
                      key={link.id}
                      className="cursor-pointer"
                      onClick={(e) => {
                        e.stopPropagation()
                        setSelectedLinkId(link.id)
                        setSelectedNodeId(null)
                      }}
                    >
                      {/* Wide invisible hit area for clicking */}
                      <path d={path.d} fill="none" stroke="transparent" strokeWidth={8} vectorEffect="non-scaling-stroke" />
                      {/* Accent halo (fiber/vpn coloured glow + selection highlight) */}
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
                      {/* Base stroke (utilization color overrides status color when live data exists) */}
                      <path
                        d={path.d}
                        fill="none"
                        vectorEffect="non-scaling-stroke"
                        strokeWidth={baseWidth}
                        strokeDasharray={kindStyle.dash}
                        className={cn(utilStroke || color, 'opacity-70')}
                      />
                      {/* Animated flow inner stroke for live + healthy/degraded links */}
                      {animate && (
                        <path
                          d={path.d}
                          fill="none"
                          vectorEffect="non-scaling-stroke"
                          strokeWidth={flowWidth}
                          className={cn(
                            utilStroke || color,
                            // Faster flow when more loaded
                            utilPct != null && utilPct >= 60 ? 'nm-flow' : 'nm-flow-slow',
                          )}
                        />
                      )}
                    </g>
                  )
                })}

                {/* Discovered (LLDP/CDP) link suggestions — faint dashed "ghost"
                    links between placed devices that have a real adjacency but
                    no manual link yet. Click the chip (HTML layer below) to add. */}
                {suggestedLinks.map((s) => {
                  const source = nodeMap.get(s.source_node_id)
                  const target = nodeMap.get(s.target_node_id)
                  if (!source || !target) return null
                  const a = positionFor(source)
                  const b = positionFor(target)
                  const p = edgePath(defaultShape, a.x_pct, a.y_pct, b.x_pct, b.y_pct)
                  return (
                    <path
                      key={`ghost-${s.source_node_id}-${s.target_node_id}`}
                      d={p.d}
                      fill="none"
                      vectorEffect="non-scaling-stroke"
                      strokeWidth={2}
                      strokeDasharray="1 2.5"
                      strokeLinecap="round"
                      className="stroke-teal-400 opacity-60 nm-flow-slow"
                    />
                  )
                })}

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
                            r={0.7}
                            className="cursor-pointer fill-surface stroke-primary"
                            strokeWidth={0.25}
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
                          r={1.1}
                          className="cursor-move fill-primary stroke-surface"
                          strokeWidth={0.4}
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

                {/* Interface port labels — SVG-native, rotated on the cable */}
                {view.zoom >= 0.7 && links.map((link) => {
                  const md = link.metadata || {}
                  const anchors = linkIfaceAnchors.get(link.id)
                  if (!anchors) return null
                  return (
                    <g key={`iface-${link.id}`} pointerEvents="none">
                      {md.src_interface && anchors.src && (
                        <IfaceCableLabel label={md.src_interface} anchor={anchors.src} style={md.src_label} />
                      )}
                      {md.dst_interface && anchors.dst && (
                        <IfaceCableLabel label={md.dst_interface} anchor={anchors.dst} style={md.dst_label} />
                      )}
                    </g>
                  )
                })}
              </svg>

              {/* Link annotations — speed/throughput badge mid-link (HTML) */}
              {links.map((link) => {
                const source = nodeMap.get(link.source_node_id)
                const target = nodeMap.get(link.target_node_id)
                if (!source || !target) return null
                const a = positionFor(source)
                const b = positionFor(target)
                const shape = linkShape(link)
                const wps = waypointsFor(link)
                const perpRef = canonicalPerpRef(
                  link.source_node_id, link.target_node_id,
                  a.x_pct, a.y_pct, b.x_pct, b.y_pct,
                )
                const path = edgePath(shape, a.x_pct, a.y_pct, b.x_pct, b.y_pct, wps, linkParallelOffsets.get(link.id) || 0, perpRef)
                const md = link.metadata || {}
                const live = liveById[link.id]
                const bps = live ? Math.max(
                  (live.source.in_bps || 0) + (live.source.out_bps || 0),
                  (live.target.in_bps || 0) + (live.target.out_bps || 0),
                ) : 0
                const utilPct = live ? Math.max(live.source.util_pct || 0, live.target.util_pct || 0) : null
                const showThroughput = live && bps > 0
                const midText = showThroughput
                  ? `${formatBps(bps)}${utilPct != null && utilPct > 0 ? ` · ${utilPct.toFixed(0)}%` : ''}`
                  : (md.speed || link.label || '')
                if (view.zoom < 0.7 || !midText) return null
                return (
                  <div key={`anno-${link.id}`} className="pointer-events-none">
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
                  </div>
                )
              })}

              {/* Discovered-link "+ Add" chips — one per suggestion, at mid-link.
                  Click to instantiate the real LLDP/CDP link (interface-bound). */}
              {suggestedLinks.map((s) => {
                const source = nodeMap.get(s.source_node_id)
                const target = nodeMap.get(s.target_node_id)
                if (!source || !target) return null
                const a = positionFor(source)
                const b = positionFor(target)
                const p = edgePath(defaultShape, a.x_pct, a.y_pct, b.x_pct, b.y_pct)
                if (view.zoom < 0.55) return null
                return (
                  <button
                    key={`sugg-${s.source_node_id}-${s.target_node_id}`}
                    type="button"
                    className="absolute z-20 flex -translate-x-1/2 -translate-y-1/2 items-center gap-1 rounded-full border border-teal-400/50 bg-teal-500/20 px-1.5 py-0.5 text-[9px] font-semibold text-teal-200 shadow-sm backdrop-blur transition-colors hover:border-teal-300 hover:bg-teal-500/40"
                    style={{ left: `${p.mid.x}%`, top: `${p.mid.y}%` }}
                    title={`Discovered ${(s.protocol || '').toUpperCase()} link · ${s.src_interface || '?'} ⇄ ${s.dst_interface || '?'}${s.physical_links > 1 ? ` · ${s.physical_links} physical` : ''}\nClick to add`}
                    onPointerDown={(e) => e.stopPropagation()}
                    onClick={(e) => { e.stopPropagation(); addDiscoveredLink.mutate(s) }}
                  >
                    <Plus className="h-2.5 w-2.5" /> {(s.protocol || 'link').toUpperCase()}
                  </button>
                )
              })}

              {/* Nodes */}
              {nodes.map((node) => (
                <NodeCard
                  key={node.id}
                  node={node}
                  position={positionFor(node)}
                  labelOffset={labelOffsetFor(node)}
                  zoom={view.zoom}
                  selected={selectedNodeId === node.id}
                  live={mode === 'live'}
                  connectMode={inConnectFlow}
                  isConnectSource={connectFrom === node.id}
                  onPointerDown={(e) => beginNodeDrag(e, node)}
                  onClick={() => {
                    if (connectFrom) {
                      finishConnect(node)
                    } else if (tool === 'connect') {
                      // In connect mode, clicking any node starts the connect.
                      setSelectedNodeId(node.id)
                      setSelectedLinkId(null)
                      setConnectFrom(node.id)
                    } else {
                      setSelectedNodeId(node.id)
                      setSelectedLinkId(null)
                    }
                  }}
                  onLabelOffsetChange={(dx, dy, commit) => setNodeLabelOffset(node.id, dx, dy, commit)}
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
        {!noc && (
        <InspectorRail
          open={inspectorOpen}
          toggle={() => setInspectorOpen((v) => !v)}
          selectedNode={selectedNode}
          selectedLink={selectedLinkId ? links.find((l) => l.id === selectedLinkId) || null : null}
          selectedLinkLive={selectedLinkId ? liveById[selectedLinkId] || null : null}
          nodeMap={nodeMap}
          labelDraft={labelDraft}
          setLabelDraft={setLabelDraft}
          onSaveLabel={saveLabel}
          onChangeIcon={(icon) => selectedNode && updateNode.mutate({ id: selectedNode.id, patch: { icon } })}
          onDeleteNode={() => selectedNode && deleteNode.mutate(selectedNode.id)}
          onUpdateLink={(id, patch) => updateLink.mutate({ id, patch })}
          onDeleteLink={(id) => deleteLink.mutate(id)}
          onDeselectLink={() => setSelectedLinkId(null)}
          currentMap={currentMap}
          totals={statusTotals}
          links={links}
          nodes={nodes}
          onDeleteMap={() => setDeleteMapOpen(true)}
          mode={mode}
        />
        )}
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

function GridBackground({ view }: { view: { x: number; y: number; zoom: number } }) {
  const fine = 24 * view.zoom
  const major = fine * 4
  return (
    <div
      className="absolute inset-0 opacity-60 dark:opacity-50"
      style={{
        backgroundImage:
          `linear-gradient(to right, rgba(148,163,184,0.10) 1px, transparent 1px),` +
          `linear-gradient(to bottom, rgba(148,163,184,0.10) 1px, transparent 1px),` +
          `linear-gradient(to right, rgba(148,163,184,0.18) 1px, transparent 1px),` +
          `linear-gradient(to bottom, rgba(148,163,184,0.18) 1px, transparent 1px)`,
        backgroundSize: `${fine}px ${fine}px, ${fine}px ${fine}px, ${major}px ${major}px, ${major}px ${major}px`,
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

function NodeCard({
  node,
  position,
  labelOffset,
  zoom,
  selected,
  live,
  connectMode,
  isConnectSource,
  onPointerDown,
  onClick,
  onLabelOffsetChange,
}: {
  node: ManualMapNode
  position: { x_pct: number; y_pct: number }
  labelOffset: { dx: number; dy: number }
  zoom: number
  selected: boolean
  live: boolean
  connectMode: boolean
  isConnectSource: boolean
  onPointerDown: (e: ReactPointerEvent<HTMLButtonElement>) => void
  onClick: () => void
  onLabelOffsetChange: (dx: number, dy: number, commit: boolean) => void
}) {
  const iconKey = iconForNode(node)
  const sk = statusKey(node.status)
  const color = STATUS_COLOR[sk]
  const pulsing = live && (sk === 'down' || sk === 'degraded')
  const dim = connectMode && !isConnectSource
  const moved = Math.hypot(labelOffset.dx, labelOffset.dy) > 4
  const labelEditable = !live

  const startLabelDrag = (e: ReactPointerEvent) => {
    if (!labelEditable) return
    e.stopPropagation()
    e.preventDefault()
    const sx = e.clientX
    const sy = e.clientY
    const base = { ...labelOffset }
    const move = (ev: PointerEvent) => {
      onLabelOffsetChange(
        base.dx + (ev.clientX - sx) / zoom,
        base.dy + (ev.clientY - sy) / zoom,
        false,
      )
    }
    const up = (ev: PointerEvent) => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      onLabelOffsetChange(
        base.dx + (ev.clientX - sx) / zoom,
        base.dy + (ev.clientY - sy) / zoom,
        true,
      )
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const labelTop = NODE_LABEL_TOP + labelOffset.dy
  const labelLeft = labelOffset.dx

  return (
    <div
      className={cn(
        'group absolute z-20 -translate-x-1/2 -translate-y-1/2 transition-opacity',
        dim && 'opacity-60',
      )}
      style={{ left: `${position.x_pct}%`, top: `${position.y_pct}%` }}
    >
      {moved && (
        <svg
          className="pointer-events-none absolute overflow-visible"
          style={{ left: '50%', top: NODE_ICON_H / 2, transform: 'translateX(-50%)' }}
          width={1}
          height={1}
        >
          <line
            x1={0}
            y1={0}
            x2={labelLeft}
            y2={labelTop - NODE_ICON_H / 2}
            className="stroke-border"
            strokeWidth={1}
            strokeDasharray="3 3"
          />
        </svg>
      )}

      <button
        type="button"
        onPointerDown={onPointerDown}
        onClick={onClick}
        title={`${node.hostname} · ${node.ip_address}`}
        className="relative flex flex-col items-center"
      >
        <div className="relative">
          {pulsing && (
            <span
              aria-hidden
              className={cn(
                'absolute inset-0 rounded-lg',
                sk === 'down' ? 'bg-danger/40' : 'bg-warning/40',
                'nm-ping',
              )}
            />
          )}
          <div
            className={cn(
              'relative flex h-14 w-[3.75rem] items-center justify-center rounded-lg border-2 shadow-md transition',
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
            <NetworkIcon name={iconKey} className="h-8 w-8" />
          </div>
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
      </button>

      <div
        role="presentation"
        onPointerDown={startLabelDrag}
        title={labelEditable ? 'Drag to reposition label' : undefined}
        className={cn(
          'absolute max-w-[8rem] rounded-md border px-2 py-0.5 text-center text-[11px] font-semibold leading-tight shadow-sm backdrop-blur',
          'bg-surface/90 border-border',
          labelEditable && 'cursor-move hover:border-primary/60',
          selected && 'border-primary/50',
        )}
        style={{
          left: `calc(50% + ${labelLeft}px)`,
          top: `${labelTop}px`,
          transform: 'translateX(-50%)',
        }}
      >
        <div className="truncate text-text">{node.label || node.hostname}</div>
        <div className="truncate text-[10px] font-normal text-muted">{node.ip_address}</div>
      </div>
    </div>
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
  selectedLinkLive,
  nodeMap,
  labelDraft,
  setLabelDraft,
  onSaveLabel,
  onChangeIcon,
  onDeleteNode,
  onUpdateLink,
  onDeleteLink,
  onDeselectLink,
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
  selectedLinkLive: LiveLinkData | null
  nodeMap: Map<string, ManualMapNode>
  labelDraft: string
  setLabelDraft: (v: string) => void
  onSaveLabel: () => void
  onChangeIcon: (icon: string) => void
  onDeleteNode: () => void
  onUpdateLink: (id: string, patch: Partial<Pick<ManualMapLink, 'label' | 'link_type'>> & { metadata?: LinkMetadata }) => void
  onDeleteLink: (id: string) => void
  onDeselectLink: () => void
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
            ) : (
              <MapInspector
                currentMap={currentMap}
                totals={totals}
                links={links}
                nodes={nodes}
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

function DeviceInspector({
  node,
  labelDraft,
  setLabelDraft,
  onSaveLabel,
  onChangeIcon,
  onDeleteNode,
  mode,
}: {
  node: ManualMapNode
  labelDraft: string
  setLabelDraft: (v: string) => void
  onSaveLabel: () => void
  onChangeIcon: (icon: string) => void
  onDeleteNode: () => void
  mode: 'design' | 'live'
}) {
  const sk = statusKey(node.status)
  const color = STATUS_COLOR[sk]
  const currentIcon = iconForNode(node)
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <div className={cn('flex h-12 w-[3.25rem] items-center justify-center rounded-lg border-2', color.ring, 'bg-surface')}>
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
        {mode === 'design' && (
          <p className="mt-1 text-[10px] text-muted">Drag the name badge on the canvas to reposition it independently of the device icon.</p>
        )}
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
  onDeleteLink,
  onDeleteMap,
  mode,
}: {
  currentMap: ManualMapListItem | ManualMapDetail | null
  totals: Record<string, number>
  links: ManualMapLink[]
  nodes: ManualMapNode[]
  onDeleteLink: (id: string) => void
  onDeleteMap: () => void
  mode: 'design' | 'live'
}) {
  const nodeMap = new Map(nodes.map((n) => [n.id, n]))
  return (
    <div className="space-y-4">
      {currentMap && (currentMap as any).description && (
        <div className="rounded-md border border-border bg-surface2/40 p-2.5 text-xs text-text2">
          {(currentMap as any).description}
        </div>
      )}

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

/* ── Interface label on cable (SVG) ──────────────────────────── */

function IfaceCableLabel({
  label,
  anchor,
  style,
}: {
  label: string
  anchor: IfaceAnchor
  style?: IfaceLabelStyle | null
}) {
  const pad = 0.32
  const charW = 0.68
  const fontSize = style?.fontSize ?? IFACE_LABEL_FONT_DEFAULT
  const angle = style?.angle != null ? style.angle : anchor.angle
  const along = style?.along ?? 0
  const perp = style?.perp ?? 0
  const w = label.length * charW + pad * 2
  const h = 1.85 * (fontSize / IFACE_LABEL_FONT_DEFAULT)
  const textAnchor = style?.angle != null ? 'middle' as const : anchor.textAnchor
  const xOff = textAnchor === 'start' ? 0 : textAnchor === 'end' ? -w : -w / 2
  const textX = textAnchor === 'start' ? xOff + pad : textAnchor === 'end' ? xOff + w - pad : 0

  const rectFill = style?.bgColor || undefined
  const rectStroke = style?.borderColor || undefined
  const textFill = style?.textColor || undefined

  return (
    <g transform={`translate(${anchor.x}, ${anchor.y}) rotate(${angle})`}>
      <g transform={`translate(${along}, ${perp})`}>
        <rect
          x={xOff}
          y={-h / 2}
          width={w}
          height={h}
          rx={0.38}
          fill={rectFill}
          stroke={rectStroke}
          className={cn(!rectFill && 'fill-surface/95', !rectStroke && 'stroke-border')}
          strokeWidth={0.12}
          vectorEffect="non-scaling-stroke"
        />
        <text
          x={textX}
          y={0}
          textAnchor={textAnchor}
          dominantBaseline="central"
          fill={textFill || undefined}
          className={cn('font-mono font-semibold', !textFill && 'fill-text2')}
          style={{ fontSize: `${fontSize}px` }}
        >
          {label}
        </text>
      </g>
    </g>
  )
}

function IfaceLabelStyleEditor({
  title,
  style,
  disabled,
  onChange,
  onReset,
}: {
  title: string
  style?: IfaceLabelStyle | null
  disabled?: boolean
  onChange: (next: IfaceLabelStyle) => void
  onReset: () => void
}) {
  const s: IfaceLabelStyle = style || {}
  const num = (v: string) => (v === '' ? null : Number(v))

  const patch = (next: Partial<IfaceLabelStyle>) => onChange({ ...s, ...next })
  const nudge = (along: number, perp: number) => {
    patch({
      along: (s.along ?? 0) + along,
      perp: (s.perp ?? 0) + perp,
    })
  }

  return (
    <div className="rounded-lg border border-border bg-surface2/30 p-2.5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">{title}</span>
        {hasIfaceLabelStyle(style) && !disabled && (
          <button
            type="button"
            onClick={onReset}
            className="text-[9px] font-medium text-primary hover:underline"
          >
            Reset
          </button>
        )}
      </div>

      <div className="mb-2 flex flex-wrap gap-1">
        {([
          { label: '←', title: 'Left (perp −)', along: 0, perp: -0.5 },
          { label: '→', title: 'Right (perp +)', along: 0, perp: 0.5 },
          { label: '↑', title: 'Toward device (along −)', along: -0.5, perp: 0 },
          { label: '↓', title: 'Along cable (along +)', along: 0.5, perp: 0 },
        ] as const).map((btn) => (
          <button
            key={btn.label}
            type="button"
            disabled={disabled}
            title={btn.title}
            onClick={() => nudge(btn.along, btn.perp)}
            className="h-7 w-7 rounded border border-border bg-surface text-xs font-semibold hover:border-primary/50 hover:bg-primary/10 disabled:opacity-50"
          >
            {btn.label}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <FormField label="Distance" hint="From device, %">
          <Input
            type="number"
            step={0.1}
            min={0}
            disabled={disabled}
            value={s.dist ?? ''}
            placeholder="Auto"
            className="h-8 font-mono text-xs"
            onChange={(e) => patch({ dist: num(e.target.value) })}
          />
        </FormField>
        <FormField label="Font size" hint="SVG units">
          <Input
            type="number"
            step={0.05}
            min={0.5}
            max={2}
            disabled={disabled}
            value={s.fontSize ?? ''}
            placeholder={String(IFACE_LABEL_FONT_DEFAULT)}
            className="h-8 font-mono text-xs"
            onChange={(e) => patch({ fontSize: num(e.target.value) })}
          />
        </FormField>
        <FormField label="Along cable" hint="+ toward far end">
          <Input
            type="number"
            step={0.1}
            disabled={disabled}
            value={s.along ?? ''}
            placeholder="0"
            className="h-8 font-mono text-xs"
            onChange={(e) => patch({ along: num(e.target.value) })}
          />
        </FormField>
        <FormField label="Perpendicular" hint="+ right of cable">
          <Input
            type="number"
            step={0.1}
            disabled={disabled}
            value={s.perp ?? ''}
            placeholder="0"
            className="h-8 font-mono text-xs"
            onChange={(e) => patch({ perp: num(e.target.value) })}
          />
        </FormField>
        <FormField label="Angle °" hint="Empty = auto">
          <Input
            type="number"
            step={1}
            min={-180}
            max={180}
            disabled={disabled}
            value={s.angle ?? ''}
            placeholder="Auto"
            className="h-8 font-mono text-xs"
            onChange={(e) => patch({ angle: num(e.target.value) })}
          />
        </FormField>
      </div>

      <div className="mt-2 grid grid-cols-3 gap-2">
        <label className="flex flex-col gap-1 text-[10px]">
          <span className="text-muted">Text</span>
          <input
            type="color"
            disabled={disabled}
            value={s.textColor || '#94a3b8'}
            onChange={(e) => patch({ textColor: e.target.value })}
            className="h-8 w-full cursor-pointer rounded border border-border bg-surface disabled:opacity-50"
          />
        </label>
        <label className="flex flex-col gap-1 text-[10px]">
          <span className="text-muted">Background</span>
          <input
            type="color"
            disabled={disabled}
            value={s.bgColor || '#1e293b'}
            onChange={(e) => patch({ bgColor: e.target.value })}
            className="h-8 w-full cursor-pointer rounded border border-border bg-surface disabled:opacity-50"
          />
        </label>
        <label className="flex flex-col gap-1 text-[10px]">
          <span className="text-muted">Border</span>
          <input
            type="color"
            disabled={disabled}
            value={s.borderColor || '#334155'}
            onChange={(e) => patch({ borderColor: e.target.value })}
            className="h-8 w-full cursor-pointer rounded border border-border bg-surface disabled:opacity-50"
          />
        </label>
      </div>
    </div>
  )
}

/* ── Link annotation chip (HTML — speed / throughput mid-link) ── */

function LinkChip({
  x, y, variant, children, tone,
}: {
  x: number
  y: number
  variant: 'speed' | 'live'
  children: ReactNode
  tone?: 'success' | 'warning' | 'danger'
}) {
  let cls: string
  if (variant === 'speed') {
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
        'whitespace-nowrap rounded border px-1 py-px font-mono text-[9px] font-semibold leading-none tracking-tight shadow-sm backdrop-blur',
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
  const srcQ = useDeviceInterfaces(source?.device_id)
  const dstQ = useDeviceInterfaces(target?.device_id)
  const [kindTouched, setKindTouched] = useState(false)
  const [speedTouched, setSpeedTouched] = useState(false)
  const speedParts = parseStoredSpeed(md.speed)
  const [speedPreset, setSpeedPreset] = useState<LinkSpeed | ''>(speedParts.preset)
  const [speedCustom, setSpeedCustom] = useState(speedParts.custom)

  useEffect(() => {
    const next = parseStoredSpeed(md.speed)
    setSpeedPreset(next.preset)
    setSpeedCustom(next.custom)
    setKindTouched(false)
    setSpeedTouched(false)
  }, [link.id])

  const patchMeta = (next: Partial<LinkMetadata>) => {
    onChange({ metadata: { ...md, ...next } })
  }

  const applyInferred = useCallback((srcName: string, dstName: string) => {
    const next = inferLinkProps(
      findIface(srcQ.data, srcName),
      findIface(dstQ.data, dstName),
      srcName,
      dstName,
    )
    if (!kindTouched && (srcName || dstName)) {
      onChange({ link_type: next.kind, metadata: { ...md, kind: next.kind } })
    }
    if (!speedTouched && (srcName || dstName)) {
      const speed = resolveSpeedValue(next.speedPreset, next.speedCustom)
      setSpeedPreset(next.speedPreset)
      setSpeedCustom(next.speedCustom)
      patchMeta({ speed: speed || null })
    }
  }, [srcQ.data, dstQ.data, kindTouched, speedTouched, md, onChange])

  const inferred = useMemo(() => inferLinkProps(
    findIface(srcQ.data, md.src_interface || ''),
    findIface(dstQ.data, md.dst_interface || ''),
    md.src_interface || '',
    md.dst_interface || '',
  ), [srcQ.data, dstQ.data, md.src_interface, md.dst_interface])

  const kindAuto = !kindTouched && !!(md.src_interface || md.dst_interface)
  const speedAuto = !speedTouched && !!(md.src_interface || md.dst_interface)

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

      <DeviceInterfaceSelect
        deviceId={source?.device_id || ''}
        hostname={source?.hostname || 'Source'}
        value={md.src_interface || ''}
        disabled={ro || !source}
        onChange={(v) => {
          patchMeta({ src_interface: v || null })
          applyInferred(v, md.dst_interface || '')
        }}
      />

      <DeviceInterfaceSelect
        deviceId={target?.device_id || ''}
        hostname={target?.hostname || 'Target'}
        value={md.dst_interface || ''}
        disabled={ro || !target}
        onChange={(v) => {
          patchMeta({ dst_interface: v || null })
          applyInferred(md.src_interface || '', v)
        }}
      />

      {(md.src_interface || md.dst_interface) && (
        <div className="space-y-2">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">Port label style</div>
          {md.src_interface && (
            <IfaceLabelStyleEditor
              title={`${source?.hostname || 'Source'} label`}
              style={md.src_label}
              disabled={ro}
              onChange={(next) => patchMeta({ src_label: hasIfaceLabelStyle(next) ? next : null })}
              onReset={() => patchMeta({ src_label: null })}
            />
          )}
          {md.dst_interface && (
            <IfaceLabelStyleEditor
              title={`${target?.hostname || 'Target'} label`}
              style={md.dst_label}
              disabled={ro}
              onChange={(next) => patchMeta({ dst_label: hasIfaceLabelStyle(next) ? next : null })}
              onReset={() => patchMeta({ dst_label: null })}
            />
          )}
        </div>
      )}

      <div className="grid grid-cols-2 gap-2">
        <LinkSpeedSelect
          preset={speedPreset}
          custom={speedCustom}
          autoDetected={speedAuto}
          disabled={ro}
          onChange={(preset, custom) => {
            setSpeedTouched(true)
            setSpeedPreset(preset)
            setSpeedCustom(custom)
            patchMeta({ speed: resolveSpeedValue(preset, custom) || null })
          }}
        />
        <FormField
          label="Kind"
          hint={kindAuto ? `Auto: ${LINK_KINDS.find((k) => k.value === inferred.kind)?.label || inferred.kind}` : undefined}
        >
          <select
            value={kind}
            disabled={ro}
            onChange={(e) => {
              setKindTouched(true)
              onChange({ link_type: e.target.value, metadata: { ...md, kind: e.target.value as LinkKind } })
            }}
            className="h-9 w-full rounded-md border border-border bg-surface px-2 text-xs disabled:opacity-50"
          >
            {LINK_KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </select>
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
  const srcQ = useDeviceInterfaces(source.device_id)
  const dstQ = useDeviceInterfaces(target.device_id)
  const [kind, setKind] = useState<LinkKind>('ethernet')
  const [kindTouched, setKindTouched] = useState(false)
  const [shape, setShape] = useState<LinkShape>(defaultShape)
  const [src, setSrc] = useState('')
  const [dst, setDst] = useState('')
  const [speedPreset, setSpeedPreset] = useState<LinkSpeed | ''>('')
  const [speedCustom, setSpeedCustom] = useState('')
  const [speedTouched, setSpeedTouched] = useState(false)

  const inferred = useMemo(() => inferLinkProps(
    findIface(srcQ.data, src),
    findIface(dstQ.data, dst),
    src,
    dst,
  ), [srcQ.data, dstQ.data, src, dst])

  useEffect(() => {
    if (!kindTouched && (src || dst)) setKind(inferred.kind)
  }, [inferred.kind, kindTouched, src, dst])

  useEffect(() => {
    if (!speedTouched && (src || dst)) {
      setSpeedPreset(inferred.speedPreset)
      setSpeedCustom(inferred.speedCustom)
    }
  }, [inferred.speedPreset, inferred.speedCustom, speedTouched, src, dst])

  const kindAuto = !kindTouched && !!(src || dst)
  const speedAuto = !speedTouched && !!(src || dst)

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
              speed: resolveSpeedValue(speedPreset, speedCustom) || null,
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

          <div className="grid grid-cols-2 gap-3">
            <DeviceInterfaceSelect
              deviceId={source.device_id}
              hostname={source.hostname}
              value={src}
              autoFocus
              onChange={setSrc}
            />
            <DeviceInterfaceSelect
              deviceId={target.device_id}
              hostname={target.hostname}
              value={dst}
              onChange={setDst}
            />
          </div>

          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">Link kind</span>
              {kindAuto && (
                <span className="text-[9px] font-medium text-primary">
                  Auto · {LINK_KINDS.find((k) => k.value === inferred.kind)?.label}
                </span>
              )}
            </div>
            <div className="grid grid-cols-3 gap-1.5">
              {LINK_KINDS.map((k) => (
                <button
                  key={k.value}
                  type="button"
                  onClick={() => { setKindTouched(true); setKind(k.value) }}
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

          <LinkSpeedSelect
            preset={speedPreset}
            custom={speedCustom}
            autoDetected={speedAuto}
            onChange={(preset, custom) => {
              setSpeedTouched(true)
              setSpeedPreset(preset)
              setSpeedCustom(custom)
            }}
          />

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
