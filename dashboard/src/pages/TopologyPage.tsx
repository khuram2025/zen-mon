import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Box,
  Cable,
  CheckCircle2,
  CornerUpRight,
  Cpu,
  GitBranch,
  Spline,
  HardDrive,
  Loader2,
  Maximize2,
  Minimize2,
  Minus,
  MapPinned,
  Monitor,
  Network,
  Plus,
  Printer,
  RefreshCw,
  Router,
  Server,
  Shield,
  ShieldAlert,
  Trash2,
  Waypoints,
  Wifi,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, formatBps, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'

type NodeStatus = 'up' | 'down' | 'degraded' | 'unknown' | 'maintenance'

type TopologyNode = {
  id: string
  hostname: string
  ip_address: string
  device_type: string
  status: NodeStatus
  location?: string | null
  group_name?: string | null
  vendor?: string | null
  model?: string | null
  interface_count: number
  is_dependency_parent: boolean
  is_dependency_child: boolean
  is_mapped: boolean
}

type TopologyLink = {
  id: string
  source: string
  target: string | null
  local_hostname: string
  remote_hostname?: string | null
  local_if_index?: number | null
  local_if_name?: string | null
  remote_if_name?: string | null
  protocol: 'lldp' | 'cdp' | 'manual' | 'inferred'
  confidence: number
  last_seen_at?: string | null
  metadata?: { shape?: string; label?: string; manual?: boolean } | null
}

type LinkShape = 'curve' | 'straight' | 'orthogonal'

type Dependency = {
  id: string
  parent_device_id: string
  child_device_id: string
  parent_hostname: string
  child_hostname: string
  parent_status: NodeStatus
  child_status: NodeStatus
  dependency_type: string
  suppress_alerts: boolean
  enabled: boolean
  notes?: string | null
}

type TopologyData = {
  generated_at: string
  summary: {
    devices: number
    links: number
    dependencies: number
    dependency_suppression: number
    unmapped_devices: number
    suppressed_alerts_24h: number
    status_counts: Record<string, number>
    protocol_counts: Record<string, number>
  }
  nodes: TopologyNode[]
  links: TopologyLink[]
  dependencies: Dependency[]
}

type PositionedNode = TopologyNode & { x: number; y: number; depth: number }

const WIDTH = 1180
const HEIGHT = 660

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n))

// Utilization → weathermap colour ramp (green→amber→red), per the NOC spec.
function utilColor(pct: number): string {
  if (pct >= 95) return '#ef4444'
  if (pct >= 85) return '#fb923c'
  if (pct >= 70) return '#f59e0b'
  if (pct >= 50) return '#a3e635'
  if (pct > 0) return '#22c55e'
  return '#475569'
}

const deviceIcons: Record<string, React.ComponentType<{ className?: string }>> = {
  router: Router,
  switch: Network,
  firewall: Shield,
  server: Server,
  access_point: Wifi,
  printer: Printer,
  other: HardDrive,
}

const statusTone: Record<string, string> = {
  up: 'border-success/50 bg-success/10 text-success',
  down: 'border-danger/60 bg-danger/10 text-danger',
  degraded: 'border-warning/60 bg-warning/10 text-warning',
  unknown: 'border-border bg-surface2 text-muted',
  maintenance: 'border-info/50 bg-info/10 text-info',
}

