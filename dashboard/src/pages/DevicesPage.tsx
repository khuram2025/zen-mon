import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import {
  Activity,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronDown,
  Columns3,
  Download,
  MapPin,
  Pencil,
  Plus,
  RefreshCw,
  Router,
  Search,
  Server,
  Shield,
  SlidersHorizontal,
  Trash2,
  Wifi,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { StatusDot, deviceStatusKind } from '@/components/ui/StatusDot'
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

// IDs of table columns that can be hidden. Hostname/IP/actions stay pinned.
const HIDEABLE_COLUMNS = [
  'protocol',
  'vendor',
  'type',
  'group',
  'location',
  'rtt',
  'last_seen',
] as const
type HideableCol = (typeof HIDEABLE_COLUMNS)[number]

const DEFAULT_VISIBLE: Record<HideableCol, boolean> = {
  protocol: true,
  vendor: true,
  type: true,
  group: true,
  location: true,
  rtt: true,
  last_seen: true,
}

const COLUMN_LABELS: Record<HideableCol, string> = {
  protocol: 'Protocol',
  vendor: 'Vendor / Model',
  type: 'Type',
  group: 'Group',
  location: 'Location',
  rtt: 'RTT',
  last_seen: 'Last seen',
}

// Persist column visibility + density across sessions.
const PREFS_KEY = 'zp-devices-prefs-v1'
type Prefs = { visible: Record<HideableCol, boolean>; density: 'compact' | 'comfortable' }
function loadPrefs(): Prefs {
  try {
    const raw = localStorage.getItem(PREFS_KEY)
    if (raw) {
      const p = JSON.parse(raw)
      return {
        visible: { ...DEFAULT_VISIBLE, ...(p.visible || {}) },
        density: p.density === 'comfortable' ? 'comfortable' : 'compact',
      }
    }
  } catch {
    /* ignore */
  }
  return { visible: { ...DEFAULT_VISIBLE }, density: 'compact' }
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
  const [params, setParams] = useSearchParams()

  // All user-facing filter/sort state lives in the URL so that refreshes,
  // back/forward nav, and shared links round-trip correctly.
  const search = params.get('q') || ''
  const statusFilter = params.get('status') || ''
  const typeFilter = params.get('type') || ''
  const groupFilter = params.get('group') || ''
  const locationFilter = params.get('loc') || ''
  const protocolFilter = params.get('proto') || '' // '', 'ping', 'snmp'
  const sortKey = (params.get('sort') as SortKey) || 'hostname'
  const sortOrder = (params.get('order') as SortOrder) || 'asc'

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

  // -----------------------------------------------------------------------
  // Data fetch — one call, then we filter/sort client-side for snappy UX.
  // Backend still accepts search/status for wire-efficient responses when
  // those are set.
  // -----------------------------------------------------------------------

  const { data, isLoading, isFetching, refetch, dataUpdatedAt } = useQuery<{
    data: Device[]
    meta: any
  }>({
    queryKey: ['devices', 'list', { search, statusFilter }],
    queryFn: async () => {
      const qs: string[] = ['limit=200']
      if (search) qs.push(`search=${encodeURIComponent(search)}`)
      if (statusFilter) qs.push(`status=${encodeURIComponent(statusFilter)}`)
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

  const devices = data?.data || []

  // -----------------------------------------------------------------------
  // Client-side filter + sort
  // -----------------------------------------------------------------------

  const filtered = useMemo(() => {
    return devices.filter((d) => {
      if (typeFilter && d.device_type !== typeFilter) return false
      if (groupFilter && d.group_id !== groupFilter) return false
      if (locationFilter && (d.location || '') !== locationFilter) return false
      if (protocolFilter === 'snmp' && !d.snmp_enabled) return false
      if (protocolFilter === 'ping' && d.snmp_enabled) return false
      return true
    })
  }, [devices, typeFilter, groupFilter, locationFilter, protocolFilter])

  const sorted = useMemo(() => {
    const arr = [...filtered]
    const dir = sortOrder === 'asc' ? 1 : -1
    arr.sort((a, b) => {
      const va = extractSortVal(a, sortKey)
      const vb = extractSortVal(b, sortKey)
      if (va === vb) return 0
      if (va == null) return 1
      if (vb == null) return -1
      return va < vb ? -1 * dir : 1 * dir
    })
    return arr
  }, [filtered, sortKey, sortOrder])

  // Aggregate counts are derived from the full list (pre-filter) so users
  // still see the total network picture even with an active filter.
  const counts = useMemo(() => {
    const total = devices.length
    const up = devices.filter((d) => d.status === 'up').length
    const down = devices.filter((d) => d.status === 'down').length
    const degraded = devices.filter((d) => d.status === 'degraded').length
    const snmp = devices.filter((d) => d.snmp_enabled).length
    const rtts = devices.map((d) => d.last_rtt_ms).filter((v): v is number => v != null && v > 0)
    const avgRtt = rtts.length ? rtts.reduce((a, b) => a + b, 0) / rtts.length : null
    return { total, up, down, degraded, snmp, avgRtt }
  }, [devices])

  const activeFilterCount =
    (typeFilter ? 1 : 0) +
    (groupFilter ? 1 : 0) +
    (locationFilter ? 1 : 0) +
    (protocolFilter ? 1 : 0)

  // -----------------------------------------------------------------------
  // Selection housekeeping — drop ids that no longer match the view.
  // -----------------------------------------------------------------------

  useEffect(() => {
    if (selected.size === 0) return
    const alive = new Set(sorted.map((d) => d.id))
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
    if (sorted.length === 0) return
    if (sorted.every((d) => selected.has(d.id))) {
      const n = new Set(selected)
      sorted.forEach((d) => n.delete(d.id))
      setSelected(n)
    } else {
      const n = new Set(selected)
      sorted.forEach((d) => n.add(d.id))
      setSelected(n)
    }
  }

  function onSortClick(key: SortKey) {
    if (sortKey === key) {
      patchParams({ order: sortOrder === 'asc' ? 'desc' : 'asc' })
    } else {
      patchParams({ sort: key, order: 'asc' })
    }
  }

  function clearFilters() {
    patchParams({ type: null, group: null, loc: null, proto: null, status: null })
  }

  function exportCsv() {
    const visibleColsFirst: Array<[string, (d: Device) => string]> = [
      ['hostname', (d) => d.hostname],
      ['ip_address', (d) => d.ip_address],
      ['device_type', (d) => d.device_type],
      ['vendor', (d) => d.vendor || ''],
      ['model', (d) => d.model || ''],
      ['group', (d) => d.group_name || ''],
      ['location', (d) => d.location || ''],
      ['status', (d) => d.status],
      ['snmp', (d) => (d.snmp_enabled ? 'yes' : 'no')],
      ['last_rtt_ms', (d) => (d.last_rtt_ms != null ? d.last_rtt_ms.toFixed(2) : '')],
      ['last_seen', (d) => d.last_seen || ''],
    ]
    const rows = [
      visibleColsFirst.map(([h]) => h).join(','),
      ...sorted.map((d) =>
        visibleColsFirst.map(([, f]) => csvEscape(f(d))).join(','),
      ),
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

  // Keyboard shortcut: `/` focuses search when not already in an input.
  const searchRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return
      const t = e.target as HTMLElement
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      e.preventDefault()
      searchRef.current?.focus()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const rowPadding = prefs.density === 'compact' ? 'py-1.5' : 'py-3'
  const allSelectedOnPage =
    sorted.length > 0 && sorted.every((d) => selected.has(d.id))
  const someSelectedOnPage =
    sorted.some((d) => selected.has(d.id)) && !allSelectedOnPage

  const visibleCount = filtered.length
  const totalCount = devices.length

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Server className="h-6 w-6 text-primary" /> Devices
          </h1>
          <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
            <span>{counts.total} monitored</span>
            <span>·</span>
            <span className="inline-flex items-center gap-1">
              <Shield className="h-3 w-3" />
              {counts.snmp} SNMP
            </span>
            {counts.avgRtt != null && (
              <>
                <span>·</span>
                <span className="inline-flex items-center gap-1">
                  <Activity className="h-3 w-3" />
                  avg {counts.avgRtt.toFixed(1)}ms
                </span>
              </>
            )}
            <span>·</span>
            <span className="inline-flex items-center gap-1">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  isFetching ? 'animate-pulse bg-primary' : 'bg-success'
                }`}
              />
              updated {dataUpdatedAt ? relativeTime(new Date(dataUpdatedAt).toISOString()) : '—'}
            </span>
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            onClick={() => refetch()}
            disabled={isFetching}
            title="Refresh now"
          >
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
          <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
            <Plus className="h-4 w-4" />
            Add device
          </Button>
        </div>
      </div>

      {/* Stat pills */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        <StatCard
          label="Total"
          value={counts.total}
          icon={<Server className="h-3.5 w-3.5" />}
          active={!statusFilter}
          onClick={() => patchParams({ status: null })}
        />
        <StatCard
          label="Online"
          value={counts.up}
          tone="success"
          icon={<span className="h-1.5 w-1.5 rounded-full bg-success" />}
          active={statusFilter === 'up'}
          onClick={() => patchParams({ status: statusFilter === 'up' ? null : 'up' })}
        />
        <StatCard
          label="Offline"
          value={counts.down}
          tone={counts.down > 0 ? 'danger' : undefined}
          icon={<span className="h-1.5 w-1.5 rounded-full bg-danger" />}
          active={statusFilter === 'down'}
          onClick={() => patchParams({ status: statusFilter === 'down' ? null : 'down' })}
        />
        <StatCard
          label="Degraded"
          value={counts.degraded}
          tone={counts.degraded > 0 ? 'warning' : undefined}
          icon={<span className="h-1.5 w-1.5 rounded-full bg-warning" />}
          active={statusFilter === 'degraded'}
          onClick={() =>
            patchParams({ status: statusFilter === 'degraded' ? null : 'degraded' })
          }
        />
        <StatCard
          label="SNMP"
          value={counts.snmp}
          icon={<Shield className="h-3.5 w-3.5" />}
          active={protocolFilter === 'snmp'}
          onClick={() => patchParams({ proto: protocolFilter === 'snmp' ? null : 'snmp' })}
        />
      </div>

      <Card>
        <CardContent className="space-y-3 pt-4">
          {/* Toolbar */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-[240px] max-w-md flex-1">
              <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <Input
                ref={searchRef}
                placeholder="Search hostname, IP, description…"
                value={search}
                onChange={(e) => patchParams({ q: e.target.value || null })}
                className="pl-8 pr-16"
              />
              <kbd className="pointer-events-none absolute right-2 top-1/2 hidden -translate-y-1/2 rounded border border-border bg-surface2 px-1.5 py-0.5 text-[10px] font-medium text-muted sm:inline-block">
                /
              </kbd>
            </div>

            <FilterDropdown
              label="Type"
              icon={<Router className="h-3.5 w-3.5" />}
              value={typeFilter}
              onChange={(v) => patchParams({ type: v || null })}
              options={(deviceTypes || DEVICE_TYPES).map((t) => ({
                value: t,
                label: titleCase(t.replace('_', ' ')),
              }))}
            />
            <FilterDropdown
              label="Group"
              icon={<Tag className="h-3.5 w-3.5" />}
              value={groupFilter}
              onChange={(v) => patchParams({ group: v || null })}
              options={(groups || []).map((g) => ({
                value: g.id,
                label: g.name,
                color: g.color || undefined,
              }))}
            />
            <FilterDropdown
              label="Location"
              icon={<MapPin className="h-3.5 w-3.5" />}
              value={locationFilter}
              onChange={(v) => patchParams({ loc: v || null })}
              options={(locations || []).map((l) => ({ value: l, label: l }))}
            />

            {/* Protocol chip toggle */}
            <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5">
              {[
                { label: 'All', value: '' },
                { label: 'PING', value: 'ping' },
                { label: 'SNMP', value: 'snmp' },
              ].map((p) => (
                <button
                  key={p.value}
                  onClick={() => patchParams({ proto: p.value || null })}
                  className={`rounded px-2 py-1 text-[11px] font-medium transition-colors ${
                    protocolFilter === p.value
                      ? 'bg-surface text-text shadow-sm'
                      : 'text-muted hover:text-text'
                  }`}
                >
                  {p.label}
                </button>
              ))}
            </div>

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

            <div className="ml-auto flex items-center gap-1">
              <Button
                variant="ghost"
                size="sm"
                onClick={exportCsv}
                disabled={sorted.length === 0}
                title="Export visible rows as CSV"
              >
                <Download className="h-4 w-4" />
                Export
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setColumnsOpen(true)}
                title="Columns"
              >
                <Columns3 className="h-4 w-4" />
                Columns
              </Button>
              <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5">
                {(['compact', 'comfortable'] as const).map((d) => (
                  <button
                    key={d}
                    onClick={() => setPrefs({ ...prefs, density: d })}
                    className={`rounded px-2 py-1 text-[11px] font-medium capitalize transition-colors ${
                      prefs.density === d
                        ? 'bg-surface text-text shadow-sm'
                        : 'text-muted hover:text-text'
                    }`}
                    title={`${d} row height`}
                  >
                    {d}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Bulk action bar */}
          {selected.size > 0 && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-primary/30 bg-primary/5 px-3 py-2">
              <div className="flex items-center gap-2 text-sm">
                <span className="font-semibold text-primary">{selected.size}</span>
                <span className="text-muted">of {visibleCount} selected</span>
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
                  <Pencil className="h-3.5 w-3.5" />
                  Edit
                </Button>
                <Button variant="destructive" size="sm" onClick={() => setBulkDeleteOpen(true)}>
                  <Trash2 className="h-3.5 w-3.5" />
                  Delete
                </Button>
              </div>
            </div>
          )}

          {/* Table */}
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="sticky top-0 z-10 bg-surface2/80 backdrop-blur">
                <Tr>
                  <Th className="w-8">
                    <input
                      type="checkbox"
                      aria-label="Select all"
                      checked={allSelectedOnPage}
                      ref={(el) => {
                        if (el) el.indeterminate = someSelectedOnPage
                      }}
                      onChange={toggleAll}
                    />
                  </Th>
                  <SortableTh
                    label=""
                    srLabel="Status"
                    col="status"
                    current={sortKey}
                    order={sortOrder}
                    onClick={onSortClick}
                    className="w-8"
                  />
                  <SortableTh
                    label="Hostname"
                    col="hostname"
                    current={sortKey}
                    order={sortOrder}
                    onClick={onSortClick}
                  />
                  <SortableTh
                    label="IP address"
                    col="ip_address"
                    current={sortKey}
                    order={sortOrder}
                    onClick={onSortClick}
                  />
                  {prefs.visible.protocol && <Th>Protocol</Th>}
                  {prefs.visible.vendor && (
                    <SortableTh
                      label="Vendor / Model"
                      col="vendor"
                      current={sortKey}
                      order={sortOrder}
                      onClick={onSortClick}
                    />
                  )}
                  {prefs.visible.type && (
                    <SortableTh
                      label="Type"
                      col="device_type"
                      current={sortKey}
                      order={sortOrder}
                      onClick={onSortClick}
                    />
                  )}
                  {prefs.visible.group && (
                    <SortableTh
                      label="Group"
                      col="group_name"
                      current={sortKey}
                      order={sortOrder}
                      onClick={onSortClick}
                    />
                  )}
                  {prefs.visible.location && (
                    <SortableTh
                      label="Location"
                      col="location"
                      current={sortKey}
                      order={sortOrder}
                      onClick={onSortClick}
                    />
                  )}
                  {prefs.visible.rtt && (
                    <SortableTh
                      label="RTT"
                      col="last_rtt_ms"
                      current={sortKey}
                      order={sortOrder}
                      onClick={onSortClick}
                      className="text-right"
                    />
                  )}
                  {prefs.visible.last_seen && (
                    <SortableTh
                      label="Last seen"
                      col="last_seen"
                      current={sortKey}
                      order={sortOrder}
                      onClick={onSortClick}
                    />
                  )}
                  <Th className="w-10"></Th>
                </Tr>
              </THead>
              <TBody>
                {isLoading && (
                  <Tr>
                    <Td colSpan={12}>
                      <SkeletonTable rows={6} cols={8} />
                    </Td>
                  </Tr>
                )}
                {!isLoading && sorted.length === 0 && (
                  <Tr>
                    <Td colSpan={12} className="py-10">
                      <div className="flex flex-col items-center gap-2 text-center text-muted">
                        <Server className="h-8 w-8 opacity-50" />
                        <div className="text-sm font-medium text-text">No devices match</div>
                        <div className="text-xs">
                          {totalCount === 0 ? (
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
                              <button
                                type="button"
                                onClick={clearFilters}
                                className="text-primary hover:underline"
                              >
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
                {sorted.map((d) => {
                  const isSel = selected.has(d.id)
                  const rtt = d.last_rtt_ms
                  const rttTone =
                    rtt == null ? 'muted' : rtt < 20 ? 'text' : rtt < 100 ? 'warning' : 'danger'
                  return (
                    <Tr
                      key={d.id}
                      className={`${isSel ? 'bg-primary/5' : ''} [&>td]:${rowPadding}`}
                    >
                      <Td>
                        <input
                          type="checkbox"
                          aria-label={`Select ${d.hostname}`}
                          checked={isSel}
                          onChange={() => toggleRow(d.id)}
                        />
                      </Td>
                      <Td>
                        <StatusDot
                          status={deviceStatusKind(d.status)}
                          pulse={d.status === 'up'}
                        />
                      </Td>
                      <Td>
                        <Link
                          to={`/devices/${d.id}`}
                          className="font-medium text-text hover:text-primary hover:underline"
                          title={d.hostname}
                        >
                          {d.hostname}
                        </Link>
                      </Td>
                      <Td className="font-mono text-xs text-muted">{d.ip_address}</Td>
                      {prefs.visible.protocol && (
                        <Td>
                          <ProtocolBadges device={d} />
                        </Td>
                      )}
                      {prefs.visible.vendor && (
                        <Td className="text-sm">
                          {d.vendor || d.model ? (
                            <div>
                              <div className="font-medium leading-tight">{d.vendor || '—'}</div>
                              {d.model && (
                                <div className="text-[11px] text-muted">{d.model}</div>
                              )}
                            </div>
                          ) : (
                            <span className="text-muted">—</span>
                          )}
                        </Td>
                      )}
                      {prefs.visible.type && (
                        <Td className="text-sm capitalize">
                          {d.device_type.replace('_', ' ')}
                        </Td>
                      )}
                      {prefs.visible.group && (
                        <Td className="text-sm">
                          {d.group_name ? (
                            <span className="inline-flex items-center gap-1.5">
                              <GroupDot
                                color={groups?.find((g) => g.id === d.group_id)?.color}
                              />
                              {d.group_name}
                            </span>
                          ) : (
                            <span className="text-muted">—</span>
                          )}
                        </Td>
                      )}
                      {prefs.visible.location && (
                        <Td className="text-sm">{d.location || '—'}</Td>
                      )}
                      {prefs.visible.rtt && (
                        <Td className={`text-right font-mono text-xs text-${rttTone}`}>
                          {rtt != null ? `${rtt.toFixed(1)}ms` : '—'}
                        </Td>
                      )}
                      {prefs.visible.last_seen && (
                        <Td className="text-xs text-muted">{relativeTime(d.last_seen)}</Td>
                      )}
                      <Td>
                        <div className="flex gap-0.5">
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            onClick={() => { setEditing(d); setFormOpen(true) }}
                            title="Edit"
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 text-muted hover:text-danger"
                            onClick={() => setDeleting(d)}
                            title="Delete"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </Td>
                    </Tr>
                  )
                })}
              </TBody>
            </Table>
          </div>

          {/* Footer */}
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
            <span>
              Showing <span className="font-medium text-text">{sorted.length}</span> of{' '}
              <span className="font-medium text-text">{totalCount}</span>
              {activeFilterCount > 0 && (
                <>
                  {' '}· {activeFilterCount} filter{activeFilterCount === 1 ? '' : 's'} applied
                </>
              )}
            </span>
            <span className="inline-flex items-center gap-2">
              <SlidersHorizontal className="h-3 w-3" />
              sort: {sortKey} {sortOrder === 'asc' ? '↑' : '↓'}
            </span>
          </div>
        </CardContent>
      </Card>

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

// -------------------------------------------------------------------------
// Sub-components
// -------------------------------------------------------------------------

/** Health-aware PING + SNMP badges.
 *
 * PING:   green when `up`, red when `down`/`degraded`, amber when `unknown`,
 *         muted outline when ping is disabled.
 * SNMP:   green when discovery has succeeded (sys_object_id present),
 *         red when enabled but misconfigured (v3 without user/auth, or no
 *         credential + no sys_object_id), amber when waiting for first poll,
 *         muted outline when SNMP is disabled.
 */
function ProtocolBadges({ device }: { device: Device }) {
  // ---- PING ----
  let pingTitle = 'Ping disabled'
  let pingClass = 'border-border bg-surface2 text-muted opacity-60'
  if (device.ping_enabled) {
    if (device.status === 'up') {
      pingTitle = 'Ping OK'
      pingClass = 'border-success/30 bg-success/10 text-success'
    } else if (device.status === 'down') {
      pingTitle = 'Ping failing — device unreachable'
      pingClass = 'border-danger/30 bg-danger/10 text-danger'
    } else if (device.status === 'degraded') {
      pingTitle = 'Ping degraded — packet loss or high RTT'
      pingClass = 'border-danger/30 bg-danger/10 text-danger'
    } else {
      pingTitle = 'Ping unknown — no sample yet'
      pingClass = 'border-warning/30 bg-warning/10 text-warning'
    }
  }

  // ---- SNMP ----
  let snmpShown = true
  let snmpTitle = 'SNMP disabled'
  let snmpClass = 'border-border bg-surface2 text-muted opacity-60'
  if (!device.snmp_enabled) {
    snmpShown = false
  } else {
    const v3Missing =
      device.snmp_version === '3' &&
      !(device.snmp_v3_username && device.snmp_auth_configured)
    const discovered = !!device.sys_object_id
    if (v3Missing) {
      snmpTitle = 'SNMPv3 not configured — missing username or auth'
      snmpClass = 'border-danger/30 bg-danger/10 text-danger'
    } else if (discovered) {
      snmpTitle = 'SNMP responding'
      snmpClass = 'border-success/30 bg-success/10 text-success'
    } else {
      snmpTitle = 'SNMP enabled but never responded — check credentials / reachability'
      snmpClass = 'border-danger/30 bg-danger/10 text-danger'
    }
  }

  return (
    <div className="flex items-center gap-1">
      <span
        title={pingTitle}
        className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${pingClass}`}
      >
        <Wifi className="h-3 w-3" />
        PING
      </span>
      {snmpShown && (
        <span
          title={snmpTitle}
          className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${snmpClass}`}
        >
          <Shield className="h-3 w-3" />
          SNMP
        </span>
      )}
    </div>
  )
}

function StatCard({
  label,
  value,
  tone,
  icon,
  active,
  onClick,
}: {
  label: string
  value: number
  tone?: 'success' | 'warning' | 'danger'
  icon?: React.ReactNode
  active?: boolean
  onClick?: () => void
}) {
  const color =
    tone === 'success'
      ? 'text-success'
      : tone === 'warning'
        ? 'text-warning'
        : tone === 'danger'
          ? 'text-danger'
          : 'text-text'
  return (
    <button
      type="button"
      onClick={onClick}
      className={`group flex items-center justify-between rounded-lg border p-3 text-left transition-colors ${
        active
          ? 'border-primary/50 bg-primary/5'
          : 'border-border bg-surface hover:border-border-strong hover:bg-surface2/50'
      }`}
    >
      <div>
        <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wider text-muted">
          {icon}
          {label}
        </div>
        <div className={`mt-0.5 text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
      </div>
    </button>
  )
}

function SortableTh({
  label,
  srLabel,
  col,
  current,
  order,
  onClick,
  className,
}: {
  label: string
  srLabel?: string
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
        {label || <span className="sr-only">{srLabel}</span>}
        {isActive ? (
          order === 'asc' ? (
            <ArrowUp className="h-3 w-3" />
          ) : (
            <ArrowDown className="h-3 w-3" />
          )
        ) : (
          <ArrowUpDown className="h-3 w-3 opacity-40" />
        )}
      </button>
    </Th>
  )
}

function FilterDropdown({
  label,
  icon,
  value,
  onChange,
  options,
}: {
  label: string
  icon?: React.ReactNode
  value: string
  onChange: (v: string) => void
  options: Array<{ value: string; label: string; color?: string }>
}) {
  const selected = options.find((o) => o.value === value)
  return (
    <div className="min-w-[140px]">
      <Select value={value || '__all__'} onValueChange={(v) => onChange(v === '__all__' ? '' : v)}>
        <SelectTrigger
          className={`h-8 text-xs ${value ? 'border-primary/40 bg-primary/5' : ''}`}
        >
          <span className="flex items-center gap-1.5 text-muted">
            {icon}
            <span className="text-text">
              {label}
              {selected && <span className="text-muted">: {selected.label}</span>}
            </span>
          </span>
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="__all__">
            <span className="text-muted">All {label.toLowerCase()}</span>
          </SelectItem>
          {options.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              <span className="flex items-center gap-2">
                {o.color && <GroupDot color={o.color} />}
                {o.label}
              </span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}

function GroupDot({ color }: { color?: string | null }) {
  return (
    <span
      className="h-2 w-2 shrink-0 rounded-full border border-black/20"
      style={{ backgroundColor: color || 'var(--color-muted)' }}
    />
  )
}

function Tag({ className }: { className?: string }) {
  // Minimal "tag" glyph as a hollow circle — used in the Group filter header.
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className={className}>
      <circle cx="12" cy="12" r="8" />
    </svg>
  )
}

function ColumnsDialog({
  open,
  onOpenChange,
  visible,
  onChange,
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
            <Columns3 className="h-5 w-5 text-primary" />
            Columns
          </DialogTitle>
          <DialogDescription>
            Toggle which columns appear in the table. Hostname, IP and actions stay pinned.
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
          <Button
            variant="outline"
            onClick={() => onChange({ ...DEFAULT_VISIBLE })}
          >
            Reset
          </Button>
          <Button onClick={() => onOpenChange(false)}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// -------------------------------------------------------------------------
// Bulk edit dialog (unchanged from prior turn — Group + SNMP inclusive)
// -------------------------------------------------------------------------

function BulkEditDialog({
  open,
  onOpenChange,
  count,
  loading,
  onSubmit,
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
                    <SelectItem value="__clear__">
                      <span className="text-muted">Remove from group</span>
                    </SelectItem>
                    {(groups || []).map((g) => (
                      <SelectItem key={g.id} value={g.id}>
                        <span className="flex items-center gap-2">
                          {g.color && <GroupDot color={g.color} />}
                          {g.name}
                        </span>
                      </SelectItem>
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
                  type="number"
                  min={10}
                  max={3600}
                  placeholder="e.g. 60"
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
              <FormField
                label="Saved credential"
                hint="Applies the credential's version, port, community/v3 to every selected device"
              >
                <Select value={credentialId} onValueChange={setCredentialId}>
                  <SelectTrigger>
                    <SelectValue placeholder="Select credential…" />
                  </SelectTrigger>
                  <SelectContent>
                    {(credentials || []).map((c) => (
                      <SelectItem key={c.id} value={c.id}>
                        <span className="flex items-center gap-2">
                          {c.name}
                          <span className="text-xs text-muted">
                            v{c.snmp_version} · port {c.port}
                          </span>
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
                    placeholder="public"
                    disabled={snmpVersion === '3'}
                  />
                </FormField>
                <FormField label="Port">
                  <Input
                    type="number"
                    min={1}
                    max={65535}
                    value={snmpPort}
                    onChange={(e) => setSnmpPort(e.target.value)}
                  />
                </FormField>
              </div>
            )}

            <FormField label="Poll interval (seconds)" hint="Blank = keep existing">
              <Input
                type="number"
                min={30}
                max={3600}
                placeholder="e.g. 60"
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
    <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">
      {children}
    </div>
  )
}

// -------------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------------

function extractSortVal(d: Device, k: SortKey): string | number | null {
  switch (k) {
    case 'hostname':
      return d.hostname.toLowerCase()
    case 'ip_address':
      // Sort IPs numerically by octet.
      return ipToInt(d.ip_address)
    case 'device_type':
      return d.device_type
    case 'vendor':
      return (d.vendor || '').toLowerCase()
    case 'location':
      return (d.location || '').toLowerCase()
    case 'group_name':
      return (d.group_name || '').toLowerCase()
    case 'status':
      return d.status
    case 'last_rtt_ms':
      return d.last_rtt_ms ?? Infinity
    case 'last_seen':
      return d.last_seen || ''
    default:
      return null
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

function titleCase(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}
