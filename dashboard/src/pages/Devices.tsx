import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useDevices, useDeviceGroups, useDeviceLocations } from '@/hooks/useDevices'
import { StatusIndicator } from '@/components/dashboard/StatusIndicator'
import { formatRTT, timeAgo, statusColors, cn } from '@/lib/utils'
import { api } from '@/lib/api'
import {
  Plus, Search, Upload, Filter, X, Trash2,
  AlertTriangle, ChevronDown, CheckSquare, Square, MinusSquare,
  Monitor, Server, Shield, Wifi, Radio, Printer, HelpCircle,
} from 'lucide-react'
import type { Device, DeviceStatus } from '@/types'

const statusOptions = [
  { value: 'up', label: 'Online', color: '#22C55E' },
  { value: 'down', label: 'Offline', color: '#EF4444' },
  { value: 'degraded', label: 'Degraded', color: '#EAB308' },
  { value: 'unknown', label: 'Unknown', color: '#6B7280' },
  { value: 'maintenance', label: 'Maintenance', color: '#3B82F6' },
]

const deviceTypeOptions = [
  { value: 'router', label: 'Router', icon: Radio },
  { value: 'switch', label: 'Switch', icon: Monitor },
  { value: 'firewall', label: 'Firewall', icon: Shield },
  { value: 'server', label: 'Server', icon: Server },
  { value: 'access_point', label: 'Access Point', icon: Wifi },
  { value: 'printer', label: 'Printer', icon: Printer },
  { value: 'other', label: 'Other', icon: HelpCircle },
]

