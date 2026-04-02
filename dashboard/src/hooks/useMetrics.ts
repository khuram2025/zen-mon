import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { MetricResponse } from '@/types'

export function useDeviceMetrics(
  deviceId: string,
  params?: { from?: string; to?: string; granularity?: string }
) {
  const searchParams = new URLSearchParams()
  if (params?.from) searchParams.set('from', params.from)
  if (params?.to) searchParams.set('to', params.to)
  if (params?.granularity) searchParams.set('granularity', params.granularity)

  const qs = searchParams.toString()
  return useQuery({
    queryKey: ['device-metrics', deviceId, params],
    queryFn: () =>
      api.get<MetricResponse>(`/devices/${deviceId}/metrics${qs ? `?${qs}` : ''}`),
    enabled: !!deviceId,
    refetchInterval: 60_000,
  })
}