export function TopologyPage() {
  const qc = useQueryClient()
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [dependencyOpen, setDependencyOpen] = useState(false)
  const [deleteDependency, setDeleteDependency] = useState<Dependency | null>(null)
  const [connectMode, setConnectMode] = useState(false)
  const [connectFrom, setConnectFrom] = useState<string | null>(null)
  const [linkShape, setLinkShape] = useState<LinkShape>('curve')
  const [selectedLinkId, setSelectedLinkId] = useState<string | null>(null)
  const [view, setView] = useState({ x: 0, y: 0, zoom: 1 })
  const [fullscreen, setFullscreen] = useState(false)
  const [refreshMs, setRefreshMs] = useState(30_000)
  const panRef = useRef<{ px: number; py: number; vx: number; vy: number } | null>(null)
  const mapBoxRef = useRef<HTMLDivElement | null>(null)
  // Manual node repositioning — overrides the auto-layout, persisted per browser.
  const [overrides, setOverrides] = useState<Record<string, { x: number; y: number }>>(() => {
    try { return JSON.parse(localStorage.getItem('topology-node-pos') || '{}') } catch { return {} }
  })
  const dragRef = useRef<{ id: string; moved: boolean } | null>(null)
  useEffect(() => {
    try { localStorage.setItem('topology-node-pos', JSON.stringify(overrides)) } catch { /* ignore */ }
  }, [overrides])

  const topology = useQuery<TopologyData>({
    queryKey: ['topology', 'map'],
    queryFn: async () => (await api.get('/topology/map')).data,
    refetchInterval: refreshMs,
  })

  // Live per-device stats (CPU / memory / status freshness) overlaid on nodes.
  const metricsQuery = useQuery<{ devices: Record<string, { cpu?: number; memory?: number; uptime?: number; _ts?: string }> }>({
    queryKey: ['devices', 'current-metrics'],
    queryFn: async () => (await api.get('/devices/current-metrics')).data,
    refetchInterval: refreshMs,
  })
  const metricsById = metricsQuery.data?.devices || {}

  // Live link utilization (weathermap) from SNMP interface counters.
  const linksLiveQuery = useQuery<{ data: Record<string, { in_bps: number; out_bps: number; util_pct: number | null; oper_status: number; speed: number }> }>({
    queryKey: ['topology', 'links-live'],
    queryFn: async () => (await api.get('/topology/links-live')).data,
    refetchInterval: refreshMs,
  })
  const linkLiveById = linksLiveQuery.data?.data || {}

  // Pan / zoom
  const onMapWheel = (e: React.WheelEvent) => {
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12
    setView((v) => ({ ...v, zoom: clamp(v.zoom * factor, 0.3, 3) }))
  }
  const onMapPointerDown = (e: React.PointerEvent) => {
    if ((e.target as HTMLElement).closest('[data-node]')) return
    panRef.current = { px: e.clientX, py: e.clientY, vx: view.x, vy: view.y }
    ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
  }
  const onMapPointerMove = (e: React.PointerEvent) => {
    // Dragging a node → write a position override (in canvas coords).
    if (dragRef.current) {
      const box = mapBoxRef.current
      if (!box) return
      const rect = box.getBoundingClientRect()
      const sx = (e.clientX - rect.left - view.x) / view.zoom
      const sy = (e.clientY - rect.top - view.y) / view.zoom
      const x = clamp((sx / rect.width) * WIDTH, 0, WIDTH)
      const y = Math.max(40, (sy / rect.height) * contentH)
      dragRef.current.moved = true
      const id = dragRef.current.id
      setOverrides((o) => ({ ...o, [id]: { x, y } }))
      return
    }
    if (!panRef.current) return
    setView((v) => ({ ...v, x: panRef.current!.vx + (e.clientX - panRef.current!.px), y: panRef.current!.vy + (e.clientY - panRef.current!.py) }))
  }
  const onMapPointerUp = () => { dragRef.current = null; panRef.current = null }
  const startNodeDrag = (e: React.PointerEvent, id: string) => {
    e.stopPropagation()
    dragRef.current = { id, moved: false }
    mapBoxRef.current?.setPointerCapture?.(e.pointerId)
  }
  const resetView = () => setView({ x: 0, y: 0, zoom: 1 })
  const resetLayout = () => setOverrides({})

  function onNodeClick(id: string) {
    if (dragRef.current?.moved) return
    if (connectMode) {
      if (!connectFrom) setConnectFrom(id)
      else if (connectFrom !== id) { createLink.mutate({ source: connectFrom, target: id, shape: linkShape }); setConnectFrom(null) }
      return
    }
    setSelectedLinkId(null)
    setSelectedId(id)
  }
  // Esc cancels connect / clears selection.
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (connectFrom) setConnectFrom(null)
      else if (connectMode) setConnectMode(false)
      else { setSelectedLinkId(null); setSelectedId(null) }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [connectMode, connectFrom])

  // Fullscreen NOC: hide the app sidebar for a clean wall display, exit on Esc.
  useEffect(() => {
    if (!fullscreen) return
    const aside = document.querySelector('aside') as HTMLElement | null
    const prev = aside?.style.display ?? ''
    if (aside) aside.style.display = 'none'
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') setFullscreen(false) }
    window.addEventListener('keydown', h)
    return () => { window.removeEventListener('keydown', h); if (aside) aside.style.display = prev }
  }, [fullscreen])

  const discover = useMutation({
    mutationFn: async () => (await api.post('/topology/discover', { auto_dependencies: true })).data,
    onSuccess: (data) => {
      toast.success('Topology discovery complete', `${data.links_found || 0} LLDP/CDP links found`)
      qc.invalidateQueries({ queryKey: ['topology'] })
    },
    onError: (e: any) => toast.error('Discovery failed', apiErrorMessage(e)),
  })

  const createLink = useMutation({
    mutationFn: async (v: { source: string; target: string; shape: LinkShape }) =>
      (await api.post('/topology/links', { source_device_id: v.source, target_device_id: v.target, shape: v.shape })).data,
    onSuccess: () => { toast.success('Manual link added'); qc.invalidateQueries({ queryKey: ['topology'] }) },
    onError: (e: any) => toast.error('Link failed', apiErrorMessage(e)),
  })
  const updateLinkShape = useMutation({
    mutationFn: async (v: { id: string; shape: LinkShape }) => (await api.put(`/topology/links/${v.id}`, { shape: v.shape })).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['topology'] }),
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })
  const deleteLink = useMutation({
    mutationFn: async (id: string) => api.delete(`/topology/links/${id}`),
    onSuccess: () => { toast.success('Link removed'); setSelectedLinkId(null); qc.invalidateQueries({ queryKey: ['topology'] }) },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const removeDependency = useMutation({
    mutationFn: async (id: string) => api.delete(`/topology/dependencies/${id}`),
    onSuccess: () => {
      toast.success('Dependency removed')
      setDeleteDependency(null)
      qc.invalidateQueries({ queryKey: ['topology'] })
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const data = topology.data
  const nodes = data?.nodes || []
  const links = data?.links || []
  const dependencies = data?.dependencies || []
  const baseLayout = useMemo(() => layoutNodes(nodes, dependencies, links), [nodes, dependencies, links])
  const positioned = useMemo(
    () => baseLayout.map((n) => (overrides[n.id] ? { ...n, x: overrides[n.id].x, y: overrides[n.id].y } : n)),
    [baseLayout, overrides],
  )
  const contentH = useMemo(() => Math.max(HEIGHT, ...positioned.map((n) => n.y), 0) + 90, [positioned])
  const nodeMap = useMemo(() => new Map(positioned.map((n) => [n.id, n])), [positioned])
  const selected = selectedId ? nodeMap.get(selectedId) || null : null
  const selectedLink = selectedLinkId ? links.find((l) => l.id === selectedLinkId) || null : null
  const selectedManual = selectedLink?.protocol === 'manual'
  const unresolvedLinks = links.filter((link) => !link.target)

  if (topology.error) {
    return (
      <div className="rounded-lg border border-danger/30 bg-danger/10 p-4 text-sm text-danger">
        Failed to load topology: {apiErrorMessage(topology.error as any)}
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <MapPinned className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Automated Maps</h1>
            <p className="max-w-3xl text-xs text-muted">
              LLDP/CDP topology, dependency mapping, and upstream-aware alert suppression.
            </p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => qc.invalidateQueries({ queryKey: ['topology'] })}>
            <RefreshCw className="h-4 w-4" />
            Refresh
          </Button>
          <Button variant="outline" onClick={() => setDependencyOpen(true)}>
            <Plus className="h-4 w-4" />
            Dependency
          </Button>
          <Button onClick={() => discover.mutate()} disabled={discover.isPending}>
            {discover.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Waypoints className="h-4 w-4" />}
            Discover LLDP/CDP
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Metric icon={Monitor} label="Devices" value={data?.summary.devices || 0} />
        <Metric icon={Cable} label="Links" value={data?.summary.links || 0} />
        <Metric icon={GitBranch} label="Dependencies" value={data?.summary.dependencies || 0} />
        <Metric icon={ShieldAlert} label="Suppression" value={data?.summary.dependency_suppression || 0} />
        <Metric icon={AlertTriangle} label="Suppressed 24h" value={data?.summary.suppressed_alerts_24h || 0} />
        <Metric icon={Box} label="Unmapped" value={data?.summary.unmapped_devices || 0} />
      </div>

      <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[minmax(0,1fr)_380px]">
        <Card className={cn(fullscreen && 'fixed inset-0 z-[60] rounded-none border-0 shadow-2xl')}>
          <CardContent className="p-0">
            <div className="flex flex-col gap-2 border-b border-border p-3 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex items-center gap-3">
                <div>
                  <h2 className="text-sm font-semibold">Network Map</h2>
                  <p className="text-xs text-muted">
                    {topology.isLoading ? 'Loading topology' : `${positioned.length} nodes · ${links.length} links · ${dependencies.length} dependencies`}
                  </p>
                </div>
                {/* Live status roll-up */}
                <div className="flex items-center gap-1">
                  {(['down', 'degraded', 'up', 'unknown'] as const).map((s) => {
                    const n = data?.summary.status_counts?.[s] || 0
                    if (!n) return null
                    return <StatusPill key={s} status={s} count={n} />
                  })}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                {/* Utilization ramp legend (weathermap) */}
                <div className="flex items-center gap-1 rounded-md border border-border bg-bg px-1.5 py-0.5" title="Link utilization (green → red)">
                  <span className="text-muted">Util</span>
                  <span className="flex h-2 w-16 overflow-hidden rounded-full">
                    {['#22c55e', '#a3e635', '#f59e0b', '#fb923c', '#ef4444'].map((c) => (
                      <span key={c} className="h-full flex-1" style={{ background: c }} />
                    ))}
                  </span>
                </div>
                <Legend color="border-warning/70 bg-warning/10" label="Dependency" />
                <div className="mx-0.5 h-5 w-px bg-border/60" />
                <select
                  value={refreshMs}
                  onChange={(e) => setRefreshMs(Number(e.target.value))}
                  className="h-7 rounded-md border border-border bg-surface px-1.5 text-[11px] text-muted outline-none"
                  title="Auto-refresh cadence"
                >
                  <option value={10_000}>10s</option>
                  <option value={30_000}>30s</option>
                  <option value={60_000}>60s</option>
                  <option value={300_000}>5m</option>
                </select>
                <div className="mx-0.5 h-5 w-px bg-border/60" />
                {/* Manual link: connect tool + shape selector */}
                <MapBtn onClick={() => { setConnectMode((m) => !m); setConnectFrom(null) }} icon={Cable} title="Connect — draw a manual link between two devices" active={connectMode} />
                <div className="flex items-center gap-0.5 rounded-md border border-border bg-surface p-0.5">
                  {([['curve', Spline], ['straight', Minus], ['orthogonal', CornerUpRight]] as const).map(([s, Ic]) => {
                    const activeShape = selectedManual ? (selectedLink?.metadata?.shape || 'curve') : linkShape
                    return (
                      <button
                        key={s}
                        title={selectedManual ? `Set selected link to ${s}` : `${s} shape for new links`}
                        onClick={() => { if (selectedManual && selectedLinkId) updateLinkShape.mutate({ id: selectedLinkId, shape: s }); else setLinkShape(s) }}
                        className={cn('flex h-6 w-6 items-center justify-center rounded transition-colors', activeShape === s ? 'bg-primary/20 text-primary' : 'text-muted hover:text-text')}
                      >
                        <Ic className="h-3.5 w-3.5" />
                      </button>
                    )
                  })}
                </div>
                {selectedManual && (
                  <MapBtn onClick={() => selectedLinkId && deleteLink.mutate(selectedLinkId)} icon={Trash2} title="Delete selected manual link" />
                )}
                <div className="mx-0.5 h-5 w-px bg-border/60" />
                <MapBtn onClick={() => setView((v) => ({ ...v, zoom: clamp(v.zoom * 1.15, 0.3, 3) }))} icon={Plus} title="Zoom in" />
                <span className="w-9 text-center tabular-nums text-muted">{Math.round(view.zoom * 100)}%</span>
                <MapBtn onClick={() => setView((v) => ({ ...v, zoom: clamp(v.zoom / 1.15, 0.3, 3) }))} icon={Minus} title="Zoom out" />
                <MapBtn onClick={resetView} icon={Maximize2} title="Fit / reset zoom" />
                {Object.keys(overrides).length > 0 && (
                  <MapBtn onClick={resetLayout} icon={Waypoints} title="Reset node positions to auto-layout" />
                )}
                <MapBtn onClick={() => setFullscreen((f) => !f)} icon={fullscreen ? Minimize2 : Monitor} title={fullscreen ? 'Exit NOC mode (Esc)' : 'NOC fullscreen'} active={fullscreen} />
              </div>
            </div>

            <div
              ref={mapBoxRef}
              className={cn(
                'relative overflow-hidden bg-[radial-gradient(circle_at_20%_20%,rgba(59,130,246,0.08),transparent_26%),linear-gradient(180deg,rgba(148,163,184,0.06),transparent)]',
                fullscreen ? 'h-[calc(100vh-56px)]' : 'h-[680px] rounded-b-lg',
                panRef.current ? 'cursor-grabbing' : 'cursor-grab',
              )}
              onWheel={onMapWheel}
              onPointerDown={onMapPointerDown}
              onPointerMove={onMapPointerMove}
              onPointerUp={onMapPointerUp}
              onPointerLeave={onMapPointerUp}
            >
              <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(to_right,rgba(148,163,184,0.08)_1px,transparent_1px),linear-gradient(to_bottom,rgba(148,163,184,0.08)_1px,transparent_1px)] bg-[size:42px_42px]" />

              {connectMode && (
                <div className="pointer-events-none absolute left-1/2 top-3 z-20 -translate-x-1/2 rounded-md border border-teal-400/40 bg-teal-500/15 px-3 py-1.5 text-[11px] font-medium text-teal-200 backdrop-blur">
                  {connectFrom ? 'Click the target device to draw the link · Esc to cancel' : `Click a device to start a ${linkShape} link · Esc to exit`}
                </div>
              )}

              {/* Pan/zoom scene */}
              <div
                className="absolute inset-0"
                style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`, transformOrigin: '0 0' }}
              >
                <svg className="absolute inset-0 h-full w-full overflow-visible" viewBox={`0 0 ${WIDTH} ${contentH}`} preserveAspectRatio="none" role="img">
                  <defs>
                    <marker id="depArrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                      <path d="M 0 0 L 10 5 L 0 10 z" className="fill-warning" />
                    </marker>
                  </defs>
                  {links.filter((link) => link.target && nodeMap.has(link.source) && nodeMap.has(link.target)).map((link) => {
                    const a = nodeMap.get(link.source)!
                    const b = nodeMap.get(link.target!)!
                    const dim = selectedId && selectedId !== link.source && selectedId !== link.target
                    const isManual = link.protocol === 'manual'
                    // Manual links: user-chosen shape, distinct teal style, selectable.
                    if (isManual) {
                      const shape = link.metadata?.shape || 'curve'
                      const d = shapePath(shape, a, b)
                      const sel = selectedLinkId === link.id
                      return (
                        <g key={link.id} className="cursor-pointer" onClick={(e) => { e.stopPropagation(); setSelectedLinkId(link.id); setSelectedId(null) }}>
                          <path d={d} fill="none" stroke="transparent" strokeWidth={10} />
                          <path d={d} fill="none" strokeWidth={sel ? 3 : 2.4} strokeDasharray="6 4"
                            className={cn(sel ? 'stroke-teal-300' : 'stroke-teal-400/80', dim && 'opacity-25')} />
                        </g>
                      )
                    }
                    const live = linkLiveById[link.id]
                    const util = live?.util_pct
                    // Weathermap: stroke width scales with link capacity, colour with load.
                    const speedG = live?.speed ? live.speed / 1e9 : 0
                    const width = speedG >= 40 ? 5 : speedG >= 10 ? 4 : speedG >= 1 ? 3 : 2.4
                    return (
                      <path
                        key={link.id}
                        d={curvePath(a, b)}
                        fill="none"
                        strokeWidth={util != null ? width : 2.2}
                        stroke={util != null ? utilColor(util) : undefined}
                        strokeOpacity={dim ? 0.18 : util != null ? 0.95 : undefined}
                        className={cn(
                          'transition-opacity',
                          util == null && (link.protocol === 'cdp' ? 'stroke-info/50' : 'stroke-primary/50'),
                          util == null && dim && 'opacity-20',
                        )}
                      />
                    )
                  })}
                  {dependencies.filter((dep) => nodeMap.has(dep.parent_device_id) && nodeMap.has(dep.child_device_id)).map((dep) => {
                    const a = nodeMap.get(dep.parent_device_id)!
                    const b = nodeMap.get(dep.child_device_id)!
                    return (
                      <path
                        key={dep.id}
                        d={curvePath(a, b, 28)}
                        markerEnd="url(#depArrow)"
                        className={cn(
                          'fill-none stroke-[2.8]',
                          dep.enabled && dep.suppress_alerts ? 'stroke-warning/80' : 'stroke-muted/35 stroke-dasharray-[6_6]',
                        )}
                      />
                    )
                  })}
                </svg>

                {/* Live link utilization labels (bps · %) at mid-link */}
                {view.zoom >= 0.7 && links.filter((l) => l.target && nodeMap.has(l.source) && nodeMap.has(l.target!)).map((link) => {
                  const live = linkLiveById[link.id]
                  if (!live || live.util_pct == null) return null
                  const bps = Math.max(live.in_bps, live.out_bps)
                  // Declutter: only label meaningful flows (or any link on the
                  // selected node, so inspection still shows everything).
                  const onSelected = selectedId === link.source || selectedId === link.target
                  if (!onSelected && bps < 1_000_000 && (live.util_pct || 0) < 1) return null
                  const a = nodeMap.get(link.source)!
                  const b = nodeMap.get(link.target!)!
                  const dim = selectedId && selectedId !== link.source && selectedId !== link.target
                  const mx = ((a.x + b.x) / 2 / WIDTH) * 100
                  const my = ((a.y + b.y) / 2 / contentH) * 100
                  return (
                    <div
                      key={`ll-${link.id}`}
                      className={cn('pointer-events-none absolute z-[5] -translate-x-1/2 -translate-y-1/2 whitespace-nowrap rounded-full border px-1.5 py-0.5 text-[9px] font-semibold tabular-nums shadow-sm backdrop-blur', dim && 'opacity-20')}
                      style={{ left: `${mx}%`, top: `${my}%`, borderColor: utilColor(live.util_pct), color: utilColor(live.util_pct), background: 'rgba(15,23,42,0.82)' }}
                    >
                      {formatBps(bps)} · {live.util_pct}%
                    </div>
                  )
                })}

                {!topology.isLoading && positioned.length > 0 && positioned.map((node) => (
                  <TopologyNodeCard
                    key={node.id}
                    node={node}
                    canvasH={contentH}
                    metrics={metricsById[node.id]}
                    selected={selectedId === node.id}
                    dimmed={!!selectedId && selectedId !== node.id}
                    big={fullscreen}
                    connectSource={connectFrom === node.id}
                    onPointerDown={(e) => { if (!connectMode) startNodeDrag(e, node.id) }}
                    onSelect={() => onNodeClick(node.id)}
                  />
                ))}
              </div>

              {topology.isLoading ? (
                <div className="absolute inset-0 flex items-center justify-center">
                  <div className="rounded-lg border border-border bg-surface/90 px-4 py-3 text-sm text-muted shadow-lg">
                    <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />
                    Loading topology
                  </div>
                </div>
              ) : positioned.length === 0 ? (
                <div className="absolute inset-0 flex items-center justify-center">
                  <div className="max-w-md rounded-lg border border-border bg-surface/90 p-6 text-center shadow-lg">
                    <MapPinned className="mx-auto h-8 w-8 text-primary" />
                    <div className="mt-3 text-sm font-semibold">No devices to map</div>
                    <p className="mt-1 text-xs text-muted">Add devices or import discovery results before building topology.</p>
                  </div>
                </div>
              ) : null}
            </div>
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardContent className="space-y-3 p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h2 className="text-sm font-semibold">Selection</h2>
                  <p className="text-xs text-muted">Device and dependency context.</p>
                </div>
                {selected && (
                  <button className="rounded p-1 text-muted hover:bg-surface2 hover:text-text" onClick={() => setSelectedId(null)}>
                    <X className="h-4 w-4" />
                  </button>
                )}
              </div>
              {selected ? (
                <SelectedNode node={selected} dependencies={dependencies} links={links} />
              ) : (
                <div className="rounded-md border border-dashed border-border p-6 text-center text-xs text-muted">
                  Select a map node to inspect links, dependencies, and suppression behavior.
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="space-y-3 p-4">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-sm font-semibold">Dependencies</h2>
                  <p className="text-xs text-muted">Parent failures suppress child alerts.</p>
                </div>
                <Button size="sm" variant="outline" onClick={() => setDependencyOpen(true)}>
                  <Plus className="h-3.5 w-3.5" />
                  Add
                </Button>
              </div>
              {dependencies.length === 0 ? (
                <div className="rounded-md border border-dashed border-border p-5 text-center text-xs text-muted">
                  No dependencies yet. Run discovery or add critical uplinks manually.
                </div>
              ) : (
                <div className="max-h-[380px] space-y-2 overflow-y-auto pr-1">
                  {dependencies.map((dep) => (
                    <div key={dep.id} className="rounded-md border border-border bg-bg p-3">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-medium">{dep.parent_hostname}</div>
                          <div className="mt-1 flex items-center gap-1.5 text-xs text-muted">
                            <GitBranch className="h-3.5 w-3.5" />
                            <span className="truncate">{dep.child_hostname}</span>
                          </div>
                        </div>
                        <button
                          className="rounded p-1 text-muted hover:bg-surface2 hover:text-danger"
                          onClick={() => setDeleteDependency(dep)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        <Badge variant={dep.enabled ? 'success' : 'outline'}>{dep.enabled ? 'Enabled' : 'Disabled'}</Badge>
                        <Badge variant={dep.suppress_alerts ? 'warning' : 'outline'}>
                          {dep.suppress_alerts ? 'Suppress alerts' : 'Observe only'}
                        </Badge>
                        <Badge variant="outline">{dep.dependency_type}</Badge>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="space-y-3 p-4">
              <div>
                <h2 className="text-sm font-semibold">Unresolved Neighbors</h2>
                <p className="text-xs text-muted">LLDP/CDP peers not matched to a monitored device.</p>
              </div>
              {unresolvedLinks.length === 0 ? (
                <div className="rounded-md border border-border bg-bg p-3 text-xs text-muted">No unresolved peers in the current topology.</div>
              ) : (
                <div className="max-h-56 space-y-2 overflow-y-auto pr-1">
                  {unresolvedLinks.slice(0, 8).map((link) => (
                    <div key={link.id} className="rounded-md border border-border bg-bg px-3 py-2">
                      <div className="text-sm font-medium">{link.remote_hostname || 'Unknown peer'}</div>
                      <div className="mt-0.5 text-[11px] text-muted">
                        {link.local_hostname} · {link.local_if_name || `ifIndex ${link.local_if_index || '?'}`} · {link.protocol.toUpperCase()}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <DependencyDialog
        open={dependencyOpen}
        onOpenChange={setDependencyOpen}
        nodes={nodes}
      />

      <ConfirmDialog
        open={!!deleteDependency}
        onOpenChange={(open) => !open && setDeleteDependency(null)}
        title="Delete dependency"
        description={<>Remove dependency from <span className="font-semibold text-text">{deleteDependency?.parent_hostname}</span> to <span className="font-semibold text-text">{deleteDependency?.child_hostname}</span>?</>}
        confirmText="Delete"
        destructive
        loading={removeDependency.isPending}
        onConfirm={() => deleteDependency && removeDependency.mutate(deleteDependency.id)}
      />
    </div>
  )
}

const statusRing: Record<string, string> = {
  up: 'ring-success/70',
  down: 'ring-danger/80',
  degraded: 'ring-warning/80',
  unknown: 'ring-border',
  maintenance: 'ring-info/70',
}

function TopologyNodeCard({ node, canvasH, metrics, selected, dimmed, big, connectSource, onPointerDown, onSelect }: {
  node: PositionedNode
  canvasH: number
  metrics?: { cpu?: number; memory?: number; uptime?: number }
  selected: boolean
  dimmed?: boolean
  big?: boolean
  connectSource?: boolean
  onPointerDown: (e: React.PointerEvent) => void
  onSelect: () => void
}) {
  const Icon = deviceIcons[node.device_type] || deviceIcons.other
  const risk = node.status === 'down' || node.status === 'degraded'
  const cpu = metrics?.cpu != null ? Math.round(metrics.cpu) : null
  const mem = metrics?.memory != null ? Math.round(metrics.memory) : null
  // Status drives the icon colour (no separate "Up" badge — saves space).
  const dot = node.status === 'down' ? 'bg-danger' : node.status === 'degraded' ? 'bg-warning'
    : node.status === 'maintenance' ? 'bg-info' : node.status === 'unknown' ? 'bg-muted' : 'bg-success'
  return (
    <button
      type="button"
      data-node
      onPointerDown={onPointerDown}
      onClick={onSelect}
      title={`${node.hostname} · ${node.ip_address}\n${node.device_type} · ${node.status}${cpu != null ? ` · CPU ${cpu}%` : ''}${mem != null ? ` · MEM ${mem}%` : ''}\nDrag to reposition`}
      className={cn(
        'group absolute z-10 -translate-x-1/2 -translate-y-1/2 cursor-grab touch-none rounded-md border bg-surface/95 p-1.5 text-left shadow-md backdrop-blur transition-colors hover:z-20 hover:border-primary/60 active:cursor-grabbing',
        big ? 'w-[150px]' : 'w-[128px]',
        selected ? 'border-primary ring-2 ring-primary/30' : 'border-border',
        connectSource && 'border-teal-400 ring-2 ring-teal-400/50',
        risk && 'border-danger/60',
        dimmed && 'opacity-40',
      )}
      style={{ left: `${(node.x / WIDTH) * 100}%`, top: `${(node.y / canvasH) * 100}%` }}
    >
      <div className="flex items-center gap-1.5">
        <div
          className={cn(
            'relative flex shrink-0 items-center justify-center rounded-md ring-2',
            big ? 'h-9 w-9' : 'h-8 w-8',
            statusTone[node.status] || statusTone.unknown,
            statusRing[node.status] || statusRing.unknown,
            risk && 'animate-pulse-soft',
          )}
        >
          <Icon className={big ? 'h-5 w-5' : 'h-[18px] w-[18px]'} />
          {/* status dot */}
          <span className={cn('absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full ring-2 ring-surface', dot)} />
        </div>
        <div className="min-w-0 flex-1">
          <div className={cn('truncate font-semibold text-text', big ? 'text-xs' : 'text-[11px]')}>{node.hostname}</div>
          <div className={cn('truncate font-mono text-muted', big ? 'text-[10px]' : 'text-[9px]')}>{node.ip_address}</div>
        </div>
        {node.is_dependency_parent && <ShieldAlert className="h-3 w-3 shrink-0 text-warning" />}
      </div>
      {/* Compact live CPU/MEM bars */}
      {(cpu != null || mem != null) && (
        <div className="mt-1 space-y-0.5">
          {cpu != null && <MiniBar label="CPU" pct={cpu} />}
          {mem != null && <MiniBar label="MEM" pct={mem} />}
        </div>
      )}
    </button>
  )
}

function MiniBar({ label, pct }: { label: string; pct: number }) {
  const color = pct >= 90 ? 'bg-danger' : pct >= 75 ? 'bg-warning' : 'bg-success'
  return (
    <div className="flex items-center gap-1" title={`${label} ${pct}%`}>
      <span className="w-3.5 shrink-0 text-[8px] font-semibold uppercase text-muted">{label[0]}</span>
      <div className="h-1 flex-1 overflow-hidden rounded-full bg-surface2">
        <div className={cn('h-full rounded-full', color)} style={{ width: `${clamp(pct, 0, 100)}%` }} />
      </div>
      <span className="w-6 shrink-0 text-right text-[8px] tabular-nums text-muted">{pct}%</span>
    </div>
  )
}

function StatusPill({ status, count }: { status: string; count: number }) {
  const dot = status === 'down' ? 'bg-danger' : status === 'degraded' ? 'bg-warning' : status === 'up' ? 'bg-success' : 'bg-muted'
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-border bg-bg px-1.5 py-0.5 text-[10px] font-medium tabular-nums">
      <span className={cn('h-1.5 w-1.5 rounded-full', dot)} />
      {count}
    </span>
  )
}

function MapBtn({ onClick, icon: Icon, title, active }: { onClick: () => void; icon: React.ComponentType<{ className?: string }>; title: string; active?: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={cn(
        'flex h-7 w-7 items-center justify-center rounded-md border transition-colors',
        active ? 'border-primary/50 bg-primary/15 text-primary' : 'border-border bg-surface text-muted hover:text-text',
      )}
    >
      <Icon className="h-3.5 w-3.5" />
    </button>
  )
}

function SelectedNode({ node, dependencies, links }: { node: TopologyNode; dependencies: Dependency[]; links: TopologyLink[] }) {
  const upstream = dependencies.filter((dep) => dep.child_device_id === node.id)
  const downstream = dependencies.filter((dep) => dep.parent_device_id === node.id)
  const observed = links.filter((link) => link.source === node.id || link.target === node.id)
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-border bg-bg p-3">
        <div className="flex items-center gap-3">
          <div className={cn('flex h-10 w-10 items-center justify-center rounded-lg border', statusTone[node.status] || statusTone.unknown)}>
            {(() => {
              const Icon = deviceIcons[node.device_type] || deviceIcons.other
              return <Icon className="h-5 w-5" />
            })()}
          </div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold">{node.hostname}</div>
            <div className="font-mono text-xs text-muted">{node.ip_address}</div>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
          <Fact label="Type" value={node.device_type} />
          <Fact label="Status" value={node.status} />
          <Fact label="Location" value={node.location || '-'} />
          <Fact label="Interfaces" value={node.interface_count} />
          <Fact label="Vendor" value={node.vendor || '-'} />
          <Fact label="Model" value={node.model || '-'} />
        </div>
      </div>

      <div>
        <div className="mb-2 text-xs font-semibold uppercase text-muted">Upstream Parents</div>
        {upstream.length === 0 ? (
          <div className="rounded-md border border-border bg-bg p-3 text-xs text-muted">No upstream dependency configured.</div>
        ) : upstream.map((dep) => (
          <DependencyMini key={dep.id} dep={dep} mode="upstream" />
        ))}
      </div>

      <div>
        <div className="mb-2 text-xs font-semibold uppercase text-muted">Downstream Children</div>
        {downstream.length === 0 ? (
          <div className="rounded-md border border-border bg-bg p-3 text-xs text-muted">No downstream children configured.</div>
        ) : downstream.map((dep) => (
          <DependencyMini key={dep.id} dep={dep} mode="downstream" />
        ))}
      </div>

      <div>
        <div className="mb-2 text-xs font-semibold uppercase text-muted">Observed Links</div>
        {observed.length === 0 ? (
          <div className="rounded-md border border-border bg-bg p-3 text-xs text-muted">No LLDP/CDP links observed.</div>
        ) : observed.slice(0, 5).map((link) => (
          <div key={link.id} className="mb-2 rounded-md border border-border bg-bg p-3">
            <div className="flex items-center justify-between gap-2">
              <div className="truncate text-sm font-medium">{link.remote_hostname || link.local_hostname}</div>
              <Badge variant={link.protocol === 'cdp' ? 'info' : 'default'}>{link.protocol.toUpperCase()}</Badge>
            </div>
            <div className="mt-1 text-[11px] text-muted">
              {link.local_if_name || `ifIndex ${link.local_if_index || '?'}`} → {link.remote_if_name || 'remote port unknown'}
            </div>
            {link.last_seen_at && <div className="mt-1 text-[10px] text-muted">Seen {relativeTime(link.last_seen_at)}</div>}
          </div>
        ))}
      </div>
    </div>
  )
}

function DependencyDialog({ open, onOpenChange, nodes }: { open: boolean; onOpenChange: (open: boolean) => void; nodes: TopologyNode[] }) {
  const qc = useQueryClient()
  const [parent, setParent] = useState('')
  const [child, setChild] = useState('')
  const [type, setType] = useState('uplink')
  const [suppress, setSuppress] = useState(true)
  const [enabled, setEnabled] = useState(true)
  const [notes, setNotes] = useState('')

  const save = useMutation({
    mutationFn: async () => (await api.post('/topology/dependencies', {
      parent_device_id: parent,
      child_device_id: child,
      dependency_type: type,
      suppress_alerts: suppress,
      enabled,
      notes: notes || null,
    })).data,
    onSuccess: () => {
      toast.success('Dependency saved')
      qc.invalidateQueries({ queryKey: ['topology'] })
      onOpenChange(false)
      setParent('')
      setChild('')
      setNotes('')
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>New Dependency</DialogTitle>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            save.mutate()
          }}
        >
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <FormField label="Parent / upstream device" required>
              <DeviceSelect value={parent} onChange={setParent} nodes={nodes} exclude={child} />
            </FormField>
            <FormField label="Child / downstream device" required>
              <DeviceSelect value={child} onChange={setChild} nodes={nodes} exclude={parent} />
            </FormField>
          </div>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <FormField label="Type">
              <Select value={type} onValueChange={setType}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="uplink">Uplink</SelectItem>
                  <SelectItem value="wan">WAN</SelectItem>
                  <SelectItem value="power">Power</SelectItem>
                  <SelectItem value="site">Site</SelectItem>
                  <SelectItem value="service">Service</SelectItem>
                  <SelectItem value="manual">Manual</SelectItem>
                </SelectContent>
              </Select>
            </FormField>
            <ToggleField label="Suppress child alerts" checked={suppress} onChange={setSuppress} />
            <ToggleField label="Enabled" checked={enabled} onChange={setEnabled} />
          </div>
          <FormField label="Notes">
            <Textarea value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Core firewall is upstream of this branch switch..." />
          </FormField>
          <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-xs text-warning">
            When the parent is down, degraded, or unknown, matching child device and service alerts are recorded as suppressed history and notifications are not sent.
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={!parent || !child || parent === child || save.isPending}>
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
              Save dependency
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function DeviceSelect({ value, onChange, nodes, exclude }: { value: string; onChange: (value: string) => void; nodes: TopologyNode[]; exclude?: string }) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger>
        <SelectValue placeholder="Select device" />
      </SelectTrigger>
      <SelectContent>
        {nodes.filter((node) => node.id !== exclude).map((node) => (
          <SelectItem key={node.id} value={node.id}>{node.hostname} · {node.ip_address}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function ToggleField({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <div className="space-y-1.5">
      <div className="text-xs font-medium text-muted">{label}</div>
      <div className="flex h-9 items-center justify-between rounded-md border border-border px-3">
        <span className="text-xs text-text2">{checked ? 'Yes' : 'No'}</span>
        <Switch checked={checked} onCheckedChange={onChange} />
      </div>
    </div>
  )
}

// Tiered hierarchy: derive core→distribution→access tiers from the observed
// LLDP/CDP adjacency (BFS from the highest-degree node), falling back to
// device-type ranking when there are no links. Tiers stack top-to-bottom and
// wrap wide tiers into rows; the canvas height grows to fit (caller reads the
// max y and lets pan/zoom + a stretched viewBox auto-fit it).
function layoutNodes(nodes: TopologyNode[], dependencies: Dependency[], links: TopologyLink[]): PositionedNode[] {
  if (nodes.length === 0) return []
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const adj = new Map<string, Set<string>>()
  for (const n of nodes) adj.set(n.id, new Set())
  for (const l of links) {
    if (!l.target || !byId.has(l.source) || !byId.has(l.target)) continue
    adj.get(l.source)!.add(l.target)
    adj.get(l.target)!.add(l.source)
  }
  // Dependencies also imply hierarchy (parent above child).
  for (const d of dependencies.filter((d) => d.enabled)) {
    if (byId.has(d.parent_device_id) && byId.has(d.child_device_id)) {
      adj.get(d.parent_device_id)!.add(d.child_device_id)
      adj.get(d.child_device_id)!.add(d.parent_device_id)
    }
  }
  const deg = new Map<string, number>(nodes.map((n) => [n.id, adj.get(n.id)!.size]))

  // BFS tiers from highest-degree roots (covers multiple components).
  const tier = new Map<string, number>()
  const order = [...nodes].sort((a, b) => (deg.get(b.id)! - deg.get(a.id)!))
  for (const root of order) {
    if (tier.has(root.id) || deg.get(root.id)! === 0) continue
    tier.set(root.id, 0)
    const queue = [root.id]
    while (queue.length) {
      const cur = queue.shift()!
      for (const nb of adj.get(cur)!) {
        if (!tier.has(nb)) { tier.set(nb, tier.get(cur)! + 1); queue.push(nb) }
      }
    }
  }
  // Isolated nodes (no links) → their own tier below everything, grouped by type.
  const maxTier = tier.size ? Math.max(...tier.values()) : 0
  for (const n of nodes) if (!tier.has(n.id)) tier.set(n.id, maxTier + 1 + Math.min(2, fallbackDepth(n)))

  const groups = new Map<number, TopologyNode[]>()
  for (const n of nodes) {
    const t = tier.get(n.id)!
    if (!groups.has(t)) groups.set(t, [])
    groups.get(t)!.push(n)
  }

  const output: PositionedNode[] = []
  const tiers = Array.from(groups.keys()).sort((a, b) => a - b)
  const perRow = 7
  const rowH = 92
  const tierGap = 70
  let yCursor = 80
  tiers.forEach((t) => {
    const layer = groups.get(t)!.sort((a, b) => (deg.get(b.id)! - deg.get(a.id)!) || a.hostname.localeCompare(b.hostname))
    const rows = Math.ceil(layer.length / perRow)
    layer.forEach((node, i) => {
      const row = Math.floor(i / perRow)
      const col = i % perRow
      const inRow = Math.min(layer.length - row * perRow, perRow)
      const xGap = WIDTH / (inRow + 1)
      output.push({ ...node, depth: t, x: xGap * (col + 1), y: yCursor + row * rowH })
    })
    yCursor += rows * rowH + tierGap
  })
  return output
}

function fallbackDepth(node: TopologyNode) {
  if (node.device_type === 'firewall' || node.device_type === 'router') return 0
  if (node.device_type === 'switch') return 1
  if (node.device_type === 'access_point') return 2
  if (node.device_type === 'server') return 3
  return 4
}

function curvePath(a: { x: number; y: number }, b: { x: number; y: number }, lift = 18) {
  const midY = (a.y + b.y) / 2 - lift
  return `M ${a.x} ${a.y} C ${a.x} ${midY}, ${b.x} ${midY}, ${b.x} ${b.y}`
}

// Path for a manual link in the chosen shape.
function shapePath(shape: string, a: { x: number; y: number }, b: { x: number; y: number }) {
  if (shape === 'straight') return `M ${a.x} ${a.y} L ${b.x} ${b.y}`
  if (shape === 'orthogonal') {
    const my = (a.y + b.y) / 2
    return `M ${a.x} ${a.y} L ${a.x} ${my} L ${b.x} ${my} L ${b.x} ${b.y}`
  }
  return curvePath(a, b, 0)
}

function Metric({ icon: Icon, label, value }: { icon: React.ComponentType<{ className?: string }>; label: string; value: number }) {
  return (
    <Card>
      <CardContent className="flex items-center justify-between gap-3 p-4">
        <div>
          <div className="text-xs font-medium uppercase text-muted">{label}</div>
          <div className="mt-1 text-2xl font-semibold tracking-tight">{value}</div>
        </div>
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Icon className="h-4 w-4" />
        </div>
      </CardContent>
    </Card>
  )
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn('h-2.5 w-2.5 rounded-full border', color)} />
      {label}
    </span>
  )
}

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md border border-border bg-surface px-2 py-1.5">
      <div className="text-[10px] uppercase text-muted">{label}</div>
      <div className="mt-0.5 truncate text-xs font-medium">{value}</div>
    </div>
  )
}

function DependencyMini({ dep, mode }: { dep: Dependency; mode: 'upstream' | 'downstream' }) {
  const name = mode === 'upstream' ? dep.parent_hostname : dep.child_hostname
  const status = mode === 'upstream' ? dep.parent_status : dep.child_status
  return (
    <div className="mb-2 rounded-md border border-border bg-bg p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="truncate text-sm font-medium">{name}</div>
        <span className={cn('rounded-full border px-1.5 py-0.5 text-[10px] capitalize', statusTone[status] || statusTone.unknown)}>
          {status}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        <Badge variant={dep.enabled ? 'success' : 'outline'}>{dep.enabled ? 'enabled' : 'disabled'}</Badge>
        <Badge variant={dep.suppress_alerts ? 'warning' : 'outline'}>{dep.suppress_alerts ? 'suppression' : 'observe'}</Badge>
      </div>
    </div>
  )
}
