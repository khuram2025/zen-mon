import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { ServiceCheck, ServiceCheckSummary, ServiceMetricResponse, PaginatedResponse } from '@/types'

export function useServiceChecks(params?: {
  device_id?: string
  check_type?: string
  status?: string
  search?: string
  skip?: number
  limit?: number
}) {
  const sp = new URLSearchParams()
  if (params?.device_id) sp.set('device_id', params.device_id)
  if (params?.check_type) sp.set('check_type', params.check_type)
  if (params?.status) sp.set('status', params.status)
  if (params?.search) sp.set('search', params.search)
  if (params?.skip !== undefined) sp.set('skip', String(params.skip))
  if (params?.limit !== undefined) sp.set('limit', String(params.limit))
  const qs = sp.toString()

  return useQuery({
    queryKey: ['service-checks', params],
    queryFn: () => api.get<PaginatedResponse<ServiceCheck>>(`/service-checks${qs ? `?${qs}` : ''}`),
    refetchInterval: 30_000,
  })
}

export function useServiceCheck(id: string) {
  return useQuery({
    queryKey: ['service-check', id],
    queryFn: () => api.get<ServiceCheck>(`/service-checks/${id}`),
    enabled: !!id,
  })
}

export function useServiceCheckSummary() {
  return useQuery({
    queryKey: ['service-check-summary'],
    queryFn: () => api.get<ServiceCheckSummary>('/service-checks/summary'),
    refetchInterval: 10_000,
  })
}

export function useDeviceServiceChecks(deviceId: string) {
  return useQuery({
    queryKey: ['device-service-checks', deviceId],
    queryFn: () => api.get<ServiceCheck[]>(`/devices/${deviceId}/service-checks`),
    enabled: !!deviceId,
  })
}

export function useServiceCheckMetrics(
  checkId: string,
  params?: { from?: string; to?: string; granularity?: string }
) {
  const sp = new URLSearchParams()
  if (params?.from) sp.set('from', params.from)
  if (params?.to) sp.set('to', params.to)
  if (params?.granularity) sp.set('granularity', params.granularity)
  const qs = sp.toString()

  return useQuery({
    queryKey: ['service-check-metrics', checkId, params],
    queryFn: () => api.get<ServiceMetricResponse>(`/service-checks/${checkId}/metrics${qs ? `?${qs}` : ''}`),
    enabled: !!checkId,
    refetchInterval: 60_000,
  })
}
