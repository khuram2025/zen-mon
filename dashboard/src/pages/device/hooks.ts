import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { Insights } from '@/components/devices/TemplateInsightsSection'
import type { DeviceRecord, DeviceSummary, MetricSeriesMap, PingPoint, DeviceRange } from './types'

/* Queries shared by the shell and the tabs. Every polled query keeps the last
 * good data while it refetches, so tiles never blank out mid-refresh; a tab's
 * own queries run only while that tab is mounted. */

export function useDeviceRecord(id: string | undefined, onServerTime?: (ms: number) => void) {
  return useQuery<DeviceRecord>({
    queryKey: ['device', id],
    queryFn: async () => {
      const response = await api.get(`/devices/${id}`)
      const serverTime = Date.parse(response.headers.date || '')
      if (Number.isFinite(serverTime)) onServerTime?.(Math.floor(serverTime / 60_000) * 60_000)
      return response.data
    },
    refetchInterval: 15_000,
    placeholderData: keepPreviousData,
    enabled: !!id,
    // A missing device will not appear by retrying.
    retry: (count, error: any) => error?.response?.status !== 404 && count < 2,
  })
}

export function useDeviceSummary(id: string | undefined) {
  return useQuery<DeviceSummary>({
    queryKey: ['device', id, 'summary'],
    queryFn: async () => (await api.get(`/devices/${id}/summary`)).data,
    refetchInterval: 15_000,
    placeholderData: keepPreviousData,
    enabled: !!id,
  })
}

export function useTemplateInsights(id: string, enabled = true) {
  return useQuery<Insights>({
    queryKey: ['device', id, 'template-insights'],
    queryFn: async () => (await api.get(`/devices/${id}/template-insights`)).data,
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
    enabled,
  })
}

export function usePingMetrics(id: string, range: DeviceRange, enabled: boolean) {
  return useQuery<{ points: PingPoint[] }>({
    queryKey: ['device', id, 'ping-metrics', range.fromISO, range.toISO],
    queryFn: async () => (await api.get(
      `/devices/${id}/metrics?from=${encodeURIComponent(range.fromISO)}&to=${encodeURIComponent(range.toISO)}`,
    )).data,
    // A custom (historical) window does not change; only sliding presets poll.
    refetchInterval: range.isCustom ? false : 30_000,
    placeholderData: keepPreviousData,
    enabled,
  })
}

export function useSnmpMetrics(id: string, hours: number, enabled: boolean, poll = true) {
  return useQuery<MetricSeriesMap>({
    queryKey: ['device', id, 'snmp-metrics', hours],
    queryFn: async () => (await api.get(`/devices/${id}/snmp-metrics?hours=${hours}`)).data,
    refetchInterval: poll ? 30_000 : false,
    placeholderData: keepPreviousData,
    enabled,
  })
}

/** SNMP readings for the window `offsetHours` before the current one (the previous-period comparison). */
export function useSnmpMetricsBefore(id: string, hours: number, offsetHours: number, enabled: boolean) {
  return useQuery<MetricSeriesMap>({
    queryKey: ['device', id, 'snmp-metrics', hours, 'offset', offsetHours],
    queryFn: async () => (await api.get(`/devices/${id}/snmp-metrics?hours=${hours}&offset_hours=${offsetHours}`)).data,
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
    enabled,
  })
}

/** The availability headline: the shared uptime-stats calculation, narrowed to this device. */
export function useDeviceAvailability(id: string, range: DeviceRange, enabled: boolean) {
  return useQuery<{ devices: Record<string, number>; failed_checks?: Record<string, number> }>({
    queryKey: ['uptime-stats', range.fromISO, range.toISO, id],
    queryFn: async () => (await api.get(
      `/devices/dashboard/uptime-stats?from=${encodeURIComponent(range.fromISO)}&to=${encodeURIComponent(range.toISO)}&device_id=${id}`,
    )).data,
    staleTime: 30_000,
    refetchInterval: range.isCustom ? false : 60_000,
    placeholderData: keepPreviousData,
    enabled,
  })
}
