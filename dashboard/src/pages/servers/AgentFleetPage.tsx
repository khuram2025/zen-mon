/** Agent Fleet: fleet-wide agent health, filtering and bulk operations
 *  (policy / update ring changes, diagnostics, certificate rotation). */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, Ban, Bot, CheckCircle2, ChevronLeft, ChevronRight, Clock, CloudOff, CopyPlus,
  FileDown, HardDrive, Inbox, KeyRound, Plus, RefreshCw, Search, ShieldAlert, ShieldOff, Trash2,
  UserCheck, Wifi, WifiOff, X,
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
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'
import { AgentStatusBadge, AuthorizationBadge, KpiTile, TagList } from '@/components/servers/shared'
import { InstallTokenDialog } from '@/components/servers/InstallTokenDialog'
import type { AgentItem, AgentPolicy, UpdateRing } from '@/types/servers'

interface AgentFleetSummary {
  online: number
  stale: number
  offline: number
  disabled: number
  pending_authorization: number
  registration_conflicts: number
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
  | 'authorize'
  | 'revoke'
  | 'change_policy'
  | 'change_update_ring'
  | 'request_diagnostics'
  | 'rotate_certificate'
  | 'trigger_upgrade'
  | 'disable'
  | 'enable'

type ConflictResolutionAction = 'replace' | 'register_clone' | 'block'

const BULK_ACTIONS: { value: BulkAction; label: string }[] = [
  { value: 'authorize', label: 'Authorize' },
  { value: 'revoke', label: 'Revoke authorization' },
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

const AUTHORIZATION_FILTERS = [
  { value: 'all', label: 'All authorization' },
  { value: 'pending', label: 'Awaiting authorization' },
  { value: 'authorized', label: 'Authorized' },
  { value: 'revoked', label: 'Revoked' },
  { value: 'conflict', label: 'Registration conflicts' },
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

const COLS = 13

const PAGE_SIZE = 50

export function AgentFleetPage() {
  const qc = useQueryClient()
  const [qDraft, setQDraft] = useState('')
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('all')
  const [platform, setPlatform] = useState('all')
  const [ring, setRing] = useState('all')
  const [authFilter, setAuthFilter] = useState('all')
  const [page, setPage] = useState(1)
  const [deployOpen, setDeployOpen] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkAction, setBulkAction] = useState<BulkAction>('request_diagnostics')
  const [bulkPolicy, setBulkPolicy] = useState('')
  const [bulkRing, setBulkRing] = useState<UpdateRing>('stable')
  const [bulkVersion, setBulkVersion] = useState('')
  const [confirmDisable, setConfirmDisable] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<AgentItem | null>(null)
  const [revokeTarget, setRevokeTarget] = useState<AgentItem | null>(null)
  const [conflictTarget, setConflictTarget] = useState<AgentItem | null>(null)
  const [conflictAction, setConflictAction] = useState<ConflictResolutionAction>('register_clone')
  const [cloneDisplayName, setCloneDisplayName] = useState('')
  const [authorizeClone, setAuthorizeClone] = useState(false)
  const [confirmBulkRevoke, setConfirmBulkRevoke] = useState(false)
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
  }, [q, status, platform, ring, authFilter])
  useEffect(() => {
    if (!conflictTarget) return
    const hostname = conflictTarget.registration_conflict_hostname || conflictTarget.hostname || 'Cloned server'
    const ip = conflictTarget.registration_conflict_ip
    setConflictAction('register_clone')
    setCloneDisplayName(`${hostname} (clone${ip ? ` ${ip}` : ''})`)
    setAuthorizeClone(false)
  }, [conflictTarget])

