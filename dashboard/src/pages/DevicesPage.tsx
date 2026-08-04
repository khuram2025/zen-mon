import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  ArrowUpRight,
  ArrowDownRight,
  Boxes,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Columns3,
  Download,
  Filter,
  HardDrive,
  Layers,
  MapPin,
  MoreVertical,
  Pencil,
  Plus,
  PlusCircle,
  Radar,
  RefreshCw,
  Router,
  Search,
  Server,
  Shield,
  ShieldAlert,
  Sparkles,
  Trash2,
  Upload,
  Wifi,
  X,
  XCircle,
  Database,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { SkeletonTable } from '@/components/ui/Skeleton'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/Select'
import { DeviceFormDialog } from '@/components/forms/DeviceFormDialog'
import { toast } from '@/components/ui/Toast'

// -------------------------------------------------------------------------
// Types
// -------------------------------------------------------------------------

type Device = {
  id: string
  hostname: string
  ip_address: string
  device_type: string
  location: string | null
  group_id: string | null
  group_name?: string | null
  status: string
  last_seen: string | null
  last_rtt_ms: number | null
  created_at?: string | null
  ping_enabled: boolean
  snmp_enabled: boolean
  snmp_version?: string | null
  snmp_credential_id?: string | null
  snmp_v3_username?: string | null
  snmp_auth_configured?: boolean
  snmp_priv_configured?: boolean
  sys_object_id?: string | null
  vendor: string | null
  model: string | null
  os_version: string | null
}

type Group = { id: string; name: string; color?: string | null; device_count?: number }
type Credential = {
  id: string
  name: string
  snmp_version: '1' | '2c' | '3'
  community: string | null
  port: number
  timeout_ms: number
  retries: number
  is_default?: boolean
}

type SortKey =
  | 'hostname'
  | 'ip_address'
  | 'device_type'
  | 'vendor'
  | 'location'
  | 'group_name'
  | 'status'
  | 'last_rtt_ms'
  | 'last_seen'
type SortOrder = 'asc' | 'desc'

type HealthKind = 'healthy' | 'warning' | 'critical' | 'offline' | 'maintenance'
type AvailabilityRow = {
  key: string
  label: string
  total: number
  available: number
  availability: number
}
type DeviceAlertCount = {
  total: number
  /** Currently-active alerts (any age), not window-scoped. */
  active: number
  active_critical: number
  critical: number
  warning: number
  info: number
  last_triggered_at: string | null
}

const DEVICE_TYPES = [
  'router',
  'switch',
  'firewall',
  'server',
  'access_point',
  'printer',
  'storage',
  'ups',
  'other',
]

const HIDEABLE_COLUMNS = [
  'type',
  'group_location',
  'cpu',
  'memory',
  'uptime',
  'last_seen',
] as const
type HideableCol = (typeof HIDEABLE_COLUMNS)[number]

const DEFAULT_VISIBLE: Record<HideableCol, boolean> = {
  type: true,
  group_location: true,
  cpu: true,
  memory: true,
  uptime: true,
  last_seen: true,
}

const COLUMN_LABELS: Record<HideableCol, string> = {
  type: 'Type',
  group_location: 'Group / Location',
  cpu: 'CPU',
  memory: 'Memory',
  uptime: 'Uptime',
  last_seen: 'Last seen',
}

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100]

const PREFS_KEY = 'zp-devices-prefs-v2'
type Prefs = { visible: Record<HideableCol, boolean>; pageSize: number }
function loadPrefs(): Prefs {
  try {
    const raw = localStorage.getItem(PREFS_KEY)
    if (raw) {
      const p = JSON.parse(raw)
      return {
        visible: { ...DEFAULT_VISIBLE, ...(p.visible || {}) },
        pageSize: PAGE_SIZE_OPTIONS.includes(p.pageSize) ? p.pageSize : 10,
      }
    }
  } catch {
    /* ignore */
  }
  return { visible: { ...DEFAULT_VISIBLE }, pageSize: 10 }
}
function savePrefs(p: Prefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(p))
  } catch {
    /* ignore */
  }
}

// -------------------------------------------------------------------------
// Page
// -------------------------------------------------------------------------

