import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import { useDevices, useDeviceSummary, useDeviceGroups, useDeviceLocations } from '@/hooks/useDevices'
import { StatusIndicator } from '@/components/dashboard/StatusIndicator'
import { formatRTT, timeAgo, statusColors, statusLabels, cn } from '@/lib/utils'
import { api } from '@/lib/api'
import type { Device, DeviceStatus, DeviceSummary } from '@/types'
import {
  Monitor,
  Wifi,
  WifiOff,
  AlertTriangle,
  HelpCircle,
  Radio,
  Shield,
  Server,
  Printer,
  Search,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  X,
  Trash2,
  Check,
  ChevronsLeft,
  ChevronsRight,
  AlertCircle,
  Loader2,
  LayoutGrid,
} from 'lucide-react'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DEVICE_TYPE_ICONS: Record<string, React.ElementType> = {
  router: Radio,
  switch: Monitor,
  firewall: Shield,
  server: Server,
  access_point: Wifi,
  printer: Printer,
  other: HelpCircle,
}

const DEVICE_TYPE_LABELS: Record<string, string> = {
  router: 'Router',
  switch: 'Switch',
  firewall: 'Firewall',
  server: 'Server',
  access_point: 'Access Point',
  printer: 'Printer',
  other: 'Other',
}

const PAGE_SIZE = 25

// ---------------------------------------------------------------------------
// Inline FilterDropdown
// ---------------------------------------------------------------------------

