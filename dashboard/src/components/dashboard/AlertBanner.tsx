import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { severityColors, timeAgo } from '@/lib/utils'
import { AlertTriangle, AlertCircle, Info } from 'lucide-react'
import type { Alert, PaginatedResponse } from '@/types'

const severityIcons = {
  critical: AlertCircle,
  warning: AlertTriangle,
  info: Info,
}

export function AlertBanner() {
  const { data } = useQuery({
    queryKey: ['active-alerts'],
    queryFn: () => api.get<PaginatedResponse<Alert>>('/alerts?status=active&limit=10'),
    refetchInterval: 15_000,
  })

  const alerts = data?.data || []

  return (
    <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
      <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">
        Active Alerts ({alerts.length})
      </h3>
      <div className="space-y-3 max-h-80 overflow-y-auto">
        {alerts.length === 0 ? (
          <p className="text-sm text-[var(--text-muted)] text-center py-8">No active alerts</p>
        ) : (
          alerts.map((alert) => {
            const Icon = severityIcons[alert.severity]
            return (
              <div
                key={alert.id}
                className="flex items-start gap-3 p-3 bg-[var(--bg-tertiary)] rounded-lg"
              >
                <Icon
                  className="w-4 h-4 mt-0.5 flex-shrink-0"
                  style={{ color: severityColors[alert.severity] }}
                />
                <div className="flex-1 min-w-0">
                  <p className="text-sm text-[var(--text-primary)] truncate">
                    {alert.message}
                  </p>
                  <p className="text-xs text-[var(--text-muted)] mt-0.5">
                    {alert.device_hostname || alert.device_ip} &middot; {timeAgo(alert.triggered_at)}
                  </p>
                </div>
                <span
                  className="text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded"
                  style={{
                    color: severityColors[alert.severity],
                    backgroundColor: `${severityColors[alert.severity]}20`,
                  }}
                >
                  {alert.severity}
                </span>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
