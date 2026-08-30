import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  Globe,
  LockKeyhole,
  Network,
  Pause,
  Pencil,
  Play,
  Plug,
  Plus,
  Radar,
  Route,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  Wrench,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Card } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Skeleton } from '@/components/ui/Skeleton'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { ServiceCheckFormDialog } from '@/components/forms/ServiceCheckFormDialog'
import { toast } from '@/components/ui/Toast'
import {
  BAND_TEXT,
  PulseDot,
  UptimeBars,
  buildDaySeries,
  dayTitle,
  pulseStatusOf,
  uptimeBand,
} from '@/components/services/uptime'
import type { ServiceCheck, ServiceCheckGroup, ServiceCheckSummary } from '@/types'

const PAGE_SIZE = 20
const BAR_DAYS = 30

type ListResponse = { data: ServiceCheck[]; meta: { total: number; skip: number; limit: number } }
type DailyUptimeResponse = {
  days: number
  checks: Record<string, Array<{ date: string; uptime_pct: number | null; sample_count: number }>>
}

const TYPE_META: Record<string, { label: string; Icon: typeof Globe }> = {
  http: { label: 'HTTP', Icon: Globe },
  tcp: { label: 'TCP', Icon: Plug },
  tls: { label: 'TLS', Icon: ShieldCheck },
  icmp: { label: 'ICMP', Icon: Radar },
  dns: { label: 'DNS', Icon: Network },
}

