import { Monitor, Wifi, WifiOff, AlertTriangle } from 'lucide-react'
import { useDeviceSummary, useDevices } from '@/hooks/useDevices'
import { StatusCard } from '@/components/dashboard/StatusCard'
import { AlertBanner } from '@/components/dashboard/AlertBanner'
import { StatusHeatmap } from '@/components/charts/StatusHeatmap'
import { useSSE } from '@/hooks/useSSE'
import { useQueryClient } from '@tanstack/react-query'

export function DashboardPage() {
  const { data: summary } = useDeviceSummary()
  const { data: devicesData } = useDevices({ limit: 200 })
  const queryClient = useQueryClient()

  // SSE for real-time updates
  useSSE('/api/v1/stream/status', {
    onMessage: () => {
      // Invalidate queries on status changes to refresh data
      queryClient.invalidateQueries({ queryKey: ['device-summary'] })
      queryClient.invalidateQueries({ queryKey: ['devices'] })
      queryClient.invalidateQueries({ queryKey: ['active-alerts'] })
    },
  })

  const devices = devicesData?.data || []

  return (
    <div>
      <h2 className="text-xl font-semibold text-[var(--text-primary)] mb-6">Dashboard</h2>

      {/* KPI Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <StatusCard
          title="Total Devices"
          value={summary?.total ?? 0}
          icon={<Monitor className="w-5 h-5" />}
          color="var(--accent)"
        />
        <StatusCard
          title="Online"
          value={summary?.up ?? 0}
          subtitle={summary ? `${((summary.up / Math.max(summary.total, 1)) * 100).toFixed(1)}%` : ''}
          icon={<Wifi className="w-5 h-5" />}
          color="var(--status-up)"
        />
        <StatusCard
          title="Degraded"
          value={summary?.degraded ?? 0}
          icon={<AlertTriangle className="w-5 h-5" />}
          color="var(--status-degraded)"
        />
        <StatusCard
          title="Offline"
          value={summary?.down ?? 0}
          icon={<WifiOff className="w-5 h-5" />}
          color="var(--status-down)"
        />
      </div>

      {/* Main content grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-6">
          {/* Status Heatmap */}
          <StatusHeatmap devices={devices} />
        </div>

        <div className="space-y-6">
          {/* Active Alerts */}
          <AlertBanner />
        </div>
      </div>
    </div>
  )
}
