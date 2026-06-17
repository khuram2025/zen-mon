/** Server Inventory — searchable, filterable fleet list with live resource metrics. */

import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Bot,
  ChevronLeft,
  ChevronRight,
  CloudOff,
  Gauge,
  KeyRound,
  LayoutGrid,
  List,
  Plus,
  Search,
  Server,
  Tags,
  Trash2,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, formatBps, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Input } from '@/components/ui/Input'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { InstallTokenDialog } from '@/components/servers/InstallTokenDialog'
import { ServerFormDialog } from '@/components/servers/ServerFormDialog'
import {
  AgentStatusBadge, OsIcon, ServerStatusBadge, TagList,
} from '@/components/servers/shared'
import type {
  ServerFacets, ServerItem, ServerListResponse, ServerLiveMetrics, ServerStatus,
} from '@/types/servers'

const SORTABLE: Record<string, string> = {
  display_name: 'Server',
  status: 'Status',
  os_type: 'OS',
  last_seen: 'Last seen',
}

const STATUS_CHIPS: { key: ServerStatus | ''; label: string }[] = [
  { key: '', label: 'All' },
  { key: 'healthy', label: 'Healthy' },
  { key: 'warning', label: 'Warning' },
  { key: 'critical', label: 'Critical' },
  { key: 'stale', label: 'Stale' },
  { key: 'unknown', label: 'Unknown' },
]

function UsageCell({ pct, warn = 85, crit = 95 }: { pct: number | null | undefined; warn?: number; crit?: number }) {
  if (pct == null || Number.isNaN(pct)) return <span className="text-xs text-muted">—</span>
  const clamped = Math.max(0, Math.min(100, pct))
  const tone = clamped >= crit ? 'from-danger to-danger/70' : clamped >= warn ? 'from-warning to-warning/70' : 'from-info to-primary'
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-14 overflow-hidden rounded-full bg-surface2">
        <div className={cn('h-full rounded-full bg-gradient-to-r', tone)} style={{ width: `${clamped}%` }} />
      </div>
      <span className={cn(
        'w-10 text-right text-[11px] font-semibold tabular-nums',
        clamped >= crit ? 'text-danger' : clamped >= warn ? 'text-warning' : 'text-text2',
      )}>
        {clamped.toFixed(0)}%
      </span>
    </div>
  )
}

function PanelHeader({ icon, title, hint, right }: {
  icon: React.ReactNode
  title: string
  hint?: string
  right?: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2.5">
      <div className="flex min-w-0 items-center gap-2">
        <span className="text-muted">{icon}</span>
        <h3 className="text-sm font-semibold tracking-tight">{title}</h3>
        {hint && <span className="truncate text-[10px] uppercase tracking-wider text-muted">{hint}</span>}
      </div>
      {right}
    </div>
  )
}

