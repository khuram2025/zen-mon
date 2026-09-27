/** Data hooks and tab routing for the server detail page.
 *
 *  Queries are keyed so the header, the tabs and the badges share one fetch
 *  per resource; React Query de-duplicates them. */

import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Activity, Bell, Bot, ClipboardCheck, Cpu, Gauge, ScrollText, Server as ServerIcon, Wrench,
} from 'lucide-react'
import { api } from '@/lib/api'
import type {
  AgentItem, ComplianceSummary, ServerFilesystem, ServerItem, ServerLiveMetrics, ServerProcess,
  ServerService,
} from '@/types/servers'

/* ── Tabs ─────────────────────────────────────────────────────────── */

export const TABS = [
  { key: 'overview', label: 'Overview', icon: Gauge },
  { key: 'performance', label: 'Performance', icon: Activity },
  { key: 'processes', label: 'Processes', icon: Cpu },
  { key: 'services', label: 'Services', icon: Wrench },
  { key: 'system', label: 'System', icon: ServerIcon },
  { key: 'events', label: 'Events', icon: ScrollText },
  { key: 'alerts', label: 'Alerts', icon: Bell },
  { key: 'compliance', label: 'Compliance', icon: ClipboardCheck },
  { key: 'agent', label: 'Agent', icon: Bot },
] as const

export type TabKey = typeof TABS[number]['key']
const TAB_KEYS = TABS.map((t) => t.key) as readonly string[]

/** Links from before the redesign (?tab=inventory&sub=services, ?tab=manage)
 *  keep landing on the same content. */
const LEGACY: Record<string, { tab: TabKey; sub?: string }> = {
  'inventory:processes': { tab: 'processes' },
  'inventory:services': { tab: 'services' },
  'inventory:software': { tab: 'system', sub: 'software' },
  'inventory:storage': { tab: 'system', sub: 'storage' },
  'inventory:network': { tab: 'system', sub: 'network' },
  inventory: { tab: 'processes' },
  'alerts:events': { tab: 'events' },
  manage: { tab: 'agent' },
  'manage:settings': { tab: 'agent', sub: 'settings' },
}

/** Active tab and sub-section, deep-linked through ?tab= and ?sub=. */
export function useTabState() {
  const [params, setParams] = useSearchParams()
  const rawTab = params.get('tab') || 'overview'
  const rawSub = params.get('sub') || ''
  const legacy = LEGACY[`${rawTab}:${rawSub}`] ?? (TAB_KEYS.includes(rawTab) ? undefined : LEGACY[rawTab])
  const tab: TabKey = legacy?.tab ?? (TAB_KEYS.includes(rawTab) ? (rawTab as TabKey) : 'overview')
  const sub = legacy ? legacy.sub ?? '' : rawSub

  const go = (t: TabKey, s?: string) => {
    const next = new URLSearchParams(params)
    next.set('tab', t)
    // A sub-section belongs to the tab it came from.
    if (s) next.set('sub', s)
    else next.delete('sub')
    setParams(next, { replace: false })
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }
  const setSub = (s: string) => {
    const next = new URLSearchParams(params)
    next.set('sub', s)
    setParams(next, { replace: true })
  }
  return { tab, sub, go, setSub }
}

/* ── Resources ────────────────────────────────────────────────────── */

export function useServer(id: string | undefined) {
  return useQuery<ServerItem>({
    queryKey: ['servers', id],
    queryFn: async () => (await api.get(`/servers/${id}`)).data,
    refetchInterval: 15_000,
    enabled: Boolean(id),
  })
}

export function useLiveMetrics(id: string | undefined): ServerLiveMetrics {
  const { data } = useQuery<{ servers: Record<string, ServerLiveMetrics> }>({
    queryKey: ['servers', 'latest-metrics'],
    queryFn: async () => (await api.get('/servers/latest-metrics')).data,
    refetchInterval: 15_000,
  })
  return (id && data?.servers?.[id]) || {}
}

export function useProcesses(id: string | undefined) {
  return useQuery<{ items: ServerProcess[]; mem_total_bytes?: number }>({
    queryKey: ['servers', id, 'processes'],
    queryFn: async () => (await api.get(`/servers/${id}/processes`)).data,
    refetchInterval: 30_000,
    enabled: Boolean(id),
  })
}

export function useServices(id: string | undefined) {
  return useQuery<{ items: ServerService[] }>({
    queryKey: ['servers', id, 'services'],
    queryFn: async () => (await api.get(`/servers/${id}/services`)).data,
    refetchInterval: 30_000,
    enabled: Boolean(id),
  })
}

