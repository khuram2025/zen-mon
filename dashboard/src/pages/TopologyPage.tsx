import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Box,
  Cable,
  CheckCircle2,
  GitBranch,
  HardDrive,
  Loader2,
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
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
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
}

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

  const topology = useQuery<TopologyData>({
    queryKey: ['topology', 'map'],
    queryFn: async () => (await api.get('/topology/map')).data,
    refetchInterval: 30_000,
  })

  const discover = useMutation({
    mutationFn: async () => (await api.post('/topology/discover', { auto_dependencies: true })).data,
    onSuccess: (data) => {
      toast.success('Topology discovery complete', `${data.links_found || 0} LLDP/CDP links found`)
      qc.invalidateQueries({ queryKey: ['topology'] })
    },
    onError: (e: any) => toast.error('Discovery failed', apiErrorMessage(e)),
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
  const positioned = useMemo(() => layoutNodes(nodes, dependencies), [nodes, dependencies])
  const nodeMap = useMemo(() => new Map(positioned.map((n) => [n.id, n])), [positioned])
  const selected = selectedId ? nodeMap.get(selectedId) || null : null
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
        <Card>
          <CardContent className="p-0">
            <div className="flex flex-col gap-2 border-b border-border p-4 md:flex-row md:items-center md:justify-between">
              <div>
                <h2 className="text-sm font-semibold">Network Map</h2>
                <p className="text-xs text-muted">
                  {topology.isLoading ? 'Loading topology' : `${positioned.length} nodes · ${links.length} observed links · ${dependencies.length} dependencies`}
                </p>
              </div>
              <div className="flex flex-wrap gap-2 text-[11px]">
                <Legend color="border-primary/60 bg-primary/10" label="LLDP/CDP" />
                <Legend color="border-warning/70 bg-warning/10" label="Dependency" />
                <Legend color="border-danger/70 bg-danger/10" label="Downstream suppressed" />
              </div>
            </div>

            <div className="relative h-[680px] overflow-hidden rounded-b-lg bg-[radial-gradient(circle_at_20%_20%,rgba(59,130,246,0.08),transparent_26%),linear-gradient(180deg,rgba(148,163,184,0.06),transparent)]">
              <div className="absolute inset-0 bg-[linear-gradient(to_right,rgba(148,163,184,0.08)_1px,transparent_1px),linear-gradient(to_bottom,rgba(148,163,184,0.08)_1px,transparent_1px)] bg-[size:42px_42px]" />
              <svg className="absolute inset-0 h-full w-full" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img">
                <defs>
                  <marker id="depArrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                    <path d="M 0 0 L 10 5 L 0 10 z" className="fill-warning" />
                  </marker>
                </defs>
                {links.filter((link) => link.target && nodeMap.has(link.source) && nodeMap.has(link.target)).map((link) => {
                  const a = nodeMap.get(link.source)!
                  const b = nodeMap.get(link.target!)!
                  return (
                    <path
                      key={link.id}
                      d={curvePath(a, b)}
                      className={cn(
                        'fill-none stroke-[2.2]',
                        link.protocol === 'cdp' ? 'stroke-info/45' : 'stroke-primary/45',
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
              ) : (
                positioned.map((node) => (
                  <TopologyNodeCard
                    key={node.id}
                    node={node}
                    selected={selectedId === node.id}
                    onSelect={() => setSelectedId(node.id)}
                  />
                ))
              )}
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

function TopologyNodeCard({ node, selected, onSelect }: { node: PositionedNode; selected: boolean; onSelect: () => void }) {
  const Icon = deviceIcons[node.device_type] || deviceIcons.other
  const risk = node.status === 'down' || node.status === 'degraded'
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        'absolute z-10 w-[176px] -translate-x-1/2 -translate-y-1/2 rounded-lg border bg-surface/95 p-2 text-left shadow-lg backdrop-blur transition-all hover:-translate-y-[53%] hover:border-primary/50',
        selected ? 'border-primary ring-2 ring-primary/20' : 'border-border',
        risk && 'border-danger/60',
      )}
      style={{ left: `${(node.x / WIDTH) * 100}%`, top: `${(node.y / HEIGHT) * 100}%` }}
    >
      <div className="flex items-center gap-2">
        <div className={cn('flex h-8 w-8 shrink-0 items-center justify-center rounded-md border', statusTone[node.status] || statusTone.unknown)}>
          <Icon className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-semibold text-text">{node.hostname}</div>
          <div className="truncate font-mono text-[10px] text-muted">{node.ip_address}</div>
        </div>
      </div>
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className={cn('rounded-full border px-1.5 py-0.5 text-[10px] font-medium capitalize', statusTone[node.status] || statusTone.unknown)}>
          {node.status}
        </span>
        {node.is_dependency_parent && <ShieldAlert className="h-3.5 w-3.5 text-warning" />}
        {!node.is_mapped && <span className="text-[10px] text-muted">unmapped</span>}
      </div>
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

function layoutNodes(nodes: TopologyNode[], dependencies: Dependency[]): PositionedNode[] {
  const depth = new Map<string, number>()
  for (const node of nodes) {
    depth.set(node.id, fallbackDepth(node))
  }
  for (let i = 0; i < 8; i++) {
    let changed = false
    for (const dep of dependencies.filter((d) => d.enabled)) {
      const parentDepth = depth.get(dep.parent_device_id) ?? 0
      const childDepth = depth.get(dep.child_device_id) ?? 1
      if (childDepth <= parentDepth) {
        depth.set(dep.child_device_id, Math.min(4, parentDepth + 1))
        changed = true
      }
      if ((depth.get(dep.parent_device_id) ?? 0) > 2) {
        depth.set(dep.parent_device_id, 0)
        changed = true
      }
    }
    if (!changed) break
  }

  const groups = new Map<number, TopologyNode[]>()
  for (const node of nodes) {
    const d = Math.min(4, Math.max(0, depth.get(node.id) ?? 3))
    const arr = groups.get(d) || []
    arr.push(node)
    groups.set(d, arr)
  }

  const output: PositionedNode[] = []
  const sortedDepths = Array.from(groups.keys()).sort((a, b) => a - b)
  const layerGap = HEIGHT / Math.max(2, sortedDepths.length + 1)
  sortedDepths.forEach((d, layerIndex) => {
    const layer = (groups.get(d) || []).sort((a, b) => a.hostname.localeCompare(b.hostname))
    const y = Math.min(HEIGHT - 70, Math.max(70, layerGap * (layerIndex + 1)))
    layer.forEach((node, idx) => {
      const cols = Math.min(layer.length, 6)
      const col = idx % cols
      const row = Math.floor(idx / cols)
      const xGap = WIDTH / (cols + 1)
      const rowOffset = row * 72
      output.push({
        ...node,
        depth: d,
        x: xGap * (col + 1),
        y: Math.min(HEIGHT - 70, y + rowOffset),
      })
    })
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
