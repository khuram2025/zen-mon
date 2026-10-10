// Pure helpers for the Storage tab. Self-contained (no app imports) so
// scripts/test-storage-ui.mjs can exercise them directly.
import type { Consumer, Forecast, MountInfo, Severity, StorageEvent, StorageIssue, DiagnosticLog, Layout } from './types'

export const GIB = 1024 ** 3

/** Binary units, labelled as such (GiB), everywhere on the Storage page. */
export function bytes(n: number, decimals = 1): string {
  if (!n || !Number.isFinite(n) || n <= 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)))
  return `${(n / 1024 ** i).toFixed(i === 0 ? 0 : decimals)} ${units[i]}`
}

export function usageBarTone(pct: number): string {
  return pct >= 95 ? 'bg-danger' : pct >= 85 ? 'bg-warning' : 'bg-primary'
}

export function usageTextTone(pct: number): string {
  return pct >= 95 ? 'text-danger' : pct >= 85 ? 'text-warning' : 'text-text'
}

export const SEVERITY_STYLE: Record<Severity, { box: string; text: string; label: string }> = {
  critical: { box: 'border-danger/40 bg-danger/10', text: 'text-danger', label: 'Critical' },
  warning: { box: 'border-warning/40 bg-warning/10', text: 'text-warning', label: 'Warning' },
  notice: { box: 'border-warning/25 bg-warning/5', text: 'text-warning', label: 'Notice' },
  info: { box: 'border-info/30 bg-info/5', text: 'text-info', label: 'Info' },
}

/**
 * Bytes to allocate for a GiB amount typed by the user: whole extents only,
 * never more than what is free. Returns null for invalid input.
 */
export function allocationBytes(gibText: string, extentBytes: number, freeBytes: number): number | null {
  const gib = Number(gibText)
  if (!gibText.trim() || !Number.isFinite(gib) || gib <= 0) return null
  const extent = extentBytes > 0 ? extentBytes : 4 * 1024 ** 2
  const wanted = Math.floor((gib * GIB) / extent) * extent
  if (wanted <= 0) return null
  return Math.min(wanted, Math.floor(freeBytes / extent) * extent)
}

export interface SpaceSegment {
  id: string
  label: string
  bytes: number
  hint: string | null
}

/**
 * What fills a mount: the known consumers living on it plus "Other" for the
 * remainder of used space (OS, applications, user files). Sorted largest first.
 */
export function spaceSegments(mount: MountInfo, consumers: Consumer[], allMountsShared: boolean): SpaceSegment[] {
  const mine = consumers.filter((c) =>
    allMountsShared || (mount.roles.includes('data') ? c.mount === '/data' : c.mount === '/'),
  )
  const known = mine.reduce((sum, c) => sum + c.bytes, 0)
  const segments: SpaceSegment[] = mine
    .filter((c) => c.bytes > 0)
    .map((c) => ({ id: c.id, label: consumerLabel(c.id), bytes: c.bytes, hint: consumerHint(c.id) }))
  // The remainder is shown, not hidden: the reconciliation gap between what
  // the filesystem reports and what ZenPlus can attribute.
  const other = mount.used_bytes - known
  if (other > 0) {
    segments.push({
      id: 'other',
      label: mount.roles.includes('system')
        ? 'System & other storage'
        : 'Other storage',
      bytes: other,
      hint: 'Remaining used capacity; category measurements may update at different times',
    })
  }
  return segments.sort((a, b) => b.bytes - a.bytes)
}

export function forecastSummary(f: Forecast | undefined | null): { tone: 'ok' | 'warning' | 'critical'; text: string } | null {
  if (!f || f.current_bytes <= 0) return null
  const perDay = `~${bytes(f.daily_bytes)}/day`
  if (f.unbounded_categories.length > 0) {
    const days = f.days_until_full
    return {
      tone: days != null && days < 30 ? 'critical' : 'warning',
      text: `Historical metrics footprint averages ${perDay} and ${f.unbounded_categories.length} categor${f.unbounded_categories.length === 1 ? 'y has' : 'ies have'} no retention limit` +
        (days != null ? ` — estimated free space lasts about ${days} days.` : '.'),
    }
  }
  if (f.fits) {
    return {
      tone: 'ok',
      text: `Historical metrics footprint averages ${perDay} and level off around ${bytes(f.steady_state_bytes)} with current retention — this fits in the free space.`,
    }
  }
  return {
    tone: f.days_until_full != null && f.days_until_full < 30 ? 'critical' : 'warning',
    text: `Historical metrics footprint averages ${perDay} and need about ${bytes(f.growth_remaining_bytes)} more before retention levels them off` +
      (f.days_until_full != null ? ` — estimated free space runs out in about ${f.days_until_full} days.` : '.'),
  }
}

