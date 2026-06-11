/** Server Inventory — fleet-wide list of monitored servers with live
 *  health, resource gauges, tags, and agent state. Entry point of the
 *  Servers module. */

import { useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Bot,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CloudOff,
  KeyRound,
  Plus,
  Search,
  Server,
  Tags,
  Trash2,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, formatBps, relativeTime } from '@/lib/utils'
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
  AgentStatusBadge, KpiTile, OsIcon, ServerStatusBadge, TagList, UsageBar,
} from '@/components/servers/shared'
import type {
  ServerFacets, ServerItem, ServerListResponse, ServerLiveMetrics, ServerMonitoringOverview,
} from '@/types/servers'

const SORTABLE: Record<string, string> = {
  display_name: 'Server',
  status: 'Status',
  os_type: 'OS',
  last_seen: 'Last seen',
}

export function ServersPage() {
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

  const { data: overview } = useQuery<ServerMonitoringOverview>({
    queryKey: ['server-monitoring', 'overview'],
    queryFn: async () => (await api.get('/server-monitoring/overview')).data,
    refetchInterval: 30_000,
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

  const counts = overview?.status_counts || {}
  const agentOnline = overview?.agent_counts?.online || 0
  const attention = (counts.warning || 0) + (counts.critical || 0)

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

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <Server className="h-5 w-5 text-primary" />
            Server Inventory
          </h1>
          <p className="text-xs text-muted">
            {total} server{total === 1 ? '' : 's'} monitored via agent and agentless collection
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" onClick={() => setFormOpen(true)}>
            <Plus className="h-4 w-4" /> Register server
          </Button>
          <Button onClick={() => setDeployOpen(true)}>
            <KeyRound className="h-4 w-4" /> Deploy agent
          </Button>
        </div>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <KpiTile icon={Server} label="Servers" value={overview?.total ?? '—'} />
        <KpiTile icon={CheckCircle2} label="Healthy" value={counts.healthy || 0} tone="success" />
        <KpiTile
          icon={AlertTriangle} label="Needs attention" value={attention}
          tone={attention > 0 ? 'danger' : 'default'}
          sub={attention > 0 ? `${counts.critical || 0} critical · ${counts.warning || 0} warning` : undefined}
        />
        <KpiTile icon={CloudOff} label="Stale" value={counts.stale || 0} tone={(counts.stale || 0) > 0 ? 'warning' : 'default'} />
        <KpiTile icon={Bot} label="Agents online" value={agentOnline} tone="info" />
      </div>

      {/* Filters */}
      <Card>
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
          <Select value={status || 'all'} onValueChange={(v) => setParam('status', v === 'all' ? '' : v)}>
            <SelectTrigger className="h-9 w-[140px]"><SelectValue placeholder="Status" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              {(facets?.status || []).map((f) => (
                <SelectItem key={f.value} value={f.value}>{f.value} ({f.count})</SelectItem>
              ))}
            </SelectContent>
          </Select>
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
              <X className="h-3.5 w-3.5" /> Clear
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

      {/* Table */}
      <Card>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th className="w-8">
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
                  <Th className="w-10" />
                </Tr>
              </THead>
              <TBody>
                {isLoading && (
                  [...Array(3)].map((_, i) => (
                    <Tr key={i}>
                      <Td colSpan={12}><Skeleton className="h-8 w-full" /></Td>
                    </Tr>
                  ))
                )}
                {!isLoading && items.length === 0 && (
                  <Tr>
                    <Td colSpan={12}>
                      <div className="flex flex-col items-center gap-2 py-10 text-center">
                        <Server className="h-8 w-8 text-muted/50" />
                        <div className="text-sm font-medium">
                          {filtersActive ? 'No servers match the current filters' : 'No servers yet'}
                        </div>
                        <div className="max-w-sm text-xs text-muted">
                          {filtersActive
                            ? 'Try clearing filters or broadening the search.'
                            : 'Deploy an agent to a Windows or Linux host — it registers itself here and starts reporting within a minute.'}
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
                {items.map((s) => {
                  const lm = liveById[s.id] || {}
                  const liveStale = s.status === 'stale' || s.status === 'disabled'
                  return (
                    <Tr
                      key={s.id}
                      className="cursor-pointer"
                      onClick={() => navigate(`/servers/${s.id}`)}
                    >
                      <Td onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox" className="accent-primary"
                          checked={selected.has(s.id)}
                          onChange={() => toggleOne(s.id)}
                        />
                      </Td>
                      <Td>
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
                      <Td>{liveStale ? <span className="text-xs text-muted">—</span> : <UsageBar pct={lm.cpu_pct ?? null} warn={90} crit={98} />}</Td>
                      <Td>{liveStale ? <span className="text-xs text-muted">—</span> : <UsageBar pct={lm.memory_pct ?? null} warn={90} crit={97} />}</Td>
                      <Td>{liveStale ? <span className="text-xs text-muted">—</span> : <UsageBar pct={lm.disk_max_pct ?? null} />}</Td>
                      <Td>
                        <span className={cn('text-xs tabular-nums', liveStale && 'text-muted')}>
                          {liveStale || lm.net_bps == null ? '—' : formatBps(lm.net_bps * 8)}
                        </span>
                      </Td>
                      <Td>
                        <div className="flex flex-col gap-0.5">
                          <AgentStatusBadge status={s.agent_status} />
                          {s.agent_version && (
                            <span className="text-[10px] text-muted">v{s.agent_version}</span>
                          )}
                        </div>
                      </Td>
                      <Td onClick={(e) => e.stopPropagation()}>
                        <TagList tags={s.tags} onTagClick={(t) => setParam('tag', t === tag ? '' : t)} activeTag={tag} />
                      </Td>
                      <Td>
                        <span className="text-xs text-muted">{relativeTime(s.last_seen)}</span>
                      </Td>
                      <Td onClick={(e) => e.stopPropagation()}>
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

          {/* Pagination */}
          {pages > 1 && (
            <div className="mt-3 flex items-center justify-between text-xs text-muted">
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

      {/* Top consumers strip */}
      {overview && (overview.top_cpu.length > 0 || overview.top_memory.length > 0) && (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {([
            ['Top CPU', overview.top_cpu, '%'],
            ['Top Memory', overview.top_memory, '%'],
            ['Top Disk', overview.top_disk, '%'],
            ['Top Network', overview.top_network, 'bps'],
          ] as const).map(([title, list, unit]) => (
            <Card key={title}>
              <CardContent className="p-3">
                <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted">
                  <Activity className="h-3 w-3" /> {title} <span className="font-normal normal-case">(10 min)</span>
                </div>
                {list.length === 0 ? (
                  <div className="py-2 text-xs text-muted">No recent samples</div>
                ) : (
                  <div className="space-y-1">
                    {list.map((t) => (
                      <button
                        key={t.server_id}
                        type="button"
                        onClick={() => navigate(`/servers/${t.server_id}`)}
                        className="flex w-full items-center justify-between gap-2 rounded px-1.5 py-1 text-left text-xs hover:bg-surface2"
                      >
                        <span className="truncate">{t.display_name || t.hostname || t.server_id.slice(0, 8)}</span>
                        <span className="shrink-0 font-medium tabular-nums">
                          {unit === 'bps' ? formatBps(t.value * 8) : `${t.value.toFixed(1)}%`}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Dialogs */}
      <InstallTokenDialog open={deployOpen} onOpenChange={setDeployOpen} />
      <ServerFormDialog open={formOpen} onOpenChange={setFormOpen} />
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(o) => { if (!o) setDeleteTarget(null) }}
        title="Delete server"
        description={`Remove ${deleteTarget?.display_name} and all its inventory, metrics references, and alerts? The agent (if any) will be rejected until re-enrolled.`}
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
