import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { SystemHistory, SystemStatus } from './healthMath'

export const SYSTEM_STATUS_KEY = ['system', 'status'] as const

/** Current CPU, memory, services and data stores of the appliance itself. */
export function useSystemStatus(enabled = true) {
  return useQuery<SystemStatus>({
    queryKey: [...SYSTEM_STATUS_KEY, 'now'],
    enabled,
    queryFn: async () => (await api.get('/system/status')).data,
    refetchInterval: 15_000,
    staleTime: 10_000,
  })
}

/** One-minute samples of the last hours (the appliance keeps 24). */
export function useSystemHistory(hours: number, enabled = true) {
  return useQuery<SystemHistory>({
    queryKey: [...SYSTEM_STATUS_KEY, 'history', hours],
    enabled,
    queryFn: async () => (await api.get(`/system/status/history?hours=${hours}`)).data,
    refetchInterval: 60_000,
    staleTime: 50_000,
  })
}