export function ServicesPage() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()

  const search = params.get('q') || ''
  const typeFilter = params.get('type') || ''
  const statusFilter = params.get('status') || ''
  const groupFilter = params.get('group') || ''
  const tagFilter = params.get('tag') || ''
  const levelFilter = params.get('level') || ''
  const page = Math.max(1, Number(params.get('page') || '1') || 1)

  function patchParams(p: Record<string, string | null>) {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(p)) {
      if (!v) next.delete(k)
      else next.set(k, v)
    }
    setParams(next, { replace: true })
  }

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ServiceCheck | null>(null)
  const [deleting, setDeleting] = useState<ServiceCheck | null>(null)
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const { data: summary } = useQuery<ServiceCheckSummary>({
    queryKey: ['service-checks', 'summary'],
    queryFn: async () => (await api.get('/service-checks/summary')).data,
    refetchInterval: 15_000,
  })

  const { data: listData, isFetching, isLoading, refetch } = useQuery<ListResponse>({
    queryKey: [
      'service-checks',
      'list',
      { search, typeFilter, statusFilter, groupFilter, tagFilter, levelFilter, page },
    ],
    queryFn: async () => {
      const qs = new URLSearchParams()
      qs.set('limit', String(PAGE_SIZE))
      qs.set('skip', String((page - 1) * PAGE_SIZE))
      if (search) qs.set('search', search)
      if (typeFilter) qs.set('check_type', typeFilter)
      if (statusFilter) qs.set('status', statusFilter)
      if (groupFilter) qs.set('group_id', groupFilter)
      if (tagFilter) qs.set('tag', tagFilter)
      if (levelFilter) qs.set('level', levelFilter)
      return (await api.get(`/service-checks?${qs.toString()}`)).data
    },
    refetchInterval: 15_000,
  })

  const { data: groupsList = [] } = useQuery<ServiceCheckGroup[]>({
    queryKey: ['service-check-groups'],
    queryFn: async () => (await api.get('/service-check-groups')).data,
    refetchInterval: 60_000,
  })

  const { data: tagsList = [] } = useQuery<string[]>({
    queryKey: ['service-checks', 'tags'],
    queryFn: async () => (await api.get('/service-checks/tags')).data,
    refetchInterval: 60_000,
  })

  // Failing monitors surface first within the page, the way uptime tools triage.
  const statusRank = (c: ServiceCheck) =>
    !c.enabled ? 4 : c.status === 'down' ? 0 : c.status === 'warning' || c.status === 'degraded' ? 1 : c.status === 'unknown' ? 3 : 2
  const checks = [...(listData?.data || [])].sort((a, b) => statusRank(a) - statusRank(b) || a.name.localeCompare(b.name))
  const totalRecords = listData?.meta?.total || 0
  const totalPages = Math.max(1, Math.ceil(totalRecords / PAGE_SIZE))

  const { data: uptimeStats } = useQuery<{ checks: Record<string, number> }>({
    queryKey: ['service-checks', 'uptime-stats', 24],
    queryFn: async () => (await api.get('/service-checks/uptime-stats?hours=24')).data,
    refetchInterval: 30_000,
  })
  const uptimeMap = uptimeStats?.checks || {}

  const { data: dailyUptime } = useQuery<DailyUptimeResponse>({
    queryKey: ['service-checks', 'daily-uptime', BAR_DAYS],
    queryFn: async () => (await api.get(`/service-checks/daily-uptime?days=${BAR_DAYS}`)).data,
    refetchInterval: 120_000,
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/service-checks/${id}`),
    onSuccess: () => {
      toast.success('Service check deleted')
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const bulkDelete = useMutation({
    mutationFn: async (ids: string[]) =>
      (await api.post('/service-checks/bulk-delete', { check_ids: ids })).data as { deleted: number },
    onSuccess: (res) => {
      toast.success(`Deleted ${res.deleted} check${res.deleted === 1 ? '' : 's'}`)
      setSelected(new Set())
      setBulkDeleteOpen(false)
      qc.invalidateQueries({ queryKey: ['service-checks'] })
    },
    onError: (e: any) => toast.error('Bulk delete failed', apiErrorMessage(e)),
  })

  const testNow = useMutation({
    mutationFn: async (id: string) => (await api.post(`/service-checks/${id}/test`)).data,
    onSuccess: (data: any) => {
      const up = data?.status === 'up' || data?.is_up
      const ms = data?.response_time_ms ?? data?.response_ms
      const stepSummary = data?.details?.steps_total
        ? ` • ${data.details.steps_passed}/${data.details.steps_total} steps passed`
        : ''
      if (up) toast.success('Test complete', `Up${ms != null ? ` • ${Math.round(ms)} ms` : ''}${stepSummary}`)
      else toast.error('Test detected a failure', `${data?.error || 'No response'}${stepSummary}`)
      qc.invalidateQueries({ queryKey: ['service-checks'] })
    },
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  function handleExport() {
    api
      .get<unknown[]>('/service-checks/export/json')
      .then((res: any) => {
        const payload = Array.isArray(res) ? res : res?.data
        const blob = new Blob([JSON.stringify(payload ?? [], null, 2)], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `service-checks-${new Date().toISOString().slice(0, 10)}.json`
        a.click()
        URL.revokeObjectURL(url)
        toast.success('Exported', `${(payload ?? []).length} check${(payload ?? []).length === 1 ? '' : 's'}`)
      })
      .catch((e) => toast.error('Export failed', apiErrorMessage(e)))
  }

  function toggleRow(id: string) {
    const n = new Set(selected)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    setSelected(n)
  }

  function toggleAll() {
    if (checks.length === 0) return
    const allOn = checks.every((c) => selected.has(c.id))
    const n = new Set(selected)
    if (allOn) checks.forEach((c) => n.delete(c.id))
    else checks.forEach((c) => n.add(c.id))
    setSelected(n)
  }

  function openClone(c: ServiceCheck) {
    setEditing({ ...c, id: '', name: `${c.name} (copy)` } as ServiceCheck)
    setFormOpen(true)
  }

  const activeFilterCount =
    (typeFilter ? 1 : 0) +
    (statusFilter ? 1 : 0) +
    (groupFilter ? 1 : 0) +
    (tagFilter ? 1 : 0) +
    (levelFilter ? 1 : 0) +
    (search ? 1 : 0)

  const warnCount = (summary?.warning || 0) + (summary?.degraded || 0)

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight">Services</h1>
          <p className="mt-0.5 text-xs text-muted">
            Synthetic uptime monitoring — HTTP, TCP, TLS, ICMP and DNS probes from this appliance.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <QuietLink to="/services/groups" icon={<Route className="h-3.5 w-3.5" />} label="Groups" />
          <QuietLink to="/services/maintenance" icon={<Wrench className="h-3.5 w-3.5" />} label="Maintenance" />
          <QuietLink to="/services/templates" icon={<Copy className="h-3.5 w-3.5" />} label="Templates" />
          <span className="hidden h-5 w-px bg-border sm:inline-block" />
          <Button variant="outline" size="sm" className="h-8" onClick={handleExport}>
            <Download className="h-3.5 w-3.5" /> Export
          </Button>
          <Button variant="outline" size="sm" className="h-8" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={cn('h-3.5 w-3.5', isFetching && 'animate-spin')} /> Refresh
          </Button>
          <Button
            size="sm"
            className="h-8"
            onClick={() => {
              setEditing(null)
              setFormOpen(true)
            }}
          >
            <Plus className="h-3.5 w-3.5" /> Add monitor
          </Button>
        </div>
      </div>

      {/* Status chips + filters */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1 rounded-lg border border-border bg-surface p-1">
          <StatusChip
            label="All"
            count={summary?.total || 0}
            active={!statusFilter}
            onClick={() => patchParams({ status: null, page: '1' })}
          />
          <StatusChip
            label="Up"
            count={summary?.up || 0}
            dot="bg-success"
            active={statusFilter === 'up'}
            onClick={() => patchParams({ status: statusFilter === 'up' ? null : 'up', page: '1' })}
          />
          <StatusChip
            label="Down"
            count={summary?.down || 0}
            dot="bg-danger"
            attention={(summary?.down || 0) > 0}
            active={statusFilter === 'down'}
            onClick={() => patchParams({ status: statusFilter === 'down' ? null : 'down', page: '1' })}
          />
          <StatusChip
            label="Warning"
            count={warnCount}
            dot="bg-warning"
            active={statusFilter === 'warning'}
            onClick={() => patchParams({ status: statusFilter === 'warning' ? null : 'warning', page: '1' })}
          />
          <StatusChip
            label="Unknown"
            count={summary?.unknown || 0}
            dot="bg-muted/50"
            active={statusFilter === 'unknown'}
            onClick={() => patchParams({ status: statusFilter === 'unknown' ? null : 'unknown', page: '1' })}
          />
        </div>

        <div className="relative min-w-[200px] max-w-xs flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <Input
            value={search}
            onChange={(e) => patchParams({ q: e.target.value || null, page: '1' })}
            placeholder="Search monitors…"
            className="h-8 pl-8 text-xs"
          />
        </div>
        <Select value={typeFilter || 'all'} onValueChange={(v) => patchParams({ type: v === 'all' ? null : v, page: '1' })}>
          <SelectTrigger className="h-8 w-[120px] text-xs"><SelectValue placeholder="Type" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All types</SelectItem>
            <SelectItem value="http">HTTP(S)</SelectItem>
            <SelectItem value="tcp">TCP</SelectItem>
            <SelectItem value="tls">TLS</SelectItem>
            <SelectItem value="icmp">ICMP</SelectItem>
            <SelectItem value="dns">DNS</SelectItem>
          </SelectContent>
        </Select>
        <Select value={levelFilter || 'all'} onValueChange={(v) => patchParams({ level: v === 'all' ? null : v, page: '1' })}>
          <SelectTrigger className="h-8 w-[130px] text-xs"><SelectValue placeholder="Level" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All levels</SelectItem>
            <SelectItem value="1">L1 · availability</SelectItem>
            <SelectItem value="2">L2 · health</SelectItem>
            <SelectItem value="3">L3 · transaction</SelectItem>
          </SelectContent>
        </Select>
        <Select value={groupFilter || 'all'} onValueChange={(v) => patchParams({ group: v === 'all' ? null : v, page: '1' })}>
          <SelectTrigger className="h-8 w-[150px] text-xs"><SelectValue placeholder="Group" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All groups</SelectItem>
            {groupsList.map((g) => (
              <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        {tagsList.length > 0 && (
          <Select value={tagFilter || 'all'} onValueChange={(v) => patchParams({ tag: v === 'all' ? null : v, page: '1' })}>
            <SelectTrigger className="h-8 w-[130px] text-xs"><SelectValue placeholder="Tag" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All tags</SelectItem>
              {tagsList.map((t) => (
                <SelectItem key={t} value={t}>{t}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        {activeFilterCount > 0 && (
          <Button
            variant="ghost"
            size="sm"
            className="h-8"
            onClick={() => patchParams({ q: null, type: null, status: null, group: null, tag: null, level: null, page: '1' })}
          >
            <X className="h-3.5 w-3.5" /> Clear
          </Button>
        )}
      </div>

      {/* Selection toolbar */}
      {selected.size > 0 && (
        <div className="flex items-center justify-between rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs">
          <span className="font-medium text-primary">
            {selected.size} monitor{selected.size > 1 ? 's' : ''} selected
          </span>
          <div className="flex items-center gap-1">
            <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>Clear</Button>
            <Button
              variant="outline"
              size="sm"
              className="text-danger hover:text-danger"
              onClick={() => setBulkDeleteOpen(true)}
            >
              <Trash2 className="h-3.5 w-3.5" /> Delete selected
            </Button>
          </div>
        </div>
      )}

      {/* Monitor list */}
      <Card className="overflow-hidden">
        <div className="hidden items-center gap-3 border-b border-border bg-surface2/40 px-4 py-2 text-[10px] font-semibold uppercase tracking-wider text-muted lg:flex">
          <span className="w-4 shrink-0">
            <input
              type="checkbox"
              aria-label="Select all on page"
              checked={checks.length > 0 && checks.every((c) => selected.has(c.id))}
              onChange={toggleAll}
              className="h-3.5 w-3.5 accent-primary"
            />
          </span>
          <span className="w-[30%] min-w-[220px]">Monitor</span>
          <span className="min-w-0 flex-1">Last {BAR_DAYS} days</span>
          <span className="w-16 text-right">24 h</span>
          <span className="w-[72px] text-right">Response</span>
          <span className="w-14 text-right">Every</span>
          <span className="w-[132px] text-right">Actions</span>
        </div>

        {isLoading ? (
          <div className="space-y-0 divide-y divide-border/60">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="px-4 py-3"><Skeleton className="h-10 w-full" /></div>
            ))}
          </div>
        ) : checks.length === 0 ? (
          <div className="flex flex-col items-center justify-center px-4 py-16 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-primary/10 text-primary">
              <Activity className="h-5 w-5" />
            </span>
            <h3 className="mt-3 text-sm font-semibold">
              {activeFilterCount > 0 ? 'No monitors match these filters' : 'No monitors yet'}
            </h3>
            <p className="mt-1 max-w-sm text-xs leading-relaxed text-muted">
              {activeFilterCount > 0
                ? 'Try clearing a filter, or search for a different name, host or URL.'
                : 'Create your first uptime monitor — an HTTP endpoint, TCP port, ping target, TLS certificate or DNS record.'}
            </p>
            <div className="mt-4">
              {activeFilterCount > 0 ? (
                <Button variant="outline" size="sm" onClick={() => patchParams({ q: null, type: null, status: null, group: null, tag: null, level: null, page: '1' })}>
                  Clear filters
                </Button>
              ) : (
                <Button size="sm" onClick={() => { setEditing(null); setFormOpen(true) }}>
                  <Plus className="h-3.5 w-3.5" /> Add monitor
                </Button>
              )}
            </div>
          </div>
        ) : (
          <div className="divide-y divide-border/60">
            {checks.map((c) => (
              <MonitorRow
                key={c.id}
                check={c}
                selected={selected.has(c.id)}
                onToggle={() => toggleRow(c.id)}
                uptime24h={uptimeMap[c.id]}
                dailyRows={dailyUptime?.checks?.[c.id]}
                onOpen={() => navigate(`/services/${c.id}`)}
                onTest={() => testNow.mutate(c.id)}
                testPending={testNow.isPending}
                onEdit={() => { setEditing(c); setFormOpen(true) }}
                onClone={() => openClone(c)}
                onDelete={() => setDeleting(c)}
              />
            ))}
          </div>
        )}

        {/* Pagination */}
        {totalRecords > PAGE_SIZE && (
          <div className="flex items-center justify-between border-t border-border px-4 py-2.5 text-xs text-muted">
            <span>
              Showing <span className="text-text">{(page - 1) * PAGE_SIZE + 1}</span>–
              <span className="text-text">{Math.min(page * PAGE_SIZE, totalRecords)}</span> of{' '}
              <span className="text-text">{totalRecords}</span>
            </span>
            <div className="flex items-center gap-1">
              <Button variant="outline" size="icon" className="h-7 w-7" disabled={page <= 1} onClick={() => patchParams({ page: String(page - 1) })}>
                <ChevronLeft className="h-3.5 w-3.5" />
              </Button>
              <span className="min-w-[60px] text-center text-text">Page {page} / {totalPages}</span>
              <Button variant="outline" size="icon" className="h-7 w-7" disabled={page >= totalPages} onClick={() => patchParams({ page: String(page + 1) })}>
                <ChevronRight className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>
        )}
      </Card>

      <ServiceCheckFormDialog open={formOpen} onOpenChange={setFormOpen} check={editing} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete service check"
        description={
          <>
            Delete <span className="font-semibold text-text">{deleting?.name}</span>? This cannot be undone.
          </>
        }
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => {
          if (deleting) del.mutate(deleting.id)
        }}
      />
      <ConfirmDialog
        open={bulkDeleteOpen}
        onOpenChange={setBulkDeleteOpen}
        title={`Delete ${selected.size} service check${selected.size > 1 ? 's' : ''}`}
        description="This cannot be undone."
        confirmText="Delete all"
        destructive
        loading={bulkDelete.isPending}
        onConfirm={() => bulkDelete.mutate(Array.from(selected))}
      />
    </div>
  )
}

/* ─── Row ───────────────────────────────────────────────────────────────── */

function MonitorRow({
  check: c, selected, onToggle, uptime24h, dailyRows, onOpen, onTest, testPending, onEdit, onClone, onDelete,
}: {
  check: ServiceCheck
  selected: boolean
  onToggle: () => void
  uptime24h: number | undefined
  dailyRows?: Array<{ date: string; uptime_pct: number | null; sample_count: number }>
  onOpen: () => void
  onTest: () => void
  testPending: boolean
  onEdit: () => void
  onClone: () => void
  onDelete: () => void
}) {
  const t = TYPE_META[c.check_type] || TYPE_META.http
  const target = c.target_url || `${c.target_host}${c.target_port ? `:${c.target_port}` : ''}`
  const pulse = pulseStatusOf(c.status, c.enabled)
  const statusText = !c.enabled
    ? 'Paused'
    : c.status === 'up' ? 'Up'
      : c.status === 'down' ? 'Down'
        : c.status === 'unknown' ? 'Pending'
          : 'Warning'
  const statusTone = !c.enabled
    ? 'text-muted'
    : c.status === 'up' ? 'text-success'
      : c.status === 'down' ? 'text-danger'
        : c.status === 'unknown' ? 'text-muted'
          : 'text-warning'
  const cells = buildDaySeries(dailyRows || [], BAR_DAYS).map((d) => ({ key: d.key, pct: d.pct, title: dayTitle(d) }))
  const band = uptimeBand(uptime24h)

  return (
    <div
      role="link"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => { if (e.key === 'Enter') onOpen() }}
      className="group flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3 transition-colors hover:bg-surface2/40 focus:outline-none focus-visible:bg-surface2/60 lg:flex-nowrap"
    >
      <span className="w-4 shrink-0" onClick={(e) => e.stopPropagation()}>
        <input type="checkbox" checked={selected} onChange={onToggle} className="h-3.5 w-3.5 accent-primary" aria-label={`Select ${c.name}`} />
      </span>

      {/* Identity */}
      <div className="flex w-full min-w-[220px] items-center gap-3 lg:w-[30%]">
        <PulseDot status={pulse} size="md" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-semibold text-text">{c.name}</span>
            <span className="inline-flex shrink-0 items-center gap-1 rounded border border-border bg-surface2/60 px-1.5 py-px text-[9.5px] font-semibold uppercase tracking-wider text-muted">
              <t.Icon className="h-2.5 w-2.5" />{t.label}
            </span>
            {c.in_maintenance && (
              <span className="inline-flex shrink-0 items-center gap-1 rounded bg-info/10 px-1.5 py-px text-[9.5px] font-semibold uppercase tracking-wider text-info" title="In maintenance — alerts suppressed">
                <Wrench className="h-2.5 w-2.5" />Maint
              </span>
            )}
            {c.credential_id && (
              <LockKeyhole className="h-3 w-3 shrink-0 text-success" aria-label="Authenticated check" />
            )}
            {c.check_type === 'tls' && c.tls_days_remaining != null && c.tls_days_remaining < 30 && (
              <span className={cn(
                'shrink-0 rounded px-1.5 py-px font-mono text-[9.5px] font-bold',
                c.tls_days_remaining < 7 ? 'bg-danger/15 text-danger' : 'bg-warning/15 text-warning',
              )}>
                cert {c.tls_days_remaining}d
              </span>
            )}
          </div>
          <div className="mt-0.5 flex items-center gap-1.5 text-[11px]">
            <span className={cn('shrink-0 font-medium', statusTone)}>{statusText}</span>
            <span className="text-border-strong">·</span>
            <span className="truncate font-mono text-muted" title={target}>{target}</span>
          </div>
        </div>
      </div>

      {/* 30-day bars */}
      <div className="hidden h-7 min-w-0 flex-1 lg:block">
        <UptimeBars cells={cells} className="h-full" />
      </div>

      {/* 24h uptime */}
      <span className={cn('w-16 shrink-0 text-right font-mono text-xs font-medium tabular-nums', BAND_TEXT[band])}>
        {uptime24h != null ? `${uptime24h.toFixed(uptime24h >= 99.995 ? 0 : 2)}%` : '—'}
      </span>

      {/* Response */}
      <span className="w-[72px] shrink-0 text-right font-mono text-xs tabular-nums text-text2">
        {c.last_response_ms != null ? `${c.last_response_ms.toFixed(0)} ms` : '—'}
      </span>

      {/* Interval */}
      <span className="w-14 shrink-0 text-right text-[11px] tabular-nums text-muted" title={`Checked every ${c.check_interval}s · last ${relativeTime(c.last_check_at) || 'never'}`}>
        {c.check_interval}s
      </span>

      {/* Actions */}
      <div className="flex w-[132px] shrink-0 justify-end gap-0.5 opacity-60 transition-opacity group-hover:opacity-100" onClick={(e) => e.stopPropagation()}>
        <Button variant="ghost" size="icon" className="h-7 w-7" title="Test now" onClick={onTest} disabled={testPending}>
          {c.enabled ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
        </Button>
        <Button variant="ghost" size="icon" className="h-7 w-7" title="Edit" onClick={onEdit}>
          <Pencil className="h-3.5 w-3.5" />
        </Button>
        <Button variant="ghost" size="icon" className="h-7 w-7" title="Duplicate" onClick={onClone}>
          <Copy className="h-3.5 w-3.5" />
        </Button>
        <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" title="Delete" onClick={onDelete}>
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  )
}

/* ─── Small pieces ──────────────────────────────────────────────────────── */

function StatusChip({ label, count, dot, active, attention, onClick }: {
  label: string
  count: number
  dot?: string
  active?: boolean
  attention?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors',
        active ? 'bg-surface2 text-text shadow-sm ring-1 ring-border' : 'text-muted hover:text-text',
      )}
    >
      {dot && <span className={cn('h-1.5 w-1.5 rounded-full', dot, attention && 'animate-pulse-soft')} />}
      {label}
      <span className={cn('font-mono tabular-nums', attention ? 'font-semibold text-danger' : 'text-muted')}>{count}</span>
    </button>
  )
}

function QuietLink({ to, icon, label }: { to: string; icon: React.ReactNode; label: string }) {
  return (
    <Link
      to={to}
      className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 text-xs font-medium text-muted transition-colors hover:border-border-strong hover:text-text"
    >
      {icon}
      {label}
    </Link>
  )
}
