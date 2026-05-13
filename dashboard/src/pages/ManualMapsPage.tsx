import { useEffect, useMemo, useRef, useState, type ComponentType, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Cable,
  CheckCircle2,
  GitBranch,
  HardDrive,
  Layers,
  Loader2,
  MapPinned,
  Monitor,
  Network,
  Plus,
  Printer,
  RefreshCw,
  Router,
  Save,
  Server,
  Shield,
  Sparkles,
  Trash2,
  Wifi,
  XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'

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
}

type ManualMapLink = {
  id: string
  map_id: string
  source_node_id: string
  target_node_id: string
  label?: string | null
  link_type: string
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

const iconOptions: Array<{ value: string; label: string; icon: ComponentType<{ className?: string }> }> = [
  { value: 'auto', label: 'Auto', icon: Sparkles },
  { value: 'router', label: 'Router', icon: Router },
  { value: 'switch', label: 'Switch', icon: Network },
  { value: 'firewall', label: 'Firewall', icon: Shield },
  { value: 'server', label: 'Server', icon: Server },
  { value: 'access_point', label: 'Access point', icon: Wifi },
  { value: 'printer', label: 'Printer', icon: Printer },
  { value: 'storage', label: 'Storage', icon: HardDrive },
]

const deviceIcons: Record<string, ComponentType<{ className?: string }>> = {
  router: Router,
  switch: Network,
  firewall: Shield,
  server: Server,
  access_point: Wifi,
  printer: Printer,
  storage: HardDrive,
  other: HardDrive,
}

const statusStyles: Record<string, {
  badge: 'success' | 'warning' | 'danger' | 'info' | 'outline'
  dot: string
  node: string
  line: string
}> = {
  up: {
    badge: 'success',
    dot: 'bg-success',
    node: 'border-success/55 bg-success/10 shadow-success/10',
    line: 'stroke-success/45',
  },
  down: {
    badge: 'danger',
    dot: 'bg-danger',
    node: 'border-danger/65 bg-danger/10 shadow-danger/10',
    line: 'stroke-danger/55',
  },
  degraded: {
    badge: 'warning',
    dot: 'bg-warning',
    node: 'border-warning/65 bg-warning/10 shadow-warning/10',
    line: 'stroke-warning/55',
  },
  maintenance: {
    badge: 'info',
    dot: 'bg-info',
    node: 'border-info/55 bg-info/10 shadow-info/10',
    line: 'stroke-info/45',
  },
  unknown: {
    badge: 'outline',
    dot: 'bg-muted',
    node: 'border-border bg-surface/95 shadow-black/5',
    line: 'stroke-muted/35',
  },
}

const defaultNewMap = { name: '', description: '' }
const defaultNewNode = { device_id: '', icon: 'auto' }
const defaultNewLink = { source_node_id: '', target_node_id: '', label: '' }

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value))
}

function styleForStatus(status?: NodeStatus) {
  return statusStyles[String(status || 'unknown').toLowerCase()] || statusStyles.unknown
}

function iconForNode(node: Pick<ManualMapNode, 'icon' | 'device_type'>) {
  if (node.icon && node.icon !== 'auto') return deviceIcons[node.icon] || HardDrive
  return deviceIcons[node.device_type] || HardDrive
}

function statusLabel(status?: NodeStatus) {
  const value = String(status || 'unknown')
  return value.replace(/_/g, ' ')
}

