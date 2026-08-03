/** Agent Fleet: fleet-wide agent health, filtering and bulk operations
 *  (policy / update ring changes, diagnostics, certificate rotation). */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Bot, ChevronLeft, ChevronRight, Clock, CloudOff, Download, FileDown,
  HardDrive, Inbox, KeyRound, Plus, Search, Trash2, Wifi, WifiOff, X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, formatBytes, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Skeleton } from '@/components/ui/Skeleton'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import { AgentStatusBadge, KpiTile, TagList } from '@/components/servers/shared'
import { InstallTokenDialog } from '@/components/servers/InstallTokenDialog'
import { DownloadAgentDialog } from '@/components/servers/DownloadAgentDialog'
import type { AgentItem, AgentPolicy, UpdateRing } from '@/types/servers'

interface AgentFleetSummary {
  online: number
  stale: number
  offline: number
  disabled: number
  total: number
  queue_depth: number
  spool_bytes: number
}

interface AgentFleetResponse {
  items: AgentItem[]
  total: number
  page: number
  page_size: number
  summary?: AgentFleetSummary
}

type BulkAction =
  | 'change_policy'
  | 'change_update_ring'
  | 'request_diagnostics'
  | 'rotate_certificate'
  | 'trigger_upgrade'
  | 'disable'
  | 'enable'

const BULK_ACTIONS: { value: BulkAction; label: string }[] = [
  { value: 'change_policy', label: 'Change policy' },
  { value: 'change_update_ring', label: 'Change update ring' },
  { value: 'request_diagnostics', label: 'Request diagnostics' },
  { value: 'rotate_certificate', label: 'Rotate certificate' },
  { value: 'trigger_upgrade', label: 'Trigger upgrade' },
  { value: 'disable', label: 'Disable' },
  { value: 'enable', label: 'Enable' },
]

/** Clocks off by more than this get a visible warning in the fleet table. */
const CLOCK_SKEW_WARN_S = 120

const STATUS_FILTERS = [
  { value: 'all', label: 'All statuses' },
  { value: 'online', label: 'Online' },
  { value: 'stale', label: 'Stale' },
  { value: 'offline', label: 'Offline' },
  { value: 'disabled', label: 'Disabled' },
  { value: 'error', label: 'Error' },
]

const PLATFORM_FILTERS = [
  { value: 'all', label: 'All platforms' },
  { value: 'windows', label: 'Windows' },
  { value: 'linux', label: 'Linux' },
  { value: 'macos', label: 'macOS' },
]

const RING_FILTERS = [
  { value: 'all', label: 'All rings' },
  { value: 'canary', label: 'Canary' },
  { value: 'beta', label: 'Beta' },
  { value: 'stable', label: 'Stable' },
  { value: 'pinned', label: 'Pinned' },
]

const UPDATE_RINGS: UpdateRing[] = ['canary', 'beta', 'stable', 'pinned']

const COLS = 11

const PAGE_SIZE = 50