function FilterDropdown({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: { value: string; label: string }[]
  onChange: (v: string) => void
}) {
  const [open, setOpen] = useState(false)

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={cn(
          'flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors',
          value
            ? 'border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--accent)]'
            : 'border-[var(--bg-tertiary)] bg-[var(--bg-secondary)] text-[var(--text-secondary)] hover:border-[var(--bg-elevated)] hover:text-[var(--text-primary)]'
        )}
      >
        {label}
        {value && (
          <span className="rounded bg-[var(--accent)]/20 px-1.5 py-0.5 text-xs font-medium">
            {options.find((o) => o.value === value)?.label ?? value}
          </span>
        )}
        <ChevronDown className="h-3.5 w-3.5" />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute left-0 top-full z-40 mt-1 min-w-[180px] overflow-hidden rounded-lg border border-[var(--bg-tertiary)] bg-[var(--bg-secondary)] shadow-xl shadow-black/30">
            <button
              onClick={() => {
                onChange('')
                setOpen(false)
              }}
              className={cn(
                'flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-[var(--bg-tertiary)]',
                !value ? 'text-[var(--accent)]' : 'text-[var(--text-secondary)]'
              )}
            >
              {!value && <Check className="h-3.5 w-3.5" />}
              <span className={cn(!value ? '' : 'ml-5')}>All</span>
            </button>
            {options.map((opt) => (
              <button
                key={opt.value}
                onClick={() => {
                  onChange(opt.value)
                  setOpen(false)
                }}
                className={cn(
                  'flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-[var(--bg-tertiary)]',
                  value === opt.value ? 'text-[var(--accent)]' : 'text-[var(--text-secondary)]'
                )}
              >
                {value === opt.value && <Check className="h-3.5 w-3.5" />}
                <span className={cn(value === opt.value ? '' : 'ml-5')}>{opt.label}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Delete Confirmation Dialog
// ---------------------------------------------------------------------------

function DeleteDialog({
  count,
  onConfirm,
  onCancel,
  isDeleting,
}: {
  count: number
  onConfirm: () => void
  onCancel: () => void
  isDeleting: boolean
}) {
  const [typed, setTyped] = useState('')

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-xl border border-[var(--bg-tertiary)] bg-[var(--bg-secondary)] p-6 shadow-2xl shadow-black/40">
        <div className="mb-4 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-red-500/15">
            <AlertCircle className="h-5 w-5 text-red-400" />
          </div>
          <div>
            <h3 className="text-lg font-semibold text-[var(--text-primary)]">Delete {count} device{count > 1 ? 's' : ''}?</h3>
            <p className="text-sm text-[var(--text-muted)]">This action cannot be undone.</p>
          </div>
        </div>

        <p className="mb-3 text-sm text-[var(--text-secondary)]">
          Type <span className="rounded bg-[var(--bg-tertiary)] px-1.5 py-0.5 font-mono text-red-400">delete</span> to confirm.
        </p>
        <input
          autoFocus
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          placeholder="Type delete..."
          className="mb-4 w-full rounded-lg border border-[var(--bg-tertiary)] bg-[var(--bg-primary)] px-3 py-2 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-red-500/50 focus:outline-none focus:ring-1 focus:ring-red-500/30"
        />

        <div className="flex justify-end gap-3">
          <button
            onClick={onCancel}
            disabled={isDeleting}
            className="rounded-lg border border-[var(--bg-tertiary)] px-4 py-2 text-sm text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={typed !== 'delete' || isDeleting}
            className="flex items-center gap-2 rounded-lg bg-red-500 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-red-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {isDeleting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
            Delete
          </button>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export function DevicesPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  // Filters
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [typeFilter, setTypeFilter] = useState<string>('')
  const [groupFilter, setGroupFilter] = useState<string>('')
  const [locationFilter, setLocationFilter] = useState<string>('')
  const [page, setPage] = useState(0)

  // Selection
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [showDelete, setShowDelete] = useState(false)

  // Data
  const { data: summaryData } = useDeviceSummary()
  const { data: groupsData } = useDeviceGroups()
  const { data: locationsData } = useDeviceLocations()

  const summary: DeviceSummary = summaryData ?? { total: 0, up: 0, down: 0, degraded: 0, unknown: 0, maintenance: 0 }
  const groups = groupsData ?? []
  const locations = locationsData ?? []

  const { data: devicesResponse, isLoading } = useDevices({
    status: (statusFilter as DeviceStatus) || undefined,
    group_id: groupFilter || undefined,
    device_type: typeFilter || undefined,
    location: locationFilter || undefined,
    search: search || undefined,
    skip: page * PAGE_SIZE,
    limit: PAGE_SIZE,
  })

  const devices: Device[] = devicesResponse?.data ?? []
  const totalDevices = devicesResponse?.meta?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(totalDevices / PAGE_SIZE))

  // All-devices fetch for heatmap (lightweight — uses summary total as limit)
  const { data: allDevicesResponse } = useDevices({ limit: summary.total || 200, skip: 0 })
  const allDevices: Device[] = allDevicesResponse?.data ?? []

  // Bulk delete mutation
  const bulkDelete = useMutation({
    mutationFn: (ids: string[]) => api.post('/devices/bulk-delete', { device_ids: ids }),
    onSuccess: () => {
      queryClient.invalidateQueries()
      setSelected(new Set())
      setShowDelete(false)
    },
  })

  // Active filters
  const activeFilters = useMemo(() => {
    const filters: { key: string; label: string; value: string; clear: () => void }[] = []
    if (statusFilter)
      filters.push({ key: 'status', label: 'Status', value: statusLabels[statusFilter as DeviceStatus] ?? statusFilter, clear: () => setStatusFilter('') })
    if (typeFilter)
      filters.push({ key: 'type', label: 'Type', value: DEVICE_TYPE_LABELS[typeFilter] ?? typeFilter, clear: () => setTypeFilter('') })
    if (groupFilter) {
      const g = groups.find((gr) => String(gr.id) === groupFilter)
      filters.push({ key: 'group', label: 'Group', value: g?.name ?? groupFilter, clear: () => setGroupFilter('') })
    }
    if (locationFilter)
      filters.push({ key: 'location', label: 'Location', value: locationFilter, clear: () => setLocationFilter('') })
    return filters
  }, [statusFilter, typeFilter, groupFilter, locationFilter, groups])

  // Selection helpers
  const allOnPageSelected = devices.length > 0 && devices.every((d) => selected.has(String(d.id)))

  function toggleAll() {
    if (allOnPageSelected) {
      const next = new Set(selected)
      devices.forEach((d) => next.delete(String(d.id)))
      setSelected(next)
    } else {
      const next = new Set(selected)
      devices.forEach((d) => next.add(String(d.id)))
      setSelected(next)
    }
  }

  function toggleOne(id: string) {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelected(next)
  }

  // Percentage helper
  function pct(n: number) {
    return summary.total > 0 ? ((n / summary.total) * 100).toFixed(1) : '0.0'
  }

  // -----------------------------------------------------------------------
  // Summary cards config
  // -----------------------------------------------------------------------
  const summaryCards: { label: string; count: number; color: string; icon: React.ElementType; statusKey?: DeviceStatus }[] = [
    { label: 'Total Devices', count: summary.total, color: '#6366F1', icon: Monitor },
    { label: 'Online', count: summary.up, color: statusColors.up, icon: Wifi, statusKey: 'up' },
    { label: 'Offline', count: summary.down, color: statusColors.down, icon: WifiOff, statusKey: 'down' },
    { label: 'Degraded', count: summary.degraded, color: statusColors.degraded, icon: AlertTriangle, statusKey: 'degraded' },
    { label: 'Unknown', count: summary.unknown, color: statusColors.unknown, icon: HelpCircle, statusKey: 'unknown' },
  ]

  // -----------------------------------------------------------------------
  // ECharts donut config
  // -----------------------------------------------------------------------
  const ringOption = useMemo(
    () => ({
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'item' as const,
        backgroundColor: '#1A1D27',
        borderColor: '#242832',
        textStyle: { color: '#E8EAED', fontSize: 13 },
        formatter: (params: { name: string; value: number; percent: number }) =>
          `<div style="font-weight:600">${params.name}</div><div style="color:#9BA1B0">${params.value} devices &middot; ${params.percent}%</div>`,
      },
      legend: {
        bottom: 0,
        textStyle: { color: '#9BA1B0', fontSize: 12 },
        itemWidth: 10,
        itemHeight: 10,
        itemGap: 16,
        icon: 'circle',
      },
      series: [
        {
          type: 'pie',
          radius: ['58%', '80%'],
          center: ['50%', '45%'],
          avoidLabelOverlap: false,
          padAngle: 2,
          itemStyle: { borderRadius: 6 },
          label: {
            show: true,
            position: 'center' as const,
            formatter: () => `{total|${summary.total}}\n{label|Devices}`,
            rich: {
              total: { fontSize: 32, fontWeight: 700, color: '#E8EAED', lineHeight: 40 },
              label: { fontSize: 13, color: '#9BA1B0', lineHeight: 20 },
            },
          },
          emphasis: {
            label: { show: true },
            scaleSize: 6,
          },
          data: [
            { value: summary.up, name: 'Online', itemStyle: { color: statusColors.up } },
            { value: summary.down, name: 'Offline', itemStyle: { color: statusColors.down } },
            { value: summary.degraded, name: 'Degraded', itemStyle: { color: statusColors.degraded } },
            { value: summary.unknown, name: 'Unknown', itemStyle: { color: statusColors.unknown } },
            { value: summary.maintenance, name: 'Maintenance', itemStyle: { color: statusColors.maintenance } },
          ].filter((d) => d.value > 0),
        },
      ],
    }),
    [summary]
  )

  // -----------------------------------------------------------------------
  // Filter options
  // -----------------------------------------------------------------------
  const statusOptions = (['up', 'down', 'degraded', 'unknown', 'maintenance'] as DeviceStatus[]).map((s) => ({
    value: s,
    label: statusLabels[s],
  }))

  const typeOptions = Object.entries(DEVICE_TYPE_LABELS).map(([v, l]) => ({ value: v, label: l }))

  const groupOptions = groups.map((g) => ({ value: String(g.id), label: g.name }))

  const locationOptions = locations.map((l) => ({ value: l, label: l }))

  // -----------------------------------------------------------------------
  // Pagination helpers
  // -----------------------------------------------------------------------
  function pageNumbers() {
    const pages: (number | 'ellipsis')[] = []
    const maxVisible = 7
    if (totalPages <= maxVisible) {
      for (let i = 0; i < totalPages; i++) pages.push(i)
    } else {
      pages.push(0)
      if (page > 3) pages.push('ellipsis')
      const start = Math.max(1, page - 1)
      const end = Math.min(totalPages - 2, page + 1)
      for (let i = start; i <= end; i++) pages.push(i)
      if (page < totalPages - 4) pages.push('ellipsis')
      pages.push(totalPages - 1)
    }
    return pages
  }

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------
  return (
    <div className="min-h-screen w-full bg-[var(--bg-primary)] px-6 py-6">
      {/* Page header */}
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-[var(--text-primary)]">Devices</h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            Monitoring {summary.total} device{summary.total !== 1 ? 's' : ''} across your network
          </p>
        </div>
        <div className="flex items-center gap-2">
          <LayoutGrid className="h-5 w-5 text-[var(--text-muted)]" />
          <span className="text-sm text-[var(--text-muted)]">ZenPlus Network Monitor</span>
        </div>
      </div>

      {/* ================================================================== */}
      {/* 1. TOP VISUAL SECTION                                              */}
      {/* ================================================================== */}
      <div className="mb-6 overflow-hidden rounded-xl border border-[var(--bg-tertiary)] bg-[var(--bg-secondary)]">
        <div className="grid grid-cols-1 lg:grid-cols-5">
          {/* Left: Summary Cards (3 cols) */}
          <div className="col-span-1 grid grid-cols-2 gap-px border-r border-[var(--bg-tertiary)] p-4 sm:grid-cols-3 lg:col-span-3">
            {summaryCards.map((card) => {
              const Icon = card.icon
              return (
                <div
                  key={card.label}
                  className="group relative overflow-hidden rounded-lg border border-[var(--bg-tertiary)]/60 bg-[var(--bg-primary)]/50 p-4 transition-colors hover:border-[var(--bg-elevated)] hover:bg-[var(--bg-primary)]"
                >
                  {/* Colored left bar */}
                  <div className="absolute inset-y-0 left-0 w-1 rounded-l-lg" style={{ backgroundColor: card.color }} />

                  <div className="flex items-start justify-between pl-2">
                    <div>
                      <div className="mb-1 flex items-center gap-2">
                        <div
                          className="flex h-8 w-8 items-center justify-center rounded-lg"
                          style={{ backgroundColor: `${card.color}15` }}
                        >
                          <Icon className="h-4 w-4" style={{ color: card.color }} />
                        </div>
                      </div>
                      <p className="mt-2 text-xs font-medium uppercase tracking-wider text-[var(--text-muted)]">{card.label}</p>
                      <p className="mt-1 text-3xl font-bold tabular-nums text-[var(--text-primary)]">{card.count}</p>
                      {card.statusKey && (
                        <p className="mt-1 text-xs text-[var(--text-muted)]">{pct(card.count)}% of total</p>
                      )}
                      {!card.statusKey && (
                        <p className="mt-1 text-xs text-[var(--text-muted)]">All monitored</p>
                      )}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>

          {/* Right: Ring Chart (2 cols) */}
          <div className="col-span-1 flex items-center justify-center p-4 lg:col-span-2">
            <ReactECharts option={ringOption} style={{ width: '100%', height: 300 }} opts={{ renderer: 'canvas' }} />
          </div>
        </div>
      </div>

      {/* ================================================================== */}
      {/* 2. DEVICE HEATMAP STRIP                                            */}
      {/* ================================================================== */}
      {allDevices.length > 0 && (
        <div className="mb-6 overflow-hidden rounded-xl border border-[var(--bg-tertiary)] bg-[var(--bg-secondary)] p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-[var(--text-secondary)]">Device Status Map</h2>
            <div className="flex items-center gap-3 text-xs text-[var(--text-muted)]">
              {(['up', 'down', 'degraded', 'unknown', 'maintenance'] as DeviceStatus[]).map((s) => (
                <span key={s} className="flex items-center gap-1.5">
                  <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: statusColors[s] }} />
                  {statusLabels[s]}
                </span>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap gap-[3px]">
            {allDevices.map((d) => (
              <button
                key={d.id}
                title={`${d.hostname} — ${statusLabels[d.status]}`}
                onClick={() => navigate(`/devices/${d.id}`)}
                className="h-[18px] w-[18px] rounded-[3px] transition-all hover:scale-150 hover:shadow-lg focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
                style={{ backgroundColor: statusColors[d.status] }}
              />
            ))}
          </div>
        </div>
      )}

      {/* ================================================================== */}
      {/* 3. FILTER BAR                                                      */}
      {/* ================================================================== */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        {/* Search */}
        <div className="relative min-w-[260px] flex-1 lg:max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setPage(0)
            }}
            placeholder="Search hostname, IP, description..."
            className="w-full rounded-lg border border-[var(--bg-tertiary)] bg-[var(--bg-secondary)] py-2 pl-9 pr-3 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-[var(--accent)]/50 focus:outline-none focus:ring-1 focus:ring-[var(--accent)]/30"
          />
          {search && (
            <button onClick={() => setSearch('')} className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--text-muted)] hover:text-[var(--text-secondary)]">
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>

        {/* Dropdowns */}
        <FilterDropdown label="Status" value={statusFilter} options={statusOptions} onChange={(v) => { setStatusFilter(v); setPage(0) }} />
        <FilterDropdown label="Type" value={typeFilter} options={typeOptions} onChange={(v) => { setTypeFilter(v); setPage(0) }} />
        <FilterDropdown label="Group" value={groupFilter} options={groupOptions} onChange={(v) => { setGroupFilter(v); setPage(0) }} />
        <FilterDropdown label="Location" value={locationFilter} options={locationOptions} onChange={(v) => { setLocationFilter(v); setPage(0) }} />

        {activeFilters.length > 0 && (
          <button
            onClick={() => {
              setStatusFilter('')
              setTypeFilter('')
              setGroupFilter('')
              setLocationFilter('')
              setPage(0)
            }}
            className="text-xs text-[var(--text-muted)] transition-colors hover:text-[var(--accent)]"
          >
            Clear all
          </button>
        )}
      </div>

      {/* Active filter pills */}
      {activeFilters.length > 0 && (
        <div className="mb-4 flex flex-wrap gap-2">
          {activeFilters.map((f) => (
            <span
              key={f.key}
              className="inline-flex items-center gap-1.5 rounded-full border border-[var(--accent)]/30 bg-[var(--accent)]/10 px-3 py-1 text-xs font-medium text-[var(--accent)]"
            >
              {f.label}: {f.value}
              <button onClick={f.clear} className="ml-0.5 rounded-full p-0.5 transition-colors hover:bg-[var(--accent)]/20">
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      )}

      {/* ================================================================== */}
      {/* 4. SELECTION TOOLBAR                                               */}
      {/* ================================================================== */}
      {selected.size > 0 && (
        <div className="mb-4 flex items-center gap-4 rounded-lg border border-[var(--accent)]/30 bg-[var(--accent)]/5 px-4 py-2.5">
          <span className="text-sm font-medium text-[var(--accent)]">
            {selected.size} device{selected.size > 1 ? 's' : ''} selected
          </span>
          <div className="h-4 w-px bg-[var(--bg-tertiary)]" />
          <button
            onClick={() => setShowDelete(true)}
            className="flex items-center gap-1.5 rounded-lg bg-red-500/10 px-3 py-1.5 text-sm font-medium text-red-400 transition-colors hover:bg-red-500/20"
          >
            <Trash2 className="h-3.5 w-3.5" />
            Delete
          </button>
          <button
            onClick={() => setSelected(new Set())}
            className="ml-auto text-xs text-[var(--text-muted)] transition-colors hover:text-[var(--text-secondary)]"
          >
            Clear selection
          </button>
        </div>
      )}

      {/* ================================================================== */}
      {/* 5. DEVICE TABLE                                                    */}
      {/* ================================================================== */}
      <div className="overflow-hidden rounded-xl border border-[var(--bg-tertiary)] bg-[var(--bg-secondary)]">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-[var(--bg-tertiary)] bg-[var(--bg-primary)]/50 text-left text-xs font-semibold uppercase tracking-wider text-[var(--text-muted)]">
                <th className="w-12 px-4 py-3">
                  <input
                    type="checkbox"
                    checked={allOnPageSelected}
                    onChange={toggleAll}
                    className="h-4 w-4 cursor-pointer rounded border-[var(--bg-tertiary)] bg-[var(--bg-tertiary)] accent-[var(--accent)]"
                  />
                </th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Hostname</th>
                <th className="px-4 py-3">IP Address</th>
                <th className="px-4 py-3">Type</th>
                <th className="px-4 py-3">Group</th>
                <th className="px-4 py-3">Location</th>
                <th className="px-4 py-3 text-right">RTT</th>
                <th className="px-4 py-3 text-right">Last Seen</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--bg-tertiary)]/60">
              {isLoading
                ? Array.from({ length: 8 }).map((_, i) => (
                    <tr key={i}>
                      {Array.from({ length: 9 }).map((_, j) => (
                        <td key={j} className="px-4 py-3.5">
                          <div className="h-4 w-20 animate-pulse rounded bg-[var(--bg-tertiary)]" />
                        </td>
                      ))}
                    </tr>
                  ))
                : devices.map((device) => {
                    const DeviceIcon = DEVICE_TYPE_ICONS[device.device_type] ?? HelpCircle
                    const isSelected = selected.has(String(device.id))

                    return (
                      <tr
                        key={device.id}
                        onClick={() => navigate(`/devices/${device.id}`)}
                        className={cn(
                          'group cursor-pointer transition-colors',
                          isSelected
                            ? 'bg-[var(--accent)]/5'
                            : 'hover:bg-[var(--bg-primary)]/60'
                        )}
                      >
                        {/* Checkbox */}
                        <td className="w-12 px-4 py-3.5" onClick={(e) => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={isSelected}
                            onChange={() => toggleOne(String(device.id))}
                            className="h-4 w-4 cursor-pointer rounded border-[var(--bg-tertiary)] bg-[var(--bg-tertiary)] accent-[var(--accent)]"
                          />
                        </td>

                        {/* Status */}
                        <td className="px-4 py-3.5">
                          <span className="flex items-center gap-2">
                            <span
                              className="relative flex h-2.5 w-2.5"
                            >
                              {device.status === 'up' && (
                                <span
                                  className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-40"
                                  style={{ backgroundColor: statusColors[device.status] }}
                                />
                              )}
                              <span
                                className="relative inline-flex h-2.5 w-2.5 rounded-full"
                                style={{ backgroundColor: statusColors[device.status] }}
                              />
                            </span>
                            <span className="text-sm" style={{ color: statusColors[device.status] }}>
                              {statusLabels[device.status]}
                            </span>
                          </span>
                        </td>

                        {/* Hostname */}
                        <td className="px-4 py-3.5">
                          <span className="text-sm font-semibold text-[var(--text-primary)] group-hover:text-[var(--accent)]">
                            {device.hostname}
                          </span>
                          {device.description && (
                            <p className="mt-0.5 max-w-xs truncate text-xs text-[var(--text-muted)]">{device.description}</p>
                          )}
                        </td>

                        {/* IP */}
                        <td className="px-4 py-3.5">
                          <span className="font-mono text-sm text-[var(--text-secondary)]">{device.ip_address}</span>
                        </td>

                        {/* Type */}
                        <td className="px-4 py-3.5">
                          <span className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
                            <DeviceIcon className="h-4 w-4 text-[var(--text-muted)]" />
                            {DEVICE_TYPE_LABELS[device.device_type] ?? device.device_type}
                          </span>
                        </td>

                        {/* Group */}
                        <td className="px-4 py-3.5">
                          {device.group_name ? (
                            <span className="inline-flex rounded-full border border-[var(--bg-tertiary)] bg-[var(--bg-primary)] px-2.5 py-0.5 text-xs font-medium text-[var(--text-secondary)]">
                              {device.group_name}
                            </span>
                          ) : (
                            <span className="text-xs text-[var(--text-muted)]">&mdash;</span>
                          )}
                        </td>

                        {/* Location */}
                        <td className="px-4 py-3.5">
                          <span className="text-sm text-[var(--text-secondary)]">{device.location || '\u2014'}</span>
                        </td>

                        {/* RTT */}
                        <td className="px-4 py-3.5 text-right">
                          {device.last_rtt_ms != null ? (
                            <span
                              className={cn(
                                'font-mono text-sm',
                                device.last_rtt_ms < 50
                                  ? 'text-green-400'
                                  : device.last_rtt_ms < 150
                                    ? 'text-yellow-400'
                                    : 'text-red-400'
                              )}
                            >
                              {formatRTT(device.last_rtt_ms)}
                            </span>
                          ) : (
                            <span className="text-xs text-[var(--text-muted)]">&mdash;</span>
                          )}
                        </td>

                        {/* Last Seen */}
                        <td className="px-4 py-3.5 text-right">
                          <span className="text-sm text-[var(--text-secondary)]">
                            {device.last_seen ? timeAgo(device.last_seen) : '\u2014'}
                          </span>
                        </td>
                      </tr>
                    )
                  })}
            </tbody>
          </table>

          {/* Empty state */}
          {!isLoading && devices.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16 text-center">
              <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-[var(--bg-tertiary)]">
                <Monitor className="h-7 w-7 text-[var(--text-muted)]" />
              </div>
              <p className="text-sm font-medium text-[var(--text-secondary)]">No devices found</p>
              <p className="mt-1 text-xs text-[var(--text-muted)]">
                {activeFilters.length > 0 || search ? 'Try adjusting your filters or search query.' : 'Add devices to start monitoring.'}
              </p>
            </div>
          )}
        </div>

        {/* ================================================================ */}
        {/* 6. PAGINATION                                                    */}
        {/* ================================================================ */}
        {totalPages > 1 && (
          <div className="flex items-center justify-between border-t border-[var(--bg-tertiary)] px-4 py-3">
            <p className="text-xs text-[var(--text-muted)]">
              Showing {page * PAGE_SIZE + 1}&ndash;{Math.min((page + 1) * PAGE_SIZE, totalDevices)} of {totalDevices}
            </p>

            <div className="flex items-center gap-1">
              {/* First */}
              <button
                disabled={page === 0}
                onClick={() => setPage(0)}
                className="rounded-lg p-1.5 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-secondary)] disabled:opacity-30"
              >
                <ChevronsLeft className="h-4 w-4" />
              </button>
              {/* Prev */}
              <button
                disabled={page === 0}
                onClick={() => setPage((p) => p - 1)}
                className="rounded-lg p-1.5 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-secondary)] disabled:opacity-30"
              >
                <ChevronLeft className="h-4 w-4" />
              </button>

              {/* Page numbers */}
              {pageNumbers().map((pn, idx) =>
                pn === 'ellipsis' ? (
                  <span key={`e-${idx}`} className="px-1 text-xs text-[var(--text-muted)]">
                    ...
                  </span>
                ) : (
                  <button
                    key={pn}
                    onClick={() => setPage(pn)}
                    className={cn(
                      'min-w-[32px] rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors',
                      pn === page
                        ? 'bg-[var(--accent)] text-white'
                        : 'text-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-secondary)]'
                    )}
                  >
                    {pn + 1}
                  </button>
                )
              )}

              {/* Next */}
              <button
                disabled={page >= totalPages - 1}
                onClick={() => setPage((p) => p + 1)}
                className="rounded-lg p-1.5 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-secondary)] disabled:opacity-30"
              >
                <ChevronRight className="h-4 w-4" />
              </button>
              {/* Last */}
              <button
                disabled={page >= totalPages - 1}
                onClick={() => setPage(totalPages - 1)}
                className="rounded-lg p-1.5 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-secondary)] disabled:opacity-30"
              >
                <ChevronsRight className="h-4 w-4" />
              </button>
            </div>
          </div>
        )}
      </div>

      {/* ================================================================== */}
      {/* 7. DELETE DIALOG                                                   */}
      {/* ================================================================== */}
      {showDelete && (
        <DeleteDialog
          count={selected.size}
          isDeleting={bulkDelete.isPending}
          onCancel={() => setShowDelete(false)}
          onConfirm={() => bulkDelete.mutate(Array.from(selected))}
        />
      )}
    </div>
  )
}
