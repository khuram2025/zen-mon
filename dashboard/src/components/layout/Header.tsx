import { useState, useRef, useEffect } from 'react'
import { Bell, Search, User, AlertCircle, AlertTriangle, Info, ArrowRight } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api } from '@/lib/api'
import { severityColors, timeAgo } from '@/lib/utils'
import type { Alert, AlertStats, PaginatedResponse } from '@/types'

const severityIcons = {
  critical: AlertCircle,
  warning: AlertTriangle,
  info: Info,
}

export function Header() {
  const { user, logout } = useAuthStore()
  const [alertsOpen, setAlertsOpen] = useState(false)
  const alertsRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  const { data: alertStats } = useQuery({
    queryKey: ['alert-stats'],
    queryFn: () => api.get<AlertStats>('/alerts/stats'),
    refetchInterval: 15_000,
  })

  const { data: recentAlerts } = useQuery({
    queryKey: ['recent-alerts'],
    queryFn: () => api.get<PaginatedResponse<Alert>>('/alerts?status=active&limit=5'),
    refetchInterval: 15_000,
  })

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (alertsRef.current && !alertsRef.current.contains(e.target as Node)) {
        setAlertsOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  return (
    <header className="h-14 bg-[var(--bg-secondary)] border-b border-[var(--bg-elevated)] flex items-center justify-between px-6 fixed top-0 left-16 right-0 z-40">
      <div className="flex items-center gap-4">
        <h1 className="text-lg font-semibold text-[var(--text-primary)]">ZenPlus</h1>
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[var(--text-muted)]" />
          <input
            type="text"
            placeholder="Search devices..."
            className="bg-[var(--bg-tertiary)] text-[var(--text-secondary)] pl-10 pr-4 py-1.5 rounded-lg text-sm border border-[var(--bg-elevated)] focus:outline-none focus:border-[var(--accent)] w-72"
          />
        </div>
      </div>

      <div className="flex items-center gap-4">
        {/* Alert badge */}
        <div className="relative" ref={alertsRef}>
          <button
            onClick={() => setAlertsOpen(!alertsOpen)}
            className="relative p-2 hover:bg-[var(--bg-tertiary)] rounded-lg transition-colors"
          >
            <Bell className="w-5 h-5 text-[var(--text-secondary)]" />
            {alertStats && alertStats.active > 0 && (
              <span className="absolute -top-0.5 -right-0.5 bg-[var(--status-down)] text-white text-[10px] font-bold rounded-full w-5 h-5 flex items-center justify-center">
                {alertStats.active > 99 ? '99+' : alertStats.active}
              </span>
            )}
          </button>

          {alertsOpen && (
            <div className="absolute right-0 top-full mt-2 w-80 bg-[var(--bg-secondary)] border border-[var(--bg-elevated)] rounded-lg shadow-xl z-50 overflow-hidden">
              <div className="px-4 py-3 border-b border-[var(--bg-elevated)] flex items-center justify-between">
                <span className="text-sm font-semibold text-[var(--text-primary)]">Alerts</span>
                {alertStats && alertStats.active > 0 && (
                  <span className="text-xs bg-[var(--status-down)] text-white px-2 py-0.5 rounded-full">
                    {alertStats.active} active
                  </span>
                )}
              </div>

              <div className="max-h-72 overflow-y-auto">
                {recentAlerts?.data && recentAlerts.data.length > 0 ? (
                  recentAlerts.data.map((alert: Alert) => {
                    const Icon = severityIcons[alert.severity] || Info
                    return (
                      <div
                        key={alert.id}
                        className="px-4 py-3 border-b border-[var(--bg-elevated)] hover:bg-[var(--bg-tertiary)] cursor-pointer transition-colors"
                        onClick={() => {
                          setAlertsOpen(false)
                          navigate('/alerts')
                        }}
                      >
                        <div className="flex items-start gap-3">
                          <Icon className="w-4 h-4 mt-0.5 flex-shrink-0" style={{ color: severityColors[alert.severity] }} />
                          <div className="flex-1 min-w-0">
                            <p className="text-sm text-[var(--text-primary)] truncate">{alert.message}</p>
                            <p className="text-xs text-[var(--text-muted)] mt-0.5">
                              {alert.device_hostname || alert.device_ip} · {timeAgo(alert.triggered_at)}
                            </p>
                          </div>
                        </div>
                      </div>
                    )
                  })
                ) : (
                  <div className="px-4 py-8 text-center text-sm text-[var(--text-muted)]">
                    No active alerts
                  </div>
                )}
              </div>

              <button
                onClick={() => {
                  setAlertsOpen(false)
                  navigate('/alerts')
                }}
                className="w-full px-4 py-2.5 text-sm text-[var(--accent)] hover:bg-[var(--bg-tertiary)] transition-colors flex items-center justify-center gap-1 border-t border-[var(--bg-elevated)]"
              >
                View all alerts <ArrowRight className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>

        {/* User menu */}
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-[var(--accent)] rounded-full flex items-center justify-center">
            <User className="w-4 h-4 text-white" />
          </div>
          <span className="text-sm text-[var(--text-secondary)]">{user?.username || 'Admin'}</span>
          <button
            onClick={logout}
            className="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)] ml-2"
          >
            Logout
          </button>
        </div>
      </div>
    </header>
  )
}
