import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Bell, BellRing } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'

interface UpdateHistoryRecord {
  version: string
  from_version: string
  status: string
  error: string
  started_at: string
  completed_at: string
}

interface UpdateStatusLite {
  current_version: string
  appliance_id: string
  timer_active: boolean
  updater_running: boolean
  last_update: UpdateHistoryRecord | null
  active_update: UpdateHistoryRecord | null
}

/**
 * Header notification bell that lights up when an update is in flight or
 * a recent attempt failed. Click → /settings/general?tab=updates.
 *
 * Shares the `system-update-status` query cache with `UpdatesTabContent`,
 * so no duplicate network calls when both are mounted.
 */
export function UpdateNotificationBell() {
  const navigate = useNavigate()

  const { data: status } = useQuery<UpdateStatusLite>({
    queryKey: ['system-update-status'],
    queryFn: async () => (await api.get<UpdateStatusLite>('/system/update-status')).data,
    refetchInterval: (query) => {
      const d = query.state.data
      // Faster polling while an update is mid-flight; otherwise once a minute.
      return d?.updater_running || d?.active_update ? 5000 : 60000
    },
    // Cheap: this fires for every authenticated session.
    retry: false,
    refetchOnWindowFocus: true,
  })

  const inFlight = !!status?.active_update
  const failed = status?.last_update?.status === 'failed'
  const unregistered = !status?.appliance_id
  const anyAttention = inFlight || failed || unregistered

  const tooltip = inFlight
    ? `Update in progress: v${status?.active_update?.from_version} → v${status?.active_update?.version}`
    : failed
      ? `Last update failed (v${status?.last_update?.from_version} → v${status?.last_update?.version})`
      : unregistered
        ? 'Appliance not registered with zentryc.com'
        : 'Updates'

  const dotColor = inFlight
    ? 'bg-blue-400'
    : failed
      ? 'bg-red-500'
      : unregistered
        ? 'bg-amber-400'
        : 'bg-transparent'

  return (
    <Button
      variant="ghost"
      size="icon"
      className="relative h-7 w-7 text-muted hover:text-text"
      onClick={() => navigate('/settings/general?tab=updates')}
      title={tooltip}
      aria-label={tooltip}
    >
      {inFlight ? (
        <BellRing className="h-3.5 w-3.5 animate-pulse" />
      ) : (
        <Bell className="h-3.5 w-3.5" />
      )}
      {anyAttention && (
        <span
          className={cn(
            'pointer-events-none absolute right-0.5 top-0.5 inline-flex h-2 w-2 rounded-full ring-2 ring-surface',
            dotColor,
            inFlight && 'animate-pulse',
          )}
        />
      )}
    </Button>
  )
}
