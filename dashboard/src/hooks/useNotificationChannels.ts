import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export interface NotificationChannelOption {
  id: string
  name: string
  type: string
  enabled: boolean
}

export function notificationChannelList(value: unknown): NotificationChannelOption[] {
  const rows = Array.isArray(value) ? value : (value as { data?: unknown })?.data
  return Array.isArray(rows) ? rows : []
}

/** Every consumer of this cache key stores the normalized channel array. */
export function useNotificationChannels(enabled = true) {
  return useQuery<NotificationChannelOption[]>({
    queryKey: ['settings', 'channels'],
    queryFn: async () => notificationChannelList((await api.get('/settings/channels')).data),
    enabled,
    staleTime: 60_000,
  })
}