export function AgentFleetPage() {
  const qc = useQueryClient()
  const [qDraft, setQDraft] = useState('')
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('all')
  const [platform, setPlatform] = useState('all')
  const [ring, setRing] = useState('all')
  const [page, setPage] = useState(1)
  const [deployOpen, setDeployOpen] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkAction, setBulkAction] = useState<BulkAction>('request_diagnostics')
  const [bulkPolicy, setBulkPolicy] = useState('')
  const [bulkRing, setBulkRing] = useState<UpdateRing>('stable')
  const [bulkVersion, setBulkVersion] = useState('')
  const [confirmDisable, setConfirmDisable] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<AgentItem | null>(null)
  const [pendingRow, setPendingRow] = useState<string | null>(null)

  // Debounce the search box so we don't fire a request per keystroke.
  useEffect(() => {
    const t = setTimeout(() => setQ(qDraft), 300)
    return () => clearTimeout(t)
  }, [qDraft])
  // Filters change the result set — page and selection no longer apply.
  useEffect(() => {
    setPage(1)
    setSelected(new Set())
  }, [q, status, platform, ring])

  const { data, isLoading, isError, error } = useQuery<AgentFleetResponse>({
    queryKey: ['agent-fleet', q, status, platform, ring, page],
    queryFn: async () =>
      (await api.get('/agent-fleet', {
        params: {
          status: status === 'all' ? '' : status,
          platform: platform === 'all' ? '' : platform,
          update_ring: ring === 'all' ? '' : ring,
          q,
          page,
          page_size: PAGE_SIZE,
        },
      })).data,
    refetchInterval: 15_000,
  })

  const { data: policies } = useQuery<AgentPolicy[]>({
    queryKey: ['agent-policies'],
    queryFn: async () => (await api.get('/agent-policies')).data.items,
  })

  const items = data?.items || []
  const [downloadOpen, setDownloadOpen] = useState(false)
  const total = data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const hasFilters = q !== '' || status !== 'all' || platform !== 'all' || ring !== 'all'

  // Fleet-wide KPIs from the backend summary — never from the visible page,
  // which is filtered and capped at PAGE_SIZE.
  const summary = data?.summary
  const online = summary?.online ?? 0
  const down = (summary?.offline ?? 0) + (summary?.stale ?? 0)
  const queueBacklog = summary?.queue_depth ?? 0
  const spoolTotal = summary?.spool_bytes ?? 0
  const fleetTotal = summary?.total ?? total

  // ---- selection ----
  const toggleOne = (id: string) => setSelected((s) => {
    const n = new Set(s)
    if (n.has(id)) n.delete(id); else n.add(id)
    return n
  })
  const pageIds = items.map((a) => a.id)
  const allSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id))
  const toggleAll = () => setSelected((s) => {
    const n = new Set(s)
    if (allSelected) pageIds.forEach((id) => n.delete(id)); else pageIds.forEach((id) => n.add(id))
    return n
  })
  const clearSelection = () => setSelected(new Set())

  // ---- mutations ----
  const invalidate = () => qc.invalidateQueries({ queryKey: ['agent-fleet'] })

  const requestDiagnostics = useMutation({
    mutationFn: async (id: string) => { setPendingRow(id); return api.post(`/agent-fleet/${id}/request-diagnostics`) },
    onSuccess: () => {
      toast.success('Diagnostics requested', 'The agent will upload a support bundle on its next check-in')
      invalidate()
    },
    onError: (e) => toast.error('Diagnostics request failed', apiErrorMessage(e)),
    onSettled: () => setPendingRow(null),
  })

  const rotateCertificate = useMutation({
    mutationFn: async (id: string) => { setPendingRow(id); return api.post(`/agent-fleet/${id}/rotate-certificate`) },
    onSuccess: () => {
      toast.success('Certificate rotation requested')
      invalidate()
    },
    onError: (e) => toast.error('Certificate rotation failed', apiErrorMessage(e)),
    onSettled: () => setPendingRow(null),
  })

  const deleteAgent = useMutation({
    mutationFn: async (id: string) => api.delete(`/agent-fleet/${id}`),
    onSuccess: () => {
      toast.success('Agent removed', 'Its API key no longer authenticates; the host can re-enroll with a new token')
      invalidate()
    },
    onError: (e) => toast.error('Agent removal failed', apiErrorMessage(e)),
  })

  const bulk = useMutation({
    mutationFn: async () => {
      const body: {
        agent_ids: string[]
        action: BulkAction
        policy_id?: string
        update_ring?: UpdateRing
        target_version?: string
      } = { agent_ids: Array.from(selected), action: bulkAction }
      if (bulkAction === 'change_policy') body.policy_id = bulkPolicy
      if (bulkAction === 'change_update_ring') body.update_ring = bulkRing
      if (bulkAction === 'trigger_upgrade' && bulkVersion.trim()) body.target_version = bulkVersion.trim()
      return api.post('/agent-fleet/bulk', body)
    },
    onSuccess: () => {
      const label = BULK_ACTIONS.find((a) => a.value === bulkAction)?.label || 'Action'
      toast.success(`${label} applied to ${selected.size} agent${selected.size === 1 ? '' : 's'}`)
      invalidate()
      clearSelection()
      setConfirmDisable(false)
    },
    onError: (e) => toast.error('Bulk action failed', apiErrorMessage(e)),
  })

  const applyBulk = () => {
    if (bulkAction === 'disable') {
      setConfirmDisable(true)
      return
    }
    bulk.mutate()
  }
  const applyDisabled =
    bulk.isPending || selected.size === 0
    || (bulkAction === 'change_policy' && !bulkPolicy)
    || (bulkAction === 'trigger_upgrade' && !bulkVersion.trim())

  return (
    <div className="space-y-4">
      {/* header */}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Bot className="h-5 w-5 text-primary" />
            Agent Fleet
          </h1>
          <p className="text-xs text-muted">
            {fleetTotal} agent{fleetTotal === 1 ? '' : 's'} enrolled
            {hasFilters && total !== fleetTotal ? ` · ${total} matching filters` : ''}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" onClick={() => setDownloadOpen(true)}>
            <Download className="h-4 w-4" />
            Download agent
          </Button>
          <Button onClick={() => setDeployOpen(true)}>
            <Plus className="h-4 w-4" />
            Deploy agent
          </Button>
        </div>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <KpiTile icon={Wifi} label="Online" value={online} tone="success" />
        <KpiTile icon={WifiOff} label="Offline / Stale" value={down} tone="danger" />
        <KpiTile icon={Inbox} label="Queue backlog" value={queueBacklog.toLocaleString()} sub="pending batches" tone="info" />
        <KpiTile icon={HardDrive} label="Spool size" value={formatBytes(spoolTotal)} sub="buffered on disk" />
      </div>

      <Card>
        <CardContent className="space-y-3 pt-4">
          {/* filter bar */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-[220px] flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <Input
                className="pl-8"
                placeholder="Search hostname, server, IP…"
                value={qDraft}
                onChange={(e) => setQDraft(e.target.value)}
              />
            </div>
            <Select value={status} onValueChange={setStatus}>
              <SelectTrigger className="w-[150px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                {STATUS_FILTERS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
              </SelectContent>
            </Select>
            <Select value={platform} onValueChange={setPlatform}>
              <SelectTrigger className="w-[150px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                {PLATFORM_FILTERS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
              </SelectContent>
            </Select>
            <Select value={ring} onValueChange={setRing}>
              <SelectTrigger className="w-[130px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                {RING_FILTERS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>

          {/* bulk action bar */}
          {selected.size > 0 && (
            <div className="sticky top-2 z-10 flex flex-wrap items-center gap-2 rounded-md border border-primary/30 bg-surface px-3 py-2 text-xs shadow-sm">
              <span className="font-medium text-primary">{selected.size} selected</span>
              <Select value={bulkAction} onValueChange={(v) => { setBulkAction(v as BulkAction); setBulkPolicy(''); setBulkVersion('') }}>
                <SelectTrigger className="h-8 w-[180px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {BULK_ACTIONS.map((a) => <SelectItem key={a.value} value={a.value}>{a.label}</SelectItem>)}
                </SelectContent>
              </Select>
              {bulkAction === 'change_policy' && (
                <Select value={bulkPolicy} onValueChange={setBulkPolicy}>
                  <SelectTrigger className="h-8 w-[180px]"><SelectValue placeholder="Select policy…" /></SelectTrigger>
                  <SelectContent>
                    {(policies || []).map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}
                  </SelectContent>
                </Select>
              )}
              {bulkAction === 'change_update_ring' && (
                <Select value={bulkRing} onValueChange={(v) => setBulkRing(v as UpdateRing)}>
                  <SelectTrigger className="h-8 w-[130px]"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {UPDATE_RINGS.map((r) => <SelectItem key={r} value={r} className="capitalize">{r}</SelectItem>)}
                  </SelectContent>
                </Select>
              )}
              {bulkAction === 'trigger_upgrade' && (
                <Input
                  className="h-8 w-[130px]"
                  placeholder="e.g. 1.1.0"
                  value={bulkVersion}
                  onChange={(e) => setBulkVersion(e.target.value)}
                  title="Target agent version"
                />
              )}
              <Button size="sm" onClick={applyBulk} disabled={applyDisabled}>
                {bulk.isPending ? 'Applying…' : 'Apply'}
              </Button>
              <button onClick={clearSelection} className="ml-auto flex items-center gap-1 text-muted hover:text-text">
                Clear <X className="h-3 w-3" />
              </button>
            </div>
          )}

          {/* table / empty state */}
          {!isLoading && items.length === 0 && !hasFilters ? (
            <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
              <Bot className="h-10 w-10 text-muted" />
              <div>
                <div className="text-sm font-medium">No agents enrolled yet</div>
                <p className="text-xs text-muted">Deploy the monitoring agent to a server to see it here.</p>
              </div>
              <Button onClick={() => setDeployOpen(true)}>
                <Plus className="h-4 w-4" />
                Deploy agent
              </Button>
            </div>
          ) : (
            <div className="overflow-hidden rounded-md border border-border">
              <Table>
                <THead className="bg-surface2/50">
                  <Tr>
                    <Th className="w-8">
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5 cursor-pointer accent-primary"
                        checked={allSelected}
                        onChange={toggleAll}
                        title="Select all"
                      />
                    </Th>
                    <Th>Hostname</Th>
                    <Th>Platform</Th>
                    <Th>Version</Th>
                    <Th>Status</Th>
                    <Th>Policy</Th>
                    <Th>Ring</Th>
                    <Th>Heartbeat</Th>
                    <Th>Queue</Th>
                    <Th>Last IP</Th>
                    <Th className="w-20 text-right">Actions</Th>
                  </Tr>
                </THead>
                <TBody>
                  {isLoading &&
                    Array.from({ length: 3 }).map((_, i) => (
                      <Tr key={i}>
                        <Td colSpan={COLS}><Skeleton className="h-5 w-full" /></Td>
                      </Tr>
                    ))}
                  {!isLoading && isError && (
                    <Tr>
                      <Td colSpan={COLS}>
                        <div className="flex flex-col items-center gap-2 py-10 text-center">
                          <CloudOff className="h-8 w-8 text-danger/60" />
                          <div className="text-sm font-medium">Could not load the agent fleet</div>
                          <div className="text-xs text-muted">{apiErrorMessage(error)}</div>
                        </div>
                      </Td>
                    </Tr>
                  )}
                  {!isLoading && items.map((agent) => (
                    <Tr key={agent.id} className={selected.has(agent.id) ? 'bg-primary/5' : undefined}>
                      <Td>
                        <input
                          type="checkbox"
                          className="h-3.5 w-3.5 cursor-pointer accent-primary"
                          checked={selected.has(agent.id)}
                          onChange={() => toggleOne(agent.id)}
                        />
                      </Td>
                      <Td>
                        <div className="font-medium">{agent.hostname || agent.agent_uid}</div>
                        {agent.server_id && agent.server_name && (
                          <Link to={`/servers/${agent.server_id}`} className="text-xs text-primary hover:underline">
                            {agent.server_name}
                          </Link>
                        )}
                        {agent.tags.length > 0 && <TagList tags={agent.tags} max={2} />}
                      </Td>
                      <Td className="text-xs capitalize text-muted">{agent.platform}</Td>
                      <Td className="font-mono text-xs">
                        {agent.version || '—'}
                        {agent.desired_version && agent.desired_version !== agent.version && (
                          <span className="ml-1 text-muted" title={`Update pending to ${agent.desired_version}`}>
                            → {agent.desired_version}
                          </span>
                        )}
                      </Td>
                      <Td>
                        <div className="flex items-center gap-1.5">
                          <AgentStatusBadge status={agent.status} />
                          {agent.config_apply_error && (
                            <span title={agent.config_apply_error}>
                              <AlertTriangle className="h-3.5 w-3.5 text-warning" />
                            </span>
                          )}
                          {Math.abs(agent.clock_skew_s || 0) > CLOCK_SKEW_WARN_S && (
                            <span title={`Host clock is off by ${Math.round(Math.abs(agent.clock_skew_s) / 60)} min — metric timestamps are being corrected at ingest. Fix the host's clock/NTP.`}>
                              <Clock className="h-3.5 w-3.5 text-warning" />
                            </span>
                          )}
                        </div>
                      </Td>
                      <Td className="text-xs">{agent.policy_name || '—'}</Td>
                      <Td>
                        <Badge variant="outline" className="capitalize">{agent.update_ring}</Badge>
                      </Td>
                      <Td className="text-xs text-muted">{relativeTime(agent.last_heartbeat_at)}</Td>
                      <Td>
                        <div className="text-xs tabular-nums">{agent.queue_depth.toLocaleString()}</div>
                        <div className="text-[11px] tabular-nums text-muted">{formatBytes(agent.spool_bytes)}</div>
                      </Td>
                      <Td className="font-mono text-xs text-muted">{agent.last_ip || '—'}</Td>
                      <Td>
                        <div className="flex justify-end gap-0.5">
                          <Button
                            variant="ghost" size="icon" className="h-7 w-7"
                            onClick={() => requestDiagnostics.mutate(agent.id)}
                            disabled={pendingRow === agent.id}
                            title="Request diagnostics"
                          >
                            <FileDown className="h-3.5 w-3.5" />
                          </Button>
                          <Button
                            variant="ghost" size="icon" className="h-7 w-7"
                            onClick={() => rotateCertificate.mutate(agent.id)}
                            disabled={pendingRow === agent.id}
                            title="Rotate certificate"
                          >
                            <KeyRound className="h-3.5 w-3.5" />
                          </Button>
                          <Button
                            variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger"
                            onClick={() => setDeleteTarget(agent)}
                            title="Remove agent"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </Td>
                    </Tr>
                  ))}
                  {!isLoading && items.length === 0 && hasFilters && (
                    <Tr>
                      <Td colSpan={COLS} className="py-12 text-center text-muted">
                        No agents match the current filters.
                      </Td>
                    </Tr>
                  )}
                </TBody>
              </Table>
              {pages > 1 && (
                <div className="flex items-center justify-between border-t border-border px-4 py-2 text-xs text-muted">
                  <span>Page {page} of {pages} · {total} agents</span>
                  <div className="flex items-center gap-1">
                    <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                      <ChevronLeft className="h-3.5 w-3.5" />
                    </Button>
                    <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>
                      <ChevronRight className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <InstallTokenDialog open={deployOpen} onOpenChange={setDeployOpen} />
      <DownloadAgentDialog open={downloadOpen} onOpenChange={setDownloadOpen} />
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(o) => { if (!o) setDeleteTarget(null) }}
        title="Remove agent"
        description={
          <>
            Remove the agent on <span className="font-semibold text-text">{deleteTarget?.hostname || deleteTarget?.agent_uid}</span>?
            Its API key stops authenticating immediately. The server record and its
            history are kept; a live host can re-enroll with a fresh token.
          </>
        }
        confirmText="Remove"
        destructive
        loading={deleteAgent.isPending}
        onConfirm={() => {
          if (deleteTarget) deleteAgent.mutate(deleteTarget.id)
          setDeleteTarget(null)
        }}
      />
      <ConfirmDialog
        open={confirmDisable}
        onOpenChange={setConfirmDisable}
        title="Disable agents"
        description={
          <>
            Disable <span className="font-semibold text-text">{selected.size}</span> selected
            agent{selected.size === 1 ? '' : 's'}? Disabled agents stop collecting and reporting metrics.
          </>
        }
        confirmText="Disable"
        destructive
        loading={bulk.isPending}
        onConfirm={() => bulk.mutate()}
      />
    </div>
  )
}
