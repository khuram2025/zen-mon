import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { severityColors, timeAgo } from '@/lib/utils'
import { AlertCircle, AlertTriangle, Info, Check, Eye } from 'lucide-react'
import type { Alert, AlertStats, PaginatedResponse } from '@/types'

const severityIcons = {
  critical: AlertCircle,
  warning: AlertTriangle,
  info: Info,
}

const tabs = ['active', 'acknowledged', 'resolved'] as const

export function AlertsPage() {
  const [tab, setTab] = useState<string>('active')
  const queryClient = useQueryClient()

  const { data } = useQuery({
    queryKey: ['alerts', tab],
    queryFn: () => api.get<PaginatedResponse<Alert>>(`/alerts?status=${tab}&limit=100`),
    refetchInterval: 15_000,
  })

  const { data: stats } = useQuery({
    queryKey: ['alert-stats'],
    queryFn: () => api.get<AlertStats>('/alerts/stats'),
  })

  const acknowledgeMutation = useMutation({
    mutationFn: (alertId: string) => api.post(`/alerts/${alertId}/acknowledge`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alerts'] })
      queryClient.invalidateQueries({ queryKey: ['alert-stats'] })
    },
  })

  const resolveMutation = useMutation({
    mutationFn: (alertId: string) => api.post(`/alerts/${alertId}/resolve`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alerts'] })
      queryClient.invalidateQueries({ queryKey: ['alert-stats'] })
    },
  })

  const alerts = data?.data || []

  return (
    <div>
      <h2 className="text-xl font-semibold text-[var(--text-primary)] mb-6">
        Alerts {stats ? `(${stats.active} active)` : ''}
      </h2>

      {/* Tabs */}
      <div className="flex gap-1 mb-6">
        {tabs.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors capitalize ${
              tab === t
                ? 'bg-[var(--accent)] text-white'
                : 'bg-[var(--bg-tertiary)] text-[var(--text-muted)] hover:text-[var(--text-primary)]'
            }`}
          >
            {t}
            {t === 'active' && stats ? ` (${stats.active})` : ''}
            {t === 'acknowledged' && stats ? ` (${stats.acknowledged})` : ''}
          </button>
        ))}
      </div>

      {/* Alert List */}
      <div className="space-y-3">
        {alerts.length === 0 ? (
          <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-12 text-center text-[var(--text-muted)]">
            No {tab} alerts
          </div>
        ) : (
          alerts.map((alert) => {
            const Icon = severityIcons[alert.severity]
            return (
              <div
                key={alert.id}
                className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-4"
              >
                <div className="flex items-start gap-3">
                  <Icon
                    className="w-5 h-5 mt-0.5 flex-shrink-0"
                    style={{ color: severityColors[alert.severity] }}
                  />
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-1">
                      <span
                        className="text-[10px] font-bold uppercase px-2 py-0.5 rounded"
                        style={{
                          color: severityColors[alert.severity],
                          backgroundColor: `${severityColors[alert.severity]}20`,
                        }}
                      >
                        {alert.severity}
                      </span>
                      <span className="text-sm font-medium text-[var(--text-primary)]">
                        {alert.message}
                      </span>
                    </div>
                    <p className="text-xs text-[var(--text-muted)]">
                      {alert.device_hostname} ({alert.device_ip}) &middot; {timeAgo(alert.triggered_at)}
                    </p>
                  </div>

                  {tab === 'active' && (
                    <div className="flex gap-2">
                      <button
                        onClick={() => acknowledgeMutation.mutate(alert.id)}
                        className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-[var(--bg-tertiary)] text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
                      >
                        <Eye className="w-3 h-3" />
                        Ack
                      </button>
                      <button
                        onClick={() => resolveMutation.mutate(alert.id)}
                        className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-green-500/10 text-xs text-green-400 hover:bg-green-500/20 transition-colors"
                      >
                        <Check className="w-3 h-3" />
                        Resolve
                      </button>
                    </div>
                  )}
                  {tab === 'acknowledged' && (
                    <button
                      onClick={() => resolveMutation.mutate(alert.id)}
                      className="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-green-500/10 text-xs text-green-400 hover:bg-green-500/20 transition-colors"
                    >
                      <Check className="w-3 h-3" />
                      Resolve
                    </button>
                  )}
                </div>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