export function useFilesystems(id: string | undefined) {
  return useQuery<{ items: ServerFilesystem[] }>({
    queryKey: ['servers', id, 'filesystems'],
    queryFn: async () => (await api.get(`/servers/${id}/filesystems`)).data,
    refetchInterval: 60_000,
    enabled: Boolean(id),
  })
}

export function useOpenAlertCount(id: string | undefined) {
  const { data } = useQuery<{ meta: { total: number } }>({
    queryKey: ['servers', id, 'alerts', 'badge'],
    queryFn: async () =>
      (await api.get('/alerts', { params: { server_id: id, status: 'active', limit: 1 } })).data,
    refetchInterval: 30_000,
    enabled: Boolean(id),
  })
  return data?.meta?.total ?? 0
}

export function useCompliance(id: string | undefined) {
  return useQuery<{ summary: ComplianceSummary }>({
    queryKey: ['servers', id, 'compliance'],
    queryFn: async () => (await api.get(`/servers/${id}/compliance`)).data,
    refetchInterval: 60_000,
    enabled: Boolean(id),
  })
}

/** The agent record, with update status, package and monitoring profile. */
export function useServerAgent(id: string | undefined, enabled = true) {
  return useQuery<AgentItem | null>({
    queryKey: ['servers', id, 'agent'],
    queryFn: async () => (await api.get(`/servers/${id}/agent`)).data,
    refetchInterval: 15_000,
    enabled: Boolean(id) && enabled,
  })
}

/* ── Detailed metrics ─────────────────────────────────────────────── */

export interface CpuPoint { ts: string; total: number | null; user: number | null; system: number | null; queue: number | null; peak: number | null }
export interface MemoryPoint {
  ts: string; total: number | null; used: number | null; available: number | null; cached: number | null
  committed: number | null; swap_total: number | null; swap_used: number | null; used_pct: number | null
}
export interface DiskPoint {
  ts: string; read_bps: number | null; write_bps: number | null; read_iops: number | null; write_iops: number | null
  util_pct: number | null; read_ms: number | null; write_ms: number | null; queue: number | null
}
export interface FsPoint { ts: string; used_bytes: number | null; total_bytes: number | null; used_pct: number | null }
export interface IfPoint {
  ts: string; rx_bytes_ps: number | null; tx_bytes_ps: number | null; rx_pps: number | null; tx_pps: number | null
  errors_ps: number | null; drops_ps: number | null; up: boolean
}
export interface MetricDetail {
  server_id: string
  from: string
  to: string
  bucket_s: number
  cpu: CpuPoint[]
  cores: { series: { ts: string; values: (number | null)[] }[]; latest: (number | null)[] }
  memory: MemoryPoint[]
  disks: { device: string; points: DiskPoint[] }[]
  filesystems: {
    mount: string; fs_type: string; points: FsPoint[]
    growth_bytes_per_day?: number | null; days_until_full?: number | null
  }[]
  interfaces: { name: string; points: IfPoint[] }[]
}

export function useMetricDetail(id: string | undefined, fromISO: string, toISO: string, live: boolean) {
  return useQuery<MetricDetail>({
    queryKey: ['servers', id, 'metric-detail', fromISO, toISO],
    queryFn: async () => (await api.get(`/servers/${id}/metrics/detail`, { params: { from: fromISO, to: toISO } })).data,
    // Rolling windows refresh; a fixed custom window does not change.
    refetchInterval: live ? 60_000 : false,
    placeholderData: (previous) => previous,
    enabled: Boolean(id),
  })
}

/** The last hour, for header sparklines and the overview. */
export function useRecentDetail(id: string | undefined) {
  // Minute-bucketed so the query key is stable between renders.
  const to = new Date(Math.floor(Date.now() / 60_000) * 60_000)
  const from = new Date(to.getTime() - 3_600_000)
  return useMetricDetail(id, from.toISOString(), to.toISOString(), true)
}

/** Points to chart rows keyed by epoch ms. */
export function toRows<P extends { ts: string }>(points: P[], pick: (p: P) => Record<string, number | null>) {
  return points.map((p) => ({ ts: Date.parse(p.ts), ...pick(p) }))
}

/** Interfaces that carry real traffic (not loopback, tunnels or filter drivers). */
export function isPseudoInterface(name: string): boolean {
  return /loopback|isatap|teredo|6to4|pseudo|wfp|qos packet scheduler|lightweight filter|npcap|vethernet \(default/i.test(name)
}
