/** On-demand, connection-level network capture with interface traffic context. */

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, AlertTriangle, Archive, ArchiveRestore, ArrowDown, ArrowRight,
  ArrowUp, CircleStop, Clock3, Info, Network, Play, Radio, RotateCcw,
  Search, Trash2,
} from 'lucide-react'
import {
  Area, AreaChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import {
  apiErrorMessage, cn, formatBps, formatBpsAxis, formatBytes, relativeTime,
} from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Skeleton } from '@/components/ui/Skeleton'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import {
  EmptyState, ExportCsvButton, QueryError, TablePager, TableStateRow,
} from '@/components/servers/tables'
import type { AgentStatus, OsType, ServerNetworkInterface } from '@/types/servers'

type CaptureStatus =
  | 'queued'
  | 'running'
  | 'stopping'
  | 'completed'
  | 'cancelled'
  | 'failed'
  | 'expired'

interface Capture {
  id: string
  status: CaptureStatus
  interface: string | null
  duration_s: number
  requested_at: string
  started_at: string | null
  ends_at: string | null
  completed_at: string | null
  samples: number
  flow_count: number
  bytes_sent: number
  bytes_received: number
  bytes_available: boolean
  truncated: boolean
  note: string | null
  error_message: string | null
  requested_by_name: string | null
  retention_s: number
  purge_after: string | null
  archived_at: string | null
}

interface Flow {
  protocol: string
  local_ip: string
  local_port: number
  remote_ip: string
  remote_port: number
  pid: number
  process_name: string
  service_name: string
  state: string
  bytes_sent: number
  bytes_received: number
  bytes_known: boolean
  first_seen: string
  last_seen: string
  samples: number
  direction?: 'inbound' | 'outbound' | 'local' | 'unknown'
  kind?: 'connection' | 'listener' | 'endpoint' | 'unknown'
}

interface FlowResponse {
  items: Flow[]
  total: number
  page: number
  page_size: number
  bytes_available: boolean
}

interface TrafficSample {
  timestamp: string
  rx_bps: number
  tx_bps: number
  rx_utilization_pct: number | null
  tx_utilization_pct: number | null
  utilization_pct: number | null
}

interface TrafficStats {
  rx_bps: number
  tx_bps: number
  total_bps?: number
  rx_utilization_pct?: number | null
  tx_utilization_pct?: number | null
  utilization_pct: number | null
}

interface InterfaceTraffic {
  interface?: string
  link_speed_bps: number | null
  samples: TrafficSample[]
  current: TrafficStats
  avg: TrafficStats
  peak: TrafficStats
  p95: TrafficStats
}

interface TrafficResponse {
  capture_id: string
  status: CaptureStatus
  started_at: string | null
  ends_at: string | null
  interfaces: InterfaceTraffic[]
  aggregate?: InterfaceTraffic | null
}

/** Windows enumerates every NDIS filter layer, WAN miniport and tunnel as its
 *  own interface, so a host with one NIC reports ~24. The filter layers even
 *  echo the parent's link speed, which reads as duplicated traffic. Keep the
 *  ones an operator would recognise as a network connection. */
const PSEUDO_INTERFACE_RE = new RegExp(
  [
    'loopback', 'pseudo-interface', 'isatap', 'teredo', '6to4', 'ip-https',
    'kernel debugger', 'wan miniport', 'local area connection\\*',
    // NDIS filter layers attached to a real adapter.
    'wfp ', 'lightweight filter', 'qos packet scheduler', 'native mac layer',
    '802\\.3 mac layer',
  ].join('|'),
  'i',
)

export function isRealInterface(name: string | null | undefined, linkSpeedBps?: number | null): boolean {
  const value = (name || '').trim()
  if (!value) return false
  if (PSEUDO_INTERFACE_RE.test(value)) return false
  // A genuine NIC that is administratively down still has a link speed of 0,
  // so speed alone cannot decide this — it only breaks ties for odd names.
  return true
}

const DURATIONS = [
  { value: '60', label: '1 minute' },
  { value: '300', label: '5 minutes' },
  { value: '900', label: '15 minutes' },
  { value: '1800', label: '30 minutes' },
  { value: '3600', label: '1 hour' },
]

const RETENTION_WINDOWS = [
  { value: '900', label: '15 minutes' },
  { value: '3600', label: '1 hour' },
  { value: '21600', label: '6 hours' },
  { value: '86400', label: '24 hours' },
  { value: '604800', label: '7 days' },
]

const FLOW_CSV = [
  { header: 'Protocol', value: (flow: Flow) => flow.protocol },
  { header: 'Kind', value: (flow: Flow) => flow.kind || 'connection' },
  { header: 'Direction', value: (flow: Flow) => inferredDirection(flow) },
  { header: 'Local process', value: (flow: Flow) => flow.process_name },
  { header: 'Local service', value: (flow: Flow) => flow.service_name },
  { header: 'PID', value: (flow: Flow) => flow.pid },
  { header: 'Local IP', value: (flow: Flow) => flow.local_ip },
  { header: 'Local port', value: (flow: Flow) => flow.local_port },
  { header: 'Remote IP', value: (flow: Flow) => flow.remote_ip },
  { header: 'Remote port', value: (flow: Flow) => flow.remote_port },
  { header: 'Source', value: (flow: Flow) => sourceAndDestination(flow).source },
  { header: 'Destination', value: (flow: Flow) => sourceAndDestination(flow).destination },
  { header: 'Sent bytes', value: (flow: Flow) => (flow.bytes_known ? flow.bytes_sent : '') },
  { header: 'Received bytes', value: (flow: Flow) => (flow.bytes_known ? flow.bytes_received : '') },
  { header: 'State', value: (flow: Flow) => flow.state },
  { header: 'First seen', value: (flow: Flow) => flow.first_seen },
  { header: 'Last seen', value: (flow: Flow) => flow.last_seen },
]