export function ServerInventoryPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()

  const status = params.get('status') || ''
  const osType = params.get('os') || ''
  const mode = params.get('mode') || ''
  const tag = params.get('tag') || ''
  const search = params.get('search') || ''
  const sort = params.get('sort') || 'display_name'
  const order = params.get('order') || 'asc'
  const page = Math.max(1, Number(params.get('page') || 1))
  const pageSize = 50

  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [deployOpen, setDeployOpen] = useState(false)
  const [formOpen, setFormOpen] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<ServerItem | null>(null)
  const [bulkConfirm, setBulkConfirm] = useState<'delete' | 'decommission' | null>(null)
  const [bulkTags, setBulkTags] = useState('')
  const [view, setView] = useState<'table' | 'cards'>('table')

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    if (key !== 'page') next.delete('page')
    setParams(next, { replace: true })
  }

  const { data, isLoading } = useQuery<ServerListResponse>({
    queryKey: ['servers', 'list', { status, osType, mode, tag, search, sort, order, page }],
    queryFn: async () => {
      const q = new URLSearchParams()
      if (status) q.set('status', status)
      if (osType) q.set('os_type', osType)
      if (mode) q.set('collection_mode', mode)
      if (tag) q.set('tag', tag)
      if (search) q.set('q', search)
      q.set('sort', sort); q.set('order', order)
      q.set('page', String(page)); q.set('page_size', String(pageSize))
      return (await api.get(`/servers?${q}`)).data
    },
    refetchInterval: 15_000,
  })

  const { data: facets } = useQuery<ServerFacets>({
    queryKey: ['servers', 'facets'],
    queryFn: async () => (await api.get('/servers/facets')).data,
    refetchInterval: 60_000,
  })

  const { data: live } = useQuery<{ servers: Record<string, ServerLiveMetrics> }>({
    queryKey: ['servers', 'latest-metrics'],
    queryFn: async () => (await api.get('/servers/latest-metrics')).data,
    refetchInterval: 15_000,
  })

  const bulk = useMutation({
    mutationFn: async (body: { action: string; tags?: string[] }) =>
      (await api.post('/servers/bulk', { server_ids: [...selected], ...body })).data,
    onSuccess: (r) => {
      toast.success(`Updated ${r.affected} server${r.affected === 1 ? '' : 's'}`)
      setSelected(new Set())
      setBulkTags('')
      qc.invalidateQueries({ queryKey: ['servers'] })
      qc.invalidateQueries({ queryKey: ['server-monitoring'] })
    },
    onError: (e) => toast.error('Bulk action failed', apiErrorMessage(e)),
  })

  const removeOne = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/servers/${id}`)).data,
    onSuccess: () => {
      toast.success('Server deleted')
      qc.invalidateQueries({ queryKey: ['servers'] })
    },
    onError: (e) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const items = data?.items || []
  const total = data?.total || 0
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const liveById = live?.servers || {}

  const allChecked = items.length > 0 && items.every((s) => selected.has(s.id))
  const toggleAll = () =>
    setSelected(allChecked ? new Set() : new Set(items.map((s) => s.id)))
  const toggleOne = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const sortHeader = (key: string, label: string) => (
    <button
      type="button"
      className="inline-flex items-center gap-1 hover:text-text"
      onClick={() => {
        if (sort === key) setParam('order', order === 'asc' ? 'desc' : 'asc')
        else { setParam('sort', key); setParam('order', 'asc') }
      }}
    >
      {label}
      {sort === key
        ? order === 'asc' ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />
        : <ArrowUpDown className="h-3 w-3 opacity-40" />}
    </button>
  )

  const filtersActive = Boolean(status || osType || mode || tag || search)
  const tagOptions = useMemo(() => facets?.tags || [], [facets])

  const statusCount = (key: string) =>
    facets?.status?.find((f) => f.value === key)?.count

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="mb-1 flex items-center gap-2 text-xs text-muted">
            <Link to="/servers" className="inline-flex items-center gap-1 hover:text-primary">
              <Gauge className="h-3 w-3" /> Fleet dashboard
            </Link>
            <span>/</span>
            <span className="text-text2">Inventory</span>
          </div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Server className="h-5 w-5 text-primary" />
            Server Inventory
          </h1>
          <p className="text-sm text-muted">
            {total} server{total === 1 ? '' : 's'} · search, filter, and manage your fleet
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="inline-flex rounded-md border border-border bg-surface2/30 p-0.5">
            <button
              type="button"
              onClick={() => setView('table')}
              className={cn('rounded px-2 py-1', view === 'table' ? 'bg-surface text-text' : 'text-muted')}
              title="Table view"
            >
              <List className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => setView('cards')}
              className={cn('rounded px-2 py-1', view === 'cards' ? 'bg-surface text-text' : 'text-muted')}
              title="Card view"
            >
              <LayoutGrid className="h-3.5 w-3.5" />
            </button>
          </div>
          <Button variant="outline" size="sm" onClick={() => setFormOpen(true)}>
            <Plus className="h-3.5 w-3.5" /> Register
          </Button>
          <Button size="sm" onClick={() => setDeployOpen(true)}>
            <KeyRound className="h-3.5 w-3.5" /> Deploy agent
          </Button>
        </div>
      </div>

      {/* Status chips */}
      <div className="flex flex-wrap gap-1.5">
        {STATUS_CHIPS.map(({ key, label }) => {
          const active = status === key
          const count = key === '' ? total : statusCount(key)
          return (
            <button
              key={key || 'all'}
              type="button"
              onClick={() => setParam('status', key)}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition',
                active
                  ? 'border-primary/50 bg-primary/10 text-primary'
                  : 'border-border bg-surface2/30 text-muted hover:border-primary/30 hover:text-text',
              )}
            >
              {label}
              {count != null && <span className="tabular-nums opacity-70">({count})</span>}
            </button>
          )
        })}
      </div>

      {/* Filters */}
      <Card className="overflow-hidden">
        <PanelHeader icon={<Search className="h-3.5 w-3.5" />} title="Filters" hint={filtersActive ? 'active' : 'none'} />
        <CardContent className="flex flex-wrap items-center gap-2 p-3">
          <div className="relative min-w-[220px] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input
              className="h-9 pl-8"
              placeholder="Search name, IP, FQDN, owner, tags…"
              value={search}
              onChange={(e) => setParam('search', e.target.value)}
            />
          </div>
          <Select value={osType || 'all'} onValueChange={(v) => setParam('os', v === 'all' ? '' : v)}>
            <SelectTrigger className="h-9 w-[130px]"><SelectValue placeholder="OS" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All OS</SelectItem>
              {(facets?.os_type || []).map((f) => (
                <SelectItem key={f.value} value={f.value}>{f.value} ({f.count})</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={mode || 'all'} onValueChange={(v) => setParam('mode', v === 'all' ? '' : v)}>
            <SelectTrigger className="h-9 w-[150px]"><SelectValue placeholder="Collection" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All collection</SelectItem>
              {(facets?.collection_mode || []).map((f) => (
                <SelectItem key={f.value} value={f.value}>{f.value} ({f.count})</SelectItem>
              ))}
            </SelectContent>
          </Select>
          {tagOptions.length > 0 && (
            <Select value={tag || 'all'} onValueChange={(v) => setParam('tag', v === 'all' ? '' : v)}>
              <SelectTrigger className="h-9 w-[140px]">
                <Tags className="mr-1 h-3.5 w-3.5 text-muted" />
                <SelectValue placeholder="Tag" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All tags</SelectItem>
                {tagOptions.map((f) => (
                  <SelectItem key={f.value} value={f.value}>{f.value} ({f.count})</SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {filtersActive && (
            <Button
              variant="ghost" size="sm" className="text-muted"
              onClick={() => setParams(new URLSearchParams(), { replace: true })}
            >
              <X className="h-3.5 w-3.5" /> Clear all
            </Button>
          )}
        </CardContent>
      </Card>

      {/* Bulk bar */}
      {selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-primary/30 bg-primary/5 px-3 py-2">
          <span className="text-sm font-medium">{selected.size} selected</span>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Input
              className="h-8 w-44"
              placeholder="tag1, tag2"
              value={bulkTags}
              onChange={(e) => setBulkTags(e.target.value)}
            />
            <Button
              variant="outline" size="sm"
              disabled={!bulkTags.trim() || bulk.isPending}
              onClick={() => bulk.mutate({ action: 'add_tags', tags: bulkTags.split(',').map((t) => t.trim()).filter(Boolean) })}
            >
              <Tags className="h-3.5 w-3.5" /> Add tags
            </Button>
            <Button
              variant="outline" size="sm"
              disabled={!bulkTags.trim() || bulk.isPending}
              onClick={() => bulk.mutate({ action: 'remove_tags', tags: bulkTags.split(',').map((t) => t.trim()).filter(Boolean) })}
            >
              Remove tags
            </Button>
            <Button variant="outline" size="sm" onClick={() => setBulkConfirm('decommission')}>
              <CloudOff className="h-3.5 w-3.5" /> Decommission
            </Button>
            <Button variant="outline" size="sm" className="text-danger" onClick={() => setBulkConfirm('delete')}>
              <Trash2 className="h-3.5 w-3.5" /> Delete
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
              <X className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      )}

      {/* Results */}
      {view === 'table' ? (
        <Card className="overflow-hidden">
          <PanelHeader icon={<List className="h-3.5 w-3.5" />} title="Servers" hint={`page ${page} of ${pages}`} />
          <CardContent className="px-0 pb-2 pt-1">
            <div className="overflow-x-auto">
              <Table>
                <THead className="bg-surface2/40">
                  <Tr>
                    <Th className="w-8 pl-4">
                      <input type="checkbox" className="accent-primary" checked={allChecked} onChange={toggleAll} />
                    </Th>
                    <Th>{sortHeader('display_name', SORTABLE.display_name)}</Th>
                    <Th>{sortHeader('status', SORTABLE.status)}</Th>
                    <Th>{sortHeader('os_type', SORTABLE.os_type)}</Th>
                    <Th>CPU</Th>
                    <Th>Memory</Th>
                    <Th>Disk</Th>
                    <Th>Network</Th>
                    <Th>Agent</Th>
                    <Th>Tags</Th>
                    <Th>{sortHeader('last_seen', SORTABLE.last_seen)}</Th>
                    <Th className="w-10 pr-4" />
                  </Tr>
                </THead>
                <TBody>
                  {isLoading && (
                    [...Array(5)].map((_, i) => (
                      <Tr key={i}>
                        <Td colSpan={12} className="px-4"><Skeleton className="h-10 w-full" /></Td>
                      </Tr>
                    ))
                  )}
                  {!isLoading && items.length === 0 && (
                    <Tr>
                      <Td colSpan={12}>
                        <div className="flex flex-col items-center gap-2 py-12 text-center">
                          <Server className="h-8 w-8 text-muted/50" />
                          <div className="text-sm font-medium">
                            {filtersActive ? 'No servers match the current filters' : 'No servers yet'}
                          </div>
                          <div className="max-w-sm text-xs text-muted">
                            {filtersActive
                              ? 'Try clearing filters or broadening the search.'
                              : 'Deploy an agent or register a server to start monitoring.'}
                          </div>
                          {!filtersActive && (
                            <Button size="sm" className="mt-1" onClick={() => setDeployOpen(true)}>
                              <KeyRound className="h-3.5 w-3.5" /> Deploy agent
                            </Button>
                          )}
                        </div>
                      </Td>
                    </Tr>
                  )}
                  {items.map((s, i) => {
                    const lm = liveById[s.id] || {}
                    const liveStale = s.status === 'stale' || s.status === 'disabled'
                    return (
                      <Tr
                        key={s.id}
                        className={cn('cursor-pointer', i % 2 === 0 && 'bg-surface2/10')}
                        onClick={() => navigate(`/servers/${s.id}`)}
                      >
                        <Td className="pl-4" onClick={(e) => e.stopPropagation()}>
                          <input
                            type="checkbox" className="accent-primary"
                            checked={selected.has(s.id)}
                            onChange={() => toggleOne(s.id)}
                          />
                        </Td>
                        <Td className="py-2.5">
                          <div className="flex items-center gap-2.5">
                            <OsIcon os={s.os_type} />
                            <div className="min-w-0">
                              <div className="truncate text-sm font-medium">{s.display_name}</div>
                              <div className="truncate text-[11px] text-muted">
                                {s.primary_ip || s.fqdn || s.hostname || '—'}
                                {s.environment ? ` · ${s.environment}` : ''}
                              </div>
                            </div>
                          </div>
                        </Td>
                        <Td><ServerStatusBadge status={s.status} reasons={s.status_reasons} /></Td>
                        <Td>
                          <div className="text-xs">{s.os_name || s.os_type}</div>
                          <div className="text-[11px] text-muted">{s.os_version || ''}</div>
                        </Td>
                        <Td>{liveStale ? <span className="text-xs text-muted">—</span> : <UsageCell pct={lm.cpu_pct} warn={90} crit={98} />}</Td>
                        <Td>{liveStale ? <span className="text-xs text-muted">—</span> : <UsageCell pct={lm.memory_pct} warn={90} crit={97} />}</Td>
                        <Td>{liveStale ? <span className="text-xs text-muted">—</span> : <UsageCell pct={lm.disk_max_pct} />}</Td>
                        <Td>
                          <span className={cn('text-xs tabular-nums', liveStale && 'text-muted')}>
                            {liveStale || lm.net_bps == null ? '—' : formatBps(lm.net_bps * 8)}
                          </span>
                        </Td>
                        <Td>
                          <div className="flex flex-col gap-0.5">
                            <AgentStatusBadge status={s.agent_status} />
                            {s.agent_version && <span className="text-[10px] text-muted">v{s.agent_version}</span>}
                          </div>
                        </Td>
                        <Td onClick={(e) => e.stopPropagation()}>
                          <TagList tags={s.tags} onTagClick={(t) => setParam('tag', t === tag ? '' : t)} activeTag={tag} />
                        </Td>
                        <Td><span className="text-xs text-muted">{relativeTime(s.last_seen)}</span></Td>
                        <Td className="pr-4" onClick={(e) => e.stopPropagation()}>
                          <Button
                            variant="ghost" size="icon"
                            className="h-7 w-7 text-muted hover:text-danger"
                            onClick={() => setDeleteTarget(s)}
                            title="Delete server"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </Td>
                      </Tr>
                    )
                  })}
                </TBody>
              </Table>
            </div>
            {pages > 1 && (
              <div className="flex items-center justify-between border-t border-border px-4 py-2 text-xs text-muted">
                <span>Page {page} of {pages} · {total} servers</span>
                <div className="flex items-center gap-1">
                  <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setParam('page', String(page - 1))}>
                    <ChevronLeft className="h-3.5 w-3.5" />
                  </Button>
                  <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => setParam('page', String(page + 1))}>
                    <ChevronRight className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {isLoading && [...Array(6)].map((_, i) => <Skeleton key={i} className="h-40 rounded-lg" />)}
          {!isLoading && items.length === 0 && (
            <Card className="col-span-full p-10 text-center text-sm text-muted">No servers match</Card>
          )}
          {items.map((s) => {
            const lm = liveById[s.id] || {}
            const liveStale = s.status === 'stale' || s.status === 'disabled'
            return (
              <Card
                key={s.id}
                className="cursor-pointer overflow-hidden transition hover:border-primary/40"
                onClick={() => navigate(`/servers/${s.id}`)}
              >
                <div className="border-b border-border px-4 py-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <OsIcon os={s.os_type} />
                      <div className="min-w-0">
                        <div className="truncate font-semibold">{s.display_name}</div>
                        <div className="truncate text-[11px] text-muted">{s.primary_ip || s.hostname || '—'}</div>
                      </div>
                    </div>
                    <ServerStatusBadge status={s.status} />
                  </div>
                </div>
                <CardContent className="grid grid-cols-2 gap-2 p-3 text-xs">
                  <div>
                    <div className="text-[10px] uppercase text-muted">CPU</div>
                    {liveStale ? '—' : <UsageCell pct={lm.cpu_pct} />}
                  </div>
                  <div>
                    <div className="text-[10px] uppercase text-muted">Memory</div>
                    {liveStale ? '—' : <UsageCell pct={lm.memory_pct} />}
                  </div>
                  <div>
                    <div className="text-[10px] uppercase text-muted">Disk</div>
                    {liveStale ? '—' : <UsageCell pct={lm.disk_max_pct} />}
                  </div>
                  <div>
                    <div className="text-[10px] uppercase text-muted">Network</div>
                    <span className="tabular-nums">{liveStale || lm.net_bps == null ? '—' : formatBps(lm.net_bps * 8)}</span>
                  </div>
                  <div className="col-span-2 flex items-center justify-between pt-1">
                    <AgentStatusBadge status={s.agent_status} />
                    <span className="text-muted">{relativeTime(s.last_seen)}</span>
                  </div>
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}

      <InstallTokenDialog open={deployOpen} onOpenChange={setDeployOpen} />
      <ServerFormDialog open={formOpen} onOpenChange={setFormOpen} />
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(o) => { if (!o) setDeleteTarget(null) }}
        title="Delete server"
        description={`Remove ${deleteTarget?.display_name} and all its inventory, metrics references, and alerts?`}
        confirmText="Delete"
        destructive
        onConfirm={() => {
          if (deleteTarget) removeOne.mutate(deleteTarget.id)
          setDeleteTarget(null)
        }}
      />
      <ConfirmDialog
        open={Boolean(bulkConfirm)}
        onOpenChange={(o) => { if (!o) setBulkConfirm(null) }}
        title={bulkConfirm === 'delete' ? 'Delete selected servers' : 'Decommission selected servers'}
        description={
          bulkConfirm === 'delete'
            ? `Permanently delete ${selected.size} server(s) and their inventory?`
            : `Mark ${selected.size} server(s) as disabled and stop alerting on them?`
        }
        confirmText={bulkConfirm === 'delete' ? 'Delete' : 'Decommission'}
        destructive={bulkConfirm === 'delete'}
        onConfirm={() => {
          if (bulkConfirm) bulk.mutate({ action: bulkConfirm })
          setBulkConfirm(null)
        }}
      />
    </div>
  )
}

/** @deprecated use ServerInventoryPage — kept for import compatibility */
export const ServersPage = ServerInventoryPage
