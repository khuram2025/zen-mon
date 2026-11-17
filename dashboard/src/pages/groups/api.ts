import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import type { ConditionGroup, RuleCatalog } from '@/pages/discovery/types'
import type { Lookups } from '@/pages/discovery/ruleModel'
import type { RefOption } from '@/pages/discovery/ruleUi'

export type AssetKind = 'device' | 'server'

export type DeviceGroup = {
  id: string
  name: string
  description: string | null
  color: string | null
  parent_id: string | null
  kind: 'static' | 'dynamic'
  condition: ConditionGroup | null
  asset_kinds: AssetKind[]
  member_count: number
  evaluated_at: string | null
  eval_error: string | null
  primary_count: number
  static_count: number
  child_count: number
  updated_at: string
}

export type UsedBy = {
  alert_rules: { id: string; name: string; enabled: boolean }[]
  maintenance: { id: string; reason: string | null; starts_at: string; ends_at: string }[]
  sensor_assignments: { id: string; name: string | null }[]
  discovery_profiles: { id: string; name: string }[]
  discovery_rules: { id: string; name: string }[]
  subgroups: { id: string; name: string }[]
  primary_devices: number
}

export type Member = {
  asset_kind: AssetKind
  id: string
  sources: string[]
  via_subgroup: boolean
  name: string | null
  ip_address: string | null
  status: string | null
}

export type Preview = {
  count: number
  by_kind: Record<string, number>
  sample: { asset_kind: AssetKind; id: string; name: string | null; ip_address: string | null; vendor: string | null; device_type: string | null }[]
  total_considered: number
}

export type CustomField = {
  id: string
  key: string
  label: string
  field_type: 'text' | 'number' | 'date' | 'choice' | 'boolean'
  choices: string[] | null
  asset_kinds: AssetKind[]
  description: string | null
  sort_order: number
  show_in_list: boolean
  value_count: number
}

export const groupsApi = {
  list: async (): Promise<DeviceGroup[]> => (await api.get('/device-groups')).data.data,
  get: async (id: string): Promise<DeviceGroup & { used_by: UsedBy }> => (await api.get(`/device-groups/${id}`)).data,
  members: async (id: string): Promise<Member[]> => (await api.get(`/device-groups/${id}/members`)).data.data,
  catalog: async (): Promise<RuleCatalog> => {
    const d = (await api.get('/device-groups/catalog')).data
    return { ...d, actions: [], decisions: [], import_as: [], device_types: [] }
  },
  preview: async (condition: ConditionGroup, asset_kinds: AssetKind[]): Promise<Preview> =>
    (await api.post('/device-groups/preview', { condition, asset_kinds })).data,
  fields: async (): Promise<CustomField[]> => (await api.get('/custom-fields')).data.data,
}

export function useGroupCatalog() {
  return useQuery({ queryKey: ['groups-tree', 'catalog'], queryFn: groupsApi.catalog, staleTime: 60_000 })
}

function asOptions(list: any): RefOption[] {
  const arr = Array.isArray(list) ? list : list?.data ?? []
  return arr.map((x: any) => ({ id: String(x.id ?? x.key), name: String(x.name ?? x.label ?? x.key) }))
}

/** Values the condition builder offers for reference fields, and their names for summaries. */
export function useGroupLookups(): { options: Record<string, RefOption[]>; lookups: Lookups } {
  const opts = { retry: false, staleTime: 60_000 } as const
  const types = useQuery<any>({ queryKey: ['device-types'], queryFn: async () => (await api.get('/device-types')).data.data, ...opts })
  const templates = useQuery<any>({ queryKey: ['snmp-profiles'], queryFn: async () => (await api.get('/snmp/profiles')).data, ...opts })
  const groups = useQuery<any>({ queryKey: ['groups-tree'], queryFn: groupsApi.list, ...opts })
  const sites = useQuery<any>({ queryKey: ['sites'], queryFn: async () => (await api.get('/sites')).data, ...opts })
  return useMemo(() => {
    const options: Record<string, RefOption[]> = {
      device_types: asOptions(types.data),
      device_profiles: asOptions(templates.data),
      device_groups: asOptions((groups.data || []).filter((g: DeviceGroup) => g.kind === 'static')),
      sites: asOptions(sites.data),
    }
    const lookups: Lookups = {}
    for (const [source, list] of Object.entries(options)) lookups[source] = Object.fromEntries(list.map((o) => [o.id, o.name]))
    return { options, lookups }
  }, [types.data, templates.data, groups.data, sites.data])
}

/** Groups ordered as a tree (parents before children) with their depth. */
export function treeOrder(groups: DeviceGroup[]): { group: DeviceGroup; depth: number }[] {
  const byParent = new Map<string | null, DeviceGroup[]>()
  const ids = new Set(groups.map((g) => g.id))
  for (const g of groups) {
    const key = g.parent_id && ids.has(g.parent_id) ? g.parent_id : null
    byParent.set(key, [...(byParent.get(key) || []), g])
  }
  const out: { group: DeviceGroup; depth: number }[] = []
  const walk = (parent: string | null, depth: number) => {
    for (const g of (byParent.get(parent) || []).sort((a, b) => a.name.localeCompare(b.name))) {
      out.push({ group: g, depth })
      if (depth < 8) walk(g.id, depth + 1)
    }
  }
  walk(null, 0)
  return out
}

export function apiDetail(e: any): string {
  const d = e?.response?.data?.detail
  if (typeof d === 'string') return d
  if (Array.isArray(d)) return d.map((x: any) => `${(x.loc || []).slice(1).join('.')}: ${x.msg}`).join('; ')
  return e?.message || 'Request failed'
}