// ─── Filter Dropdown ───
function FilterDropdown({ label, value, options, onChange, icon }: {
  label: string
  value: string
  options: { value: string; label: string; color?: string; count?: number }[]
  onChange: (v: string) => void
  icon?: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  const selected = options.find(o => o.value === value)

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={cn(
          'flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-medium border transition-all',
          value
            ? 'bg-[var(--accent)]/10 border-[var(--accent)]/40 text-[var(--accent)]'
            : 'bg-[var(--bg-tertiary)] border-[var(--bg-elevated)] text-[var(--text-muted)] hover:text-[var(--text-secondary)]'
        )}
      >
        {icon}
        <span>{value ? (selected?.label || value) : label}</span>
        {value && (
          <button
            onClick={(e) => { e.stopPropagation(); onChange(''); setOpen(false) }}
            className="ml-0.5 hover:text-[var(--text-primary)]"
          >
            <X className="w-3 h-3" />
          </button>
        )}
        {!value && <ChevronDown className="w-3 h-3" />}
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute top-full left-0 mt-1 z-40 bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] rounded-xl shadow-2xl py-1 min-w-[180px] max-h-64 overflow-y-auto">
            <button
              onClick={() => { onChange(''); setOpen(false) }}
              className={cn(
                'w-full text-left px-3 py-2 text-xs hover:bg-[var(--bg-tertiary)] transition-colors',
                !value ? 'text-[var(--accent)]' : 'text-[var(--text-muted)]'
              )}
            >
              All {label}s
            </button>
            {options.map(opt => (
              <button
                key={opt.value}
                onClick={() => { onChange(opt.value); setOpen(false) }}
                className={cn(
                  'w-full text-left px-3 py-2 text-xs hover:bg-[var(--bg-tertiary)] transition-colors flex items-center gap-2',
                  value === opt.value ? 'text-[var(--accent)] bg-[var(--accent)]/5' : 'text-[var(--text-secondary)]'
                )}
              >
                {opt.color && <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: opt.color }} />}
                <span className="flex-1">{opt.label}</span>
                {opt.count !== undefined && (
                  <span className="text-[var(--text-muted)] text-[10px]">{opt.count}</span>
                )}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

// ─── Delete Confirmation Dialog ───
function DeleteDialog({ count, onConfirm, onCancel }: {
  count: number
  onConfirm: () => void
  onCancel: () => void
}) {
  const [confirmText, setConfirmText] = useState('')
  const confirmed = confirmText.toLowerCase() === 'delete'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onCancel} />

      {/* Dialog */}
      <div className="relative bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] rounded-2xl shadow-2xl w-full max-w-md p-6">
        {/* Warning Icon */}
        <div className="flex justify-center mb-4">
          <div className="w-16 h-16 rounded-2xl bg-red-500/10 flex items-center justify-center">
            <AlertTriangle className="w-8 h-8 text-red-400" />
          </div>
        </div>

        {/* Title */}
        <h3 className="text-lg font-semibold text-[var(--text-primary)] text-center mb-2">
          Delete {count} Device{count > 1 ? 's' : ''}?
        </h3>

        {/* Warning Message */}
        <div className="bg-red-500/5 border border-red-500/20 rounded-xl p-4 mb-4">
          <p className="text-sm text-red-400 leading-relaxed text-center">
            This action is <span className="font-bold">permanent and cannot be undone</span>.
            All monitoring data, metrics history, and alert records associated
            with {count > 1 ? 'these devices' : 'this device'} will be lost.
          </p>
        </div>

        {/* Confirmation Input */}
        <div className="mb-4">
          <label className="block text-xs text-[var(--text-muted)] mb-2 text-center">
            Type <span className="font-mono font-bold text-red-400 bg-red-500/10 px-1.5 py-0.5 rounded">delete</span> to confirm
          </label>
          <input
            type="text"
            value={confirmText}
            onChange={e => setConfirmText(e.target.value)}
            placeholder="Type 'delete' here..."
            autoFocus
            className="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-4 py-3 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-red-500/50 text-sm text-center font-mono placeholder:text-[var(--text-muted)]/40"
          />
        </div>

        {/* Actions */}
        <div className="flex gap-3">
          <button
            onClick={onCancel}
            className="flex-1 py-2.5 rounded-lg text-sm font-medium text-[var(--text-secondary)] bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={!confirmed}
            className={cn(
              'flex-1 py-2.5 rounded-lg text-sm font-semibold transition-all flex items-center justify-center gap-2',
              confirmed
                ? 'bg-red-500 text-white hover:bg-red-600 cursor-pointer'
                : 'bg-red-500/20 text-red-400/40 cursor-not-allowed'
            )}
          >
            <Trash2 className="w-4 h-4" />
            Delete {count} Device{count > 1 ? 's' : ''}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Active Filter Pills ───
function ActiveFilters({ filters, onClear, onClearAll }: {
  filters: { key: string; label: string; value: string }[]
  onClear: (key: string) => void
  onClearAll: () => void
}) {
  if (filters.length === 0) return null

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] font-semibold">Active:</span>
      {filters.map(f => (
        <span
          key={f.key}
          className="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-[var(--accent)]/10 text-[var(--accent)] text-[11px] font-medium"
        >
          {f.label}: {f.value}
          <button onClick={() => onClear(f.key)} className="hover:text-white"><X className="w-3 h-3" /></button>
        </span>
      ))}
      <button onClick={onClearAll} className="text-[11px] text-[var(--text-muted)] hover:text-red-400 transition-colors ml-1">
        Clear all
      </button>
    </div>
  )
}


