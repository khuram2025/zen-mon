import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useDevices } from '@/hooks/useDevices'
import { StatusIndicator } from '@/components/dashboard/StatusIndicator'
import { formatRTT, timeAgo } from '@/lib/utils'
import { Plus, Search, Upload, Download } from 'lucide-react'
import type { DeviceStatus } from '@/types'

const statusFilters = ['all', 'up', 'down', 'degraded', 'unknown'] as const

export function DevicesPage() {
  const navigate = useNavigate()
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const limit = 50

  const { data, isLoading } = useDevices({
    status: statusFilter === 'all' ? undefined : statusFilter,
    search: search || undefined,
    skip: page * limit,
    limit,
  })

  const devices = data?.data || []
  const total = data?.meta?.total || 0

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-semibold text-[var(--text-primary)]">Devices</h2>
        <div className="flex items-center gap-2">
          <button
            onClick={() => { navigate('/devices/new'); }}
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

      {/* Filters */}
      <div className="flex items-center gap-4 mb-4">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--text-muted)]" />
          <input
            type="text"
            placeholder="Search hostname or IP..."
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(0) }}
            className="w-full bg-[var(--bg-tertiary)] text-[var(--text-secondary)] pl-10 pr-4 py-2 rounded-lg border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] text-sm"
          />
        </div>

        <div className="flex gap-1">
          {statusFilters.map((s) => (
            <button
              key={s}
              onClick={() => { setStatusFilter(s); setPage(0) }}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors capitalize ${
                statusFilter === s
                  ? 'bg-[var(--accent)] text-white'
                  : 'bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)]'
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-[var(--bg-elevated)]">
              <th className="text-left px-4 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">Status</th>
              <th className="text-left px-4 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">Hostname</th>
              <th className="text-left px-4 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">IP Address</th>
              <th className="text-left px-4 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">Type</th>
              <th className="text-left px-4 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">Group</th>
              <th className="text-right px-4 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">RTT</th>
              <th className="text-right px-4 py-3 text-xs font-semibold text-[var(--text-muted)] uppercase">Last Seen</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td colSpan={7} className="text-center py-12 text-[var(--text-muted)]">Loading...</td>
              </tr>
            ) : devices.length === 0 ? (
              <tr>
                <td colSpan={7} className="text-center py-12 text-[var(--text-muted)]">No devices found</td>
              </tr>
            ) : (
              devices.map((device) => (
                <tr
                  key={device.id}
                  onClick={() => navigate(`/devices/${device.id}`)}
                  className="border-b border-[var(--bg-elevated)]/50 hover:bg-[var(--bg-tertiary)] cursor-pointer transition-colors"
                >
                  <td className="px-4 py-3">
                    <StatusIndicator status={device.status as DeviceStatus} showLabel />
                  </td>
                  <td className="px-4 py-3 text-sm font-medium text-[var(--text-primary)]">
                    {device.hostname}
                  </td>
                  <td className="px-4 py-3 text-sm font-mono text-[var(--text-secondary)]">
                    {device.ip_address}
                  </td>
                  <td className="px-4 py-3 text-sm text-[var(--text-secondary)] capitalize">
                    {device.device_type}
                  </td>
                  <td className="px-4 py-3 text-sm text-[var(--text-secondary)]">
                    {device.group_name || '-'}
                  </td>
                  <td className="px-4 py-3 text-sm font-mono text-right text-[var(--text-secondary)]">
                    {formatRTT(device.last_rtt_ms)}
                  </td>
                  <td className="px-4 py-3 text-sm text-right text-[var(--text-muted)]">
                    {timeAgo(device.last_seen)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>

        {/* Pagination */}
        {total > limit && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-[var(--bg-elevated)]">
            <span className="text-xs text-[var(--text-muted)]">
              Showing {page * limit + 1}-{Math.min((page + 1) * limit, total)} of {total}
            </span>
            <div className="flex gap-2">
              <button
                onClick={() => setPage(Math.max(0, page - 1))}
                disabled={page === 0}
                className="px-3 py-1 rounded bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] disabled:opacity-30"
              >
                Prev
              </button>
              <button
                onClick={() => setPage(page + 1)}
                disabled={(page + 1) * limit >= total}
                className="px-3 py-1 rounded bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] disabled:opacity-30"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