export function DevicesPage() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()

  const search = params.get('q') || ''
  const statusFilter = params.get('status') || ''
  const typeFilter = params.get('type') || ''
  const locationFilter = params.get('loc') || ''
  const groupFilter = params.get('group') || ''
  const sortKey = (params.get('sort') as SortKey) || 'hostname'
  const sortOrder = (params.get('order') as SortOrder) || 'asc'
  const page = Math.max(1, Number(params.get('page') || '1') || 1)

  // Time range that controls per-device Uptime % calculation. Default 24h.
  const RANGE_PRESETS = [
    { key: '1h', label: '1h', hours: 1 },
    { key: '24h', label: '24h', hours: 24 },
    { key: '7d', label: '7d', hours: 168 },
    { key: '1M', label: '1M', hours: 720 },
  ] as const
  const rangeKey = params.get('range') || '24h'
  const rangeHours =
    RANGE_PRESETS.find((r) => r.key === rangeKey)?.hours || 24
  const rangeLabel =
    RANGE_PRESETS.find((r) => r.key === rangeKey)?.label || '24h'

  function patchParams(p: Record<string, string | null>) {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(p)) {
      if (!v) next.delete(k)
      else next.set(k, v)
    }
    setParams(next, { replace: true })
  }

  const [prefs, setPrefs] = useState<Prefs>(() => loadPrefs())
  useEffect(() => savePrefs(prefs), [prefs])

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Device | null>(null)
  const [deleting, setDeleting] = useState<Device | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false)
  const [bulkEditOpen, setBulkEditOpen] = useState(false)
  const [columnsOpen, setColumnsOpen] = useState(false)
  const [filtersOpen, setFiltersOpen] = useState(false)

  const { data, isLoading, isFetching, refetch } = useQuery<{
    data: Device[]
    meta: any
  }>({
    queryKey: ['devices', 'list', { search }],
    queryFn: async () => {
      const qs: string[] = ['limit=200']
      if (search) qs.push(`search=${encodeURIComponent(search)}`)
      return (await api.get(`/devices?${qs.join('&')}`)).data
    },
    refetchInterval: 15_000,
  })

  const { data: groups } = useQuery<Group[]>({
    queryKey: ['devices', 'groups'],
    queryFn: async () => (await api.get('/devices/groups')).data,
  })
  const { data: locations } = useQuery<string[]>({
    queryKey: ['devices', 'locations'],
    queryFn: async () => (await api.get('/devices/locations')).data,
  })
  const { data: deviceTypes } = useQuery<string[]>({
    queryKey: ['devices', 'types'],
    queryFn: async () => (await api.get('/devices/device-types')).data,
  })

  // Real continuous uptime per device (seconds) — refreshed every 30s.
  const { data: uptimeData } = useQuery<{ devices: Record<string, number> }>({
    queryKey: ['devices', 'current-uptime'],
    queryFn: async () => (await api.get('/devices/current-uptime')).data,
    refetchInterval: 30_000,
  })
  const uptimeMap = uptimeData?.devices || {}

  // Uptime % per device over the selected time range (driven by ?range=).
  const { data: uptimePctData } = useQuery<{
    hours: number
    devices: Record<string, number>
  }>({
    queryKey: ['devices', 'uptime-pct', rangeHours],
    queryFn: async () =>
      (await api.get(`/devices/dashboard/uptime-stats?hours=${rangeHours}`)).data,
    refetchInterval: 30_000,
  })
  const uptimePctMap = uptimePctData?.devices || {}

  const { data: alertCountData } = useQuery<{ devices: Record<string, DeviceAlertCount> }>({
    queryKey: ['devices', 'alert-counts', rangeHours],
    queryFn: async () => (await api.get(`/alerts/device-counts?hours=${rangeHours}`)).data,
    refetchInterval: 30_000,
  })
  const alertCountMap = alertCountData?.devices || {}

  // Latest SNMP scalar metrics (cpu, memory, …) per device, from ClickHouse.
  const { data: metricsData } = useQuery<{
    devices: Record<string, Record<string, number | string>>
  }>({
    queryKey: ['devices', 'current-metrics'],
    queryFn: async () => (await api.get('/devices/current-metrics')).data,
    refetchInterval: 30_000,
  })
  const metricsMap = metricsData?.devices || {}

  // Recent alerts for the activity feed.
  const { data: recentAlerts } = useQuery<{
    data: Array<{
      id: string
      severity: string
      message?: string | null
      summary?: string | null
      title?: string | null
      device_id?: string | null
      device_hostname?: string | null
      triggered_at?: string | null
      created_at?: string | null
      status?: string | null
    }>
  }>({
    queryKey: ['devices', 'recent-alerts'],
    queryFn: async () => (await api.get('/alerts?limit=6')).data,
    refetchInterval: 30_000,
  })

  const devices = data?.data || []

  // -----------------------------------------------------------------------
  // Per-row metrics — CPU/memory come from the live `current-metrics` map
  // (latest SNMP scalar values in the last 15 min). Uptime is seconds since
  // the most recent `up` transition from `current-uptime`.
  // -----------------------------------------------------------------------

  const enriched = useMemo(() => {
    return devices.map((d) => {
      const health = healthOf(d)
      const m = metricsMap[d.id] || {}
      const cpu = typeof m.cpu === 'number' ? Math.round(m.cpu) : null
      const mem = typeof m.memory === 'number' ? Math.round(m.memory) : null
      const uptimeSec = uptimeMap[d.id]
      const uptime = formatRealUptime(uptimeSec, health)
      const uptimePct = uptimePctMap[d.id]
      return { device: d, health, cpu, mem, uptime, uptimePct }
    })
  }, [devices, uptimeMap, metricsMap, uptimePctMap])

  const filtered = useMemo(() => {
    return enriched.filter(({ device: d, health }) => {
      if (typeFilter && d.device_type !== typeFilter) return false
      if (groupFilter && d.group_id !== groupFilter) return false
      if (locationFilter && (d.location || '') !== locationFilter) return false
      if (statusFilter) {
        if (statusFilter === 'healthy' && health !== 'healthy') return false
        if (statusFilter === 'warning' && health !== 'warning') return false
        if (statusFilter === 'critical' && health !== 'critical') return false
        if (statusFilter === 'offline' && health !== 'offline') return false
        if (statusFilter === 'maintenance' && health !== 'maintenance') return false
      }
      return true
    })
  }, [enriched, typeFilter, groupFilter, locationFilter, statusFilter])

  const sorted = useMemo(() => {
    const arr = [...filtered]
    const dir = sortOrder === 'asc' ? 1 : -1
    arr.sort((a, b) => {
      const va = extractSortVal(a.device, sortKey)
      const vb = extractSortVal(b.device, sortKey)
      if (va === vb) return 0
      if (va == null) return 1
      if (vb == null) return -1
      return va < vb ? -1 * dir : 1 * dir
    })
    return arr
  }, [filtered, sortKey, sortOrder])

  // Pagination
  const totalPages = Math.max(1, Math.ceil(sorted.length / prefs.pageSize))
  const currentPage = Math.min(page, totalPages)
  const pageStart = (currentPage - 1) * prefs.pageSize
  const pageRows = sorted.slice(pageStart, pageStart + prefs.pageSize)

  useEffect(() => {
    // Reset to page 1 when filters/size cause overflow.
    if (page > totalPages) patchParams({ page: '1' })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [totalPages, page])

  // -----------------------------------------------------------------------
  // Aggregates for KPI row + sidebar (derived from full list, not filtered)
  // -----------------------------------------------------------------------

  const agg = useMemo(() => {
    const counts = { healthy: 0, warning: 0, critical: 0, offline: 0, maintenance: 0 }
    const byCategory: Record<string, number> = {
      Server: 0, Network: 0, Security: 0, Wireless: 0, Other: 0,
    }
    const sevenDaysAgo = Date.now() - 7 * 86_400_000
    let newThisWeek = 0
    enriched.forEach(({ device, health }) => {
      counts[health]++
      byCategory[categoryOf(device.device_type)]++
      const createdMs = device.created_at ? Date.parse(device.created_at) : NaN
      if (!Number.isNaN(createdMs) && createdMs >= sevenDaysAgo) newThisWeek++
    })
    const total = enriched.length
    return { total, ...counts, byCategory, newThisWeek }
  }, [enriched])

  const availabilityByLocation = useMemo(
    () => buildAvailabilityRows(
      enriched,
      ({ device }) => device.location || 'Unassigned',
      (label) => label,
    ),
    [enriched],
  )

  const availabilityByType = useMemo(
    () => buildAvailabilityRows(
      enriched,
      ({ device }) => device.device_type || 'other',
      (label) => titleCase(label.replace('_', ' ')),
    ),
    [enriched],
  )

  const activeFilterCount =
    (typeFilter ? 1 : 0) +
    (groupFilter ? 1 : 0) +
    (locationFilter ? 1 : 0) +
    (statusFilter ? 1 : 0)

  // -----------------------------------------------------------------------
  // Selection
  // -----------------------------------------------------------------------

  useEffect(() => {
    if (selected.size === 0) return
    const alive = new Set(sorted.map((r) => r.device.id))
    let changed = false
    const next = new Set<string>()
    selected.forEach((id) => {
      if (alive.has(id)) next.add(id)
      else changed = true
    })
    if (changed) setSelected(next)
  }, [sorted, selected])

  // -----------------------------------------------------------------------
  // Mutations
  // -----------------------------------------------------------------------

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/devices/${id}`),
    onSuccess: () => {
      toast.success('Device deleted')
      qc.invalidateQueries({ queryKey: ['devices'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const bulkDelete = useMutation({
    mutationFn: async (ids: string[]) =>
      (await api.post('/devices/bulk-delete', { device_ids: ids })).data as { deleted: number },
    onSuccess: (res) => {
      toast.success(`Deleted ${res.deleted} device${res.deleted === 1 ? '' : 's'}`)
      setSelected(new Set())
      setBulkDeleteOpen(false)
      qc.invalidateQueries({ queryKey: ['devices'] })
    },
    onError: (e: any) => toast.error('Bulk delete failed', apiErrorMessage(e)),
  })

  const bulkEdit = useMutation({
    mutationFn: async (payload: { ids: string[]; patch: Record<string, unknown> }) => {
      const results = await Promise.allSettled(
        payload.ids.map((id) => api.put(`/devices/${id}`, payload.patch)),
      )
      const ok = results.filter((r) => r.status === 'fulfilled').length
      const fail = results.length - ok
      return { ok, fail }
    },
    onSuccess: ({ ok, fail }) => {
      setBulkEditOpen(false)
      setSelected(new Set())
      qc.invalidateQueries({ queryKey: ['devices'] })
      if (fail === 0) toast.success(`Updated ${ok} device${ok === 1 ? '' : 's'}`)
      else toast.error(`Updated ${ok}, failed ${fail}`)
    },
  })

  // -----------------------------------------------------------------------
  // Handlers
  // -----------------------------------------------------------------------

  function toggleRow(id: string) {
    const n = new Set(selected)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    setSelected(n)
  }

  function toggleAll() {
    if (pageRows.length === 0) return
    if (pageRows.every((r) => selected.has(r.device.id))) {
      const n = new Set(selected)
      pageRows.forEach((r) => n.delete(r.device.id))
      setSelected(n)
    } else {
      const n = new Set(selected)
      pageRows.forEach((r) => n.add(r.device.id))
      setSelected(n)
    }
  }

  function onSortClick(key: SortKey) {
    if (sortKey === key) patchParams({ order: sortOrder === 'asc' ? 'desc' : 'asc' })
    else patchParams({ sort: key, order: 'asc' })
  }

  function clearFilters() {
    patchParams({ type: null, group: null, loc: null, status: null })
  }

  function exportCsv() {
    const cols: Array<[string, (row: (typeof sorted)[number]) => string]> = [
      ['hostname', (r) => r.device.hostname],
      ['ip_address', (r) => r.device.ip_address],
      ['device_type', (r) => r.device.device_type],
      ['group', (r) => r.device.group_name || ''],
      ['location', (r) => r.device.location || ''],
      ['status', (r) => r.health],
      ['cpu_pct', (r) => r.cpu == null ? '' : String(r.cpu)],
      ['memory_pct', (r) => r.mem == null ? '' : String(r.mem)],
      ['uptime', (r) => r.uptime],
      ['last_seen', (r) => r.device.last_seen || ''],
    ]
    const rows = [
      cols.map(([h]) => h).join(','),
      ...sorted.map((r) => cols.map(([, f]) => csvEscape(f(r))).join(',')),
    ]
    const blob = new Blob([rows.join('\n')], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `devices-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
    toast.success(`Exported ${sorted.length} device${sorted.length === 1 ? '' : 's'}`)
  }

  const searchRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const cmd = e.metaKey || e.ctrlKey
      if (cmd && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        searchRef.current?.focus()
        return
      }
      if (e.key === '/' && !cmd && !e.altKey) {
        const t = e.target as HTMLElement
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
        e.preventDefault()
        searchRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const allSelectedOnPage =
    pageRows.length > 0 && pageRows.every((r) => selected.has(r.device.id))
  const someSelectedOnPage =
    pageRows.some((r) => selected.has(r.device.id)) && !allSelectedOnPage

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  return (
    <div className="space-y-4">
      {/* ───────────────────── Header bar ───────────────────── */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-4">
          <div className="min-w-0 flex-1">
            <h1 className="text-[28px] font-bold leading-tight tracking-tight">Devices</h1>
            <p className="mt-0.5 text-sm text-muted">
              Monitor, search, and manage all network and server devices
            </p>
          </div>
          <div className="w-full max-w-xl flex-shrink-0 md:w-[520px]">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <Input
                ref={searchRef}
                placeholder="Search devices, IPs, tags…"
                value={search}
                onChange={(e) => patchParams({ q: e.target.value || null, page: '1' })}
                className="h-10 pl-10 pr-14 text-sm"
              />
              <kbd className="pointer-events-none absolute right-2.5 top-1/2 hidden -translate-y-1/2 items-center gap-0.5 rounded border border-border bg-surface2 px-1.5 py-0.5 text-[10px] font-medium text-muted sm:inline-flex">
                ⌘ K
              </kbd>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2">
          <TimeRangePicker
            value={rangeKey}
            onChange={(k) => patchParams({ range: k === '24h' ? null : k })}
          />
          <FilterInline
            label="Status"
            value={statusFilter}
            onChange={(v) => patchParams({ status: v || null, page: '1' })}
            options={[
              { value: 'healthy', label: 'Healthy' },
              { value: 'warning', label: 'Warning' },
              { value: 'critical', label: 'Critical' },
              { value: 'offline', label: 'Offline' },
              { value: 'maintenance', label: 'Maintenance' },
            ]}
          />
          <FilterInline
            label="Type"
            value={typeFilter}
            onChange={(v) => patchParams({ type: v || null, page: '1' })}
            options={(deviceTypes || DEVICE_TYPES).map((t) => ({
              value: t,
              label: titleCase(t.replace('_', ' ')),
            }))}
          />
          <FilterInline
            label="Location"
            value={locationFilter}
            onChange={(v) => patchParams({ loc: v || null, page: '1' })}
            options={(locations || []).map((l) => ({ value: l, label: l }))}
          />
          <Button variant="outline" size="default" className="h-10" onClick={exportCsv}>
            <Download className="h-4 w-4" />
            Export
          </Button>
          <Button size="default" className="h-10" onClick={() => { setEditing(null); setFormOpen(true) }}>
            <Plus className="h-4 w-4" />
            Add Device
          </Button>
          <Button variant="outline" size="default" className="h-10" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {/* ───────────────────── KPI row ───────────────────── */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <KpiCard
          label="Total Devices"
          value={agg.total}
          icon={<Boxes className="h-4 w-4" />}
          color="primary"
          active={!statusFilter}
          onClick={() => patchParams({ status: null, page: '1' })}
        />
        <KpiCard
          label="Online"
          value={agg.healthy}
          icon={<CheckCircle2 className="h-4 w-4" />}
          color="success"
          active={statusFilter === 'healthy'}
          onClick={() =>
            patchParams({ status: statusFilter === 'healthy' ? null : 'healthy', page: '1' })
          }
        />
        <KpiCard
          label="Offline"
          value={agg.offline}
          icon={<XCircle className="h-4 w-4" />}
          color="muted"
          active={statusFilter === 'offline'}
          onClick={() =>
            patchParams({ status: statusFilter === 'offline' ? null : 'offline', page: '1' })
          }
        />
        <KpiCard
          label="Warning"
          value={agg.warning}
          icon={<AlertTriangle className="h-4 w-4" />}
          color="warning"
          active={statusFilter === 'warning'}
          onClick={() =>
            patchParams({ status: statusFilter === 'warning' ? null : 'warning', page: '1' })
          }
        />
        <KpiCard
          label="Critical"
          value={agg.critical}
          icon={<ShieldAlert className="h-4 w-4" />}
          color="danger"
          active={statusFilter === 'critical'}
          onClick={() =>
            patchParams({ status: statusFilter === 'critical' ? null : 'critical', page: '1' })
          }
        />
        <KpiCard
          label="New This Week"
          value={agg.newThisWeek}
          icon={<Sparkles className="h-4 w-4" />}
          color="accent"
          active={false}
        />
      </div>

      {/* ───────────────────── Main grid ───────────────────── */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        {/* ---------- Devices list ---------- */}
        <Card>
          <CardContent className="space-y-3 p-4">
            {/* Sub-toolbar */}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <h2 className="text-base font-semibold">Devices List</h2>
                <span className="rounded-md bg-surface2 px-2 py-0.5 text-xs font-medium text-muted">
                  {sorted.length.toLocaleString()}
                </span>
              </div>
              <div className="flex items-center gap-1.5">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-8"
                  onClick={() => setColumnsOpen(true)}
                >
                  <Columns3 className="h-3.5 w-3.5" />
                  Columns
                  <ChevronDown className="h-3 w-3 opacity-60" />
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className={`h-8 w-8 p-0 ${activeFilterCount > 0 ? 'border-primary/40 text-primary' : ''}`}
                  onClick={() => setFiltersOpen((o) => !o)}
                  title="More filters"
                >
                  <Filter className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>

            {/* Inline extra filters */}
            {filtersOpen && (
              <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface2/40 p-2">
                <FilterInline
                  label="Group"
                  value={groupFilter}
                  onChange={(v) => patchParams({ group: v || null, page: '1' })}
                  options={(groups || []).map((g) => ({ value: g.id, label: g.name }))}
                />
                {activeFilterCount > 0 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={clearFilters}
                    className="h-8 text-xs text-muted hover:text-text"
                  >
                    <X className="h-3.5 w-3.5" /> Clear ({activeFilterCount})
                  </Button>
                )}
              </div>
            )}

            {/* Bulk bar */}
            {selected.size > 0 && (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-primary/30 bg-primary/5 px-3 py-2">
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-semibold text-primary">{selected.size}</span>
                  <span className="text-muted">selected</span>
                  <button
                    type="button"
                    onClick={() => setSelected(new Set())}
                    className="text-xs text-muted underline-offset-2 hover:underline"
                  >
                    Clear
                  </button>
                </div>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" onClick={() => setBulkEditOpen(true)}>
                    <Pencil className="h-3.5 w-3.5" /> Edit
                  </Button>
                  <Button variant="destructive" size="sm" onClick={() => setBulkDeleteOpen(true)}>
                    <Trash2 className="h-3.5 w-3.5" /> Delete
                  </Button>
                </div>
              </div>
            )}

            {/* Table */}
            <div className="overflow-hidden rounded-md border border-border">
              <Table>
                <THead className="bg-surface2/60">
                  <Tr className="hover:bg-transparent">
                    <Th className="w-8">
                      <input
                        type="checkbox"
                        aria-label="Select all on page"
                        checked={allSelectedOnPage}
                        ref={(el) => {
                          if (el) el.indeterminate = someSelectedOnPage
                        }}
                        onChange={toggleAll}
                      />
                    </Th>
                    <SortableTh label="Device Name" col="hostname" current={sortKey} order={sortOrder} onClick={onSortClick} />
                    <SortableTh label="IP Address" col="ip_address" current={sortKey} order={sortOrder} onClick={onSortClick} />
                    {prefs.visible.type && (
                      <SortableTh label="Type" col="device_type" current={sortKey} order={sortOrder} onClick={onSortClick} />
                    )}
                    {prefs.visible.group_location && (
                      <SortableTh label="Group / Location" col="group_name" current={sortKey} order={sortOrder} onClick={onSortClick} />
                    )}
                    <SortableTh label="Status" col="status" current={sortKey} order={sortOrder} onClick={onSortClick} />
                    {prefs.visible.cpu && <Th className="min-w-[140px]">CPU</Th>}
                    {prefs.visible.memory && <Th className="min-w-[140px]">Memory</Th>}
                    {prefs.visible.uptime && (
                      <Th className="whitespace-nowrap">
                        Uptime <span className="font-normal text-muted">({rangeLabel})</span>
                      </Th>
                    )}
                    {prefs.visible.last_seen && (
                      <SortableTh label="Last Seen" col="last_seen" current={sortKey} order={sortOrder} onClick={onSortClick} />
                    )}
                    <Th className="w-12 text-right">Actions</Th>
                  </Tr>
                </THead>
                <TBody>
                  {isLoading && (
                    <Tr>
                      <Td colSpan={12}>
                        <SkeletonTable rows={6} cols={10} />
                      </Td>
                    </Tr>
                  )}
                  {!isLoading && pageRows.length === 0 && (
                    <Tr>
                      <Td colSpan={12} className="py-14">
                        <div className="flex flex-col items-center gap-2 text-center text-muted">
                          <Server className="h-8 w-8 opacity-50" />
                          <div className="text-sm font-medium text-text">No devices match</div>
                          <div className="text-xs">
                            {agg.total === 0 ? (
                              <>
                                Add one manually, or{' '}
                                <Link to="/discovery" className="text-primary hover:underline">
                                  run a discovery sweep
                                </Link>
                                .
                              </>
                            ) : (
                              <>
                                Try{' '}
                                <button type="button" onClick={clearFilters} className="text-primary hover:underline">
                                  clearing filters
                                </button>
                                .
                              </>
                            )}
                          </div>
                        </div>
                      </Td>
                    </Tr>
                  )}
                  {pageRows.map(({ device: d, health, cpu, mem, uptime, uptimePct }) => {
                    const isSel = selected.has(d.id)
                    const typeInfo = TYPE_STYLE[normalizeType(d.device_type)] || TYPE_STYLE.other
                    const alertCount = alertCountMap[d.id]
                    // Badge counts alerts that are open RIGHT NOW — the range
                    // filter only affects the history shown in the tooltip.
                    // Counting the window's total here made a healthy device
                    // wear a big red number for long-resolved noise.
                    const openAlerts = alertCount?.active || 0
                    const alertTone = alertCount?.active_critical ? 'bg-danger text-white' : 'bg-warning text-black'
                    return (
                      <Tr key={d.id} className={isSel ? 'bg-primary/5' : ''}>
                        <Td className="py-2.5">
                          <input
                            type="checkbox"
                            aria-label={`Select ${d.hostname}`}
                            checked={isSel}
                            onChange={() => toggleRow(d.id)}
                          />
                        </Td>
                        <Td className="py-2.5">
                          <div className="flex items-center gap-2.5">
                            <span
                              className={`relative flex h-8 w-8 shrink-0 items-center justify-center rounded-md border ${typeInfo.badge}`}
                              title={alertCount
                                ? `${openAlerts} active alert${openAlerts === 1 ? '' : 's'} now · ${alertCount.total} total in ${rangeLabel}`
                                : undefined}
                            >
                              <typeInfo.Icon className="h-4 w-4" />
                              {openAlerts > 0 && (
                                <span className={`absolute -right-1.5 -top-1.5 min-w-[17px] rounded-full px-1 text-center text-[9px] font-bold leading-[17px] shadow-sm ring-2 ring-surface ${alertTone}`}>
                                  {openAlerts > 99 ? '99+' : openAlerts}
                                </span>
                              )}
                            </span>
                            <Link
                              to={`/devices/${d.id}`}
                              className="truncate font-medium text-text hover:text-primary hover:underline"
                              title={d.hostname}
                            >
                              {d.hostname}
                            </Link>
                          </div>
                        </Td>
                        <Td className="font-mono text-xs text-muted">{d.ip_address}</Td>
                        {prefs.visible.type && (
                          <Td className="text-sm capitalize">
                            {titleCase(d.device_type.replace('_', ' '))}
                          </Td>
                        )}
                        {prefs.visible.group_location && (
                          <Td className="text-sm">
                            {d.group_name || d.location ? (
                              <div className="leading-tight">
                                <div>{d.group_name || '—'}</div>
                                {d.location && (
                                  <div className="text-[11px] text-muted">{d.location}</div>
                                )}
                              </div>
                            ) : (
                              <span className="text-muted">—</span>
                            )}
                          </Td>
                        )}
                        <Td>
                          <HealthPill kind={health} />
                        </Td>
                        {prefs.visible.cpu && (
                          <Td>
                            <MetricCell value={cpu} />
                          </Td>
                        )}
                        {prefs.visible.memory && (
                          <Td>
                            <MetricCell value={mem} />
                          </Td>
                        )}
                        {prefs.visible.uptime && (
                          <Td className="whitespace-nowrap text-sm">
                            <UptimePctCell pct={uptimePct} fallback={uptime} />
                          </Td>
                        )}
                        {prefs.visible.last_seen && (
                          <Td className="whitespace-nowrap text-xs">
                            <span className="inline-flex items-center gap-1.5">
                              {relativeTime(d.last_seen) || '—'}
                              <span
                                className={`h-1.5 w-1.5 rounded-full ${
                                  health === 'healthy' ? 'bg-success' :
                                  health === 'warning' ? 'bg-warning' :
                                  health === 'critical' ? 'bg-danger' : 'bg-muted'
                                }`}
                              />
                            </span>
                          </Td>
                        )}
                        <Td className="text-right">
                          <RowMenu
                            onEdit={() => { setEditing(d); setFormOpen(true) }}
                            onDelete={() => setDeleting(d)}
                            onView={() => navigate(`/devices/${d.id}`)}
                          />
                        </Td>
                      </Tr>
                    )
                  })}
                </TBody>
              </Table>
            </div>

            {/* Footer / pagination */}
            <div className="flex flex-wrap items-center justify-between gap-2 pt-1 text-xs text-muted">
              <span>
                Showing{' '}
                <span className="font-medium text-text">
                  {sorted.length === 0 ? 0 : pageStart + 1}–{Math.min(pageStart + prefs.pageSize, sorted.length)}
                </span>{' '}
                of <span className="font-medium text-text">{sorted.length.toLocaleString()}</span> devices
              </span>
              <div className="flex items-center gap-2">
                <Pagination
                  page={currentPage}
                  totalPages={totalPages}
                  onPage={(p) => patchParams({ page: String(p) })}
                />
                <PageSizeSelect
                  value={prefs.pageSize}
                  onChange={(n) => {
                    setPrefs({ ...prefs, pageSize: n })
                    patchParams({ page: '1' })
                  }}
                />
              </div>
            </div>
          </CardContent>
        </Card>

        {/* ---------- Right sidebar ---------- */}
        <div className="space-y-4">
          <DistributionCard byCategory={agg.byCategory} total={agg.total} />
          <AvailabilityChartCard
            title="Availability by Location"
            subtitle={`${rangeLabel} average uptime by site`}
            icon={MapPin}
            rows={availabilityByLocation}
            onPick={(location) => patchParams({ loc: location === 'Unassigned' ? null : location, page: '1' })}
          />
          <AvailabilityChartCard
            title="Availability by Type"
            subtitle={`${rangeLabel} average uptime by device type`}
            icon={Layers}
            rows={availabilityByType}
            onPick={(type) => patchParams({ type: typeFilter === type ? null : type, page: '1' })}
          />
          <StatusBreakdownCard
            healthy={agg.healthy}
            warning={agg.warning}
            critical={agg.critical}
            offline={agg.offline}
            maintenance={(agg as any).maintenance}
            onPickStatus={(s) => patchParams({ status: statusFilter === s ? null : s, page: '1' })}
          />
          <RecentActivityCard alerts={recentAlerts?.data || []} />
          <QuickActionsCard
            onAdd={() => { setEditing(null); setFormOpen(true) }}
            onDiscover={() => navigate('/discovery')}
            onImport={() => navigate('/discovery')}
            onBulk={() => {
              if (selected.size > 0) setBulkEditOpen(true)
              else toast.error('No devices selected', 'Pick devices in the list first')
            }}
          />
        </div>
      </div>

      {/* Dialogs */}
      <DeviceFormDialog open={formOpen} onOpenChange={setFormOpen} device={editing} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete device"
        description={
          <>
            Delete <span className="font-semibold text-text">{deleting?.hostname}</span>? This
            also removes its metrics history.
          </>
        }
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
      <ConfirmDialog
        open={bulkDeleteOpen}
        onOpenChange={setBulkDeleteOpen}
        title={`Delete ${selected.size} device${selected.size === 1 ? '' : 's'}`}
        description={
          <>
            This will permanently remove{' '}
            <span className="font-semibold text-text">{selected.size}</span> device
            {selected.size === 1 ? '' : 's'} and all associated metrics history. This cannot be
            undone.
          </>
        }
        confirmText={`Delete ${selected.size}`}
        destructive
        loading={bulkDelete.isPending}
        onConfirm={() => bulkDelete.mutate(Array.from(selected))}
      />
      <BulkEditDialog
        open={bulkEditOpen}
        onOpenChange={setBulkEditOpen}
        count={selected.size}
        loading={bulkEdit.isPending}
        onSubmit={(patch) => {
          if (Object.keys(patch).length === 0) {
            toast.error('Nothing to update', 'Change at least one field')
            return
          }
          bulkEdit.mutate({ ids: Array.from(selected), patch })
        }}
      />
      <ColumnsDialog
        open={columnsOpen}
        onOpenChange={setColumnsOpen}
        visible={prefs.visible}
        onChange={(visible) => setPrefs({ ...prefs, visible })}
      />
    </div>
  )
}

// =========================================================================
// KPI Card
// =========================================================================

function KpiCard({
  label, value, trend, icon, color, active, onClick,
}: {
  label: string
  value: number
  trend?: number | null
  icon: React.ReactNode
  color: 'primary' | 'success' | 'warning' | 'danger' | 'accent' | 'muted'
  active?: boolean
  onClick?: () => void
}) {
  const COLOR_MAP: Record<typeof color, { icon: string; ring: string }> = {
    primary: { icon: 'text-primary bg-primary/10', ring: 'ring-primary/30 border-primary/30' },
    success: { icon: 'text-success bg-success/10', ring: 'ring-success/30 border-success/30' },
    warning: { icon: 'text-warning bg-warning/10', ring: 'ring-warning/40 border-warning/40' },
    danger:  { icon: 'text-danger bg-danger/10',   ring: 'ring-danger/30 border-danger/30' },
    accent:  { icon: 'text-accent bg-accent/10',   ring: 'ring-accent/30 border-accent/30' },
    muted:   { icon: 'text-muted bg-muted/10',     ring: 'ring-border-strong/40 border-border-strong' },
  }
  const c = COLOR_MAP[color]
  const hasTrend = trend != null && Number.isFinite(trend)
  const trendUp = hasTrend ? (trend as number) >= 0 : false

  const Wrapper: any = onClick ? 'button' : 'div'
  return (
    <Wrapper
      type={onClick ? 'button' : undefined}
      onClick={onClick}
      className={`group relative flex flex-col gap-2 overflow-hidden rounded-xl border p-4 text-left transition-all ${
        active ? `${c.ring} ring-1 bg-surface` : 'border-border bg-surface hover:border-border-strong'
      }`}
    >
      <div className="flex items-center justify-between">
        <span className={`inline-flex h-7 w-7 items-center justify-center rounded-md ${c.icon}`}>
          {icon}
        </span>
        {hasTrend && (
          <span
            className={`inline-flex items-center gap-0.5 text-[11px] font-medium ${
              trendUp ? 'text-success' : 'text-danger'
            }`}
          >
            {trendUp ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
            {Math.abs(trend as number).toFixed(1)}%
          </span>
        )}
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

// =========================================================================
// Metric cell (CPU / Memory)
// =========================================================================

function MetricCell({ value }: { value: number | null }) {
  if (value == null) {
    return <span className="text-xs text-muted">—</span>
  }
  const tone =
    value >= 80 ? 'danger' : value >= 60 ? 'warning' : 'success'
  const barColor =
    tone === 'danger' ? 'bg-danger' : tone === 'warning' ? 'bg-warning' : 'bg-success'
  return (
    <div className="flex items-center gap-2">
      <div className="min-w-[32px] text-xs font-medium tabular-nums">{value}%</div>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface2">
        <div
          className={`h-full rounded-full ${barColor}`}
          style={{ width: `${value}%` }}
        />
      </div>
    </div>
  )
}

// =========================================================================
// Health pill (Healthy / Warning / Critical / Offline)
// =========================================================================

function HealthPill({ kind }: { kind: HealthKind }) {
  const map: Record<HealthKind, { label: string; className: string; dot: string }> = {
    healthy:  { label: 'Healthy',  className: 'border-success/30 bg-success/10 text-success', dot: 'bg-success' },
    warning:  { label: 'Warning',  className: 'border-warning/30 bg-warning/10 text-warning', dot: 'bg-warning' },
    critical: { label: 'Critical', className: 'border-danger/30 bg-danger/10 text-danger',    dot: 'bg-danger' },
    offline:  { label: 'Offline',  className: 'border-border bg-surface2 text-muted',         dot: 'bg-muted' },
    maintenance: { label: 'Maintenance', className: 'border-primary/30 bg-primary/10 text-primary', dot: 'bg-primary' },
  }
  const v = map[kind]
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${v.className}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${v.dot}`} />
      {v.label}
    </span>
  )
}

// =========================================================================
// Filter dropdown — "Label  All ▾"
// =========================================================================

function FilterInline({
  label, value, onChange, options,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  options: Array<{ value: string; label: string }>
}) {
  const sel = options.find((o) => o.value === value)
  return (
    <div className="w-[160px] flex-none">
      <Select value={value || '__all__'} onValueChange={(v) => onChange(v === '__all__' ? '' : v)}>
        <SelectTrigger
          className={`h-10 gap-2 px-3 text-sm ${
            value ? 'border-primary/40 bg-primary/5' : ''
          }`}
        >
          <span className="flex items-center gap-2 truncate">
            <span className="text-muted">{label}</span>
            <span className="truncate font-medium">{sel?.label || 'All'}</span>
          </span>
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="__all__">
            <span className="text-muted">All {label.toLowerCase()}</span>
          </SelectItem>
          {options.map((o) => (
            <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}

// =========================================================================
// Distribution donut
// =========================================================================

const CATEGORY_COLORS: Record<string, string> = {
  Server: 'rgb(var(--primary))',
  Network: 'rgb(var(--success))',
  Security: 'rgb(var(--danger))',
  Wireless: 'rgb(var(--accent))',
  Other: 'rgb(var(--muted))',
}

function DistributionCard({ byCategory, total }: { byCategory: Record<string, number>; total: number }) {
  const entries = (Object.keys(CATEGORY_COLORS) as (keyof typeof CATEGORY_COLORS)[]).map((k) => ({
    key: k,
    value: byCategory[k] || 0,
    color: CATEGORY_COLORS[k],
  }))
  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">Device Distribution by Type</h3>
          <button className="text-xs text-primary hover:underline">View all</button>
        </div>
        <div className="flex items-center gap-4">
          <Donut
            segments={entries.filter((e) => e.value > 0)}
            total={total}
            size={120}
            thickness={18}
          />
          <div className="flex-1 space-y-1.5">
            {entries.map((e) => {
              const pct = total > 0 ? (e.value / total) * 100 : 0
              return (
                <div key={e.key} className="flex items-center justify-between text-xs">
                  <span className="flex items-center gap-1.5">
                    <span className="h-2 w-2 rounded-full" style={{ backgroundColor: e.color }} />
                    <span className="text-muted">{e.key}</span>
                  </span>
                  <span className="font-medium tabular-nums">
                    {e.value} <span className="text-muted">({pct.toFixed(1)}%)</span>
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function AvailabilityChartCard({
  title,
  subtitle,
  icon: Icon,
  rows,
  onPick,
}: {
  title: string
  subtitle: string
  icon: React.ComponentType<{ className?: string }>
  rows: AvailabilityRow[]
  onPick: (key: string) => void
}) {
  const visible = rows.slice(0, 6)
  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-2">
            <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
              <Icon className="h-3.5 w-3.5" />
            </span>
            <div className="min-w-0">
              <h3 className="text-sm font-semibold">{title}</h3>
              <p className="truncate text-[11px] text-muted">{subtitle}</p>
            </div>
          </div>
        </div>

        {visible.length === 0 ? (
          <div className="rounded-md border border-dashed border-border bg-surface2/30 p-5 text-center text-xs text-muted">
            No devices to chart.
          </div>
        ) : (
          <div className="space-y-2.5">
            {visible.map((row) => {
              const tone =
                row.availability >= 99 ? 'bg-success' :
                row.availability >= 95 ? 'bg-primary' :
                row.availability >= 90 ? 'bg-warning' : 'bg-danger'
              return (
                <button
                  key={row.key}
                  type="button"
                  onClick={() => onPick(row.key)}
                  className="block w-full rounded px-1 py-0.5 text-left transition-colors hover:bg-surface2/60"
                >
                  <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                    <span className="truncate font-medium">{row.label}</span>
                    <span className="shrink-0 tabular-nums">
                      {row.availability.toFixed(1)}%
                      <span className="ml-1 text-muted">({row.available}/{row.total})</span>
                    </span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-surface2">
                    <div className={`h-full rounded-full ${tone}`} style={{ width: `${Math.max(2, Math.min(100, row.availability))}%` }} />
                  </div>
                </button>
              )
            })}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function Donut({
  segments, total, size = 120, thickness = 18,
}: {
  segments: Array<{ key: string; value: number; color: string }>
  total: number
  size?: number
  thickness?: number
}) {
  const r = (size - thickness) / 2
  const c = 2 * Math.PI * r
  let offset = 0
  const sum = segments.reduce((a, s) => a + s.value, 0) || 1
  return (
    <div className="relative" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2} cy={size / 2} r={r}
          fill="none" stroke="rgb(var(--surface2))" strokeWidth={thickness}
        />
        {segments.map((s) => {
          const len = (s.value / sum) * c
          const dash = `${len} ${c - len}`
          const el = (
            <circle
              key={s.key}
              cx={size / 2} cy={size / 2} r={r}
              fill="none" stroke={s.color} strokeWidth={thickness}
              strokeDasharray={dash}
              strokeDashoffset={-offset}
              strokeLinecap="butt"
            />
          )
          offset += len
          return el
        })}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div className="text-[18px] font-bold leading-none">{total.toLocaleString()}</div>
        <div className="text-[10px] font-medium uppercase tracking-wider text-muted">Total</div>
      </div>
    </div>
  )
}

// =========================================================================
// Devices by Status card (horizontal bars)
// =========================================================================

function StatusBreakdownCard({
  healthy, warning, critical, offline, maintenance = 0, onPickStatus,
}: {
  healthy: number; warning: number; critical: number; offline: number
  maintenance?: number
  onPickStatus: (s: string) => void
}) {
  const items = [
    { key: 'healthy',  label: 'Healthy',  value: healthy,  color: 'rgb(var(--success))' },
    { key: 'warning',  label: 'Warning',  value: warning,  color: 'rgb(var(--warning))' },
    { key: 'critical', label: 'Critical', value: critical, color: 'rgb(var(--danger))' },
    { key: 'offline',  label: 'Offline',  value: offline,  color: 'rgb(var(--muted))' },
    { key: 'maintenance', label: 'Maintenance', value: maintenance, color: 'rgb(var(--primary))' },
  ]
  const max = Math.max(1, ...items.map((i) => i.value))
  const ticks = [0, Math.round(max / 4), Math.round(max / 2), Math.round((max * 3) / 4), max]
  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">Devices by Status</h3>
          <button className="text-xs text-primary hover:underline">View all</button>
        </div>
        <div className="space-y-2">
          {items.map((it) => {
            const pct = (it.value / max) * 100
            return (
              <button
                key={it.key}
                onClick={() => onPickStatus(it.key)}
                className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left transition-colors hover:bg-surface2/60"
              >
                <span className="w-16 text-xs text-muted">{it.label}</span>
                <div className="h-3 flex-1 overflow-hidden rounded-sm bg-surface2">
                  <div
                    className="h-full rounded-sm"
                    style={{ width: `${pct}%`, backgroundColor: it.color }}
                  />
                </div>
                <span className="w-10 text-right text-xs font-medium tabular-nums">{it.value.toLocaleString()}</span>
              </button>
            )
          })}
        </div>
        <div className="mt-2 flex justify-between text-[10px] text-muted">
          {ticks.map((t, i) => (
            <span key={i}>{t.toLocaleString()}</span>
          ))}
        </div>
      </CardContent>
    </Card>
  )
}

// =========================================================================
// Recent activity
// =========================================================================

type RecentAlert = {
  id: string
  severity: string
  message?: string | null
  device_id?: string | null
  device_hostname?: string | null
  triggered_at?: string | null
  status?: string | null
}

function RecentActivityCard({ alerts }: { alerts: RecentAlert[] }) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold">Recent Device Activity</h3>
          <Link to="/alerts" className="text-xs text-primary hover:underline">View all</Link>
        </div>
        <div className="space-y-3">
          {alerts.length === 0 && (
            <div className="py-4 text-center text-xs text-muted">No recent activity.</div>
          )}
          {alerts.map((a) => {
            const sev = (a.severity || '').toLowerCase()
            const tone =
              sev === 'critical' ? 'danger' :
              sev === 'warning' ? 'warning' :
              a.status === 'resolved' ? 'success' : 'primary'
            const Icon =
              tone === 'danger' ? AlertTriangle :
              tone === 'warning' ? AlertTriangle :
              tone === 'success' ? CheckCircle2 : Activity
            const label = a.message || `${a.severity || 'alert'} on ${a.device_hostname || 'device'}`
            const text = a.device_hostname
              ? `${a.device_hostname}: ${label}`
              : label
            return (
              <div key={a.id} className="flex items-start gap-2.5">
                <span
                  className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full ${
                    tone === 'success' ? 'bg-success/15 text-success'
                    : tone === 'warning' ? 'bg-warning/15 text-warning'
                    : tone === 'danger' ? 'bg-danger/15 text-danger'
                    : 'bg-primary/15 text-primary'
                  }`}
                >
                  <Icon className="h-3 w-3" />
                </span>
                <div className="min-w-0 flex-1">
                  {a.device_id ? (
                    <Link
                      to={`/devices/${a.device_id}`}
                      className="block truncate text-xs font-medium hover:underline"
                    >
                      {text}
                    </Link>
                  ) : (
                    <div className="truncate text-xs font-medium">{text}</div>
                  )}
                  <div className="text-[10px] text-muted">
                    {relativeTime(a.triggered_at || null) || '—'}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      </CardContent>
    </Card>
  )
}

// =========================================================================
// Quick actions card
// =========================================================================

function QuickActionsCard({
  onAdd, onDiscover, onImport, onBulk,
}: { onAdd: () => void; onDiscover: () => void; onImport: () => void; onBulk: () => void }) {
  return (
    <Card>
      <CardContent className="p-4">
        <h3 className="mb-3 text-sm font-semibold">Quick Actions</h3>
        <div className="grid grid-cols-2 gap-2">
          <Button variant="outline" size="sm" className="h-10 justify-start" onClick={onAdd}>
            <PlusCircle className="h-4 w-4 text-primary" />
            Add Device
          </Button>
          <Button variant="outline" size="sm" className="h-10 justify-start" onClick={onDiscover}>
            <Radar className="h-4 w-4 text-primary" />
            Discover Network
          </Button>
          <Button variant="outline" size="sm" className="h-10 justify-start" onClick={onImport}>
            <Upload className="h-4 w-4 text-primary" />
            Import Devices
          </Button>
          <Button variant="outline" size="sm" className="h-10 justify-start" onClick={onBulk}>
            <Layers className="h-4 w-4 text-primary" />
            Bulk Actions
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

// =========================================================================
// Row menu (3-dot)
// =========================================================================

function RowMenu({ onEdit, onDelete, onView }: { onEdit: () => void; onDelete: () => void; onView: () => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    function onDocClick(e: MouseEvent) {
      if (!ref.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [open])
  return (
    <div ref={ref} className="relative inline-block">
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7 text-muted hover:text-text"
        onClick={() => setOpen((o) => !o)}
        title="More"
      >
        <MoreVertical className="h-3.5 w-3.5" />
      </Button>
      {open && (
        <div className="absolute right-0 z-20 mt-1 w-36 overflow-hidden rounded-md border border-border bg-surface shadow-lg">
          <button
            type="button"
            className="flex w-full items-center gap-2 px-3 py-1.5 text-xs hover:bg-surface2"
            onClick={() => { setOpen(false); onView() }}
          >
            <Activity className="h-3.5 w-3.5" /> View details
          </button>
          <button
            type="button"
            className="flex w-full items-center gap-2 px-3 py-1.5 text-xs hover:bg-surface2"
            onClick={() => { setOpen(false); onEdit() }}
          >
            <Pencil className="h-3.5 w-3.5" /> Edit
          </button>
          <button
            type="button"
            className="flex w-full items-center gap-2 px-3 py-1.5 text-xs text-danger hover:bg-danger/5"
            onClick={() => { setOpen(false); onDelete() }}
          >
            <Trash2 className="h-3.5 w-3.5" /> Delete
          </button>
        </div>
      )}
    </div>
  )
}

// =========================================================================
// Pagination + page size
// =========================================================================

function Pagination({
  page, totalPages, onPage,
}: { page: number; totalPages: number; onPage: (p: number) => void }) {
  const pages = buildPageList(page, totalPages)
  return (
    <div className="flex items-center gap-0.5">
      <IconBtn onClick={() => onPage(Math.max(1, page - 1))} disabled={page <= 1}>
        <ChevronLeft className="h-3.5 w-3.5" />
      </IconBtn>
      {pages.map((p, i) =>
        p === '…' ? (
          <span key={`dots-${i}`} className="px-2 text-xs text-muted">…</span>
        ) : (
          <button
            key={p}
            onClick={() => onPage(p as number)}
            className={`h-8 min-w-[28px] rounded px-2 text-xs ${
              p === page
                ? 'bg-primary text-white'
                : 'text-muted hover:bg-surface2 hover:text-text'
            }`}
          >
            {p}
          </button>
        ),
      )}
      <IconBtn onClick={() => onPage(Math.min(totalPages, page + 1))} disabled={page >= totalPages}>
        <ChevronRight className="h-3.5 w-3.5" />
      </IconBtn>
    </div>
  )
}
function IconBtn({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="flex h-8 w-8 items-center justify-center rounded text-muted hover:bg-surface2 hover:text-text disabled:opacity-40 disabled:hover:bg-transparent"
    >
      {children}
    </button>
  )
}

function buildPageList(page: number, total: number): Array<number | '…'> {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1)
  const set = new Set<number>([1, total, page, page - 1, page + 1])
  if (page <= 3) { set.add(2); set.add(3); set.add(4) }
  if (page >= total - 2) { set.add(total - 1); set.add(total - 2); set.add(total - 3) }
  const list = [...set].filter((n) => n >= 1 && n <= total).sort((a, b) => a - b)
  const out: Array<number | '…'> = []
  for (let i = 0; i < list.length; i++) {
    out.push(list[i])
    if (i < list.length - 1 && list[i + 1] - list[i] > 1) out.push('…')
  }
  return out
}

function PageSizeSelect({ value, onChange }: { value: number; onChange: (n: number) => void }) {
  return (
    <Select value={String(value)} onValueChange={(v) => onChange(Number(v))}>
      <SelectTrigger className="h-8 w-[96px] text-xs">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {PAGE_SIZE_OPTIONS.map((n) => (
          <SelectItem key={n} value={String(n)}>{n} / page</SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

// =========================================================================
// Columns dialog
// =========================================================================

function ColumnsDialog({
  open, onOpenChange, visible, onChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  visible: Record<HideableCol, boolean>
  onChange: (v: Record<HideableCol, boolean>) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Columns3 className="h-5 w-5 text-primary" /> Columns
          </DialogTitle>
          <DialogDescription>
            Toggle which columns appear in the table. Device Name, IP, Status and actions stay pinned.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          {HIDEABLE_COLUMNS.map((c) => (
            <label
              key={c}
              className="flex cursor-pointer items-center justify-between rounded-md border border-border px-3 py-2 text-sm hover:bg-surface2/50"
            >
              <span>{COLUMN_LABELS[c]}</span>
              <input
                type="checkbox"
                checked={visible[c]}
                onChange={(e) => onChange({ ...visible, [c]: e.target.checked })}
              />
            </label>
          ))}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onChange({ ...DEFAULT_VISIBLE })}>Reset</Button>
          <Button onClick={() => onOpenChange(false)}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// =========================================================================
// Bulk Edit dialog
// =========================================================================

function BulkEditDialog({
  open, onOpenChange, count, loading, onSubmit,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  count: number
  loading: boolean
  onSubmit: (patch: Record<string, unknown>) => void
}) {
  const [type, setType] = useState<string>('__keep__')
  const [location, setLocation] = useState<string>('')
  const [changeLocation, setChangeLocation] = useState(false)
  const [pingInterval, setPingInterval] = useState<string>('')
  const [pingEnabled, setPingEnabled] = useState<string>('__keep__')
  const [group, setGroup] = useState<string>('__keep__')
  const [snmpEnabled, setSnmpEnabled] = useState<string>('__keep__')
  const [snmpMode, setSnmpMode] = useState<'__keep__' | 'saved' | 'manual'>('__keep__')
  const [credentialId, setCredentialId] = useState<string>('')
  const [snmpVersion, setSnmpVersion] = useState<'2c' | '1' | '3'>('2c')
  const [snmpCommunity, setSnmpCommunity] = useState<string>('public')
  const [snmpPort, setSnmpPort] = useState<string>('161')
  const [snmpPollInterval, setSnmpPollInterval] = useState<string>('')

  const { data: groups } = useQuery<Group[]>({
    queryKey: ['devices', 'groups'],
    queryFn: async () => (await api.get('/devices/groups')).data,
    enabled: open,
  })
  const { data: credentials } = useQuery<Credential[]>({
    queryKey: ['snmp-credentials'],
    queryFn: async () => (await api.get('/snmp-credentials')).data,
    enabled: open,
  })

  useEffect(() => {
    if (!open) {
      setType('__keep__'); setLocation(''); setChangeLocation(false)
      setPingInterval(''); setPingEnabled('__keep__'); setGroup('__keep__')
      setSnmpEnabled('__keep__'); setSnmpMode('__keep__'); setCredentialId('')
      setSnmpVersion('2c'); setSnmpCommunity('public'); setSnmpPort('161')
      setSnmpPollInterval('')
    }
  }, [open])

  function handleSubmit() {
    const patch: Record<string, unknown> = {}
    if (type !== '__keep__') patch.device_type = type
    if (changeLocation) patch.location = location || null
    if (pingInterval) { const n = Number(pingInterval); if (!Number.isNaN(n)) patch.ping_interval = n }
    if (pingEnabled !== '__keep__') patch.ping_enabled = pingEnabled === 'on'
    if (group !== '__keep__') patch.group_id = group === '__clear__' ? null : group
    if (snmpEnabled !== '__keep__') patch.snmp_enabled = snmpEnabled === 'on'

    if (snmpMode === 'saved' && credentialId) {
      const cred = credentials?.find((c) => c.id === credentialId)
      if (cred) {
        patch.snmp_credential_id = cred.id
        patch.snmp_version = cred.snmp_version
        patch.snmp_port = cred.port
        patch.snmp_timeout_ms = cred.timeout_ms
        patch.snmp_retries = cred.retries
        if (cred.snmp_version !== '3') patch.snmp_community = cred.community || 'public'
        if (snmpEnabled === '__keep__') patch.snmp_enabled = true
      }
    } else if (snmpMode === 'manual') {
      patch.snmp_credential_id = null
      patch.snmp_version = snmpVersion
      const portNum = Number(snmpPort)
      if (!Number.isNaN(portNum)) patch.snmp_port = portNum
      if (snmpVersion !== '3') patch.snmp_community = snmpCommunity || 'public'
      if (snmpEnabled === '__keep__') patch.snmp_enabled = true
    }
    if (snmpPollInterval) {
      const n = Number(snmpPollInterval)
      if (!Number.isNaN(n)) patch.snmp_poll_interval = n
    }
    onSubmit(patch)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Pencil className="h-5 w-5 text-primary" />
            Edit {count} device{count === 1 ? '' : 's'}
          </DialogTitle>
          <DialogDescription>
            Only the fields you change are applied. Unchanged fields stay as-is on each device.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-5">
          <section className="space-y-3">
            <SectionTitle>General</SectionTitle>
            <div className="grid gap-3 md:grid-cols-2">
              <FormField label="Device type">
                <Select value={type} onValueChange={setType}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__keep__">Keep existing</SelectItem>
                    {DEVICE_TYPES.map((t) => (
                      <SelectItem key={t} value={t}>
                        <span className="capitalize">{t.replace('_', ' ')}</span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Group">
                <Select value={group} onValueChange={setGroup}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__keep__">Keep existing</SelectItem>
                    <SelectItem value="__clear__"><span className="text-muted">Remove from group</span></SelectItem>
                    {(groups || []).map((g) => (
                      <SelectItem key={g.id} value={g.id}>{g.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
            </div>
            <FormField label="Location">
              <div className="flex items-center gap-2">
                <label className="flex items-center gap-1.5 text-xs text-muted">
                  <input
                    type="checkbox"
                    checked={changeLocation}
                    onChange={(e) => setChangeLocation(e.target.checked)}
                  />
                  Change
                </label>
                <Input
                  placeholder="e.g. Riyadh HQ (blank to clear)"
                  value={location}
                  onChange={(e) => setLocation(e.target.value)}
                  disabled={!changeLocation}
                  className="flex-1"
                />
              </div>
            </FormField>
          </section>

          <section className="space-y-3">
            <SectionTitle>Ping</SectionTitle>
            <div className="grid gap-3 md:grid-cols-2">
              <FormField label="Ping enabled">
                <Select value={pingEnabled} onValueChange={setPingEnabled}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__keep__">Keep existing</SelectItem>
                    <SelectItem value="on">Enable</SelectItem>
                    <SelectItem value="off">Disable</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Ping interval (seconds)" hint="Blank = keep existing">
                <Input
                  type="number" min={10} max={3600} placeholder="e.g. 60"
                  value={pingInterval}
                  onChange={(e) => setPingInterval(e.target.value)}
                />
              </FormField>
            </div>
          </section>

          <section className="space-y-3">
            <SectionTitle>SNMP</SectionTitle>
            <div className="grid gap-3 md:grid-cols-2">
              <FormField label="SNMP polling">
                <Select value={snmpEnabled} onValueChange={setSnmpEnabled}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__keep__">Keep existing</SelectItem>
                    <SelectItem value="on">Enable</SelectItem>
                    <SelectItem value="off">Disable</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
              <FormField label="Credential source">
                <Select value={snmpMode} onValueChange={(v) => setSnmpMode(v as typeof snmpMode)}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__keep__">Keep existing</SelectItem>
                    <SelectItem value="saved">Use saved credential</SelectItem>
                    <SelectItem value="manual">Set manually</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
            </div>

            {snmpMode === 'saved' && (
              <FormField label="Saved credential" hint="Applies the credential's version, port, community/v3 to every selected device">
                <Select value={credentialId} onValueChange={setCredentialId}>
                  <SelectTrigger><SelectValue placeholder="Select credential…" /></SelectTrigger>
                  <SelectContent>
                    {(credentials || []).map((c) => (
                      <SelectItem key={c.id} value={c.id}>
                        <span className="flex items-center gap-2">
                          {c.name}
                          <span className="text-xs text-muted">v{c.snmp_version} · port {c.port}</span>
                          {c.is_default && <Badge variant="info">default</Badge>}
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
            )}

            {snmpMode === 'manual' && (
              <div className="grid gap-3 md:grid-cols-3">
                <FormField label="Version">
                  <Select value={snmpVersion} onValueChange={(v) => setSnmpVersion(v as typeof snmpVersion)}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="2c">v2c</SelectItem>
                      <SelectItem value="1">v1</SelectItem>
                      <SelectItem value="3">v3</SelectItem>
                    </SelectContent>
                  </Select>
                </FormField>
                <FormField label="Community" hint="Ignored for v3">
                  <Input
                    value={snmpCommunity}
                    onChange={(e) => setSnmpCommunity(e.target.value)}
                    placeholder="public" disabled={snmpVersion === '3'}
                  />
                </FormField>
                <FormField label="Port">
                  <Input
                    type="number" min={1} max={65535}
                    value={snmpPort}
                    onChange={(e) => setSnmpPort(e.target.value)}
                  />
                </FormField>
              </div>
            )}

            <FormField label="Poll interval (seconds)" hint="Blank = keep existing">
              <Input
                type="number" min={30} max={3600} placeholder="e.g. 60"
                value={snmpPollInterval}
                onChange={(e) => setSnmpPollInterval(e.target.value)}
              />
            </FormField>
          </section>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={loading}>
            <X className="h-4 w-4" /> Cancel
          </Button>
          <Button onClick={handleSubmit} disabled={loading}>
            {loading ? 'Saving…' : `Apply to ${count}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">{children}</div>
  )
}

// =========================================================================
// Time range picker — drives the per-device Uptime % column
// =========================================================================

function TimeRangePicker({
  value,
  onChange,
}: {
  value: string
  onChange: (key: string) => void
}) {
  const presets = [
    { key: '1h', label: '1h' },
    { key: '24h', label: '24h' },
    { key: '7d', label: '7d' },
    { key: '1M', label: '1M' },
  ]
  return (
    <div
      className="inline-flex h-10 items-center gap-1 rounded-md border border-border bg-surface px-1"
      role="tablist"
      aria-label="Uptime time range"
    >
      <span className="px-1.5 text-[11px] font-medium uppercase tracking-wider text-muted">
        Uptime
      </span>
      {presets.map((p) => {
        const active = value === p.key
        return (
          <button
            key={p.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(p.key)}
            className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
              active
                ? 'bg-primary/15 text-primary'
                : 'text-muted hover:bg-surface2 hover:text-text'
            }`}
          >
            {p.label}
          </button>
        )
      })}
    </div>
  )
}

// =========================================================================
// Uptime % cell — colored by SLA threshold
// =========================================================================

function UptimePctCell({ pct, fallback }: { pct: number | undefined; fallback: string }) {
  if (typeof pct !== 'number' || !Number.isFinite(pct)) {
    return <span className="text-muted">{fallback || '—'}</span>
  }
  const color =
    pct >= 99.9 ? 'text-success'
    : pct >= 99 ? 'text-text'
    : pct >= 95 ? 'text-warning'
    : 'text-danger'
  // Bar visualises the gap from 100%; saturates at 100.
  const barPct = Math.max(0, Math.min(100, pct))
  return (
    <div className="flex flex-col gap-1">
      <span className={`font-mono text-sm font-semibold tabular-nums ${color}`}>
        {pct.toFixed(2)}%
      </span>
      <div className="h-1 w-24 overflow-hidden rounded-full bg-surface2">
        <div
          className={`h-full rounded-full ${
            pct >= 99.9 ? 'bg-success'
            : pct >= 99 ? 'bg-primary'
            : pct >= 95 ? 'bg-warning'
            : 'bg-danger'
          }`}
          style={{ width: `${barPct}%` }}
        />
      </div>
    </div>
  )
}

// =========================================================================
// Sortable header
// =========================================================================

function SortableTh({
  label, col, current, order, onClick, className,
}: {
  label: string
  col: SortKey
  current: SortKey
  order: SortOrder
  onClick: (c: SortKey) => void
  className?: string
}) {
  const isActive = current === col
  return (
    <Th className={className}>
      <button
        type="button"
        onClick={() => onClick(col)}
        className={`inline-flex items-center gap-1 text-xs font-medium uppercase tracking-wider transition-colors ${
          isActive ? 'text-text' : 'text-muted hover:text-text'
        }`}
      >
        {label}
        {isActive ? (
          order === 'asc' ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />
        ) : null}
      </button>
    </Th>
  )
}

// =========================================================================
// Helpers
// =========================================================================

const TYPE_STYLE: Record<string, { Icon: React.ComponentType<{ className?: string }>; badge: string }> = {
  router:        { Icon: Router,    badge: 'border-primary/20 bg-primary/10 text-primary' },
  switch:        { Icon: Router,    badge: 'border-success/20 bg-success/10 text-success' },
  firewall:      { Icon: Shield,    badge: 'border-danger/20 bg-danger/10 text-danger' },
  server:        { Icon: Server,    badge: 'border-primary/20 bg-primary/10 text-primary' },
  access_point:  { Icon: Wifi,      badge: 'border-accent/20 bg-accent/10 text-accent' },
  printer:       { Icon: HardDrive, badge: 'border-muted/20 bg-muted/10 text-muted' },
  storage:       { Icon: Database,  badge: 'border-accent/20 bg-accent/10 text-accent' },
  ups:           { Icon: HardDrive, badge: 'border-warning/20 bg-warning/10 text-warning' },
  hypervisor:    { Icon: Server,    badge: 'border-accent/20 bg-accent/10 text-accent' },
  other:         { Icon: Server,    badge: 'border-border bg-surface2 text-muted' },
}

function normalizeType(t: string): string {
  if (TYPE_STYLE[t]) return t
  if (t.includes('switch')) return 'switch'
  if (t.includes('router')) return 'router'
  if (t.includes('firewall')) return 'firewall'
  if (t.includes('server')) return 'server'
  if (t.includes('access') || t.includes('ap')) return 'access_point'
  return 'other'
}

function categoryOf(t: string): 'Server' | 'Network' | 'Security' | 'Wireless' | 'Other' {
  const n = normalizeType(t)
  if (n === 'server' || n === 'hypervisor' || n === 'storage') return 'Server'
  if (n === 'router' || n === 'switch') return 'Network'
  if (n === 'firewall') return 'Security'
  if (n === 'access_point') return 'Wireless'
  return 'Other'
}

function healthOf(d: Device): HealthKind {
  if (d.status === 'up') return 'healthy'
  if (d.status === 'degraded') return 'warning'
  if (d.status === 'down') return 'critical'
  if (d.status === 'maintenance') return 'maintenance'
  return 'offline'
}

function extractSortVal(d: Device, k: SortKey): string | number | null {
  switch (k) {
    case 'hostname':     return d.hostname.toLowerCase()
    case 'ip_address':   return ipToInt(d.ip_address)
    case 'device_type':  return d.device_type
    case 'vendor':       return (d.vendor || '').toLowerCase()
    case 'location':     return (d.location || '').toLowerCase()
    case 'group_name':   return (d.group_name || '').toLowerCase()
    case 'status':       return d.status
    case 'last_rtt_ms':  return d.last_rtt_ms ?? Infinity
    case 'last_seen':    return d.last_seen || ''
    default:             return null
  }
}
function ipToInt(ip: string): number {
  const parts = ip.split('.').map(Number)
  if (parts.length !== 4 || parts.some((n) => Number.isNaN(n))) return Number.MAX_SAFE_INTEGER
  return (parts[0] << 24) + (parts[1] << 16) + (parts[2] << 8) + parts[3]
}
function csvEscape(v: string): string {
  if (v == null) return ''
  if (/[",\n]/.test(v)) return `"${v.replace(/"/g, '""')}"`
  return v
}

function buildAvailabilityRows(
  rows: Array<{ device: Device; health: HealthKind; uptimePct?: number }>,
  getKey: (row: { device: Device; health: HealthKind; uptimePct?: number }) => string,
  labelOf: (key: string) => string,
): AvailabilityRow[] {
  const grouped = new Map<string, { total: number; available: number; score: number }>()
  for (const row of rows) {
    const key = getKey(row) || 'Unassigned'
    const current = grouped.get(key) || { total: 0, available: 0, score: 0 }
    const availability = typeof row.uptimePct === 'number'
      ? row.uptimePct
      : row.health === 'healthy'
        ? 100
        : row.health === 'warning'
          ? 75
          : 0
    current.total += 1
    current.available += availability > 0 ? 1 : 0
    current.score += availability
    grouped.set(key, current)
  }
  return Array.from(grouped.entries())
    .map(([key, value]) => ({
      key,
      label: labelOf(key),
      total: value.total,
      available: value.available,
      availability: value.total ? value.score / value.total : 0,
    }))
    .sort((a, b) => b.total - a.total || a.label.localeCompare(b.label))
}

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function formatRealUptime(seconds: number | undefined, health: HealthKind): string {
  if (health !== 'healthy' && health !== 'warning') return '—'
  if (seconds == null || !isFinite(seconds) || seconds <= 0) return '—'
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}
