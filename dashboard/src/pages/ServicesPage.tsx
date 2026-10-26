import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useIsFetching, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ClockAlert,
  Copy,
  EyeOff,
  FlaskConical,
  FolderInput,
  HelpCircle,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Route,
  Search,
  SlidersHorizontal,
  Tags,
  Trash2,
  Wrench,
  X,
  XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { serverNow, useClockOffset } from '@/lib/serverClock'
import { apiErrorMessage, cn } from '@/lib/utils'
import { useAuth } from '@/stores/auth'
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
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { ServiceCheckFormDialog } from '@/components/forms/ServiceCheckFormDialog'
import { toast } from '@/components/ui/Toast'
import { ClockSkewBanner } from '@/components/services/list/ClockSkewBanner'
import { SavedViews } from '@/components/services/list/SavedViews'
import { ExportMenu, ImportDialog } from '@/components/services/list/ExportImport'
import { BAR_DAYS, MonitorRow, type LoadState } from '@/components/services/list/MonitorRow'
import {
  BULK_TEST_CAP, BulkActionDialog, type BulkAction, type BulkItem, type BulkResult,
} from '@/components/services/list/BulkActionDialog'
import {
  DEFAULT_SORT,
  FILTER_PARAM_KEYS,
  LEVEL_META,
  PAGE_SIZES,
  SORT_OPTIONS,
  STATUS_SETS,
  browserTimezone,
  canOperate,
  clientSort,
  isStale,
  parseStatusSet,
  statusParamFor,
  statusSetKeyOf,
  type DailyUptimeResponse,
  type ListResponse,
  type ListRow,
  type StatusSetKey,
  type SummaryExt,
} from '@/components/services/list/model'
import type { ServiceCheck, ServiceCheckGroup } from '@/types'

type KpiKey = 'all' | StatusSetKey | 'paused' | 'stale' | 'maint'

/** Appliance "now", re-evaluated periodically so freshness labels age on screen. */
function useServerNow(everyMs: number): number {
  const [now, setNow] = useState(() => serverNow())
  useEffect(() => {
    const t = window.setInterval(() => setNow(serverNow()), everyMs)
    return () => window.clearInterval(t)
  }, [everyMs])
  return now
}

function boolParam(v: string): boolean | null {
  return v === 'true' ? true : v === 'false' ? false : null
}

