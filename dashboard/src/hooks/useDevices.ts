import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { Device, DeviceSummary, PaginatedResponse } from '@/types'

export function useDevices(params?: {
  status?: string
  group_id?: string
  search?: string
  skip?: number
  limit?: number
}) {
  const searchParams = new URLSearchParams()
  if (params?.status) searchParams.set('status', params.status)
  if (params?.group_id) searchParams.set('group_id', params.group_id)
  if (params?.search) searchParams.set('search', params.search)
  if (params?.skip !== undefined) searchParams.set('skip', String(params.skip))
  if (params?.limit !== undefined) searchParams.set('limit', String(params.limit))

  const qs = searchParams.toString()
  return useQuery({
    queryKey: ['devices', params],
    queryFn: () => api.get<PaginatedResponse<Device>>(`/devices${qs ? `?${qs}` : ''}`),
    refetchInterval: 30_000,
  })
}

export function useDevice(id: string) {
  return useQuery({
    queryKey: ['device', id],
    queryFn: () => api.get<Device>(`/devices/${id}`),
    enabled: !!id,
  })
}

export function useDeviceSummary() {
  return useQuery({
    queryKey: ['device-summary'],
    queryFn: () => api.get<DeviceSummary>('/devices/summary'),
    refetchInterval: 10_000,
  })
}
