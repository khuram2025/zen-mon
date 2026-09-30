/* Shared definitions for the Services list page (Services review B20–B23,
   UI 1/8/9 and the list features). Types for fields added by the services
   review contract are optional: an older appliance omits them and the list
   degrades (client-side staleness, page-local ordering). */
import { hasPermission, type User } from '@/stores/auth'
import type { ServiceCheck, ServiceCheckSummary } from '@/types'

export type ListRow = ServiceCheck & {
  stale?: boolean
  evidence_age_sec?: number | null
  owner_team?: string | null
  runbook_url?: string | null
}

export type ListMeta = { total: number; skip: number; limit: number; server_time?: string }
export type ListResponse = { data: ListRow[]; meta: ListMeta }

export type SummaryExt = ServiceCheckSummary & {
  paused?: number
  stale?: number
  in_maintenance?: number
  server_time?: string
}

export type DailyRow = { date: string; uptime_pct: number | null; sample_count: number }
export type DailyUptimeResponse = { days: number; checks: Record<string, DailyRow[]> }

/* ── Status sets (B20) ─────────────────────────────────────────────────────
   One definition drives both the KPI count and the filter it applies. */
export const STATUS_SETS = {
  up: ['up'],
  down: ['down'],
  warning: ['warning', 'degraded'],
  unknown: ['unknown'],
} as const
export type StatusSetKey = keyof typeof STATUS_SETS

export function parseStatusSet(raw: string): string[] {
  return raw.split(',').map((s) => s.trim()).filter(Boolean)
}

export function statusParamFor(key: StatusSetKey): string {
  return STATUS_SETS[key].join(',')
}

/** Which named set (if any) the current `status` URL value corresponds to. */
export function statusSetKeyOf(raw: string): StatusSetKey | null {
  const cur = [...parseStatusSet(raw)].sort().join(',')
  if (!cur) return null
  for (const k of Object.keys(STATUS_SETS) as StatusSetKey[]) {
    if ([...STATUS_SETS[k]].sort().join(',') === cur) return k
  }
  return null
}

/* ── Levels ─────────────────────────────────────────────────────────────── */
export const LEVEL_META: Record<number, { short: string; label: string; hint: string }> = {
  1: { short: 'L1', label: 'L1 · Reachability', hint: 'Host or port reachability (ICMP, TCP)' },
  2: { short: 'L2', label: 'L2 · Request', hint: 'One request or protocol check (HTTP, TLS, DNS)' },
  3: { short: 'L3', label: 'L3 · Journey', hint: 'Multi-step HTTP user journey' },
}

/* ── Sorting (B21) ──────────────────────────────────────────────────────── */
export const SORT_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'status', label: 'Triage (failing first)' },
  { value: 'name', label: 'Name A–Z' },
  { value: '-name', label: 'Name Z–A' },
  { value: 'last_check', label: 'Oldest evidence first' },
  { value: 'type', label: 'Type' },
]
export const DEFAULT_SORT = 'status'
export const PAGE_SIZES = [20, 50, 100] as const

/* ── Freshness (UI 1) ───────────────────────────────────────────────────── */
export function parseUtc(iso: string | null | undefined): number | null {
  if (!iso) return null
  let s = iso
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) s = s.replace(' ', 'T') + 'Z'
  const t = new Date(s).getTime()
  return Number.isFinite(t) ? t : null
}

/** Contract definition, used only when the appliance does not send `stale`. */
export function isStale(c: ListRow, nowMs: number): boolean {
  if (typeof c.stale === 'boolean') return c.stale
  if (!c.enabled) return false
  const last = parseUtc(c.last_check_at)
  if (last == null) return true
  const interval = c.status === 'down' ? (c.down_check_interval || 30) : (c.check_interval || 60)
  const graceSec = 3 * interval + (c.timeout || 10) + 60
  return (nowMs - last) / 1000 > graceSec
}

/** Triage rank matching the contract's `sort=status` order. */
export function triageRank(c: ListRow, nowMs: number): number {
  if (!c.enabled) return 4
  if (c.status === 'down') return 0
  if (c.status === 'warning' || c.status === 'degraded') return 1
  if (c.status === 'unknown' || isStale(c, nowMs)) return 2
  return 3
}

/** Page-local ordering used only when the appliance ignores `sort`. */
export function clientSort(rows: ListRow[], sort: string, nowMs: number): ListRow[] {
  const byName = (a: ListRow, b: ListRow) => a.name.localeCompare(b.name)
  const out = [...rows]
  switch (sort) {
    case '-name': return out.sort((a, b) => byName(b, a))
    case 'type': return out.sort((a, b) => a.check_type.localeCompare(b.check_type) || byName(a, b))
    case 'last_check':
      return out.sort((a, b) => (parseUtc(a.last_check_at) ?? -Infinity) - (parseUtc(b.last_check_at) ?? -Infinity) || byName(a, b))
    case 'status': return out.sort((a, b) => triageRank(a, nowMs) - triageRank(b, nowMs) || byName(a, b))
    default: return out.sort(byName)
  }
}

export function formatExact(iso: string | null | undefined): string {
  const t = parseUtc(iso)
  if (t == null) return 'never'
  return new Date(t).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
  })
}

/* ── Permissions ────────────────────────────────────────────────────────── */
const OPERATOR_ROLES = new Set(['admin', 'owner', 'operator'])

/** Mirrors the API's require_operator_user (role or devices.manage / system.admin). */
export function canOperate(user: User | null): boolean {
  if (!user) return false
  return OPERATOR_ROLES.has(user.role) || hasPermission(user, 'devices.manage')
}

/* ── URL filter state ───────────────────────────────────────────────────── */
/** Params a saved view captures (everything except the page number). */
export const VIEW_PARAM_KEYS = [
  'q', 'type', 'status', 'group', 'tag', 'level', 'enabled', 'stale', 'maint', 'auth', 'sensor', 'sort', 'size',
] as const

/** Keys that narrow the result set (drive "No monitors match" and Clear). */
export const FILTER_PARAM_KEYS = [
  'q', 'type', 'status', 'group', 'tag', 'level', 'enabled', 'stale', 'maint', 'auth', 'sensor',
] as const

/* ── Downloads ──────────────────────────────────────────────────────────── */
export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1_000)
}

export function filenameFromDisposition(header: unknown, fallback: string): string {
  if (typeof header !== 'string') return fallback
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(header)
  if (star) {
    try { return decodeURIComponent(star[1].trim().replace(/^"|"$/g, '')) } catch { /* fall through */ }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header)
  return plain ? plain[1].trim() : fallback
}

/** Error detail from an axios error whose body was requested as a Blob. */
export async function blobErrorMessage(e: any, fallback: string): Promise<string> {
  const data = e?.response?.data
  if (data instanceof Blob) {
    try {
      const text = await data.text()
      const parsed = JSON.parse(text)
      if (typeof parsed?.detail === 'string') return parsed.detail
    } catch { /* ignore */ }
  }
  const d = e?.response?.data?.detail
  if (typeof d === 'string') return d
  return e?.message || fallback
}

/** True when an axios error means "this appliance has no such route". */
export function isRouteMissing(e: any): boolean {
  const s = e?.response?.status
  if (s === 405) return true
  return s === 404 && (e?.response?.data?.detail === 'Not Found' || e?.response?.data?.detail == null)
}

export function browserTimezone(): string {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' } catch { return 'UTC' }
}