export const EVENT_LABELS: Record<string, string> = {
  auto_purge: 'Automatic purge',
  auto_purge_skipped: 'Automatic purge skipped',
  manual_purge: 'Manual purge',
  retention_applied: 'Retention policy applied',
  backup_created: 'Backup created',
  backup_failed: 'Backup failed',
  backup_deleted: 'Backup deleted',
  restore_started: 'Restore started',
  restore_completed: 'Restore completed',
  restore_failed: 'Restore failed',
  os_cleanup: 'Appliance cleanup',
  disk_added: 'Disk added',
  volume_extended: 'Volume extended',
  space_claimed: 'Disk space claimed',
  disk_evacuation_started: 'Data move started',
  disk_evacuation_aborted: 'Data move aborted',
  disk_removed: 'Disk removed',
  missing_disk_removed: 'Missing disk forgotten',
  watchdog_purge: 'Low-space purge',
  watchdog_reclaim: 'Space reclaimed',
  watchdog_exhausted: 'Nothing left to purge',
  watchdog_insufficient: 'Purge cannot free enough',
  watchdog_application_spared: 'Monitoring data spared',
  watchdog_ingest_paused: 'Ingestion paused',
  watchdog_ingest_resumed: 'Ingestion resumed',
  watchdog_error: 'Protection check failed',
}

/** Never render free-form service errors, commands, paths or table names. */
export function eventSummary(e: StorageEvent): string {
  const d = e.details || {}
  const amount = (n: unknown) => bytes(typeof n === 'number' ? n : 0)
  switch (e.event_type) {
    case 'disk_added': return `${amount(d.pv_size)} added · ${amount(d.added_bytes)} allocated${d.extend_error ? ' · Allocation incomplete; review disks before retrying' : ''}`
    case 'volume_extended': return `${amount(d.added_bytes)} added · capacity ${amount(d.size_after)}`
    case 'space_claimed': return `${amount(d.added_bytes)} ready to allocate`
    case 'disk_evacuation_started': return `${amount(d.bytes_to_move)} moving; keep the disk connected until complete`
    case 'disk_removed': return 'Disk removed from appliance storage'
    case 'watchdog_purge': return `${amount(d.scheduled_bytes)} scheduled for deletion · ${d.reason === 'legacy_cleanup' ? 'Expired appliance logs' : 'Low-space protection'}`
    case 'watchdog_reclaim': return `${amount(d.scheduled_bytes)} scheduled` + (typeof d.measured_free_change_bytes === 'number' ? ` · free space ${d.measured_free_change_bytes >= 0 ? '+' : '−'}${amount(Math.abs(d.measured_free_change_bytes))} measured` : '')
    case 'watchdog_insufficient': return `${amount(d.planned_bytes)} eligible this pass; ${amount(d.need_bytes)} needed. Expand storage or review retention.`
    case 'watchdog_exhausted': return 'No more eligible data can be removed. Expand storage or review retention.'
    case 'watchdog_application_spared': return 'Recent and protected history kept; deleting eligible history would not recover enough space.'
    case 'watchdog_ingest_paused': return 'Agent and application uploads paused until free space recovers.'
    case 'watchdog_ingest_resumed': return 'Free space recovered; uploads resumed.'
    case 'watchdog_error': return 'A storage protection check failed. Contact support if this continues.'
    case 'backup_failed': return 'Backup could not complete. Check capacity and retry; contact support if this continues.'
    case 'restore_failed': return 'Restore could not complete. Contact support before retrying.'
    case 'auto_purge_skipped': return 'No eligible old history to remove within the current policy.'
    case 'auto_purge': return e.freed_bytes ? `${amount(e.freed_bytes)} reclaimed` : 'Retention checked; no space reclaimed.'
    case 'manual_purge': return `${amount(d.scheduled_bytes ?? e.freed_bytes)} scheduled for deletion`
    case 'retention_applied': return 'Retention settings applied to monitoring history.'
    case 'backup_created': return `${d.kind === 'full' ? 'Full' : 'Configuration'} backup · ${amount(d.size_bytes)}`
    case 'os_cleanup': return `${amount(e.freed_bytes)} reclaimed from appliance logs and temporary files`
    default: return 'Storage operation recorded.'
  }
}

export function storageError(e?: any): string {
  const status = e?.response?.status
  if (status === 401) return 'Your session has expired. Sign in again.'
  if (status === 403) return 'Administrator access is required for this operation.'
  if (status === 409) return 'Storage changed or another operation is running. Refresh and review the current state.'
  if (status === 400 || status === 422) return 'The request could not be applied. Check the selected disk, capacity and settings, then refresh before retrying.'
  return 'The operation could not complete. Refresh to check its status before retrying; contact support if this continues.'
}

