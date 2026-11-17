import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

/** A row of the device type catalogue (GET /device-types, migrate-189). */
export type DeviceTypeInfo = {
  key: string
  label: string
  category: string
  icon: string
  sort_order: number
  builtin: boolean
  description: string | null
  device_count: number
  template_count: number
}

/** Used until the catalogue has loaded (and on an appliance whose API predates it). */
export const FALLBACK_DEVICE_TYPES: DeviceTypeInfo[] = [
  'router', 'switch', 'firewall', 'server', 'access_point', 'printer', 'other',
].map((key, i) => ({
  key,
  label: key === 'access_point' ? 'Access Point' : key.charAt(0).toUpperCase() + key.slice(1),
  category: 'other',
  icon: key,
  sort_order: i * 10,
  builtin: true,
  description: null,
  device_count: 0,
  template_count: 0,
}))

export const DEVICE_TYPE_CATEGORIES = [
  'network', 'security', 'wireless', 'server', 'power', 'storage', 'endpoint', 'other',
] as const

export function useDeviceTypes() {
  const q = useQuery<DeviceTypeInfo[]>({
    queryKey: ['device-types'],
    queryFn: async () => (await api.get('/device-types')).data.data,
    staleTime: 5 * 60_000,
  })
  const types = q.data && q.data.length ? q.data : FALLBACK_DEVICE_TYPES
  return { ...q, types }
}

/** Display label for a type key, from the catalogue when it knows it. */
export function deviceTypeLabel(types: DeviceTypeInfo[], key: string | null | undefined): string {
  if (!key) return '—'
  const t = types.find((x) => x.key === key)
  if (t) return t.label
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}