  const { data, isLoading, isError, error } = useQuery<AgentFleetResponse>({
    queryKey: ['agent-fleet', q, status, platform, ring, authFilter, page],
    queryFn: async () =>
      (await api.get('/agent-fleet', {
        params: {
          status: status === 'all' ? '' : status,
          platform: platform === 'all' ? '' : platform,
          update_ring: ring === 'all' ? '' : ring,
          authorization: authFilter === 'all' ? '' : authFilter,
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
  const total = data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const hasFilters = q !== '' || status !== 'all' || platform !== 'all'
    || ring !== 'all' || authFilter !== 'all'

  // Fleet-wide KPIs from the backend summary — never from the visible page,
  // which is filtered and capped at PAGE_SIZE.
  const summary = data?.summary
  const online = summary?.online ?? 0
  const down = (summary?.offline ?? 0) + (summary?.stale ?? 0)
  const pendingAuth = summary?.pending_authorization ?? 0
  const registrationConflicts = summary?.registration_conflicts ?? 0
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

  const authorize = useMutation({
    mutationFn: async (id: string) => { setPendingRow(id); return api.post(`/agent-fleet/${id}/authorize`) },
    onSuccess: () => {
      toast.success('Agent authorized', 'It will receive its API key on the next check-in')
      invalidate()
    },
    onError: (e) => toast.error('Authorization failed', apiErrorMessage(e)),
    onSettled: () => setPendingRow(null),
  })

  const revoke = useMutation({
    mutationFn: async (id: string) => api.post(`/agent-fleet/${id}/revoke`),
    onSuccess: () => {
      toast.success('Authorization revoked', "The agent's API key no longer authenticates")
      invalidate()
    },
    onError: (e) => toast.error('Revoke failed', apiErrorMessage(e)),
  })

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
      toast.success('Agent removed', 'Its host and APM credentials were revoked; the host can re-enroll with a new token')
      invalidate()
    },
    onError: (e) => toast.error('Agent removal failed', apiErrorMessage(e)),
  })

  const resolveConflict = useMutation({
    mutationFn: async ({
      agent, action, displayName, authorizeImmediately,
    }: {
      agent: AgentItem
      action: ConflictResolutionAction
      displayName?: string
      authorizeImmediately?: boolean
    }) => {
      if (!agent.registration_conflict_revision) {
        throw new Error('Registration candidate is missing its review revision; refresh and try again')
      }
      setPendingRow(agent.id)
      return api.post(`/agent-fleet/${agent.id}/resolve-registration-conflict`, {
        conflict_revision: agent.registration_conflict_revision,
        action,
        display_name: action === 'register_clone' ? displayName?.trim() || undefined : undefined,
        authorize: action === 'register_clone' && Boolean(authorizeImmediately),
      })
    },
    onSuccess: (_response, variables) => {
      if (variables.action === 'register_clone') {
        toast.success(
          'Clone registered separately',
          variables.authorizeImmediately
            ? 'A new server identity was created and authorized; the clone will adopt it on retry'
            : 'A new server identity was created and is awaiting authorization',
        )
      } else if (variables.action === 'block') {
        toast.success('Candidate blocked', 'The current agent remains active and this installation cannot claim it')
      } else {
        toast.success('Replacement accepted', 'The waiting agent will receive a fresh key on its next retry')
      }
      setConflictTarget(null)
      invalidate()
    },
    onError: (e) => {
      toast.error('Could not resolve registration conflict', apiErrorMessage(e))
      invalidate()
    },
    onSettled: () => setPendingRow(null),
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
      setConfirmBulkRevoke(false)
    },
    onError: (e) => toast.error('Bulk action failed', apiErrorMessage(e)),
  })

  const applyBulk = () => {
    if (bulkAction === 'disable') {
      setConfirmDisable(true)
      return
    }
    if (bulkAction === 'revoke') {
      setConfirmBulkRevoke(true)
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
          <Button onClick={() => setDeployOpen(true)}>
            <Plus className="h-4 w-4" />
            Install agent
          </Button>
        </div>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <KpiTile icon={Wifi} label="Online" value={online} tone="success" />
        <KpiTile icon={WifiOff} label="Offline / Stale" value={down} tone="danger" />
        {pendingAuth > 0 ? (
          <button
            type="button"
            onClick={() => setAuthFilter('pending')}
            className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-left transition-colors hover:bg-warning/15"
          >
            <div className="flex items-center gap-2 text-xs font-medium text-warning">
              <ShieldAlert className="h-3.5 w-3.5" /> Awaiting authorization
            </div>
            <div className="mt-1 text-xl font-semibold text-warning">{pendingAuth}</div>
            <div className="text-[11px] text-warning/80">click to review</div>
          </button>
        ) : (
          <KpiTile icon={UserCheck} label="Awaiting authorization" value={0} />
        )}
        {registrationConflicts > 0 ? (
          <button
            type="button"
            onClick={() => setAuthFilter('conflict')}
            className="rounded-lg border border-danger/40 bg-danger/10 p-3 text-left transition-colors hover:bg-danger/15"
          >
            <div className="flex items-center gap-2 text-xs font-medium text-danger">
              <AlertTriangle className="h-3.5 w-3.5" /> Registration conflicts
            </div>
            <div className="mt-1 text-xl font-semibold text-danger">{registrationConflicts}</div>
            <div className="text-[11px] text-danger/80">click to review</div>
          </button>
        ) : (
          <KpiTile icon={RefreshCw} label="Registration conflicts" value={0} />
        )}
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
            <Select value={authFilter} onValueChange={setAuthFilter}>
              <SelectTrigger className="w-[190px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                {AUTHORIZATION_FILTERS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
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
                Install agent
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
                    <Th>Authorization</Th>
                    <Th>Policy</Th>
                    <Th>Ring</Th>
                    <Th>Heartbeat</Th>
                    <Th>Local APM</Th>
                    <Th>Queue</Th>
                    <Th>Last IP</Th>
                    <Th className="w-28 text-right">Actions</Th>
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
                    <Tr
                      key={agent.id}
                      className={selected.has(agent.id) ? 'bg-primary/5' : agent.registration_conflict ? 'bg-danger/5' : undefined}
                    >
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
                      <Td>
                        <div className="flex flex-col items-start gap-1">
                          <AuthorizationBadge state={agent.authorization_state} source={agent.authorization_source} />
                          {agent.registration_conflict && (
                            <Badge
                              variant="danger"
                              title={`A different installation retried ${agent.registration_conflict_attempts} time(s)${agent.registration_conflict_ip ? ` from ${agent.registration_conflict_ip}` : ''}`}
                            >
                              <AlertTriangle className="h-3 w-3" /> Registration conflict
                            </Badge>
                          )}
                        </div>
                      </Td>
                      <Td className="text-xs">{agent.policy_name || '—'}</Td>
                      <Td>
                        <Badge variant="outline" className="capitalize">{agent.update_ring}</Badge>
                      </Td>
                      <Td className="text-xs text-muted">{relativeTime(agent.last_heartbeat_at)}</Td>
                      <Td><AgentApmStatusBadge status={agent.apm_status} /></Td>
                      <Td>
                        <div className="text-xs tabular-nums">{agent.queue_depth.toLocaleString()}</div>
                        <div className="text-[11px] tabular-nums text-muted">{formatBytes(agent.spool_bytes)}</div>
                      </Td>
                      <Td className="font-mono text-xs text-muted">{agent.last_ip || '—'}</Td>
                      <Td>
                        <div className="flex justify-end gap-0.5">
                          {agent.registration_conflict ? (
                            <Button
                              variant="ghost" size="icon" className="h-7 w-7 text-danger hover:text-danger"
                              onClick={() => setConflictTarget(agent)}
                              disabled={pendingRow === agent.id}
                              title="Review and accept the replacement installation"
                            >
                              <RefreshCw className="h-3.5 w-3.5" />
                            </Button>
                          ) : agent.authorization_state === 'authorized' ? (
                            <>
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
                                onClick={() => setRevokeTarget(agent)}
                                title="Revoke authorization"
                              >
                                <ShieldOff className="h-3.5 w-3.5" />
                              </Button>
                            </>
                          ) : (
                            <Button
                              variant="ghost" size="icon" className="h-7 w-7 text-warning hover:text-warning"
                              onClick={() => authorize.mutate(agent.id)}
                              disabled={pendingRow === agent.id}
                              title={agent.authorization_state === 'revoked' ? 'Re-authorize agent' : 'Authorize agent'}
                            >
                              <CheckCircle2 className="h-3.5 w-3.5" />
                            </Button>
                          )}
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
      <Dialog open={Boolean(conflictTarget)} onOpenChange={(o) => { if (!o && !resolveConflict.isPending) setConflictTarget(null) }}>
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-danger" /> Resolve registration conflict
            </DialogTitle>
            <DialogDescription>
              Two installations reported the same stable agent identity. Review both endpoints and choose the intended outcome.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-lg border border-border bg-surface2 p-3">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Current installation</div>
              <div className="font-medium text-text">{conflictTarget?.hostname || conflictTarget?.agent_uid}</div>
              <div className="mt-1 space-y-1 text-xs text-muted">
                <div>IP <span className="font-mono text-text">{conflictTarget?.last_ip || 'Unknown'}</span></div>
                <div>Version <span className="font-mono text-text">{conflictTarget?.version || 'Unknown'}</span></div>
                <div>Status <span className="text-text">{conflictTarget?.status || 'Unknown'}</span></div>
              </div>
            </div>
            <div className="rounded-lg border border-danger/35 bg-danger/5 p-3">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-danger">Conflicting installation</div>
              <div className="font-medium text-text">{conflictTarget?.registration_conflict_hostname || 'Unknown host'}</div>
              <div className="mt-1 space-y-1 text-xs text-muted">
                <div>IP <span className="font-mono text-text">{conflictTarget?.registration_conflict_ip || 'Unknown'}</span></div>
                <div>Version <span className="font-mono text-text">{conflictTarget?.registration_conflict_version || 'Unknown'}</span></div>
                <div>Attempts <span className="text-text">{conflictTarget?.registration_conflict_attempts || 0}</span></div>
                {conflictTarget?.registration_conflict_at && <div>Latest retry <span className="text-text">{relativeTime(conflictTarget.registration_conflict_at)}</span></div>}
              </div>
            </div>
          </div>

          <div className="rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-xs text-muted">
            IP addresses are informational. The conflict is based on a duplicated agent identity with a different protected installation secret, which commonly happens after cloning a VM.
          </div>

          <div className="space-y-2">
            <div className="text-sm font-medium text-text">Choose an outcome</div>
            <button
              type="button"
              onClick={() => setConflictAction('register_clone')}
              className={`w-full rounded-lg border p-3 text-left transition-colors ${conflictAction === 'register_clone' ? 'border-primary bg-primary/10' : 'border-border hover:bg-surface2'}`}
            >
              <div className="flex items-start gap-3">
                <CopyPlus className="mt-0.5 h-4 w-4 text-primary" />
                <div><div className="text-sm font-medium text-text">Register as a new cloned machine</div><div className="mt-0.5 text-xs text-muted">Keep the current agent and create separate agent and server identities for the waiting clone.</div></div>
              </div>
            </button>
            <button
              type="button"
              onClick={() => setConflictAction('replace')}
              className={`w-full rounded-lg border p-3 text-left transition-colors ${conflictAction === 'replace' ? 'border-warning bg-warning/10' : 'border-border hover:bg-surface2'}`}
            >
              <div className="flex items-start gap-3">
                <RefreshCw className="mt-0.5 h-4 w-4 text-warning" />
                <div><div className="text-sm font-medium text-text">Replace the current installation</div><div className="mt-0.5 text-xs text-muted">Move this identity to the waiting installation and revoke the current host and APM credentials.</div></div>
              </div>
            </button>
            <button
              type="button"
              onClick={() => setConflictAction('block')}
              className={`w-full rounded-lg border p-3 text-left transition-colors ${conflictAction === 'block' ? 'border-danger bg-danger/10' : 'border-border hover:bg-surface2'}`}
            >
              <div className="flex items-start gap-3">
                <Ban className="mt-0.5 h-4 w-4 text-danger" />
                <div><div className="text-sm font-medium text-text">Block this candidate</div><div className="mt-0.5 text-xs text-muted">Keep the current agent and reject only this reviewed installation secret on future retries.</div></div>
              </div>
            </button>
          </div>

          {conflictAction === 'register_clone' && (
            <div className="space-y-3 rounded-lg border border-primary/25 bg-primary/5 p-3">
              <div>
                <label htmlFor="clone-display-name" className="mb-1 block text-xs font-medium text-text">Server display name</label>
                <Input id="clone-display-name" value={cloneDisplayName} maxLength={255} onChange={(e) => setCloneDisplayName(e.target.value)} />
                <p className="mt-1 text-[11px] text-muted">The appliance assigns the technical identity automatically. Rename the Windows clone separately when practical.</p>
              </div>
              <div className="flex items-center justify-between gap-4">
                <div><div className="text-xs font-medium text-text">Authorize immediately</div><div className="text-[11px] text-muted">Otherwise the new clone appears in Awaiting authorization.</div></div>
                <Switch checked={authorizeClone} onCheckedChange={setAuthorizeClone} aria-label="Authorize cloned machine immediately" />
              </div>
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" disabled={resolveConflict.isPending} onClick={() => setConflictTarget(null)}>Cancel</Button>
            <Button
              variant={conflictAction === 'register_clone' ? 'default' : 'destructive'}
              disabled={resolveConflict.isPending || (conflictAction === 'register_clone' && !cloneDisplayName.trim())}
              onClick={() => {
                if (!conflictTarget) return
                resolveConflict.mutate({
                  agent: conflictTarget,
                  action: conflictAction,
                  displayName: cloneDisplayName,
                  authorizeImmediately: authorizeClone,
                })
              }}
            >
              {resolveConflict.isPending ? 'Applying…' : conflictAction === 'register_clone' ? 'Register new clone' : conflictAction === 'replace' ? 'Accept replacement' : 'Block candidate'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
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
      <ConfirmDialog
        open={confirmBulkRevoke}
        onOpenChange={setConfirmBulkRevoke}
        title="Revoke authorization"
        description={
          <>
            Revoke authorization for <span className="font-semibold text-text">{selected.size}</span> selected
            agent{selected.size === 1 ? '' : 's'}? Each agent's API key stops authenticating immediately;
            it can be re-authorized later without re-enrolling.
          </>
        }
        confirmText="Revoke"
        destructive
        loading={bulk.isPending}
        onConfirm={() => bulk.mutate()}
      />
      <ConfirmDialog
        open={Boolean(revokeTarget)}
        onOpenChange={(o) => { if (!o) setRevokeTarget(null) }}
        title="Revoke authorization"
        description={
          <>
            Revoke authorization for <span className="font-semibold text-text">{revokeTarget?.hostname || revokeTarget?.agent_uid}</span>?
            Its API key stops authenticating immediately; it can be re-authorized later
            without re-enrolling.
          </>
        }
        confirmText="Revoke"
        destructive
        loading={revoke.isPending}
        onConfirm={() => {
          if (revokeTarget) revoke.mutate(revokeTarget.id)
          setRevokeTarget(null)
        }}
      />
    </div>
  )
}

function AgentApmStatusBadge({ status }: { status: AgentItem['apm_status'] }) {
  const failed = status?.failed ?? 0
  const state = (status?.state || '').toLowerCase()
  const hasProblem = Boolean(status?.last_error)
    || failed > 0
    || state === 'error'
    || state === 'failed'
    || (status?.enabled === true && status.gateway?.managed === true && status.gateway.healthy === false)
  const variant = status?.enabled === false
    ? 'outline'
    : hasProblem
      ? 'danger'
      : status?.gateway?.healthy
        ? 'success'
        : 'warning'
  const label = status?.enabled === false
    ? 'Disabled'
    : hasProblem
      ? 'Needs attention'
      : status?.gateway?.healthy
        ? 'Ready'
        : status?.gateway?.listening
          ? 'OTLP listening'
          : status?.enabled
            ? 'Starting'
            : 'No status'
  const detail = [
    status?.last_error,
    status?.gateway?.version ? `Gateway ${status.gateway.version}` : null,
    status?.discovered != null || status?.instrumented != null
      ? `${status?.instrumented ?? 0}/${status?.discovered ?? 0} processes instrumented`
      : null,
    failed > 0 ? `${failed} instrumentation failures` : null,
  ].filter(Boolean).join(' · ') || 'Reported by the endpoint agent'

  return <Badge variant={variant} title={detail}>{label}</Badge>
}
