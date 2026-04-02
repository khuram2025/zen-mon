import { useParams, useNavigate } from 'react-router-dom'
import { useDevice } from '@/hooks/useDevices'
import { useDeviceMetrics } from '@/hooks/useMetrics'
import { StatusIndicator } from '@/components/dashboard/StatusIndicator'
import { StatusCard } from '@/components/dashboard/StatusCard'
import { TimeSeriesChart } from '@/components/charts/TimeSeriesChart'
import { formatRTT, timeAgo } from '@/lib/utils'
import { ArrowLeft, Clock, Gauge, Zap, Activity } from 'lucide-react'
import type { DeviceStatus } from '@/types'
import { useState } from 'react'

const timeRanges = [
  { label: '1h', hours: 1 },
  { label: '6h', hours: 6 },
  { label: '24h', hours: 24 },
  { label: '7d', hours: 168 },
  { label: '30d', hours: 720 },
]

export function DeviceDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [rangeHours, setRangeHours] = useState(24)

  const { data: device, isLoading } = useDevice(id!)

  const now = new Date()
  const from = new Date(now.getTime() - rangeHours * 60 * 60 * 1000)

  const { data: metrics } = useDeviceMetrics(id!, {
    from: from.toISOString(),
    to: now.toISOString(),
    granularity: 'auto',
  })

  if (isLoading) {
    return <div className="text-[var(--text-muted)] text-center py-20">Loading...</div>
  }

  if (!device) {
    return <div className="text-[var(--text-muted)] text-center py-20">Device not found</div>
  }

  const points = metrics?.points || []
  const avgRtt = points.length > 0
    ? points.reduce((sum, p) => sum + (p.rtt_ms || 0), 0) / points.length
    : null
  const maxRtt = points.length > 0
    ? Math.max(...points.map((p) => p.max_rtt_ms || p.rtt_ms || 0))
    : null
  const avgLoss = points.length > 0
    ? points.reduce((sum, p) => sum + (p.packet_loss || 0), 0) / points.length
    : null

  return (
    <div>
      {/* Header */}
      <div className="flex items-center gap-4 mb-6">
        <button
          onClick={() => navigate('/devices')}
          className="p-2 hover:bg-[var(--bg-tertiary)] rounded-lg transition-colors"
        >
          <ArrowLeft className="w-5 h-5 text-[var(--text-secondary)]" />
        </button>
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <h2 className="text-xl font-semibold text-[var(--text-primary)]">{device.hostname}</h2>
            <StatusIndicator status={device.status as DeviceStatus} showLabel size="lg" />
          </div>
          <p className="text-sm text-[var(--text-muted)] font-mono mt-0.5">
            {device.ip_address} &middot; {device.device_type} &middot; {device.group_name || 'No group'}
          </p>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <StatusCard
          title="Avg RTT"
          value={formatRTT(avgRtt)}
          icon={<Gauge className="w-5 h-5" />}
          color="var(--accent)"
        />
        <StatusCard
          title="Max RTT"
          value={formatRTT(maxRtt)}
          icon={<Zap className="w-5 h-5" />}
          color="var(--status-degraded)"
        />
        <StatusCard
          title="Packet Loss"
          value={avgLoss !== null ? `${(avgLoss * 100).toFixed(1)}%` : '--'}
          icon={<Activity className="w-5 h-5" />}
          color={avgLoss && avgLoss > 0.01 ? 'var(--status-down)' : 'var(--status-up)'}
        />
        <StatusCard
          title="Last Seen"
          value={timeAgo(device.last_seen)}
          icon={<Clock className="w-5 h-5" />}
          color="var(--text-secondary)"
        />
      </div>

      {/* Time range selector + Chart */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-[var(--text-primary)]">Response Time & Packet Loss</h3>
          <div className="flex gap-1">
            {timeRanges.map((r) => (
              <button
                key={r.label}
                onClick={() => setRangeHours(r.hours)}
                className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
                  rangeHours === r.hours
                    ? 'bg-[var(--accent)] text-white'
                    : 'bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)]'
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        </div>
        {points.length > 0 ? (
          <TimeSeriesChart data={points} height={350} showPacketLoss />
        ) : (
          <div className="h-[350px] flex items-center justify-center text-[var(--text-muted)]">
            No data for selected time range
          </div>
        )}
      </div>

      {/* Device Info */}
      <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5 mt-6">
        <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">Device Information</h3>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 text-sm">
          <div>
            <span className="text-[var(--text-muted)]">IP Address</span>
            <p className="font-mono text-[var(--text-primary)]">{device.ip_address}</p>
          </div>
          <div>
            <span className="text-[var(--text-muted)]">Type</span>
            <p className="text-[var(--text-primary)] capitalize">{device.device_type}</p>
          </div>
          <div>
            <span className="text-[var(--text-muted)]">Location</span>
            <p className="text-[var(--text-primary)]">{device.location || '-'}</p>
          </div>
          <div>
            <span className="text-[var(--text-muted)]">Ping Interval</span>
            <p className="text-[var(--text-primary)]">{device.ping_interval}s</p>
          </div>
          <div>
            <span className="text-[var(--text-muted)]">Group</span>
            <p className="text-[var(--text-primary)]">{device.group_name || '-'}</p>
          </div>
          <div>
            <span className="text-[var(--text-muted)]">Ping Enabled</span>
            <p className="text-[var(--text-primary)]">{device.ping_enabled ? 'Yes' : 'No'}</p>
          </div>
          <div>
            <span className="text-[var(--text-muted)]">Created</span>
            <p className="text-[var(--text-primary)]">{new Date(device.created_at).toLocaleDateString()}</p>
          </div>
          <div>
            <span className="text-[var(--text-muted)]">Description</span>
            <p className="text-[var(--text-primary)]">{device.description || '-'}</p>
          </div>
        </div>
      </div>
    </div>
  )
}
