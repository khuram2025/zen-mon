import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  Globe,
  HelpCircle,
  Network,
  Pencil,
  Play,
  Plug,
  Plus,
  Radar,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  X,
  XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import { ServiceCheckFormDialog } from '@/components/forms/ServiceCheckFormDialog'
import { toast } from '@/components/ui/Toast'
import type { ServiceCheck, ServiceCheckGroup, ServiceCheckSummary } from '@/types'

const PAGE_SIZE = 20

type ListResponse = { data: ServiceCheck[]; meta: { total: number; skip: number; limit: number } }

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

  const { data: listData, isFetching, refetch } = useQuery<ListResponse>({
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

  const checks = listData?.data || []
  const totalRecords = listData?.meta?.total || 0
  const totalPages = Math.max(1, Math.ceil(totalRecords / PAGE_SIZE))

  // Per-check uptime over 24h for the uptime column.
  const { data: uptimeStats } = useQuery<{ checks: Record<string, number> }>({
    queryKey: ['service-checks', 'uptime-stats', 24],
    queryFn: async () => (await api.get('/service-checks/uptime-stats?hours=24')).data,
    refetchInterval: 30_000,
  })
  const uptimeMap = uptimeStats?.checks || {}

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
      toast.success(
        'Test complete',
        up ? `Up${ms != null ? ` • ${Math.round(ms)} ms` : ''}` : `Down: ${data?.error || 'no response'}`,
      )
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

  const kpiClasses = useMemo(
    () => ({
      total: 'border-primary/30 ring-primary/30',
      up: 'border-success/30 ring-success/30',
      down: 'border-danger/30 ring-danger/30',
      warn: 'border-warning/40 ring-warning/40',
      unknown: 'border-border-strong ring-border-strong/40',
    }),
    [],
  )

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Activity className="h-5 w-5 text-primary" />
            Services
          </h1>
          <p className="text-xs text-muted">
            {summary?.total || 0} checks • {summary?.up || 0} up • {summary?.down || 0} down
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" onClick={handleExport}>
            <Download className="h-4 w-4" />
            Export
          </Button>
          <Button variant="outline" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
          <Button
            onClick={() => {
              setEditing(null)
              setFormOpen(true)
            }}
          >
            <Plus className="h-4 w-4" />
            Add check
          </Button>
        </div>
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
        <KpiCard
          icon={<Activity className="h-4 w-4" />}
          label="Total"
          value={summary?.total || 0}
          color="primary"
          active={!statusFilter && !typeFilter}
          onClick={() => patchParams({ status: null, type: null, page: '1' })}
          ring={kpiClasses.total}
        />
        <KpiCard
          icon={<CheckCircle2 className="h-4 w-4" />}
          label="Up"
          value={summary?.up || 0}
          color="success"
          active={statusFilter === 'up'}
          onClick={() => patchParams({ status: statusFilter === 'up' ? null : 'up', page: '1' })}
          ring={kpiClasses.up}
        />
        <KpiCard
          icon={<XCircle className="h-4 w-4" />}
          label="Down"
          value={summary?.down || 0}
          color="danger"
          active={statusFilter === 'down'}
          onClick={() => patchParams({ status: statusFilter === 'down' ? null : 'down', page: '1' })}
          ring={kpiClasses.down}
        />
        <KpiCard
          icon={<AlertTriangle className="h-4 w-4" />}
          label="Warning"
          value={(summary?.warning || 0) + (summary?.degraded || 0)}
          color="warning"
          active={statusFilter === 'warning'}
          onClick={() => patchParams({ status: statusFilter === 'warning' ? null : 'warning', page: '1' })}
          ring={kpiClasses.warn}
        />
        <KpiCard
          icon={<HelpCircle className="h-4 w-4" />}
          label="Unknown"
          value={summary?.unknown || 0}
          color="muted"
          active={statusFilter === 'unknown'}
          onClick={() => patchParams({ status: statusFilter === 'unknown' ? null : 'unknown', page: '1' })}
          ring={kpiClasses.unknown}
        />
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[220px] flex-1 max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
          <Input
            value={search}
            onChange={(e) => patchParams({ q: e.target.value || null, page: '1' })}
            placeholder="Search checks, hosts, URLs…"
            className="pl-9"
          />
        </div>
        <Select
          value={typeFilter || 'all'}
          onValueChange={(v) => patchParams({ type: v === 'all' ? null : v, page: '1' })}
        >
          <SelectTrigger className="w-[160px]">
            <SelectValue placeholder="Type" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All types</SelectItem>
            <SelectItem value="icmp">ICMP</SelectItem>
            <SelectItem value="tcp">TCP</SelectItem>
            <SelectItem value="http">HTTP(S)</SelectItem>
            <SelectItem value="tls">TLS</SelectItem>
            <SelectItem value="dns">DNS</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={statusFilter || 'all'}
          onValueChange={(v) => patchParams({ status: v === 'all' ? null : v, page: '1' })}
        >
          <SelectTrigger className="w-[160px]">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="up">Up</SelectItem>
            <SelectItem value="down">Down</SelectItem>
            <SelectItem value="warning">Warning</SelectItem>
            <SelectItem value="degraded">Degraded</SelectItem>
            <SelectItem value="unknown">Unknown</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={levelFilter || 'all'}
          onValueChange={(v) => patchParams({ level: v === 'all' ? null : v, page: '1' })}
        >
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder="Level" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All levels</SelectItem>
            <SelectItem value="1">L1 · availability</SelectItem>
            <SelectItem value="2">L2 · health</SelectItem>
            <SelectItem value="3">L3 · transaction</SelectItem>
          </SelectContent>
        </Select>
        <Select
          value={groupFilter || 'all'}
          onValueChange={(v) => patchParams({ group: v === 'all' ? null : v, page: '1' })}
        >
          <SelectTrigger className="w-[180px]">
            <SelectValue placeholder="Group" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All groups</SelectItem>
            {groupsList.map((g) => (
              <SelectItem key={g.id} value={g.id}>
                {g.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {tagsList.length > 0 && (
          <Select
            value={tagFilter || 'all'}
            onValueChange={(v) => patchParams({ tag: v === 'all' ? null : v, page: '1' })}
          >
            <SelectTrigger className="w-[160px]">
              <SelectValue placeholder="Tag" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All tags</SelectItem>
              {tagsList.map((t) => (
                <SelectItem key={t} value={t}>
                  {t}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <Link
          to="/services/groups"
          className="inline-flex h-9 items-center rounded-md border border-border bg-surface px-3 text-xs font-medium text-muted hover:border-border-strong hover:text-text"
        >
          Manage groups
        </Link>
        <Link
          to="/services/maintenance"
          className="inline-flex h-9 items-center rounded-md border border-border bg-surface px-3 text-xs font-medium text-muted hover:border-border-strong hover:text-text"
        >
          Maintenance
        </Link>
        <Link
          to="/services/templates"
          className="inline-flex h-9 items-center rounded-md border border-border bg-surface px-3 text-xs font-medium text-muted hover:border-border-strong hover:text-text"
        >
          Templates
        </Link>
        {activeFilterCount > 0 && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => patchParams({ q: null, type: null, status: null, group: null, tag: null, level: null, page: '1' })}
          >
            <X className="h-3.5 w-3.5" />
            Clear
          </Button>
        )}
      </div>

      {/* Selection toolbar */}
      {selected.size > 0 && (
        <div className="flex items-center justify-between rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs">
          <span className="font-medium text-primary">
            {selected.size} check{selected.size > 1 ? 's' : ''} selected
          </span>
          <div className="flex items-center gap-1">
            <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
              Clear
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="text-danger hover:text-danger"
              onClick={() => setBulkDeleteOpen(true)}
            >
              <Trash2 className="h-3.5 w-3.5" />
              Delete selected
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
                    <input
                      type="checkbox"
                      aria-label="Select all on page"
                      checked={checks.length > 0 && checks.every((c) => selected.has(c.id))}
                      onChange={toggleAll}
                      className="h-3.5 w-3.5 accent-primary"
                    />
                  </Th>
                  <Th>Status</Th>
                  <Th>Name</Th>
                  <Th>Type</Th>
                  <Th>Level</Th>
                  <Th>Group</Th>
                  <Th>Target</Th>
                  <Th>Device</Th>
                  <Th className="text-right">Uptime 24h</Th>
                  <Th className="text-right">Response</Th>
                  <Th className="text-right">Last check</Th>
                  <Th className="w-32 text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {checks.map((c) => (
                  <Tr
                    key={c.id}
                    className="cursor-pointer hover:bg-surface2/40"
                    onClick={() => navigate(`/services/${c.id}`)}
                  >
                    <Td onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={selected.has(c.id)}
                        onChange={() => toggleRow(c.id)}
                        className="h-3.5 w-3.5 accent-primary"
                      />
                    </Td>
                    <Td>
                      <StatusPill status={c.status} />
                      {c.in_maintenance && (
                        <span
                          className="ml-1.5 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider"
                          title="In maintenance window — alerts suppressed"
                          style={{ background: 'rgba(99,102,241,0.15)', color: '#818cf8' }}
                        >
                          Maint
                        </span>
                      )}
                      {c.check_type === 'tls' &&
                        c.tls_days_remaining != null &&
                        c.tls_days_remaining < 30 && (
                          <span
                            className={`ml-1.5 rounded px-1.5 py-0.5 font-mono text-[10px] font-bold ${
                              c.tls_days_remaining < 7
                                ? 'bg-danger/15 text-danger'
                                : c.tls_days_remaining < 14
                                  ? 'bg-warning/15 text-warning'
                                  : 'bg-yellow-500/15 text-yellow-500'
                            }`}
                          >
                            {c.tls_days_remaining}d
                          </span>
                        )}
                    </Td>
                    <Td className="font-medium">
                      <Link
                        to={`/services/${c.id}`}
                        className="hover:underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {c.name}
                      </Link>
                    </Td>
                    <Td>
                      <TypePill type={c.check_type} />
                    </Td>
                    <Td>
                      <LevelBadge level={c.level} />
                    </Td>
                    <Td className="text-xs">
                      {c.group_name ? (
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            patchParams({ group: c.group_id, page: '1' })
                          }}
                          className="rounded-full border border-border bg-surface2 px-2 py-0.5 text-muted hover:text-text"
                        >
                          {c.group_name}
                        </button>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                      {c.tags.length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {c.tags.slice(0, 4).map((t) => (
                            <button
                              key={t}
                              onClick={(e) => {
                                e.stopPropagation()
                                patchParams({ tag: t, page: '1' })
                              }}
                              className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary hover:bg-primary/20"
                            >
                              {t}
                            </button>
                          ))}
                          {c.tags.length > 4 && (
                            <span className="text-[10px] text-muted">+{c.tags.length - 4}</span>
                          )}
                        </div>
                      )}
                    </Td>
                    <Td className="max-w-[280px] truncate font-mono text-xs">
                      {c.target_url || `${c.target_host}${c.target_port ? `:${c.target_port}` : ''}`}
                    </Td>
                    <Td className="text-xs text-muted">
                      {c.device_id ? (
                        <Link
                          to={`/devices/${c.device_id}`}
                          className="hover:underline"
                          onClick={(e) => e.stopPropagation()}
                        >
                          {c.device_hostname || '—'}
                        </Link>
                      ) : (
                        '—'
                      )}
                    </Td>
                    <Td className="text-right font-mono text-xs">
                      <UptimeValue pct={uptimeMap[c.id]} />
                    </Td>
                    <Td className="text-right font-mono text-xs">
                      {c.last_response_ms != null ? `${c.last_response_ms.toFixed(0)} ms` : '—'}
                    </Td>
                    <Td className="text-right text-xs text-muted">
                      {relativeTime(c.last_check_at) || '—'}
                    </Td>
                    <Td onClick={(e) => e.stopPropagation()}>
                      <div className="flex justify-end gap-0.5">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          title="Test now"
                          onClick={() => testNow.mutate(c.id)}
                          disabled={testNow.isPending}
                        >
                          <Play className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          title="Edit"
                          onClick={() => {
                            setEditing(c)
                            setFormOpen(true)
                          }}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          title="Duplicate"
                          onClick={() => openClone(c)}
                        >
                          <Copy className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 text-muted hover:text-danger"
                          title="Delete"
                          onClick={() => setDeleting(c)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
                {checks.length === 0 && (
                  <Tr>
                    <Td colSpan={12} className="py-12 text-center text-muted">
                      {activeFilterCount > 0
                        ? 'No service checks match the current filters.'
                        : 'No service checks configured yet. Click “Add check” to create one.'}
                    </Td>
                  </Tr>
                )}
              </TBody>
            </Table>
          </div>

          {/* Pagination */}
          {totalRecords > 0 && (
            <div className="mt-3 flex items-center justify-between text-xs text-muted">
              <span>
                Showing <span className="text-text">{(page - 1) * PAGE_SIZE + 1}</span>–
                <span className="text-text">{Math.min(page * PAGE_SIZE, totalRecords)}</span> of{' '}
                <span className="text-text">{totalRecords}</span>
              </span>
              <div className="flex items-center gap-1">
                <Button
                  variant="outline"
                  size="icon"
                  className="h-7 w-7"
                  disabled={page <= 1}
                  onClick={() => patchParams({ page: String(page - 1) })}
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                </Button>
                <span className="min-w-[60px] text-center text-text">
                  Page {page} / {totalPages}
                </span>
                <Button
                  variant="outline"
                  size="icon"
                  className="h-7 w-7"
                  disabled={page >= totalPages}
                  onClick={() => patchParams({ page: String(page + 1) })}
                >
                  <ChevronRight className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <ServiceCheckFormDialog open={formOpen} onOpenChange={setFormOpen} check={editing} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete service check"
        description={
          <>
            Delete <span className="font-semibold text-text">{deleting?.name}</span>? This cannot be
            undone.
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

// ─────────────────────────────────────────────────────────────────────────────

function KpiCard({
  icon,
  label,
  value,
  color,
  active,
  onClick,
  ring,
}: {
  icon: React.ReactNode
  label: string
  value: number
  color: 'primary' | 'success' | 'warning' | 'danger' | 'muted'
  active?: boolean
  onClick?: () => void
  ring?: string
}) {
  const iconCls =
    color === 'primary'
      ? 'text-primary bg-primary/10'
      : color === 'success'
        ? 'text-success bg-success/10'
        : color === 'warning'
          ? 'text-warning bg-warning/10'
          : color === 'danger'
            ? 'text-danger bg-danger/10'
            : 'text-muted bg-muted/10'
  const Wrapper: any = onClick ? 'button' : 'div'
  return (
    <Wrapper
      type={onClick ? 'button' : undefined}
      onClick={onClick}
      className={`group relative flex flex-col gap-2 overflow-hidden rounded-xl border p-4 text-left transition-all ${
        active ? `${ring || ''} ring-1 bg-surface` : 'border-border bg-surface hover:border-border-strong'
      }`}
    >
      <div className="flex items-center justify-between">
        <span className={`inline-flex h-7 w-7 items-center justify-center rounded-md ${iconCls}`}>
          {icon}
        </span>
      </div>
      <div>
        <div className="text-[11px] font-medium uppercase tracking-wider text-muted">{label}</div>
        <div className="mt-0.5 text-2xl font-bold leading-tight tracking-tight tabular-nums">
          {value.toLocaleString()}
        </div>
      </div>
    </Wrapper>
  )
}

function StatusPill({ status }: { status: ServiceCheck['status'] }) {
  const map: Record<string, { label: string; cls: string }> = {
    up: { label: 'Up', cls: 'border-success/30 bg-success/10 text-success' },
    down: { label: 'Down', cls: 'border-danger/30 bg-danger/10 text-danger' },
    warning: { label: 'Warning', cls: 'border-warning/30 bg-warning/10 text-warning' },
    degraded: { label: 'Degraded', cls: 'border-yellow-500/30 bg-yellow-500/10 text-yellow-400' },
    unknown: { label: 'Unknown', cls: 'border-border-strong bg-surface2 text-muted' },
  }
  const s = map[status] || map.unknown
  return (
    <Badge variant="outline" className={`border ${s.cls}`}>
      {s.label}
    </Badge>
  )
}

function TypePill({ type }: { type: ServiceCheck['check_type'] }) {
  const map: Record<string, { Icon: any; cls: string }> = {
    http: { Icon: Globe, cls: 'border-primary/30 bg-primary/10 text-primary' },
    tcp: { Icon: Plug, cls: 'border-success/30 bg-success/10 text-success' },
    tls: { Icon: ShieldCheck, cls: 'border-warning/30 bg-warning/10 text-warning' },
    icmp: { Icon: Radar, cls: 'border-accent/30 bg-accent/10 text-accent' },
    dns: { Icon: Network, cls: 'border-yellow-500/30 bg-yellow-500/10 text-yellow-400' },
  }
  const c = map[type] || map.http
  const Icon = c.Icon
  return (
    <Badge variant="outline" className={`border uppercase ${c.cls}`}>
      <Icon className="h-3 w-3" />
      <span>{type}</span>
    </Badge>
  )
}

function LevelBadge({ level }: { level: number | undefined }) {
  const lvl = level || 1
  const cls =
    lvl === 1 ? 'border-success/30 bg-success/10 text-success'
    : lvl === 2 ? 'border-primary/30 bg-primary/10 text-primary'
    : 'border-accent/30 bg-accent/10 text-accent'
  return (
    <Badge variant="outline" className={`border ${cls}`} title={`Monitoring level ${lvl}`}>
      L{lvl}
    </Badge>
  )
}

function UptimeValue({ pct }: { pct: number | undefined }) {
  if (pct == null) return <span className="text-muted">—</span>
  const cls = pct >= 99 ? 'text-success' : pct >= 95 ? 'text-warning' : 'text-danger'
  return <span className={`font-medium ${cls}`}>{pct.toFixed(2)}%</span>
}