export function ManualMapsPage() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const selectedMapId = params.get('map')

  const [createOpen, setCreateOpen] = useState(false)
  const [addNodeOpen, setAddNodeOpen] = useState(false)
  const [linkOpen, setLinkOpen] = useState(false)
  const [deleteMapOpen, setDeleteMapOpen] = useState(false)
  const [newMap, setNewMap] = useState(defaultNewMap)
  const [newNode, setNewNode] = useState(defaultNewNode)
  const [newLink, setNewLink] = useState(defaultNewLink)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [labelDraft, setLabelDraft] = useState('')
  const [draggingNodeId, setDraggingNodeId] = useState<string | null>(null)
  const [draftPositions, setDraftPositions] = useState<Record<string, { x_pct: number; y_pct: number }>>({})
  const canvasRef = useRef<HTMLDivElement>(null)

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
    refetchInterval: 20_000,
  })

  const devicesQuery = useQuery<{ data: Device[] }>({
    queryKey: ['devices', 'manual-map-picker'],
    queryFn: async () => (await api.get('/devices?limit=500')).data,
    refetchInterval: 30_000,
  })

  function selectMap(id: string | null) {
    const next = new URLSearchParams(params)
    if (id) next.set('map', id)
    else next.delete('map')
    setParams(next, { replace: true })
    setSelectedNodeId(null)
  }

  useEffect(() => {
    if (mapsQuery.isLoading) return
    if (maps.length === 0 && selectedMapId) {
      selectMap(null)
      return
    }
    if (maps.length > 0 && (!selectedMapId || !maps.some((map) => map.id === selectedMapId))) {
      selectMap(maps[0].id)
    }
  }, [mapsQuery.isLoading, maps, selectedMapId])

  const detail = mapQuery.data || null
  const nodes = detail?.nodes || []
  const links = detail?.links || []
  const nodeMap = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes])
  const selectedNode = selectedNodeId ? nodeMap.get(selectedNodeId) || null : null
  const currentMap = selectedMapId ? maps.find((map) => map.id === selectedMapId) || detail : null
  const selectedNodePosition = selectedNode ? draftPositions[selectedNode.id] || selectedNode : null

  useEffect(() => {
    setLabelDraft(selectedNode?.label || '')
  }, [selectedNode?.id, selectedNode?.label])

  useEffect(() => {
    if (selectedNodeId && !nodeMap.has(selectedNodeId)) setSelectedNodeId(null)
  }, [nodeMap, selectedNodeId])

  const devices = devicesQuery.data?.data || []
  const usedDeviceIds = useMemo(() => new Set(nodes.map((node) => node.device_id)), [nodes])
  const availableDevices = devices.filter((device) => !usedDeviceIds.has(device.id))

  const createMap = useMutation({
    mutationFn: async () => (await api.post('/maps', {
      name: newMap.name.trim(),
      description: newMap.description.trim() || null,
    })).data as ManualMapListItem,
    onSuccess: (created) => {
      toast.success('Map created')
      setCreateOpen(false)
      setNewMap(defaultNewMap)
      qc.invalidateQueries({ queryKey: ['manual-maps'] })
      selectMap(created.id)
    },
    onError: (e: any) => toast.error('Create failed', apiErrorMessage(e)),
  })

  const addNode = useMutation({
    mutationFn: async () => {
      if (!selectedMapId) throw new Error('No map selected')
      const index = nodes.length
      return (await api.post(`/maps/${selectedMapId}/nodes`, {
        device_id: newNode.device_id,
        icon: newNode.icon,
        x_pct: clamp(18 + (index % 4) * 20, 8, 92),
        y_pct: clamp(22 + (Math.floor(index / 4) % 3) * 24, 10, 90),
      })).data
    },
    onSuccess: (created) => {
      toast.success('Device added')
      setAddNodeOpen(false)
      setNewNode(defaultNewNode)
      if (created?.id) setSelectedNodeId(created.id)
      invalidateMap(selectedMapId)
    },
    onError: (e: any) => toast.error('Add device failed', apiErrorMessage(e)),
  })

  const updateNode = useMutation({
    mutationFn: async ({ id, patch }: { id: string; patch: Partial<Pick<ManualMapNode, 'label' | 'icon' | 'x_pct' | 'y_pct'>> }) => {
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
    mutationFn: async () => {
      if (!selectedMapId) throw new Error('No map selected')
      return (await api.post(`/maps/${selectedMapId}/links`, {
        source_node_id: newLink.source_node_id,
        target_node_id: newLink.target_node_id,
        label: newLink.label.trim() || null,
        link_type: 'manual',
      })).data
    },
    onSuccess: () => {
      toast.success('Link added')
      setLinkOpen(false)
      setNewLink(defaultNewLink)
      invalidateMap(selectedMapId)
    },
    onError: (e: any) => toast.error('Link failed', apiErrorMessage(e)),
  })

  const deleteLink = useMutation({
    mutationFn: async (id: string) => {
      if (!selectedMapId) throw new Error('No map selected')
      await api.delete(`/maps/${selectedMapId}/links/${id}`)
    },
    onSuccess: () => {
      toast.success('Link removed')
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

  function invalidateMap(id: string | null) {
    qc.invalidateQueries({ queryKey: ['manual-maps'] })
    if (id) qc.invalidateQueries({ queryKey: ['manual-map', id] })
  }

  function positionFor(node: ManualMapNode) {
    return draftPositions[node.id] || { x_pct: node.x_pct, y_pct: node.y_pct }
  }

  function pointFromEvent(event: ReactPointerEvent) {
    const rect = canvasRef.current?.getBoundingClientRect()
    if (!rect) return null
    return {
      x_pct: clamp(((event.clientX - rect.left) / rect.width) * 100, 5, 95),
      y_pct: clamp(((event.clientY - rect.top) / rect.height) * 100, 7, 93),
    }
  }

  function beginDrag(event: ReactPointerEvent<HTMLButtonElement>, node: ManualMapNode) {
    setSelectedNodeId(node.id)
    setDraggingNodeId(node.id)
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  function moveDrag(event: ReactPointerEvent) {
    if (!draggingNodeId) return
    const point = pointFromEvent(event)
    if (!point) return
    setDraftPositions((prev) => ({ ...prev, [draggingNodeId]: point }))
  }

  function finishDrag() {
    if (!draggingNodeId) return
    const point = draftPositions[draggingNodeId]
    const node = nodeMap.get(draggingNodeId)
    setDraggingNodeId(null)
    if (!point || !node) return
    if (Math.abs(point.x_pct - node.x_pct) < 0.2 && Math.abs(point.y_pct - node.y_pct) < 0.2) return
    updateNode.mutate({ id: node.id, patch: point })
  }

  function saveLabel() {
    if (!selectedNode) return
    const next = labelDraft.trim()
    if (next && next !== selectedNode.label) {
      updateNode.mutate({ id: selectedNode.id, patch: { label: next } })
    }
  }

  const downCount = Number(detail?.summary.status_counts.down || currentMap?.status_counts.down || 0)
  const degradedCount = Number(detail?.summary.status_counts.degraded || currentMap?.status_counts.degraded || 0)

  if (mapsQuery.error) {
    return (
      <div className="rounded-lg border border-danger/30 bg-danger/10 p-4 text-sm text-danger">
        Failed to load maps: {apiErrorMessage(mapsQuery.error as any)}
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
            <h1 className="text-xl font-semibold tracking-tight">Manual Maps</h1>
            <p className="max-w-3xl text-xs text-muted">
              Device-backed map canvases with live status coloring.
            </p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" asChild>
            <Link to="/maps/automated">
              <GitBranch className="h-4 w-4" />
              Automated
            </Link>
          </Button>
          <Button variant="outline" onClick={() => invalidateMap(selectedMapId)} disabled={mapQuery.isFetching}>
            {mapQuery.isFetching ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            Refresh
          </Button>
          <Button onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            New Map
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Metric icon={MapPinned} label="Maps" value={maps.length} />
        <Metric icon={Monitor} label="Devices" value={detail?.summary.nodes || currentMap?.node_count || 0} />
        <Metric icon={Cable} label="Links" value={detail?.summary.links || currentMap?.link_count || 0} />
        <Metric icon={XCircle} label="Needs Attention" value={downCount + degradedCount} tone={downCount > 0 ? 'danger' : degradedCount > 0 ? 'warning' : 'success'} />
      </div>

      <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[minmax(0,1fr)_390px]">
        <Card>
          <CardContent className="p-0">
            <div className="flex flex-col gap-2 border-b border-border p-4 md:flex-row md:items-center md:justify-between">
              <div>
                <h2 className="text-sm font-semibold">{currentMap?.name || 'Map Canvas'}</h2>
                <p className="text-xs text-muted">
                  {mapQuery.isLoading ? 'Loading map' : `${nodes.length} devices · ${links.length} links`}
                  {detail?.summary.generated_at ? ` · updated ${relativeTime(detail.summary.generated_at)}` : ''}
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" size="sm" onClick={() => setAddNodeOpen(true)} disabled={!selectedMapId}>
                  <Plus className="h-4 w-4" />
                  Device
                </Button>
                <Button variant="outline" size="sm" onClick={() => setLinkOpen(true)} disabled={!selectedMapId || nodes.length < 2}>
                  <Cable className="h-4 w-4" />
                  Link
                </Button>
              </div>
            </div>

            <div
              ref={canvasRef}
              onPointerMove={moveDrag}
              onPointerUp={finishDrag}
              onPointerCancel={finishDrag}
              className="relative h-[650px] overflow-hidden rounded-b-lg bg-[radial-gradient(circle_at_14%_18%,rgba(59,130,246,0.09),transparent_28%),linear-gradient(180deg,rgba(148,163,184,0.07),transparent)]"
            >
              <div className="absolute inset-0 bg-[linear-gradient(to_right,rgba(148,163,184,0.09)_1px,transparent_1px),linear-gradient(to_bottom,rgba(148,163,184,0.09)_1px,transparent_1px)] bg-[size:44px_44px]" />
              <svg className="absolute inset-0 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none" role="img">
                {links.map((link) => {
                  const source = nodeMap.get(link.source_node_id)
                  const target = nodeMap.get(link.target_node_id)
                  if (!source || !target) return null
                  const a = positionFor(source)
                  const b = positionFor(target)
                  const linkTone = source.status === 'down' || target.status === 'down'
                    ? statusStyles.down.line
                    : source.status === 'degraded' || target.status === 'degraded'
                      ? statusStyles.degraded.line
                      : statusStyles.up.line
                  return (
                    <line
                      key={link.id}
                      x1={a.x_pct}
                      y1={a.y_pct}
                      x2={b.x_pct}
                      y2={b.y_pct}
                      vectorEffect="non-scaling-stroke"
                      className={cn('stroke-[2.5]', linkTone)}
                    />
                  )
                })}
              </svg>

              {mapQuery.isLoading ? (
                <div className="absolute inset-0 flex items-center justify-center">
                  <div className="rounded-lg border border-border bg-surface/95 px-4 py-3 text-sm text-muted shadow-lg">
                    <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />
                    Loading map
                  </div>
                </div>
              ) : !selectedMapId || maps.length === 0 ? (
                <EmptyCanvas
                  title="No manual maps"
                  action={<Button onClick={() => setCreateOpen(true)}><Plus className="h-4 w-4" />New Map</Button>}
                />
              ) : nodes.length === 0 ? (
                <EmptyCanvas
                  title="Empty map"
                  action={<Button onClick={() => setAddNodeOpen(true)}><Plus className="h-4 w-4" />Device</Button>}
                />
              ) : (
                nodes.map((node) => (
                  <ManualNodeCard
                    key={node.id}
                    node={node}
                    position={positionFor(node)}
                    selected={selectedNodeId === node.id}
                    onPointerDown={(event) => beginDrag(event, node)}
                  />
                ))
              )}
            </div>
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardContent className="space-y-3 p-4">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <h2 className="text-sm font-semibold">MAP Library</h2>
                  <p className="text-xs text-muted">{maps.length} saved maps</p>
                </div>
                <Button variant="outline" size="sm" onClick={() => setCreateOpen(true)}>
                  <Plus className="h-4 w-4" />
                </Button>
              </div>
              <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
                {mapsQuery.isLoading ? (
                  <div className="rounded-md border border-border bg-surface2/50 p-3 text-sm text-muted">Loading maps</div>
                ) : maps.length === 0 ? (
                  <div className="rounded-md border border-dashed border-border p-4 text-center text-sm text-muted">No saved maps</div>
                ) : (
                  maps.map((map) => (
                    <button
                      key={map.id}
                      type="button"
                      onClick={() => selectMap(map.id)}
                      className={cn(
                        'w-full rounded-md border p-3 text-left transition-colors',
                        selectedMapId === map.id
                          ? 'border-primary/50 bg-primary/10'
                          : 'border-border bg-surface hover:bg-surface2',
                      )}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-medium">{map.name}</div>
                          <div className="mt-0.5 text-[11px] text-muted">
                            {map.node_count} devices · {map.link_count} links
                          </div>
                        </div>
                        <StatusMini counts={map.status_counts} />
                      </div>
                    </button>
                  ))
                )}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardContent className="space-y-4 p-4">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <h2 className="text-sm font-semibold">Selection</h2>
                  <p className="text-xs text-muted">{selectedNode ? selectedNode.hostname : currentMap?.name || 'No map selected'}</p>
                </div>
                {selectedNode && <Badge variant={styleForStatus(selectedNode.status).badge}>{statusLabel(selectedNode.status)}</Badge>}
              </div>

              {selectedNode ? (
                <div className="space-y-3">
                  <FormField label="Label">
                    <Input
                      value={labelDraft}
                      onChange={(event) => setLabelDraft(event.target.value)}
                      onBlur={saveLabel}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.currentTarget.blur()
                        }
                      }}
                    />
                  </FormField>
                  <FormField label="Icon">
                    <Select
                      value={selectedNode.icon || 'auto'}
                      onValueChange={(value) => updateNode.mutate({ id: selectedNode.id, patch: { icon: value } })}
                    >
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        {iconOptions.map((option) => (
                          <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </FormField>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <InfoTile label="IP Address" value={selectedNode.ip_address} />
                    <InfoTile label="Type" value={selectedNode.device_type || 'other'} />
                    <InfoTile label="Vendor" value={selectedNode.vendor || '-'} />
                    <InfoTile label="Model" value={selectedNode.model || '-'} />
                    <InfoTile label="Position" value={`${selectedNodePosition ? selectedNodePosition.x_pct.toFixed(1) : selectedNode.x_pct.toFixed(1)}%, ${selectedNodePosition ? selectedNodePosition.y_pct.toFixed(1) : selectedNode.y_pct.toFixed(1)}%`} />
                    <InfoTile label="Last Seen" value={selectedNode.last_seen ? relativeTime(selectedNode.last_seen) : 'Never'} />
                  </div>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" asChild className="flex-1">
                      <Link to={`/devices/${selectedNode.device_id}`}>Open Device</Link>
                    </Button>
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={() => deleteNode.mutate(selectedNode.id)}
                      disabled={deleteNode.isPending}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="rounded-md border border-dashed border-border p-5 text-center text-sm text-muted">
                  Select a device node
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="space-y-3 p-4">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <h2 className="text-sm font-semibold">Links</h2>
                  <p className="text-xs text-muted">{links.length} manual connections</p>
                </div>
                <Button variant="outline" size="sm" onClick={() => setLinkOpen(true)} disabled={!selectedMapId || nodes.length < 2}>
                  <Cable className="h-4 w-4" />
                </Button>
              </div>
              <div className="max-h-52 space-y-2 overflow-y-auto pr-1">
                {links.length === 0 ? (
                  <div className="rounded-md border border-dashed border-border p-4 text-center text-sm text-muted">No links</div>
                ) : (
                  links.map((link) => {
                    const source = nodeMap.get(link.source_node_id)
                    const target = nodeMap.get(link.target_node_id)
                    return (
                      <div key={link.id} className="flex items-center gap-2 rounded-md border border-border bg-surface2/40 p-2">
                        <Cable className="h-4 w-4 text-muted" />
                        <div className="min-w-0 flex-1 text-xs">
                          <div className="truncate font-medium">{link.label || `${source?.label || 'Source'} to ${target?.label || 'Target'}`}</div>
                          <div className="truncate text-muted">{source?.hostname || 'Source'} · {target?.hostname || 'Target'}</div>
                        </div>
                        <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => deleteLink.mutate(link.id)}>
                          <Trash2 className="h-3.5 w-3.5 text-danger" />
                        </Button>
                      </div>
                    )
                  })
                )}
              </div>
              {selectedMapId && (
                <Button variant="outline" size="sm" className="w-full text-danger hover:text-danger" onClick={() => setDeleteMapOpen(true)}>
                  <Trash2 className="h-4 w-4" />
                  Delete Map
                </Button>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create Manual Map</DialogTitle>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              if (!newMap.name.trim()) return
              createMap.mutate()
            }}
          >
            <FormField label="Name" required>
              <Input value={newMap.name} onChange={(event) => setNewMap((prev) => ({ ...prev, name: event.target.value }))} autoFocus />
            </FormField>
            <FormField label="Description">
              <Textarea value={newMap.description} onChange={(event) => setNewMap((prev) => ({ ...prev, description: event.target.value }))} rows={3} />
            </FormField>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setCreateOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={createMap.isPending || !newMap.name.trim()}>
                {createMap.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                Create
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={addNodeOpen} onOpenChange={setAddNodeOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Device</DialogTitle>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              if (!newNode.device_id) return
              addNode.mutate()
            }}
          >
            <FormField label="Device" required>
              <Select value={newNode.device_id} onValueChange={(value) => setNewNode((prev) => ({ ...prev, device_id: value }))}>
                <SelectTrigger><SelectValue placeholder="Select device" /></SelectTrigger>
                <SelectContent>
                  {availableDevices.map((device) => (
                    <SelectItem key={device.id} value={device.id}>
                      {device.hostname} · {device.ip_address}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Icon">
              <Select value={newNode.icon} onValueChange={(value) => setNewNode((prev) => ({ ...prev, icon: value }))}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {iconOptions.map((option) => (
                    <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            {availableDevices.length === 0 && (
              <div className="rounded-md border border-border bg-surface2/60 p-3 text-sm text-muted">
                All devices are already on this map.
              </div>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setAddNodeOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={addNode.isPending || !newNode.device_id}>
                {addNode.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                Add
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={linkOpen} onOpenChange={setLinkOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create Link</DialogTitle>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault()
              if (!newLink.source_node_id || !newLink.target_node_id || newLink.source_node_id === newLink.target_node_id) return
              createLink.mutate()
            }}
          >
            <FormField label="Source" required>
              <Select value={newLink.source_node_id} onValueChange={(value) => setNewLink((prev) => ({ ...prev, source_node_id: value }))}>
                <SelectTrigger><SelectValue placeholder="Select source" /></SelectTrigger>
                <SelectContent>
                  {nodes.map((node) => (
                    <SelectItem key={node.id} value={node.id}>{node.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Target" required>
              <Select value={newLink.target_node_id} onValueChange={(value) => setNewLink((prev) => ({ ...prev, target_node_id: value }))}>
                <SelectTrigger><SelectValue placeholder="Select target" /></SelectTrigger>
                <SelectContent>
                  {nodes.filter((node) => node.id !== newLink.source_node_id).map((node) => (
                    <SelectItem key={node.id} value={node.id}>{node.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Label">
              <Input value={newLink.label} onChange={(event) => setNewLink((prev) => ({ ...prev, label: event.target.value }))} />
            </FormField>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setLinkOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={createLink.isPending || !newLink.source_node_id || !newLink.target_node_id || newLink.source_node_id === newLink.target_node_id}>
                {createLink.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Cable className="h-4 w-4" />}
                Create
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

function ManualNodeCard({
  node,
  position,
  selected,
  onPointerDown,
}: {
  node: ManualMapNode
  position: { x_pct: number; y_pct: number }
  selected: boolean
  onPointerDown: (event: ReactPointerEvent<HTMLButtonElement>) => void
}) {
  const Icon = iconForNode(node)
  const style = styleForStatus(node.status)
  return (
    <button
      type="button"
      onPointerDown={onPointerDown}
      className={cn(
        'absolute z-10 w-44 -translate-x-1/2 -translate-y-1/2 rounded-lg border p-3 text-left shadow-lg backdrop-blur transition',
        'hover:scale-[1.02] hover:shadow-xl active:cursor-grabbing',
        style.node,
        selected && 'ring-2 ring-primary/45',
      )}
      style={{ left: `${position.x_pct}%`, top: `${position.y_pct}%` }}
      title={node.hostname}
    >
      <div className="flex items-start gap-2.5">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-current/20 bg-surface/80">
          <Icon className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold text-text">{node.label || node.hostname}</div>
          <div className="mt-0.5 truncate text-[11px] text-muted">{node.ip_address}</div>
          <div className="mt-2 flex items-center gap-1.5">
            <span className={cn('h-2 w-2 rounded-full', style.dot)} />
            <span className="truncate text-[11px] font-medium capitalize text-text2">{statusLabel(node.status)}</span>
          </div>
        </div>
      </div>
    </button>
  )
}

function Metric({
  icon: Icon,
  label,
  value,
  tone = 'default',
}: {
  icon: ComponentType<{ className?: string }>
  label: string
  value: number | string
  tone?: 'default' | 'success' | 'warning' | 'danger'
}) {
  const toneClass = tone === 'danger'
    ? 'bg-danger/10 text-danger'
    : tone === 'warning'
      ? 'bg-warning/10 text-warning'
      : tone === 'success'
        ? 'bg-success/10 text-success'
        : 'bg-primary/10 text-primary'
  return (
    <Card>
      <CardContent className="flex items-center gap-3 p-4">
        <div className={cn('flex h-9 w-9 items-center justify-center rounded-lg', toneClass)}>
          <Icon className="h-4 w-4" />
        </div>
        <div>
          <div className="text-lg font-semibold leading-none">{value}</div>
          <div className="mt-1 text-[11px] text-muted">{label}</div>
        </div>
      </CardContent>
    </Card>
  )
}

function StatusMini({ counts }: { counts?: Record<string, number> }) {
  const down = Number(counts?.down || 0)
  const degraded = Number(counts?.degraded || 0)
  const up = Number(counts?.up || 0)
  if (down > 0) return <Badge variant="danger">{down} down</Badge>
  if (degraded > 0) return <Badge variant="warning">{degraded} warning</Badge>
  if (up > 0) return <Badge variant="success">{up} up</Badge>
  return <Badge variant="outline">empty</Badge>
}

function InfoTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-surface2/40 p-2">
      <div className="text-[10px] uppercase tracking-[0.08em] text-muted">{label}</div>
      <div className="mt-1 truncate text-xs font-medium text-text">{value}</div>
    </div>
  )
}

function EmptyCanvas({ title, action }: { title: string; action: ReactNode }) {
  return (
    <div className="absolute inset-0 flex items-center justify-center">
      <div className="rounded-lg border border-border bg-surface/95 p-6 text-center shadow-lg">
        <Layers className="mx-auto h-8 w-8 text-primary" />
        <div className="mt-3 text-sm font-semibold">{title}</div>
        <div className="mt-4">{action}</div>
      </div>
    </div>
  )
}