// ─── Main Page ───
export function DevicesPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [typeFilter, setTypeFilter] = useState('')
  const [groupFilter, setGroupFilter] = useState('')
  const [locationFilter, setLocationFilter] = useState('')
  const [page, setPage] = useState(0)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [showDeleteDialog, setShowDeleteDialog] = useState(false)
  const limit = 50

  const { data: groupsData } = useDeviceGroups()
  const { data: locationsData } = useDeviceLocations()

  const { data, isLoading } = useDevices({
    status: statusFilter || undefined,
    group_id: groupFilter || undefined,
    device_type: typeFilter || undefined,
    location: locationFilter || undefined,
    search: search || undefined,
    skip: page * limit,
    limit,
  })

  const devices = data?.data || []
  const total = data?.meta?.total || 0

  const groups = groupsData || []
  const locations = locationsData || []

  // Selection helpers
  const allOnPageSelected = devices.length > 0 && devices.every(d => selectedIds.has(d.id))
  const someOnPageSelected = devices.some(d => selectedIds.has(d.id))

  const toggleAll = () => {
    if (allOnPageSelected) {
      const next = new Set(selectedIds)
      devices.forEach(d => next.delete(d.id))
      setSelectedIds(next)
    } else {
      const next = new Set(selectedIds)
      devices.forEach(d => next.add(d.id))
      setSelectedIds(next)
    }
  }

  const toggleOne = (id: string) => {
    const next = new Set(selectedIds)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelectedIds(next)
  }

  const resetPage = () => setPage(0)

  // Bulk delete
  const deleteMutation = useMutation({
    mutationFn: (ids: string[]) => api.post<{ deleted: number }>('/devices/bulk-delete', { device_ids: ids }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['devices'] })
      queryClient.invalidateQueries({ queryKey: ['device-summary'] })
      queryClient.invalidateQueries({ queryKey: ['device-groups'] })
      setSelectedIds(new Set())
      setShowDeleteDialog(false)
    },
  })

  // Active filters for pills
  const activeFilters = useMemo(() => {
    const f: { key: string; label: string; value: string }[] = []
    if (statusFilter) f.push({ key: 'status', label: 'Status', value: statusOptions.find(s => s.value === statusFilter)?.label || statusFilter })
    if (typeFilter) f.push({ key: 'type', label: 'Type', value: deviceTypeOptions.find(t => t.value === typeFilter)?.label || typeFilter })
    if (groupFilter) f.push({ key: 'group', label: 'Group', value: groups.find(g => g.id === groupFilter)?.name || groupFilter })
    if (locationFilter) f.push({ key: 'location', label: 'Location', value: locationFilter })
    return f
  }, [statusFilter, typeFilter, groupFilter, locationFilter, groups])

  const clearFilter = (key: string) => {
    if (key === 'status') setStatusFilter('')
    if (key === 'type') setTypeFilter('')
    if (key === 'group') setGroupFilter('')
    if (key === 'location') setLocationFilter('')
    resetPage()
  }

  const clearAllFilters = () => {
    setStatusFilter('')
    setTypeFilter('')
    setGroupFilter('')
    setLocationFilter('')
    setSearch('')
    resetPage()
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <h2 className="text-xl font-semibold text-[var(--text-primary)]">Devices</h2>
          <p className="text-xs text-[var(--text-muted)] mt-0.5">{total} device{total !== 1 ? 's' : ''} total</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => navigate('/devices/new')}
            className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
          >
            <Upload className="w-4 h-4" />
            Import
          </button>
          <button
            onClick={() => navigate('/devices/new')}
            className="flex items-center gap-2 bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors"
          >
            <Plus className="w-4 h-4" />
            Add Device
          </button>
        </div>
      </div>

      {/* Search + Filters Row */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-4 mb-4 space-y-3">
        <div className="flex items-center gap-3 flex-wrap">
          {/* Search */}
          <div className="relative flex-1 min-w-[200px] max-w-sm">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--text-muted)]" />
            <input
              type="text"
              placeholder="Search hostname or IP..."
              value={search}
              onChange={(e) => { setSearch(e.target.value); resetPage() }}
              className="w-full bg-[var(--bg-tertiary)] text-[var(--text-secondary)] pl-10 pr-4 py-2 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm"
            />
            {search && (
              <button onClick={() => { setSearch(''); resetPage() }} className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--text-muted)] hover:text-[var(--text-primary)]">
                <X className="w-3.5 h-3.5" />
              </button>
            )}
          </div>

          <div className="h-6 w-px bg-[var(--bg-elevated)]" />

          {/* Filter Dropdowns */}
          <FilterDropdown
            label="Status"
            value={statusFilter}
            onChange={(v) => { setStatusFilter(v); resetPage() }}
            options={statusOptions.map(s => ({ ...s }))}
          />
          <FilterDropdown
            label="Type"
            value={typeFilter}
            onChange={(v) => { setTypeFilter(v); resetPage() }}
            options={deviceTypeOptions.map(t => ({ value: t.value, label: t.label }))}
          />
          <FilterDropdown
            label="Group"
            value={groupFilter}
            onChange={(v) => { setGroupFilter(v); resetPage() }}
            options={groups.map(g => ({ value: g.id, label: g.name, count: g.device_count }))}
          />
          <FilterDropdown
            label="Location"
            value={locationFilter}
            onChange={(v) => { setLocationFilter(v); resetPage() }}
            options={locations.map(l => ({ value: l, label: l }))}
          />
        </div>

        {/* Active filter pills */}
        <ActiveFilters filters={activeFilters} onClear={clearFilter} onClearAll={clearAllFilters} />
      </div>

      {/* Selection toolbar */}
      {selectedIds.size > 0 && (
        <div className="flex items-center gap-3 mb-3 px-4 py-2.5 bg-[var(--accent)]/5 border border-[var(--accent)]/20 rounded-xl">
          <span className="text-sm font-medium text-[var(--accent)]">
            {selectedIds.size} device{selectedIds.size > 1 ? 's' : ''} selected
          </span>
          <div className="flex-1" />
          <button
            onClick={() => setSelectedIds(new Set())}
            className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors"
          >
            Clear selection
          </button>
          <button
            onClick={() => setShowDeleteDialog(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 text-xs font-medium hover:bg-red-500/20 transition-colors"
          >
            <Trash2 className="w-3.5 h-3.5" />
            Delete Selected
          </button>
        </div>
      )}

      {/* Table */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-[var(--bg-elevated)]">
              <th className="w-10 px-3 py-3">
                <button onClick={toggleAll} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors">
                  {allOnPageSelected
                    ? <CheckSquare className="w-4 h-4 text-[var(--accent)]" />
                    : someOnPageSelected
                      ? <MinusSquare className="w-4 h-4 text-[var(--accent)]" />
                      : <Square className="w-4 h-4" />}
                </button>
              </th>
              <th className="text-left px-3 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">Status</th>
              <th className="text-left px-3 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">Hostname</th>
              <th className="text-left px-3 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">IP Address</th>
              <th className="text-left px-3 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">Type</th>
              <th className="text-left px-3 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">Group</th>
              <th className="text-left px-3 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">Location</th>
              <th className="text-right px-3 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">RTT</th>
              <th className="text-right px-3 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">Last Seen</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td colSpan={9} className="text-center py-16 text-[var(--text-muted)]">
                  <div className="flex flex-col items-center gap-2">
                    <div className="w-6 h-6 border-2 border-[var(--accent)] border-t-transparent rounded-full animate-spin" />
                    <span className="text-sm">Loading devices...</span>
                  </div>
                </td>
              </tr>
            ) : devices.length === 0 ? (
              <tr>
                <td colSpan={9} className="text-center py-16">
                  <div className="flex flex-col items-center gap-2">
                    <Filter className="w-8 h-8 text-[var(--text-muted)]/30" />
                    <span className="text-sm text-[var(--text-muted)]">No devices found</span>
                    {activeFilters.length > 0 && (
                      <button onClick={clearAllFilters} className="text-xs text-[var(--accent)] hover:underline">
                        Clear all filters
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ) : (
              devices.map((device) => {
                const isSelected = selectedIds.has(device.id)
                const TypeIcon = deviceTypeOptions.find(t => t.value === device.device_type)?.icon || HelpCircle
                return (
                  <tr
                    key={device.id}
                    className={cn(
                      'border-b border-[var(--bg-elevated)]/50 hover:bg-[var(--bg-tertiary)] transition-colors',
                      isSelected && 'bg-[var(--accent)]/5'
                    )}
                  >
                    <td className="w-10 px-3 py-3">
                      <button
                        onClick={(e) => { e.stopPropagation(); toggleOne(device.id) }}
                        className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors"
                      >
                        {isSelected
                          ? <CheckSquare className="w-4 h-4 text-[var(--accent)]" />
                          : <Square className="w-4 h-4" />}
                      </button>
                    </td>
                    <td className="px-3 py-3 cursor-pointer" onClick={() => navigate(`/devices/${device.id}`)}>
                      <StatusIndicator status={device.status as DeviceStatus} showLabel />
                    </td>
                    <td className="px-3 py-3 text-sm font-medium text-[var(--text-primary)] cursor-pointer" onClick={() => navigate(`/devices/${device.id}`)}>
                      {device.hostname}
                    </td>
                    <td className="px-3 py-3 text-sm font-mono text-[var(--text-secondary)] cursor-pointer" onClick={() => navigate(`/devices/${device.id}`)}>
                      {device.ip_address}
                    </td>
                    <td className="px-3 py-3 cursor-pointer" onClick={() => navigate(`/devices/${device.id}`)}>
                      <span className="inline-flex items-center gap-1.5 text-xs text-[var(--text-secondary)]">
                        <TypeIcon className="w-3.5 h-3.5 text-[var(--text-muted)]" />
                        <span className="capitalize">{device.device_type.replace('_', ' ')}</span>
                      </span>
                    </td>
                    <td className="px-3 py-3 cursor-pointer" onClick={() => navigate(`/devices/${device.id}`)}>
                      {device.group_name ? (
                        <span className="text-xs px-2 py-0.5 rounded-md bg-[var(--bg-tertiary)] text-[var(--text-secondary)]">
                          {device.group_name}
                        </span>
                      ) : (
                        <span className="text-xs text-[var(--text-muted)]">-</span>
                      )}
                    </td>
                    <td className="px-3 py-3 text-xs text-[var(--text-muted)] cursor-pointer" onClick={() => navigate(`/devices/${device.id}`)}>
                      {device.location || '-'}
                    </td>
                    <td className="px-3 py-3 text-sm font-mono text-right text-[var(--text-secondary)] cursor-pointer" onClick={() => navigate(`/devices/${device.id}`)}>
                      {formatRTT(device.last_rtt_ms)}
                    </td>
                    <td className="px-3 py-3 text-xs text-right text-[var(--text-muted)] cursor-pointer" onClick={() => navigate(`/devices/${device.id}`)}>
                      {timeAgo(device.last_seen)}
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>

        {/* Pagination */}
        <div className="flex items-center justify-between px-4 py-3 border-t border-[var(--bg-elevated)]">
          <span className="text-xs text-[var(--text-muted)]">
            {total > 0 ? `Showing ${page * limit + 1}-${Math.min((page + 1) * limit, total)} of ${total}` : 'No results'}
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setPage(Math.max(0, page - 1))}
              disabled={page === 0}
              className="px-3 py-1.5 rounded-lg bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] disabled:opacity-30 hover:text-[var(--text-primary)] transition-colors"
            >
              Previous
            </button>
            {/* Page numbers */}
            {total > limit && (
              <div className="flex items-center gap-0.5">
                {Array.from({ length: Math.min(Math.ceil(total / limit), 7) }, (_, i) => (
                  <button
                    key={i}
                    onClick={() => setPage(i)}
                    className={cn(
                      'w-8 h-8 rounded-lg text-xs font-medium transition-colors',
                      page === i
                        ? 'bg-[var(--accent)] text-white'
                        : 'text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'
                    )}
                  >
                    {i + 1}
                  </button>
                ))}
              </div>
            )}
            <button
              onClick={() => setPage(page + 1)}
              disabled={(page + 1) * limit >= total}
              className="px-3 py-1.5 rounded-lg bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] disabled:opacity-30 hover:text-[var(--text-primary)] transition-colors"
            >
              Next
            </button>
          </div>
        </div>
      </div>

      {/* Delete Confirmation Dialog */}
      {showDeleteDialog && (
        <DeleteDialog
          count={selectedIds.size}
          onConfirm={() => deleteMutation.mutate(Array.from(selectedIds))}
          onCancel={() => setShowDeleteDialog(false)}
        />
      )}
    </div>
  )
}