const IN_FLIGHT = new Set<CaptureStatus>(['queued', 'running', 'stopping'])

function isInFlight(capture: Capture) {
  return IN_FLIGHT.has(capture.status)
}

function hasCaptureTelemetry(capture: Capture) {
  return capture.samples > 0 || capture.flow_count > 0
}

function formatDuration(seconds: number) {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`
  return `${Math.round(seconds / 86400)}d`
}

function retentionLabel(capture: Capture) {
  if (capture.archived_at) return 'Archived - retained until purged'
  if (!capture.purge_after) {
    return isInFlight(capture)
      ? `Auto-purge ${formatDuration(capture.retention_s || 3600)} after completion`
      : 'Pending retention schedule'
  }
  const remainingMs = Date.parse(capture.purge_after) - Date.now()
  if (remainingMs <= 0) return 'Due for automatic purge'
  const minutes = Math.max(1, Math.ceil(remainingMs / 60_000))
  if (minutes < 60) return `Auto-purge in ${minutes}m`
  const hours = Math.ceil(minutes / 60)
  return hours < 24 ? `Auto-purge in ${hours}h` : `Auto-purge in ${Math.ceil(hours / 24)}d`
}

function inferredDirection(flow: Flow) {
  if (flow.direction && flow.direction !== 'unknown') return flow.direction
  if (flow.kind === 'listener') return 'inbound'
  if (!flow.remote_ip || !flow.remote_port) return 'local'
  if (flow.local_port <= 49151 && flow.remote_port > 49151) return 'inbound'
  if (flow.local_port > 49151 && flow.remote_port <= 49151) return 'outbound'
  return 'unknown'
}

function isSystemOwner(processName: string) {
  const owner = (processName || '').trim().toLowerCase()
  return owner === 'system' || owner === 'system idle process' || !owner
}

function isSystemFlow(flow: Flow) {
  return flow.pid <= 4 || isSystemOwner(flow.process_name)
}

function endpoint(ip: string, port: number) {
  if (!ip && !port) return 'Any peer'
  const displayIP = ip || '*'
  const formattedIP = displayIP.includes(':') && displayIP !== '*' ? `[${displayIP}]` : displayIP
  return port ? `${formattedIP}:${port}` : formattedIP
}

function sourceAndDestination(flow: Flow) {
  const direction = inferredDirection(flow)
  const local = endpoint(flow.local_ip, flow.local_port)
  const remote = endpoint(flow.remote_ip, flow.remote_port)
  if (direction === 'inbound') return { source: remote, destination: local, direction }
  return { source: local, destination: remote, direction }
}

function directionBadge(direction: ReturnType<typeof inferredDirection>) {
  if (direction === 'inbound') return <Badge variant="success" className="text-[10px]">Inbound</Badge>
  if (direction === 'outbound') return <Badge variant="info" className="text-[10px]">Outbound</Badge>
  if (direction === 'local') return <Badge variant="outline" className="text-[10px]">Local endpoint</Badge>
  return <Badge variant="outline" className="text-[10px]">Unknown</Badge>
}

function statusBadge(status: CaptureStatus) {
  if (status === 'running') return <Badge variant="info">Capturing</Badge>
  if (status === 'queued') return <Badge variant="warning">Starting...</Badge>
  if (status === 'stopping') return <Badge variant="warning">Stopping...</Badge>
  if (status === 'completed') return <Badge variant="success">Complete</Badge>
  if (status === 'failed') return <Badge variant="danger">Failed</Badge>
  if (status === 'cancelled') return <Badge variant="outline">Cancelled</Badge>
  return <Badge variant="outline">Expired</Badge>
}

function versionAtLeast(version: string | null, minimum: string) {
  if (!version) return false
  const parse = (value: string) => value.split(/[.-]/).slice(0, 3).map((part) => Number(part) || 0)
  const left = parse(version)
  const right = parse(minimum)
  for (let index = 0; index < 3; index += 1) {
    if (left[index] > right[index]) return true
    if (left[index] < right[index]) return false
  }
  return true
}

function captureBlockedReason(
  agentStatus: AgentStatus | null,
  agentVersion: string | null,
  capabilities: string[],
) {
  if (!agentStatus) return 'No monitoring agent is enrolled on this server.'
  if (agentStatus !== 'online') {
    if (agentStatus === 'stale') return 'The agent is stale. Restore its heartbeat before starting a capture.'
    if (agentStatus === 'offline') return 'The agent is offline. Start the service and wait for it to check in.'
    if (agentStatus === 'disabled') return 'The agent is disabled. Enable it before starting a capture.'
    return `The agent is ${agentStatus}; wait until it is online before starting a capture.`
  }
  const advertised = capabilities.length > 0
  const supported = capabilities.includes('network_capture_v1')
    || (!advertised && versionAtLeast(agentVersion, '1.2.0'))
  if (!supported) {
    return `Network capture requires agent 1.2.0 or newer${agentVersion ? ` (installed: ${agentVersion})` : ''}.`
  }
  return null
}

export function NetworkCapturePanel({
  serverId,
  serverName,
  interfaces,
  agentStatus,
  agentVersion,
  platform,
  agentCapabilities,
}: {
  serverId: string
  serverName: string
  interfaces: ServerNetworkInterface[]
  agentStatus: AgentStatus | null
  agentVersion: string | null
  platform: OsType
  agentCapabilities: string[]
}) {
  const qc = useQueryClient()
  const [duration, setDuration] = useState('300')
  const [retention, setRetention] = useState('3600')
  const [iface, setIface] = useState('__all')
  const [selected, setSelected] = useState<string | null>(null)
  const [purging, setPurging] = useState<Capture | null>(null)

  const capturesQ = useQuery<{ items: Capture[] }>({
    queryKey: ['servers', serverId, 'captures'],
    queryFn: async () => (await api.get(`/servers/${serverId}/network-captures`)).data,
    refetchInterval: (query) => {
      const items = (query.state.data as { items: Capture[] } | undefined)?.items ?? []
      return items.some(isInFlight) ? 5_000 : 30_000
    },
  })
  const captures = capturesQ.data?.items ?? []
  const active = captures.find(isInFlight)
  const current = captures.find((capture) => capture.id === selected) || active || captures[0]
  const blockedReason = captureBlockedReason(agentStatus, agentVersion, agentCapabilities)
  const advertised = agentCapabilities.length > 0
  const stopSupported = agentCapabilities.includes('capture_stop_v1')
    || (!advertised && versionAtLeast(agentVersion, '1.3.0'))
  const trafficSupported = agentCapabilities.includes('interface_traffic_v1')

  const start = useMutation({
    mutationFn: async () => (await api.post(`/servers/${serverId}/network-capture`, {
      duration_s: Number(duration),
      retention_s: Number(retention),
      interface: iface === '__all' ? null : iface,
    })).data,
    onSuccess: (data) => {
      toast.success('Capture queued', data.detail)
      setSelected(data.id)
      qc.invalidateQueries({ queryKey: ['servers', serverId, 'captures'] })
    },
    onError: (error) => toast.error('Could not start capture', apiErrorMessage(error)),
  })

  const stop = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/servers/network-captures/${id}`)).data,
    onSuccess: () => {
      toast.success('Stop requested', 'The agent will flush its final samples and mark the capture cancelled.')
      qc.invalidateQueries({ queryKey: ['servers', serverId, 'captures'] })
    },
    onError: (error) => toast.error('Could not stop capture', apiErrorMessage(error)),
  })

  const archiveCapture = useMutation({
    mutationFn: async (capture: Capture) => {
      const action = capture.archived_at ? 'unarchive' : 'archive'
      return (await api.post(`/servers/network-captures/${capture.id}/${action}`)).data
    },
    onSuccess: (data) => {
      toast.success(
        data.archived ? 'Capture archived' : 'Capture restored to automatic retention',
        data.archived
          ? 'This capture is retained until you purge it.'
          : 'A fresh retention window has started.',
      )
      qc.invalidateQueries({ queryKey: ['servers', serverId, 'captures'] })
    },
    onError: (error) => toast.error('Could not update capture retention', apiErrorMessage(error)),
  })

  const purgeCapture = useMutation({
    mutationFn: async (capture: Capture) => (
      await api.delete(`/servers/network-captures/${capture.id}/purge`)
    ).data,
    onSuccess: (_data, capture) => {
      toast.success('Capture purged', 'Flow records, interface samples and capture metadata were removed.')
      if (selected === capture.id) setSelected(null)
      setPurging(null)
      qc.removeQueries({ queryKey: ['captures', capture.id] })
      qc.invalidateQueries({ queryKey: ['servers', serverId, 'captures'] })
    },
    onError: (error) => toast.error('Could not purge capture', apiErrorMessage(error)),
  })

  return (
    <div className="space-y-4">
      <Card className="overflow-hidden">
        <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2.5">
          <div className="flex min-w-0 items-center gap-2">
            <Radio className={cn('h-3.5 w-3.5', active ? 'animate-pulse text-danger' : 'text-muted')} />
            <h3 className="text-sm font-semibold tracking-tight">Traffic capture</h3>
            {active && statusBadge(active.status)}
          </div>

          {current && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-border/70 bg-surface2/25 px-3 py-2.5 text-xs">
              <div className="flex min-w-0 items-center gap-2">
                {statusBadge(current.status)}
                <span className="font-medium text-text">
                  {new Date(current.requested_at).toLocaleString()}
                </span>
                <span className="text-muted">
                  {formatDuration(current.duration_s)} / {current.flow_count.toLocaleString()} records
                </span>
              </div>
              <div className={cn(
                'ml-auto flex items-center gap-1.5',
                current.archived_at ? 'text-success' : 'text-muted',
              )}>
                {current.archived_at ? <Archive className="h-3.5 w-3.5" /> : <Clock3 className="h-3.5 w-3.5" />}
                <span>{retentionLabel(current)}</span>
              </div>
            </div>
          )}
        </div>
        <CardContent className="space-y-3 px-4 pb-4 pt-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="cap-dur">Capture for</Label>
              <Select value={duration} onValueChange={setDuration} disabled={Boolean(active)}>
                <SelectTrigger id="cap-dur" className="h-9 w-[150px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {DURATIONS.map((item) => (
                    <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="cap-if">Interface</Label>
              <Select value={iface} onValueChange={setIface} disabled={Boolean(active)}>
                <SelectTrigger id="cap-if" className="h-9 w-[220px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all">All interfaces</SelectItem>
                  {/* Same reason as the traffic selector: only offer NICs an
                      operator would recognise, so the list stays short. */}
                  {interfaces
                    .filter((item) => isRealInterface(item.if_name))
                    .map((item) => (
                      <SelectItem key={item.if_name} value={item.if_name}>
                        {item.if_name}
                        {item.speed_mbps && item.speed_mbps > 0
                          ? ` (${item.speed_mbps >= 1000 ? `${item.speed_mbps / 1000} Gbps` : `${item.speed_mbps} Mbps`})`
                          : ''}
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="cap-retention">Retain results</Label>
              <Select value={retention} onValueChange={setRetention} disabled={Boolean(active)}>
                <SelectTrigger id="cap-retention" className="h-9 w-[160px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {RETENTION_WINDOWS.map((item) => (
                    <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {active ? (
              <Button
                variant="destructive"
                onClick={() => stop.mutate(active.id)}
                disabled={stop.isPending || active.status === 'stopping' || !stopSupported}
                title={!stopSupported ? 'Upgrade the agent to enable remote capture cancellation.' : undefined}
              >
                <CircleStop className="h-4 w-4" />
                {active.status === 'stopping' || stop.isPending ? 'Stopping...' : 'Stop capture'}
              </Button>
            ) : (
              <Button
                onClick={() => start.mutate()}
                disabled={start.isPending || capturesQ.isLoading || Boolean(blockedReason)}
                title={blockedReason ?? undefined}
              >
                <Play className="h-4 w-4" />
                {start.isPending ? 'Starting...' : 'Start capture'}
              </Button>
            )}
            {captures.length > 1 && (
              <div className="ml-auto min-w-[260px] space-y-1.5">
                <Label htmlFor="cap-hist">Capture history</Label>
                <Select value={current?.id || ''} onValueChange={setSelected}>
                  <SelectTrigger id="cap-hist" className="h-9 w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {captures.map((capture) => (
                      <SelectItem key={capture.id} value={capture.id}>
                        {capture.archived_at ? '[Archived] ' : ''}
                        {new Date(capture.requested_at).toLocaleString()} - {capture.status} - {capture.flow_count} records
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>

          {current && (
            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border/60 pt-3">
              <div className="min-w-0 text-xs text-muted">
                {current.archived_at
                  ? 'Archived captures are kept until you purge them.'
                  : current.purge_after
                    ? `${retentionLabel(current)} (${new Date(current.purge_after).toLocaleString()}).`
                    : isInFlight(current)
                      ? `The ${retentionLabel(current).toLowerCase()} window starts when capture finishes.`
                      : 'Automatic purge is being scheduled.'}
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => archiveCapture.mutate(current)}
                  disabled={archiveCapture.isPending}
                >
                  {current.archived_at
                    ? <ArchiveRestore className="h-3.5 w-3.5" />
                    : <Archive className="h-3.5 w-3.5" />}
                  {archiveCapture.isPending
                    ? 'Saving...'
                    : current.archived_at ? 'Resume auto-purge' : 'Archive capture'}
                </Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => setPurging(current)}
                  disabled={isInFlight(current) || purgeCapture.isPending}
                  title={isInFlight(current) ? 'Stop the capture before purging it.' : undefined}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  Purge now
                </Button>
              </div>
            </div>
          )}

          {blockedReason && !active && (
            <div className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{blockedReason}</span>
            </div>
          )}
          {active && !stopSupported && (
            <div className="flex items-start gap-2 rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-warning">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>This agent cannot accept a remote stop command. The capture will end at its configured deadline.</span>
            </div>
          )}

          <p className="flex items-start gap-1.5 text-[11px] text-muted">
            <Info className="mt-0.5 h-3 w-3 shrink-0" />
            Shows the local owning process or service and its remote peer. Connection metadata
            and interface counters are collected; packet payloads are never captured or stored.
            Finished captures auto-purge after the selected retention window unless archived.
          </p>
        </CardContent>
      </Card>

      {current && (
        <CaptureDetail
          capture={current}
          serverName={serverName}
          platform={platform}
          trafficSupported={trafficSupported}
        />
      )}
      {!current && !capturesQ.isLoading && (
        <Card>
          <CardContent className="pt-4">
            <EmptyState
              icon={<Activity className="h-7 w-7" />}
              title="No captures yet"
              hint="Start one above to see which local processes are using the network and which peers they reach."
            />
          </CardContent>
        </Card>
      )}
      <ConfirmDialog
        open={Boolean(purging)}
        onOpenChange={(open) => {
          if (!open && !purgeCapture.isPending) setPurging(null)
        }}
        title="Purge capture data?"
        description={purging ? (
          <span>
            This permanently deletes the capture from {new Date(purging.requested_at).toLocaleString()},
            including its flow records, interface samples, and control metadata. This cannot be undone.
          </span>
        ) : undefined}
        confirmText={purgeCapture.isPending ? 'Purging...' : 'Purge permanently'}
        destructive
        loading={purgeCapture.isPending}
        onConfirm={() => {
          if (purging) purgeCapture.mutate(purging)
        }}
      />
    </div>
  )
}

function CaptureDetail({
  capture,
  serverName,
  platform,
  trafficSupported,
}: {
  capture: Capture
  serverName: string
  platform: OsType
  trafficSupported: boolean
}) {
  const [searchDraft, setSearchDraft] = useState('')
  const [search, setSearch] = useState('')
  const [protocol, setProtocol] = useState('__all')
  const [scope, setScope] = useState('applications')
  const [direction, setDirection] = useState('__all')
  const [kind, setKind] = useState('all')
  const [volume, setVolume] = useState('__all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSizeState] = useState(50)
  const live = isInFlight(capture)

  useEffect(() => {
    const timer = setTimeout(() => setSearch(searchDraft.trim()), 350)
    return () => clearTimeout(timer)
  }, [searchDraft])

  useEffect(() => setPage(1), [capture.id, search, protocol, scope, direction, kind, volume])

  const flowsQ = useQuery<FlowResponse>({
    queryKey: ['captures', capture.id, 'flows', search, protocol, scope, direction, kind, volume, page, pageSize],
    queryFn: async () => (await api.get(`/servers/network-captures/${capture.id}/flows`, {
      params: {
        page,
        page_size: pageSize,
        sort: 'bytes_total',
        order: 'desc',
        ...(search ? { q: search } : {}),
        ...(protocol !== '__all' ? { protocol } : {}),
        scope,
        ...(direction !== '__all' ? { direction } : {}),
        kind,
        ...(volume !== '__all' ? { bytes_known: volume === 'known' } : {}),
      },
    })).data,
    refetchInterval: live ? 5_000 : false,
    placeholderData: (previous) => previous,
  })

  const summaryQ = useQuery<{
    top_processes: { process_name: string; service_name: string; bytes_sent: number; bytes_received: number; flows: number }[]
    top_peers: { remote_ip: string; bytes_sent: number; bytes_received: number; flows: number }[]
  }>({
    queryKey: ['captures', capture.id, 'summary'],
    queryFn: async () => (await api.get(`/servers/network-captures/${capture.id}/summary`)).data,
    refetchInterval: live ? 10_000 : false,
  })

  const flows = flowsQ.data?.items ?? []
  const total = flowsQ.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const filtersActive = Boolean(search)
    || protocol !== '__all'
    || scope !== 'applications'
    || direction !== '__all'
    || kind !== 'all'
    || volume !== '__all'
  const setPageSize = (next: number) => {
    setPageSizeState(next)
    setPage(1)
  }
  const resetFilters = () => {
    setSearchDraft('')
    setSearch('')
    setProtocol('__all')
    setScope('applications')
    setDirection('__all')
    setKind('all')
    setVolume('__all')
  }

  const elapsed = useMemo(() => {
    if (!capture.started_at) return null
    const end = capture.completed_at ? Date.parse(capture.completed_at) : Date.now()
    return Math.max(0, Math.round((end - Date.parse(capture.started_at)) / 1000))
  }, [capture.started_at, capture.completed_at, capture.samples])
  const progress = capture.duration_s > 0 && elapsed != null
    ? Math.min(100, (elapsed / capture.duration_s) * 100)
    : 0

  return (
    <div className="space-y-4">
      {live && (
        <Card className="overflow-hidden border-info/30 bg-info/5">
          <CardContent className="space-y-2 pt-4">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Radio className="h-4 w-4 animate-pulse text-info" />
              <span className="font-medium">
                {capture.status === 'queued'
                  ? 'Waiting for the agent to pick this up...'
                  : capture.status === 'stopping' ? 'Stopping capture...' : 'Capture running'}
              </span>
              <span className="text-xs text-muted">
                {elapsed != null ? `${elapsed}s of ${capture.duration_s}s` : `${capture.duration_s}s window`}
                {' / '}{capture.samples} samples{' / '}{total} flows so far
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-surface2">
              <div className="h-full rounded-full bg-info transition-all" style={{ width: `${progress}%` }} />
            </div>
          </CardContent>
        </Card>
      )}

      {capture.error_message && (
        <div className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/5 p-3 text-sm text-danger">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{capture.error_message}</span>
        </div>
      )}

      {!capture.bytes_available && hasCaptureTelemetry(capture) && (
        <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/5 p-3 text-xs text-warning">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <div>
            <div className="font-medium">Per-connection traffic volume unavailable</div>
            <div className="mt-0.5 text-muted">
              {capture.note || 'Per-connection byte counters could not be read.'} Conversations,
              local processes, services, and ports remain available; only per-flow byte totals are unknown.
              {platform === 'windows'
                ? ' Verify that the Windows agent service runs as LocalSystem.'
                : ' Interface RX/TX counters may still be available below.'}
            </div>
          </div>
        </div>
      )}

      <CaptureTrafficPanel capture={capture} supported={trafficSupported} />

      <div className="grid gap-4 xl:grid-cols-2">
        <Card className="overflow-hidden">
          <div className="border-b border-border px-4 py-2.5 text-sm font-semibold">Top local processes</div>
          <CardContent className="px-0 pb-2 pt-1">
            <TopList
              rows={(summaryQ.data?.top_processes ?? [])
                .filter((process) => scope === '__all'
                  || (scope === 'system' ? isSystemOwner(process.process_name) : !isSystemOwner(process.process_name)))
                .map((process) => ({
                  label: process.process_name || 'Kernel / unattributed',
                  sub: process.service_name,
                  sent: process.bytes_sent,
                  recv: process.bytes_received,
                  flows: process.flows,
                }))}
              bytesKnown={capture.bytes_available}
            />
          </CardContent>
        </Card>
        <Card className="overflow-hidden">
          <div className="border-b border-border px-4 py-2.5 text-sm font-semibold">Top remote peers</div>
          <CardContent className="px-0 pb-2 pt-1">
            <TopList
              rows={(summaryQ.data?.top_peers ?? [])
                .filter((peer) => Boolean(peer.remote_ip))
                .map((peer) => ({
                  label: peer.remote_ip,
                  sub: '',
                  sent: peer.bytes_sent,
                  recv: peer.bytes_received,
                  flows: peer.flows,
                }))}
              bytesKnown={capture.bytes_available}
            />
          </CardContent>
        </Card>
      </div>

      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
          <div>
            <h3 className="text-sm font-semibold tracking-tight">Traffic records</h3>
            <p className="mt-0.5 text-[11px] text-muted">
              {total.toLocaleString()} shown from {capture.flow_count.toLocaleString()} captured records.
              Application traffic is selected by default; use All owners to include kernel and system endpoints.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {capture.archived_at && <Badge variant="success">Archived</Badge>}
            {statusBadge(capture.status)}
          </div>
        </div>
        <div className="grid gap-2 border-b border-border/50 px-4 py-3 xl:grid-cols-[minmax(260px,1fr)_repeat(6,auto)]">
          <div className="relative min-w-[240px]">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input
              className="h-8 pl-8"
              placeholder="Search IP, port, process, service or state..."
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
            />
          </div>
          <Select value={scope} onValueChange={setScope}>
            <SelectTrigger className="h-8 min-w-[150px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="applications">Application owners</SelectItem>
              <SelectItem value="system">System / kernel</SelectItem>
              <SelectItem value="all">All owners</SelectItem>
            </SelectContent>
          </Select>
          <Select value={kind} onValueChange={setKind}>
            <SelectTrigger className="h-8 min-w-[145px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="connection">Connections</SelectItem>
              <SelectItem value="listener">TCP listeners</SelectItem>
              <SelectItem value="endpoint">UDP endpoints</SelectItem>
              <SelectItem value="all">All record types</SelectItem>
            </SelectContent>
          </Select>
          <Select value={direction} onValueChange={setDirection}>
            <SelectTrigger className="h-8 min-w-[125px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">All directions</SelectItem>
              <SelectItem value="inbound">Inbound</SelectItem>
              <SelectItem value="outbound">Outbound</SelectItem>
              <SelectItem value="local">Local endpoints</SelectItem>
              <SelectItem value="unknown">Unknown</SelectItem>
            </SelectContent>
          </Select>
          <Select value={protocol} onValueChange={setProtocol}>
            <SelectTrigger className="h-8 min-w-[110px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">All protocols</SelectItem>
              <SelectItem value="tcp">TCP</SelectItem>
              <SelectItem value="udp">UDP</SelectItem>
            </SelectContent>
          </Select>
          <Select value={volume} onValueChange={setVolume}>
            <SelectTrigger className="h-8 min-w-[125px]"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__all">Any volume</SelectItem>
              <SelectItem value="known">Measured bytes</SelectItem>
              <SelectItem value="unknown">Metadata only</SelectItem>
            </SelectContent>
          </Select>
          <div className="flex items-center justify-end gap-2">
            {filtersActive && (
              <Button variant="ghost" size="sm" onClick={resetFilters} title="Clear every traffic filter">
                <RotateCcw className="h-3.5 w-3.5" /> Reset
              </Button>
            )}
            <ExportCsvButton
              rows={flows}
              columns={FLOW_CSV}
              filename={`${serverName}-capture-${capture.id}-page-${page}.csv`}
            />
          </div>
        </div>
        <CardContent className="px-0 pb-0 pt-0">
          <div className="overflow-x-auto">
            <Table>
              <THead className="bg-surface2/40">
                <Tr>
                  <Th className="pl-4">Local owner</Th>
                  <Th>Role</Th>
                  <Th>Source</Th>
                  <Th>Destination</Th>
                  <Th>Protocol / state</Th>
                  <Th>Traffic</Th>
                  <Th className="pr-4">Last seen</Th>
                </Tr>
              </THead>
              <TBody>
                {flowsQ.isError ? (
                  <TableStateRow colSpan={7}>
                    <QueryError error={flowsQ.error} onRetry={() => flowsQ.refetch()} />
                  </TableStateRow>
                ) : flowsQ.isLoading ? (
                  <TableStateRow colSpan={7}><div className="py-10"><Skeleton className="h-24 w-full" /></div></TableStateRow>
                ) : flows.length === 0 ? (
                  <TableStateRow colSpan={7}>
                    <EmptyState
                      icon={<Activity className="h-7 w-7" />}
                      title={filtersActive
                        ? 'No flows match this search'
                        : live ? 'Waiting for the first flows...' : 'No conversations recorded'}
                      hint={filtersActive
                        ? 'Clear the filters to see every conversation in this capture.'
                        : live ? 'Flows appear after the agent sends its first capture batch.' : undefined}
                    />
                  </TableStateRow>
                ) : flows.map((flow, index) => {
                  const path = sourceAndDestination(flow)
                  const system = isSystemFlow(flow)
                  const kindLabel = flow.kind === 'listener'
                    ? 'TCP listener'
                    : flow.kind === 'endpoint' ? 'UDP endpoint' : 'Connection'
                  return (
                    <Tr
                      key={`${flow.protocol}-${flow.local_ip}-${flow.local_port}-${flow.remote_ip}-${flow.remote_port}-${flow.pid}-${flow.kind || 'connection'}`}
                      className={cn(index % 2 === 0 && 'bg-surface2/10', 'align-top')}
                    >
                      <Td className="py-2.5 pl-4">
                        <div className="flex max-w-[240px] items-start gap-2">
                          <div className={cn(
                            'mt-0.5 rounded-md border p-1.5',
                            system ? 'border-warning/30 bg-warning/5 text-warning' : 'border-info/30 bg-info/5 text-info',
                          )}>
                            {system ? <Network className="h-3.5 w-3.5" /> : <Activity className="h-3.5 w-3.5" />}
                          </div>
                          <div className="min-w-0">
                            <div className="truncate text-sm font-medium" title={flow.process_name || 'Unattributed'}>
                              {system ? (flow.process_name || 'Kernel / unattributed') : flow.process_name}
                            </div>
                            <div className="truncate text-[11px] text-muted" title={flow.service_name || undefined}>
                              {flow.service_name || (system ? 'System-owned socket' : 'No service mapping')}
                              {' / '}pid {flow.pid}
                            </div>
                          </div>
                        </div>
                      </Td>
                      <Td className="py-2.5">
                        <div className="space-y-1">
                          {directionBadge(path.direction)}
                          <div className="text-[10px] text-muted">{kindLabel}</div>
                        </div>
                      </Td>
                      <Td className="py-2.5 font-mono text-xs">
                        <span className={cn(path.source === 'Any peer' && 'font-sans italic text-muted')}>
                          {path.source}
                        </span>
                      </Td>
                      <Td className="py-2.5 font-mono text-xs">
                        <div className="flex items-center gap-2">
                          <ArrowRight className="h-3.5 w-3.5 shrink-0 text-muted" />
                          <span className={cn(path.destination === 'Any peer' && 'font-sans italic text-muted')}>
                            {path.destination}
                          </span>
                        </div>
                      </Td>
                      <Td className="py-2.5">
                        <div className="flex flex-wrap items-center gap-1">
                          <Badge variant="outline" className="text-[10px] uppercase">{flow.protocol}</Badge>
                          {flow.state && <span className="text-[10px] uppercase text-muted">{flow.state}</span>}
                        </div>
                      </Td>
                      <Td className="py-2.5 text-xs tabular-nums">
                        {flow.bytes_known ? (
                          <div className="space-y-0.5">
                            <span className="flex items-center gap-1 text-info">
                              <ArrowUp className="h-3 w-3" />{formatBytes(flow.bytes_sent)} sent
                            </span>
                            <span className="flex items-center gap-1 text-success">
                              <ArrowDown className="h-3 w-3" />{formatBytes(flow.bytes_received)} received
                            </span>
                          </div>
                        ) : <span className="text-[11px] text-muted">Metadata only</span>}
                      </Td>
                      <Td className="py-2.5 pr-4 text-xs text-muted" title={new Date(flow.last_seen).toLocaleString()}>
                        {relativeTime(flow.last_seen)}
                      </Td>
                    </Tr>
                  )
                })}
              </TBody>
            </Table>
          </div>
          <TablePager
            page={page}
            pageCount={pageCount}
            pageSize={pageSize}
            total={total}
            setPage={setPage}
            setPageSize={setPageSize}
            noun="flows"
          />
        </CardContent>
      </Card>

      {capture.truncated && (
        <p className="text-[11px] text-warning">
          This capture hit its flow limit; additional conversations were omitted.
          Use a shorter window or a specific interface to narrow the result.
        </p>
      )}
    </div>
  )
}

function CaptureTrafficPanel({ capture, supported }: { capture: Capture; supported: boolean }) {
  const [selectedInterface, setSelectedInterface] = useState('')
  const [showAllInterfaces, setShowAllInterfaces] = useState(false)
  const live = isInFlight(capture)
  const trafficQ = useQuery<TrafficResponse>({
    queryKey: ['captures', capture.id, 'traffic'],
    queryFn: async () => (await api.get(`/servers/network-captures/${capture.id}/traffic`)).data,
    enabled: supported,
    refetchInterval: live ? 5_000 : false,
  })

  useEffect(() => setSelectedInterface(''), [capture.id])

  const options = useMemo(() => {
    const data = trafficQ.data
    if (!data) return []
    const rows: { id: string; label: string; traffic: InterfaceTraffic; real: boolean }[] = []
    if (data.aggregate) {
      rows.push({ id: '__aggregate', label: 'All interfaces', traffic: data.aggregate, real: true })
    }
    for (const item of data.interfaces ?? []) {
      rows.push({
        id: item.interface || `interface-${rows.length}`,
        label: item.interface || 'Interface',
        traffic: item,
        real: isRealInterface(item.interface, item.link_speed_bps),
      })
    }
    // Real NICs first. Windows reports every NDIS filter layer and WAN
    // miniport as its own interface, so an unsorted list buries the one
    // interface anyone wants under ~20 entries that mirror or idle.
    return rows.sort((a, b) => Number(b.real) - Number(a.real))
  }, [trafficQ.data])

  const realOptions = options.filter((item) => item.real)
  const hiddenCount = options.length - realOptions.length
  const visibleOptions = showAllInterfaces ? options : realOptions

  const selected = options.find((item) => item.id === selectedInterface)
    || options.find((item) => item.id === capture.interface)
    // Default to a real NIC rather than whichever pseudo adapter sorts first.
    || realOptions[0]
    || options[0]
  const chartData = (selected?.traffic.samples ?? []).map((sample) => ({
    ts: Date.parse(sample.timestamp),
    rx: sample.rx_bps,
    tx: sample.tx_bps,
  }))

  if (!supported) {
    return (
      <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning/5 p-3 text-xs text-warning">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <div>
          <div className="font-medium">Interface utilization is not available from this agent</div>
          <div className="mt-0.5 text-muted">
            Flow metadata remains available. Upgrade to an agent advertising interface_traffic_v1
            to see live RX/TX throughput and utilization.
          </div>
        </div>
      </div>
    )
  }

  if (trafficQ.isError) {
    return (
      <Card><CardContent><QueryError error={trafficQ.error} onRetry={() => trafficQ.refetch()} /></CardContent></Card>
    )
  }

  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <div>
          <h3 className="text-sm font-semibold tracking-tight">Interface traffic</h3>
          <p className="text-[11px] text-muted">All traffic on the interface, including flows without process byte attribution.</p>
        </div>
        {options.length > 1 && (
          <div className="flex items-center gap-2">
            {hiddenCount > 0 && (
              <button
                type="button"
                onClick={() => setShowAllInterfaces((v) => !v)}
                className="text-[11px] font-medium text-primary hover:underline"
                title="Loopback, tunnel and NDIS filter adapters"
              >
                {showAllInterfaces ? 'Hide' : 'Show'} {hiddenCount} virtual
              </button>
            )}
            <Select value={selected?.id || ''} onValueChange={setSelectedInterface}>
              <SelectTrigger className="h-8 w-[230px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                {visibleOptions.map((item) => (
                  <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
      </div>
      <CardContent className="space-y-4 px-4 pb-4 pt-3">
        {trafficQ.isLoading ? (
          <Skeleton className="h-52 w-full" />
        ) : !selected ? (
          <EmptyState
            icon={<Activity className="h-7 w-7" />}
            title={live ? 'Waiting for interface samples...' : 'No interface traffic samples'}
            hint="Interface counters appear after the agent sends its first capture batch."
          />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
              <TrafficStatCard label="Current" stats={selected.traffic.current} />
              <TrafficStatCard label="Average" stats={selected.traffic.avg} />
              <TrafficStatCard label="Peak" stats={selected.traffic.peak} />
              <TrafficStatCard label="p95" stats={selected.traffic.p95} />
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted">
              <span>{selected.label}</span>
              <span>
                Link speed: {selected.traffic.link_speed_bps
                  ? formatBps(selected.traffic.link_speed_bps)
                  : 'unknown (throughput shown without utilization %)'}
              </span>
            </div>
            {chartData.length === 0 ? (
              <EmptyState title="No RX/TX samples yet" />
            ) : (
              <div className="h-56 w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={chartData} margin={{ top: 8, right: 14, bottom: 0, left: 0 }}>
                    <defs>
                      <linearGradient id={`captureRx-${capture.id}`} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="rgb(var(--success))" stopOpacity={0.3} />
                        <stop offset="100%" stopColor="rgb(var(--success))" stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id={`captureTx-${capture.id}`} x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="rgb(var(--info))" stopOpacity={0.28} />
                        <stop offset="100%" stopColor="rgb(var(--info))" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.45} strokeDasharray="3 3" vertical={false} />
                    <XAxis
                      dataKey="ts"
                      tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                      tickFormatter={(value) => new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                      axisLine={false}
                      tickLine={false}
                    />
                    <YAxis
                      width={48}
                      tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                      tickFormatter={(value) => formatBpsAxis(Number(value))}
                      axisLine={false}
                      tickLine={false}
                    />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: 'rgb(var(--surface))',
                        border: '1px solid rgb(var(--border))',
                        borderRadius: 8,
                        color: 'rgb(var(--text))',
                        fontSize: 12,
                      }}
                      labelFormatter={(value) => new Date(Number(value)).toLocaleString()}
                      formatter={(value: number, name: string) => [formatBps(Number(value)), name === 'rx' ? 'Received' : 'Sent']}
                    />
                    <Legend
                      iconType="circle"
                      iconSize={8}
                      wrapperStyle={{ fontSize: 11 }}
                      formatter={(value) => value === 'rx' ? 'Received' : 'Sent'}
                    />
                    <Area type="monotone" dataKey="rx" stroke="rgb(var(--success))" fill={`url(#captureRx-${capture.id})`} strokeWidth={2} dot={false} isAnimationActive={false} />
                    <Area type="monotone" dataKey="tx" stroke="rgb(var(--info))" fill={`url(#captureTx-${capture.id})`} strokeWidth={2} dot={false} isAnimationActive={false} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function TrafficStatCard({ label, stats }: { label: string; stats: TrafficStats }) {
  const total = stats.total_bps ?? (stats.rx_bps || 0) + (stats.tx_bps || 0)
  return (
    <div className="rounded-md border border-border/60 bg-surface2/25 px-3 py-2">
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-1 text-sm font-semibold tabular-nums">{formatBps(total)}</div>
      <div className="mt-1 flex flex-wrap gap-x-2 text-[10px] tabular-nums text-muted">
        <span className="text-success">RX {formatBps(stats.rx_bps || 0)}</span>
        <span className="text-info">TX {formatBps(stats.tx_bps || 0)}</span>
      </div>
      <div className="mt-1 text-[10px] text-muted">
        Utilization: {stats.utilization_pct == null ? 'unknown' : `${stats.utilization_pct.toFixed(1)}%`}
      </div>
    </div>
  )
}

function TopList({
  rows,
  bytesKnown,
}: {
  rows: { label: string; sub: string; sent: number; recv: number; flows: number }[]
  bytesKnown: boolean
}) {
  if (rows.length === 0) {
    return <div className="px-4 py-6 text-center text-xs text-muted">Nothing recorded yet</div>
  }
  const max = Math.max(1, ...rows.map((row) => row.sent + row.recv))
  return (
    <div className="space-y-1.5 px-4 py-2">
      {rows.map((row) => {
        const total = row.sent + row.recv
        return (
          <div key={row.label + row.sub}>
            <div className="flex items-baseline justify-between gap-2 text-xs">
              <span className="truncate font-medium">
                {row.label}
                {row.sub && <span className="ml-1 text-[10px] text-muted">{row.sub}</span>}
              </span>
              <span className="shrink-0 tabular-nums text-muted">
                {bytesKnown ? formatBytes(total) : `${row.flows} flow${row.flows === 1 ? '' : 's'}`}
              </span>
            </div>
            {bytesKnown && (
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface2">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-primary to-info"
                  style={{ width: `${(total / max) * 100}%` }}
                />
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