export function ServicesPage() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const user = useAuth((s) => s.user)
  const operator = canOperate(user)
  const { offsetMs } = useClockOffset()
  const tickNow = useServerNow(30_000)
  // Recompute when calibration moves "now" as well as on the periodic tick.
  const nowMs = useMemo(() => serverNow(), [tickNow, offsetMs]) // eslint-disable-line react-hooks/exhaustive-deps

  const search = params.get('q') || ''
  const typeFilter = params.get('type') || ''
  const statusFilter = params.get('status') || ''
  const groupFilter = params.get('group') || ''
  const tagFilter = params.get('tag') || ''
  const levelFilter = params.get('level') || ''
  const enabledFilter = params.get('enabled') || ''
  const staleFilter = params.get('stale') || ''
  const maintFilter = params.get('maint') || ''
  const authFilter = params.get('auth') || ''
  const sensorFilter = params.get('sensor') || ''
  const sortParam = params.get('sort') || ''
  const sort = SORT_OPTIONS.some((o) => o.value === sortParam) ? sortParam : DEFAULT_SORT
  const sizeParam = Number(params.get('size'))
  const pageSize = (PAGE_SIZES as readonly number[]).includes(sizeParam) ? sizeParam : PAGE_SIZES[0]
  const page = Math.max(1, Number(params.get('page') || '1') || 1)
  const statusSet = parseStatusSet(statusFilter)

  function patchParams(p: Record<string, string | null>) {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(p)) {
      if (!v) next.delete(k)
      else next.set(k, v)
    }
    setParams(next, { replace: true })
  }

  const clearFilters = () =>
    patchParams({ ...Object.fromEntries(FILTER_PARAM_KEYS.map((k) => [k, null])), page: '1' })

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ServiceCheck | null>(null)
  const [deleting, setDeleting] = useState<ServiceCheck | null>(null)
  // id → name, so selections kept across filters/pages can still be named.
  const [selected, setSelected] = useState<Map<string, string>>(new Map())
  const [bulkAction, setBulkAction] = useState<BulkAction | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [testing, setTesting] = useState<Set<string>>(new Set())
  const [refreshing, setRefreshing] = useState(false)
  // Sticky: set once the appliance is seen to predate the list contract.
  const [legacyApi, setLegacyApi] = useState(false)
  const advancedCount = [enabledFilter, staleFilter, maintFilter, authFilter, sensorFilter].filter(Boolean).length
  const [moreOpen, setMoreOpen] = useState(advancedCount > 0)

  /* ── Queries ─────────────────────────────────────────────────────────── */

  const summaryQ = useQuery<SummaryExt>({
    queryKey: ['service-checks', 'summary'],
    queryFn: async () => (await api.get('/service-checks/summary')).data,
    refetchInterval: 15_000,
  })
  const summary = summaryQ.data

  /** API filter params shared by the list and the availability report. */
  const filterQuery = useMemo(() => {
    const qs = new URLSearchParams()
    if (search) qs.set('search', search)
    if (typeFilter) qs.set('check_type', typeFilter)
    // An older appliance matches `status` exactly; a set is then filtered on the page.
    if (statusSet.length && !(legacyApi && statusSet.length > 1)) qs.set('status', statusSet.join(','))
    if (groupFilter) qs.set('group_id', groupFilter)
    if (tagFilter) qs.set('tag', tagFilter)
    if (levelFilter) qs.set('level', levelFilter)
    if (enabledFilter) qs.set('enabled', enabledFilter)
    if (staleFilter) qs.set('stale', staleFilter)
    if (maintFilter) qs.set('in_maintenance', maintFilter)
    if (authFilter) qs.set('has_credential', authFilter)
    if (sensorFilter) qs.set('sensor_id', sensorFilter)
    return qs
  }, [search, typeFilter, statusFilter, groupFilter, tagFilter, levelFilter, enabledFilter, staleFilter, maintFilter, authFilter, sensorFilter, legacyApi]) // eslint-disable-line react-hooks/exhaustive-deps

  const listQ = useQuery<ListResponse>({
    queryKey: ['service-checks', 'list', filterQuery.toString(), sort, pageSize, page],
    queryFn: async () => {
      const qs = new URLSearchParams(filterQuery)
      qs.set('limit', String(pageSize))
      qs.set('skip', String((page - 1) * pageSize))
      qs.set('sort', sort)
      return (await api.get(`/service-checks?${qs.toString()}`)).data
    },
    placeholderData: (prev) => prev,
    refetchInterval: 15_000,
  })
  const listData = listQ.data

  useEffect(() => {
    if (!listData || legacyApi) return
    const first = listData.data?.[0]
    const modern = listData.meta?.server_time != null || (first != null && typeof first.stale === 'boolean')
    if (!modern && first != null) setLegacyApi(true)
  }, [listData, legacyApi])

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

  // Monitoring-site filter: only when this user can list sensors and some exist.
  const { data: sensors = [] } = useQuery<Array<{ id: string; name: string; site_name?: string | null }>>({
    queryKey: ['sensors', 'for-service-filter'],
    queryFn: async () => (await api.get('/sensors')).data,
    retry: false,
    staleTime: 5 * 60_000,
  })

  const uptimeQ = useQuery<{ checks: Record<string, number | null> }>({
    queryKey: ['service-checks', 'uptime-stats', 24, 'confirmed'],
    queryFn: async () => (await api.get('/service-checks/uptime-stats?hours=24&basis=confirmed')).data,
    refetchInterval: 30_000,
  })
  const uptimeMap = uptimeQ.data?.checks || {}
  const uptimeState: LoadState = uptimeQ.data ? 'ready' : uptimeQ.isError ? 'error' : 'loading'

  const reportingTimezone = browserTimezone()
  const dailyQ = useQuery<DailyUptimeResponse>({
    queryKey: ['service-checks', 'daily-uptime', BAR_DAYS, 'confirmed', reportingTimezone],
    queryFn: async () => (await api.get(`/service-checks/daily-uptime?days=${BAR_DAYS}&basis=confirmed&timezone=${encodeURIComponent(reportingTimezone)}`)).data,
    refetchInterval: 120_000,
  })
  const barsState: LoadState = dailyQ.data ? 'ready' : dailyQ.isError ? 'error' : 'loading'

  /* ── Rows ────────────────────────────────────────────────────────────── */

  const checks: ListRow[] = useMemo(() => {
    let rows = listData?.data || []
    if (legacyApi) {
      // The appliance ignored the newer filters/sort: apply them within this page.
      if (statusSet.length > 1) rows = rows.filter((c) => statusSet.includes(c.status))
      const en = boolParam(enabledFilter)
      if (en != null) rows = rows.filter((c) => c.enabled === en)
      const st = boolParam(staleFilter)
      if (st != null) rows = rows.filter((c) => isStale(c, nowMs) === st)
      const mt = boolParam(maintFilter)
      if (mt != null) rows = rows.filter((c) => !!c.in_maintenance === mt)
      const au = boolParam(authFilter)
      if (au != null) rows = rows.filter((c) => !!c.credential_id === au)
      rows = clientSort(rows, sort, nowMs)
    }
    return rows
  }, [listData, legacyApi, statusFilter, enabledFilter, staleFilter, maintFilter, authFilter, sort, nowMs]) // eslint-disable-line react-hooks/exhaustive-deps

  const totalRecords = listData?.meta?.total || 0
  const totalPages = Math.max(1, Math.ceil(totalRecords / pageSize))
  const legacyPageFilter = legacyApi && (statusSet.length > 1 || !!(enabledFilter || staleFilter || maintFilter || authFilter))

  // A bulk delete or narrower filter can leave the page past the end.
  useEffect(() => {
    if (listData && !listQ.isPlaceholderData && page > totalPages) patchParams({ page: String(totalPages) })
  }, [listData, page, totalPages]) // eslint-disable-line react-hooks/exhaustive-deps

  const visibleIds = new Set(checks.map((c) => c.id))
  const hiddenSelected = [...selected.keys()].filter((id) => !visibleIds.has(id)).length

  /* ── Mutations ───────────────────────────────────────────────────────── */

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/service-checks/${id}`),
    onSuccess: (_r, id) => {
      toast.success('Service check deleted')
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      setSelected((s) => { const n = new Map(s); n.delete(id); return n })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  async function runTest(c: ListRow) {
    setTesting((s) => new Set(s).add(c.id))
    try {
      const data: any = (await api.post(`/service-checks/${c.id}/test`)).data
      const up = data?.status === 'up' || data?.is_up
      const ms = data?.response_time_ms ?? data?.response_ms
      const stepSummary = data?.details?.steps_total
        ? ` • ${data.details.steps_passed}/${data.details.steps_total} steps passed`
        : ''
      if (up) toast.success(`${c.name}: test passed`, `Up${ms != null ? ` • ${Math.round(ms)} ms` : ''}${stepSummary}`)
      else toast.error(`${c.name}: test detected a failure`, `${data?.error || 'No response'}${stepSummary}`)
    } catch (e) {
      toast.error(`${c.name}: test failed`, apiErrorMessage(e))
    } finally {
      setTesting((s) => { const n = new Set(s); n.delete(c.id); return n })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
    }
  }

  /* ── Refresh (B23) ───────────────────────────────────────────────────── */

  const fetchingChecks = useIsFetching({ queryKey: ['service-checks'] })
  const fetchingGroups = useIsFetching({ queryKey: ['service-check-groups'] })
  const busy = refreshing || fetchingChecks + fetchingGroups > 0

  async function refreshAll() {
    const started = Date.now()
    setRefreshing(true)
    try {
      await Promise.all([
        qc.refetchQueries({ queryKey: ['service-checks'], type: 'active' }),
        qc.refetchQueries({ queryKey: ['service-check-groups'], type: 'active' }),
      ])
    } finally {
      setRefreshing(false)
    }
    const cache = qc.getQueryCache()
    const queries = [
      ...cache.findAll({ queryKey: ['service-checks'], type: 'active' }),
      ...cache.findAll({ queryKey: ['service-check-groups'], type: 'active' }),
    ]
    const failed = queries.filter((q) => q.state.status === 'error' && q.state.errorUpdatedAt >= started)
    if (failed.length) {
      toast.error('Refresh incomplete', `${failed.length} of ${queries.length} data sources failed; the last loaded values are kept and marked.`)
    }
  }

  /* ── Selection (UI 8) ────────────────────────────────────────────────── */

  function toggleRow(c: ListRow) {
    setSelected((s) => {
      const n = new Map(s)
      if (n.has(c.id)) n.delete(c.id)
      else n.set(c.id, c.name)
      return n
    })
  }

  const allOnPage = checks.length > 0 && checks.every((c) => selected.has(c.id))
  function toggleAll() {
    if (checks.length === 0) return
    setSelected((s) => {
      const n = new Map(s)
      if (allOnPage) checks.forEach((c) => n.delete(c.id))
      else checks.forEach((c) => n.set(c.id, c.name))
      return n
    })
  }

  const bulkItems: BulkItem[] = [...selected.entries()]
    .map(([id, name]) => ({ id, name, visible: visibleIds.has(id) }))
    .sort((a, b) => Number(b.visible) - Number(a.visible) || a.name.localeCompare(b.name))

  function onBulkFinished(action: BulkAction, results: BulkResult[]) {
    qc.invalidateQueries({ queryKey: ['service-checks'] })
    qc.invalidateQueries({ queryKey: ['service-check-groups'] })
    if (action === 'delete') {
      const gone = new Set(results.filter((r) => r.ok).map((r) => r.id))
      setSelected((s) => new Map([...s].filter(([id]) => !gone.has(id))))
    }
  }

  function openClone(c: ServiceCheck) {
    setEditing({ ...c, id: '', name: `${c.name} (copy)` } as ServiceCheck)
    setFormOpen(true)
  }

  /* ── KPIs (B20) ──────────────────────────────────────────────────────── */

  const statusKey = statusSetKeyOf(statusFilter)
  const otherKpiFilters = [enabledFilter, staleFilter, maintFilter].filter(Boolean).length
  const activeKpi: KpiKey | null =
    !statusFilter && !otherKpiFilters ? 'all'
      : statusKey && !otherKpiFilters ? statusKey
        : !statusFilter && otherKpiFilters === 1 && enabledFilter === 'false' ? 'paused'
          : !statusFilter && otherKpiFilters === 1 && staleFilter === 'true' ? 'stale'
            : !statusFilter && otherKpiFilters === 1 && maintFilter === 'true' ? 'maint'
              : null

  function applyKpi(k: KpiKey) {
    const reset: Record<string, string | null> = { status: null, enabled: null, stale: null, maint: null, page: '1' }
    if (k === 'all' || activeKpi === k) return patchParams(reset)
    if (k === 'paused') return patchParams({ ...reset, enabled: 'false' })
    if (k === 'stale') return patchParams({ ...reset, stale: 'true' })
    if (k === 'maint') return patchParams({ ...reset, maint: 'true' })
    return patchParams({ ...reset, status: statusParamFor(k) })
  }

  const sumCount = (keys: Array<keyof SummaryExt>): number | null => {
    if (!summary) return null
    let total = 0
    for (const k of keys) {
      const v = summary[k]
      if (typeof v !== 'number') return null
      total += v
    }
    return total
  }
  const summaryState: LoadState = summary ? 'ready' : summaryQ.isError ? 'error' : 'loading'
  const warnCount = sumCount([...STATUS_SETS.warning] as Array<keyof SummaryExt>)
  const uptimeValues = Object.values(uptimeMap).filter((value): value is number => value != null)
  const fleetUptime = uptimeValues.length
    ? uptimeValues.reduce((s, v) => s + v, 0) / uptimeValues.length
    : null
  const staleCount = sumCount(['stale'])
  const notReported = 'not reported by this appliance version'

  const activeFilterCount = FILTER_PARAM_KEYS.filter((k) => params.get(k)).length
  const statusSelectValue = !statusFilter ? 'all' : statusKey || 'custom'

  const listError = listQ.isError && !listData
  const listRefreshFailed = listQ.isError && !!listData && !listQ.isPlaceholderData
  const summaryRefreshFailed = summaryQ.isError && !!summary
  const availabilityFailed = uptimeQ.isError || dailyQ.isError

  return (
    <div className="min-w-0 space-y-4">
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
          <ExportMenu filterQuery={filterQuery} canImport={operator} onImport={() => setImportOpen(true)} />
          <Button variant="outline" size="sm" className="h-8" onClick={refreshAll} disabled={refreshing} title="Refresh the list, KPIs and availability">
            <RefreshCw className={cn('h-3.5 w-3.5', busy && 'animate-spin')} /> Refresh
          </Button>
          {operator && (
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
          )}
        </div>
      </div>

      <ClockSkewBanner />

      {/* Status stat cards — click to filter */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 2xl:grid-cols-8">
        <StatCard
          label="Monitors"
          count={sumCount(['total'])}
          state={summaryState}
          icon={<Activity className="h-3.5 w-3.5" />}
          tone="primary"
          sub={fleetUptime != null ? `${fleetUptime.toFixed(2)}% fleet uptime · 24 h` : uptimeQ.isError ? 'fleet uptime unavailable' : 'across this appliance'}
          active={activeKpi === 'all'}
          onClick={() => applyKpi('all')}
        />
        <StatCard
          label="Up"
          count={sumCount(['up'])}
          state={summaryState}
          icon={<CheckCircle2 className="h-3.5 w-3.5" />}
          tone="success"
          sub={summary?.total ? `${Math.round(((summary.up || 0) / summary.total) * 100)}% of fleet · stored status` : '—'}
          active={activeKpi === 'up'}
          onClick={() => applyKpi('up')}
        />
        <StatCard
          label="Down"
          count={sumCount(['down'])}
          state={summaryState}
          icon={<XCircle className="h-3.5 w-3.5" />}
          tone="danger"
          attention={(summary?.down || 0) > 0}
          sub={(summary?.down || 0) > 0 ? 'needs attention now' : 'all clear'}
          active={activeKpi === 'down'}
          onClick={() => applyKpi('down')}
        />
        <StatCard
          label="Warning"
          count={warnCount}
          state={summaryState}
          icon={<AlertTriangle className="h-3.5 w-3.5" />}
          tone="warning"
          sub="warning or degraded"
          active={activeKpi === 'warning'}
          onClick={() => applyKpi('warning')}
        />
        <StatCard
          label="Unknown"
          count={sumCount(['unknown'])}
          state={summaryState}
          icon={<HelpCircle className="h-3.5 w-3.5" />}
          tone="muted"
          sub="no result recorded yet"
          active={activeKpi === 'unknown'}
          onClick={() => applyKpi('unknown')}
        />
        <StatCard
          label="Stale evidence"
          count={staleCount}
          state={summaryState}
          icon={<ClockAlert className="h-3.5 w-3.5" />}
          tone="warning"
          attention={false}
          sub={staleCount == null && summary ? notReported : staleCount ? 'status is last known only' : 'all results current'}
          active={activeKpi === 'stale'}
          onClick={() => applyKpi('stale')}
        />
        <StatCard
          label="Paused"
          count={sumCount(['paused'])}
          state={summaryState}
          icon={<Pause className="h-3.5 w-3.5" />}
          tone="muted"
          sub={summary && summary.paused == null ? notReported : 'not probing or alerting'}
          active={activeKpi === 'paused'}
          onClick={() => applyKpi('paused')}
        />
        <StatCard
          label="In maintenance"
          count={sumCount(['in_maintenance'])}
          state={summaryState}
          icon={<Wrench className="h-3.5 w-3.5" />}
          tone="info"
          sub={summary && summary.in_maintenance == null ? notReported : 'alerts suppressed'}
          active={activeKpi === 'maint'}
          onClick={() => applyKpi('maint')}
        />
      </div>

      {(summaryState === 'error' || summaryRefreshFailed || availabilityFailed) && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {summaryState === 'error' && (
            <ErrorChip label="Status counts could not be loaded" onRetry={() => summaryQ.refetch()} />
          )}
          {summaryRefreshFailed && (
            <ErrorChip label={`Status counts from ${clock(summaryQ.dataUpdatedAt)} — refresh failed`} onRetry={() => summaryQ.refetch()} />
          )}
          {uptimeQ.isError && (
            <ErrorChip
              label={uptimeQ.data ? `24 h availability from ${clock(uptimeQ.dataUpdatedAt)} — refresh failed` : '24 h availability could not be loaded'}
              onRetry={() => uptimeQ.refetch()}
            />
          )}
          {dailyQ.isError && (
            <ErrorChip
              label={dailyQ.data ? `Daily history from ${clock(dailyQ.dataUpdatedAt)} — refresh failed` : 'Daily history could not be loaded (not a monitoring gap)'}
              onRetry={() => dailyQ.refetch()}
            />
          )}
        </div>
      )}

      {/* Filters */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[180px] flex-1 sm:max-w-xs">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input
              value={search}
              onChange={(e) => patchParams({ q: e.target.value || null, page: '1' })}
              placeholder="Search monitors…"
              aria-label="Search monitors"
              className="h-8 pl-8 text-xs"
            />
          </div>
          <Select
            value={statusSelectValue}
            onValueChange={(v) => {
              if (v === 'custom') return
              patchParams({ status: v === 'all' ? null : statusParamFor(v as StatusSetKey), page: '1' })
            }}
          >
            <SelectTrigger className="h-8 w-[150px] text-xs" aria-label="Status"><SelectValue placeholder="Status" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              <SelectItem value="down">Down</SelectItem>
              <SelectItem value="warning">Warning or degraded</SelectItem>
              <SelectItem value="unknown">Unknown</SelectItem>
              <SelectItem value="up">Up</SelectItem>
              {statusSelectValue === 'custom' && <SelectItem value="custom">{statusSet.join(', ')}</SelectItem>}
            </SelectContent>
          </Select>
          <Select value={typeFilter || 'all'} onValueChange={(v) => patchParams({ type: v === 'all' ? null : v, page: '1' })}>
            <SelectTrigger className="h-8 w-[112px] text-xs" aria-label="Type"><SelectValue placeholder="Type" /></SelectTrigger>
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
            <SelectTrigger className="h-8 w-[150px] text-xs" aria-label="Level" title={levelFilter ? LEVEL_META[Number(levelFilter)]?.hint : 'Monitoring depth'}>
              <SelectValue placeholder="Level" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All levels</SelectItem>
              {[1, 2, 3].map((l) => (
                <SelectItem key={l} value={String(l)}>
                  <span className="flex flex-col">
                    <span>{LEVEL_META[l].label}</span>
                    <span className="text-xs text-muted">{LEVEL_META[l].hint}</span>
                  </span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={groupFilter || 'all'} onValueChange={(v) => patchParams({ group: v === 'all' ? null : v, page: '1' })}>
            <SelectTrigger className="h-8 w-[140px] text-xs" aria-label="Group"><SelectValue placeholder="Group" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All groups</SelectItem>
              {groupsList.map((g) => (
                <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          {(tagsList.length > 0 || tagFilter) && (
            <Select value={tagFilter || 'all'} onValueChange={(v) => patchParams({ tag: v === 'all' ? null : v, page: '1' })}>
              <SelectTrigger className="h-8 w-[130px] text-xs" aria-label="Tag"><SelectValue placeholder="Tag" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All tags</SelectItem>
                {tagFilter && !tagsList.includes(tagFilter) && <SelectItem value={tagFilter}>{tagFilter}</SelectItem>}
                {tagsList.map((t) => (
                  <SelectItem key={t} value={t}>{t}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <Button
            variant={moreOpen || advancedCount ? 'secondary' : 'ghost'}
            size="sm"
            className="h-8"
            aria-expanded={moreOpen}
            onClick={() => setMoreOpen((o) => !o)}
          >
            <SlidersHorizontal className="h-3.5 w-3.5" /> More{advancedCount ? ` (${advancedCount})` : ''}
          </Button>
          {activeFilterCount > 0 && (
            <Button variant="ghost" size="sm" className="h-8" onClick={clearFilters}>
              <X className="h-3.5 w-3.5" /> Clear
            </Button>
          )}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Select value={sort} onValueChange={(v) => patchParams({ sort: v === DEFAULT_SORT ? null : v, page: '1' })}>
              <SelectTrigger className="h-8 w-[180px] text-xs" aria-label="Sort order"><SelectValue /></SelectTrigger>
              <SelectContent>
                {SORT_OPTIONS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
              </SelectContent>
            </Select>
            <SavedViews params={params} onApply={(q) => setParams(new URLSearchParams(q), { replace: true })} />
          </div>
        </div>

        {moreOpen && (
          <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface2/30 p-2">
            <TriSelect label="State" value={enabledFilter} onChange={(v) => patchParams({ enabled: v, page: '1' })} yes="Enabled" no="Paused" />
            <TriSelect label="Evidence" value={staleFilter} onChange={(v) => patchParams({ stale: v, page: '1' })} yes="Stale evidence" no="Current evidence" />
            <TriSelect label="Maintenance" value={maintFilter} onChange={(v) => patchParams({ maint: v, page: '1' })} yes="In maintenance" no="Not in maintenance" />
            <TriSelect label="Authentication" value={authFilter} onChange={(v) => patchParams({ auth: v, page: '1' })} yes="Uses a credential" no="No credential" />
            {!legacyApi && (sensors.length > 0 || sensorFilter) && (
              <Select value={sensorFilter || 'all'} onValueChange={(v) => patchParams({ sensor: v === 'all' ? null : v, page: '1' })}>
                <SelectTrigger className="h-8 w-[170px] text-xs" aria-label="Monitoring site"><SelectValue placeholder="Monitoring site" /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">Any monitoring site</SelectItem>
                  {sensorFilter && !sensors.some((s) => s.id === sensorFilter) && <SelectItem value={sensorFilter}>Selected sensor</SelectItem>}
                  {sensors.map((s) => (
                    <SelectItem key={s.id} value={s.id}>{s.site_name ? `${s.name} · ${s.site_name}` : s.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
        )}

        {legacyPageFilter && (
          <p className="text-xs text-warning">
            This appliance version filters these conditions within the current page only; counts and paging may not reflect them.
          </p>
        )}
      </div>

      {/* Selection toolbar */}
      {selected.size > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs">
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium text-primary">
              {selected.size} monitor{selected.size > 1 ? 's' : ''} selected
            </span>
            {hiddenSelected > 0 && (
              <span className="inline-flex items-center gap-1 text-warning" title="Selected monitors that the current filters or page do not show">
                <EyeOff className="h-3 w-3" /> {hiddenSelected} not visible in current view
              </span>
            )}
            <Button variant="ghost" size="sm" className="h-7 px-2" onClick={() => setSelected(new Map())}>
              <X className="h-3.5 w-3.5" /> Clear selection
            </Button>
          </span>
          {operator && (
            <div className="flex flex-wrap items-center gap-1">
              <Button variant="outline" size="sm" className="h-7" onClick={() => setBulkAction('pause')}><Pause className="h-3.5 w-3.5" /> Pause</Button>
              <Button variant="outline" size="sm" className="h-7" onClick={() => setBulkAction('resume')}><Play className="h-3.5 w-3.5" /> Resume</Button>
              <Button
                variant="outline"
                size="sm"
                className="h-7"
                onClick={() => setBulkAction('test')}
                disabled={selected.size > BULK_TEST_CAP}
                title={selected.size > BULK_TEST_CAP ? `Test runs at most ${BULK_TEST_CAP} monitors at a time` : 'Run one probe per monitor now; schedules and SLA policy are unchanged'}
              >
                <FlaskConical className="h-3.5 w-3.5" /> Test
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="outline" size="sm" className="h-7"><Tags className="h-3.5 w-3.5" /> Tags</Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent className="min-w-[160px]">
                  <DropdownMenuItem onSelect={() => setBulkAction('add_tags')}>Add tags…</DropdownMenuItem>
                  <DropdownMenuItem onSelect={() => setBulkAction('remove_tags')}>Remove tags…</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <Button variant="outline" size="sm" className="h-7" onClick={() => setBulkAction('set_group')}><FolderInput className="h-3.5 w-3.5" /> Move to group</Button>
              <Button variant="outline" size="sm" className="h-7" onClick={() => setBulkAction('maintenance')}><Wrench className="h-3.5 w-3.5" /> Maintenance</Button>
              <span className="mx-1 hidden h-5 w-px bg-border sm:inline-block" />
              <Button variant="outline" size="sm" className="h-7 text-danger hover:text-danger" onClick={() => setBulkAction('delete')}>
                <Trash2 className="h-3.5 w-3.5" /> Delete
              </Button>
            </div>
          )}
        </div>
      )}

      {listRefreshFailed && (
        <ErrorChip
          label={`Showing monitors as of ${clock(listQ.dataUpdatedAt)} — the latest refresh failed (${apiErrorMessage(listQ.error)})`}
          onRetry={() => listQ.refetch()}
        />
      )}

      {/* Monitor list */}
      <Card className={cn('overflow-hidden', listQ.isPlaceholderData && 'opacity-70 transition-opacity')}>
        <div className="hidden items-center gap-3 border-b border-border bg-surface2/40 px-4 py-2 text-2xs font-semibold uppercase tracking-wider text-muted lg:flex">
          <span className="w-4 shrink-0">
            {operator && (
              <input
                type="checkbox"
                aria-label="Select all on this page"
                title="Select all on this page"
                checked={allOnPage}
                onChange={toggleAll}
                className="h-3.5 w-3.5 accent-primary"
              />
            )}
          </span>
          <span className="w-[30%] min-w-[220px]">Monitor</span>
          <span className="min-w-0 flex-1" title={`Confirmed availability per calendar day (${reportingTimezone})`}>Last {BAR_DAYS} days</span>
          <span className="w-16 text-right">24 h</span>
          <span className="w-[64px] text-right">Response</span>
          <span className="w-14 text-right">Every</span>
          {operator && <span className="w-[128px] text-right">Actions</span>}
        </div>

        {listQ.isPending ? (
          <div className="space-y-0 divide-y divide-border/60" aria-busy="true">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="px-4 py-3"><Skeleton className="h-10 w-full" /></div>
            ))}
          </div>
        ) : listError ? (
          <div className="flex flex-col items-center justify-center px-4 py-14 text-center" role="alert">
            <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-danger/10 text-danger">
              <XCircle className="h-5 w-5" />
            </span>
            <h3 className="mt-3 text-sm font-semibold">Monitors could not be loaded</h3>
            <p className="mt-1 max-w-sm text-xs leading-relaxed text-muted">
              <span className="block">{apiErrorMessage(listQ.error, 'The request failed.')}</span>
              <span className="block">This does not mean there are no monitors.</span>
            </p>
            <Button variant="outline" size="sm" className="mt-4" onClick={() => listQ.refetch()} disabled={listQ.isFetching}>
              <RefreshCw className={cn('h-3.5 w-3.5', listQ.isFetching && 'animate-spin')} /> Retry
            </Button>
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
                <Button variant="outline" size="sm" onClick={clearFilters}>
                  Clear filters
                </Button>
              ) : operator ? (
                <Button size="sm" onClick={() => { setEditing(null); setFormOpen(true) }}>
                  <Plus className="h-3.5 w-3.5" /> Add monitor
                </Button>
              ) : null}
            </div>
          </div>
        ) : (
          <div className="divide-y divide-border/60">
            {checks.map((c) => (
              <MonitorRow
                key={c.id}
                check={c}
                stale={isStale(c, nowMs)}
                nowMs={nowMs}
                selected={selected.has(c.id)}
                onToggle={() => toggleRow(c)}
                uptime24h={uptimeMap[c.id]}
                uptimeState={uptimeState}
                dailyRows={dailyQ.data?.checks?.[c.id]}
                barsState={barsState}
                canOperate={operator}
                testing={testing.has(c.id)}
                onOpen={() => navigate(`/services/${c.id}`)}
                onTest={() => runTest(c)}
                onEdit={() => { setEditing(c); setFormOpen(true) }}
                onClone={() => openClone(c)}
                onDelete={() => setDeleting(c)}
              />
            ))}
          </div>
        )}

        {/* Pagination */}
        {totalRecords > 0 && !listError && (
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-2.5 text-xs text-muted">
            <span>
              Showing <span className="text-text">{Math.min((page - 1) * pageSize + 1, totalRecords)}</span>–
              <span className="text-text">{Math.min(page * pageSize, totalRecords)}</span> of{' '}
              <span className="text-text">{totalRecords}</span>
            </span>
            <div className="flex items-center gap-2">
              <span className="hidden sm:inline">Rows</span>
              <Select value={String(pageSize)} onValueChange={(v) => patchParams({ size: v === String(PAGE_SIZES[0]) ? null : v, page: '1' })}>
                <SelectTrigger className="h-7 w-[68px] text-xs" aria-label="Rows per page"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {PAGE_SIZES.map((n) => <SelectItem key={n} value={String(n)}>{n}</SelectItem>)}
                </SelectContent>
              </Select>
              {totalPages > 1 && (
                <div className="flex items-center gap-1">
                  <Button variant="outline" size="icon" className="h-7 w-7" aria-label="Previous page" disabled={page <= 1} onClick={() => patchParams({ page: String(page - 1) })}>
                    <ChevronLeft className="h-3.5 w-3.5" />
                  </Button>
                  <span className="min-w-[60px] text-center text-text">Page {page} / {totalPages}</span>
                  <Button variant="outline" size="icon" className="h-7 w-7" aria-label="Next page" disabled={page >= totalPages} onClick={() => patchParams({ page: String(page + 1) })}>
                    <ChevronRight className="h-3.5 w-3.5" />
                  </Button>
                </div>
              )}
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
            Delete <span className="font-semibold text-text">{deleting?.name}</span> and its results and history? This cannot be undone.
          </>
        }
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => {
          if (deleting) del.mutate(deleting.id)
        }}
      />
      <BulkActionDialog
        action={bulkAction}
        items={bulkItems}
        groups={groupsList}
        knownTags={tagsList}
        onOpenChange={(o) => { if (!o) setBulkAction(null) }}
        onFinished={onBulkFinished}
        onSelectOnly={(ids) => setSelected((s) => new Map([...s].filter(([id]) => ids.includes(id))))}
      />
      <ImportDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        onImported={() => {
          qc.invalidateQueries({ queryKey: ['service-checks'] })
          qc.invalidateQueries({ queryKey: ['service-check-groups'] })
        }}
      />
    </div>
  )
}

/* ─── Small pieces ──────────────────────────────────────────────────────── */

function clock(ms: number): string {
  return ms ? new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'earlier'
}

function ErrorChip({ label, onRetry }: { label: string; onRetry: () => void }) {
  return (
    <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-danger/30 bg-danger/10 px-2 py-1 text-xs text-danger" role="status">
      <AlertTriangle className="h-3 w-3 shrink-0" />
      <span className="min-w-0 break-words">{label}</span>
      <button type="button" onClick={onRetry} className="shrink-0 font-semibold underline-offset-2 hover:underline">Retry</button>
    </span>
  )
}

function TriSelect({ label, value, onChange, yes, no }: {
  label: string
  value: string
  onChange: (v: string | null) => void
  yes: string
  no: string
}) {
  return (
    <Select value={value || 'any'} onValueChange={(v) => onChange(v === 'any' ? null : v)}>
      <SelectTrigger className="h-8 w-[170px] text-xs" aria-label={label}><SelectValue /></SelectTrigger>
      <SelectContent>
        <SelectItem value="any">{label}: any</SelectItem>
        <SelectItem value="true">{yes}</SelectItem>
        <SelectItem value="false">{no}</SelectItem>
      </SelectContent>
    </Select>
  )
}

function StatCard({ label, count, state, icon, tone, sub, active, attention, onClick }: {
  label: string
  count: number | null
  state: LoadState
  icon: React.ReactNode
  tone: 'primary' | 'success' | 'danger' | 'warning' | 'muted' | 'info'
  sub: string
  active?: boolean
  attention?: boolean
  onClick: () => void
}) {
  const TONE: Record<typeof tone, { chip: string; value: string; ring: string }> = {
    primary: { chip: 'bg-primary/10 text-primary', value: 'text-text', ring: 'border-primary/40 ring-primary/30' },
    success: { chip: 'bg-success/10 text-success', value: 'text-success', ring: 'border-success/40 ring-success/30' },
    danger: { chip: 'bg-danger/10 text-danger', value: 'text-danger', ring: 'border-danger/40 ring-danger/30' },
    warning: { chip: 'bg-warning/10 text-warning', value: 'text-warning', ring: 'border-warning/40 ring-warning/30' },
    info: { chip: 'bg-info/10 text-info', value: 'text-info', ring: 'border-info/40 ring-info/30' },
    muted: { chip: 'bg-surface3/60 text-muted', value: 'text-text2', ring: 'border-border-strong ring-border-strong/40' },
  }
  const t = TONE[tone]
  const zero = count === 0 && tone !== 'primary'
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        'min-w-0 rounded-xl border bg-surface p-3.5 text-left transition-all',
        active ? cn('ring-1', t.ring) : 'border-border hover:border-border-strong',
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-xs font-semibold uppercase tracking-wider text-muted">{label}</span>
        <span className={cn('relative inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full', t.chip)}>
          {attention && <span className="svc-ping absolute inline-flex h-full w-full rounded-full bg-danger" />}
          {icon}
        </span>
      </div>
      <div className={cn('mt-1 text-3xl font-bold leading-none tabular-nums', count == null || zero ? 'text-muted/50' : t.value)}>
        {state === 'loading'
          ? <span className="inline-block h-[26px] w-10 animate-pulse rounded bg-surface3/60 align-middle" aria-label="Loading" />
          : count == null ? '—' : count.toLocaleString()}
      </div>
      <div className="mt-1 truncate text-xs text-muted" title={sub}>{state === 'error' ? 'unavailable' : sub}</div>
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