const CONSUMERS: Record<string, [string, string]> = {
  clickhouse: ['Monitoring history', 'Manage how long monitoring history is retained'],
  clickhouse_diagnostic: ['Appliance logs', 'Older logs expire automatically; see log lifecycle below'],
  clickhouse_inactive: ['Space being reclaimed', 'Temporary retained data is removed automatically after background maintenance'],
  clickhouse_detached: ['Recovery records', 'Retained for recovery; contact support before removing'],
  postgres: ['Settings & inventory', 'Devices, users, rules and appliance settings'],
  backups: ['Backups', 'Manage saved backups in the Backups tab'],
  journal: ['Service logs', 'Size-limited logs used for support'],
  os_logs: ['System logs', 'Logs rotate automatically by size'],
  apt_cache: ['Update cache', 'Downloaded update files that can be cleared'],
}
export const consumerLabel = (id: string) => CONSUMERS[id]?.[0] || 'Other storage'
export const consumerHint = (id: string) => CONSUMERS[id]?.[1] || null
export function diskName(layout: Layout, path: string): string {
  const index = layout.disks.findIndex(d => d.path === path || d.pvs.some(p => p.path === path))
  return index >= 0 ? `Disk ${index + 1}` : 'Selected disk'
}
export const diskStatus: Record<string, string> = {
  system: 'System disk', pool: 'In storage pool', unused: 'Available', needs_wipe: 'Contains data',
  foreign: 'Used by another system', in_use: 'In use', read_only: 'Read only',
}
export function issueText(issue: StorageIssue): [string, string] {
  const messages: Record<string, [string, string]> = {
    helper_unavailable: ['Disk management unavailable', 'Refresh and contact support if this continues.'],
    pv_missing: ['A disk containing data is missing', 'Reconnect the disk, then rescan. Affected data may be unavailable.'],
    pv_missing_empty: ['An empty disk is missing', 'Review the disk list to remove its registration.'],
    usage_critical: ['Storage is critically low', 'Free space or expand capacity now.'],
    usage_high: ['Storage is running low', 'Review retention or expand capacity soon.'],
    usage_elevated: ['Plan for more capacity', 'Review your retention needs and available disk capacity.'],
    pvmove: ['Data is moving between disks', 'Keep all disks connected until the move completes.'],
    claimable: ['Additional disk capacity detected', 'Review Disks & capacity to make the new space available.'],
    unallocated: ['Capacity is ready to allocate', 'Allocate it now or keep it in reserve.'],
    new_disk: ['A disk is available to add', 'Review its identity and contents before adding it.'],
    clickhouse_drift: ['Storage configuration needs attention', 'Contact support to verify the appliance storage configuration.'],
    watchdog_missing: ['Automatic protection is unavailable', 'Contact support to restore storage protection.'],
    watchdog_stale: ['Storage protection has not reported recently', 'Refresh; contact support if this continues.'],
    ingest_paused: ['Some uploads are paused to protect storage', 'Agent and application uploads resume when free space recovers.'],
    purge_insufficient: ['Cleanup cannot recover enough space yet', 'Expand storage or review retention.'],
    purge_exhausted: ['No more eligible history can be removed', 'Expand storage or review retention; protected records are retained.'],
    fast_growth: ['Storage is filling quickly', 'Automatic protection is removing eligible logs. Review capacity and retention.'],
    watchdog_error: ['Storage protection needs attention', 'A recent check failed. Contact support if this continues.'],
  }
  return messages[issue.code] || ['Storage needs attention', 'Review capacity and retention; contact support if this continues.']
}
export function logGroups(logs: DiagnosticLog[]) {
  const groups = new Map<string, { label: string; bytes: number; policy: string }>()
  for (const l of logs) {
    if (!(l.bytes > 0)) continue
    const label = l.evidence ? 'Recovery records' : l.legacy ? 'Retained appliance logs' : l.protected ? 'Incident logs' : 'Current appliance logs'
    const expiry = l.expires_after ? new Date(l.expires_after) : null
    const validExpiry = expiry && Number.isFinite(expiry.getTime())
    const policy = l.evidence ? 'Kept for support review'
      : l.legacy ? (validExpiry ? `Eligible from ${expiry!.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' })} (UTC)` : `${l.retention_days} days after the last entry`)
      : l.ttl_active ? `Rolling ${l.retention_days}-day retention` : 'Retention activates when new entries arrive'
    const key = `${label}:${policy}:${l.protected}`
    const old = groups.get(key)
    groups.set(key, { label, bytes: (old?.bytes || 0) + l.bytes, policy: policy + (l.legacy && l.protected ? ' · protected during low space' : '') })
  }
  return [...groups.values()]
}

export interface GroupedEvent {
  event: StorageEvent
  count: number
  oldest: string
}

/** Collapse runs of identical consecutive events (same type and summary). */
export function groupEvents(events: StorageEvent[]): GroupedEvent[] {
  const out: GroupedEvent[] = []
  for (const e of events) {
    const last = out[out.length - 1]
    if (last && last.event.event_type === e.event_type && last.event.actor === e.actor &&
        eventSummary(last.event) === eventSummary(e) && !e.freed_bytes && !last.event.freed_bytes) {
      last.count += 1
      last.oldest = e.created_at
    } else {
      out.push({ event: e, count: 1, oldest: e.created_at })
    }
  }
  return out
}
