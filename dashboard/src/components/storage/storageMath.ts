// Pure helpers for the Storage tab. Self-contained (no app imports) so
// scripts/test-storage-ui.mjs can exercise them directly.
import type { Consumer, Forecast, MountInfo, Severity, StorageEvent } from './types'

export const GIB = 1024 ** 3

export function bytes(n: number, decimals = 1): string {
  if (!n || !Number.isFinite(n) || n <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
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
    .map((c) => ({ id: c.id, label: c.label, bytes: c.bytes, hint: c.hint }))
  const other = mount.used_bytes - known
  if (other > 0) {
    segments.push({
      id: 'other',
      label: mount.roles.includes('system') ? 'Operating system, applications & other files' : 'Other files',
      bytes: other,
      hint: null,
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
      text: `Metrics grow ${perDay} and ${f.unbounded_categories.length} categor${f.unbounded_categories.length === 1 ? 'y has' : 'ies have'} no retention limit` +
        (days != null ? ` — free space lasts about ${days} days.` : '.'),
    }
  }
  if (f.fits) {
    return {
      tone: 'ok',
      text: `Metrics grow ${perDay} and level off around ${bytes(f.steady_state_bytes)} with current retention — this fits in the free space.`,
    }
  }
  return {
    tone: f.days_until_full != null && f.days_until_full < 30 ? 'critical' : 'warning',
    text: `Metrics grow ${perDay} and need about ${bytes(f.growth_remaining_bytes)} more before retention levels them off` +
      (f.days_until_full != null ? ` — free space runs out in about ${f.days_until_full} days.` : '.'),
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
  os_cleanup: 'OS cleanup',
  disk_added: 'Disk added',
  volume_extended: 'Volume extended',
  space_claimed: 'Disk space claimed',
  disk_evacuation_started: 'Data move started',
  disk_evacuation_aborted: 'Data move aborted',
  disk_removed: 'Disk removed',
  missing_disk_removed: 'Missing disk forgotten',
}

export function eventSummary(e: StorageEvent): string {
  const d = e.details || {}
  const parts: string[] = []
  switch (e.event_type) {
    case 'auto_purge':
      if (Array.isArray(d.dropped_partitions)) parts.push(`${d.dropped_partitions.length} partitions dropped`)
      if (d.usage_before_pct != null && d.usage_after_pct != null) parts.push(`${d.usage_before_pct}% → ${d.usage_after_pct}%`)
      else if (d.usage_percent != null) parts.push(`at ${d.usage_percent}% usage`)
      if (d.note) parts.push(String(d.note))
      break
    case 'auto_purge_skipped':
      if (d.usage_percent != null) parts.push(`at ${d.usage_percent}% usage`)
      if (d.note) parts.push(String(d.note))
      break
    case 'manual_purge':
      if (d.older_than_days) parts.push(`older than ${d.older_than_days} days`)
      if (Array.isArray(d.dropped_partitions)) parts.push(`${d.dropped_partitions.length} partitions`)
      break
    case 'retention_applied':
      if (Array.isArray(d.changes)) parts.push(`${d.changes.length} table TTL(s) changed`)
      break
    case 'disk_added':
      parts.push(`${d.disk}${d.pv_size ? ` (${bytes(d.pv_size)})` : ''}`)
      if (d.added_bytes) parts.push(`${bytes(d.added_bytes)} allocated`)
      else if (d.allocate === 'none') parts.push('kept unallocated')
      if (d.extend_error) parts.push(`allocation failed: ${d.extend_error}`)
      break
    case 'volume_extended':
      if (d.added_bytes) parts.push(`+${bytes(d.added_bytes)}`)
      if (d.mount) parts.push(String(d.mount))
      if (d.size_after) parts.push(`now ${bytes(d.size_after)}`)
      break
    case 'space_claimed':
      parts.push(`${d.pv}${d.added_bytes ? ` +${bytes(d.added_bytes)}` : ''}`)
      break
    case 'disk_evacuation_started':
      parts.push(`${d.pv}${d.bytes_to_move ? ` (${bytes(d.bytes_to_move)})` : ''}`)
      break
    case 'disk_removed':
      if (d.pv) parts.push(String(d.pv))
      break
    case 'backup_created':
      if (d.kind) parts.push(d.kind === 'full' ? 'full' : 'configuration')
      if (d.size_bytes) parts.push(bytes(d.size_bytes))
      break
    case 'backup_failed':
    case 'restore_failed':
      if (d.error) parts.push(String(d.error))
      break
  }
  return parts.join(' · ')
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
