import type { ComponentType } from 'react'
import { Activity, AlertTriangle, CheckCircle2, Info, Network, Power, Radar, Save, Settings as SettingsIcon, Waves } from 'lucide-react'
import {
  FLAP_END_REASON_LABEL, FLAP_KIND_LABEL, episodeSummary, flapHeldNoun, flapKindOf, groupFlapTransitions,
  type FlapEpisode, type FlapPhase,
} from '@/components/devices/deviceStatus'
import { serverNow } from '@/lib/serverClock'
import { formatDuration } from '@/lib/utils'
import { formatAbsolute, parseTime } from './format'
import type { MetricSeriesMap, StatusHistoryEvent } from './types'

/* Device activity: SNMP traps, confirmed status transitions (grouped by flap
 * episode), flapping start/stop and an inferred reboot, newest first. The
 * Summary card shows a few; the Events tab merges these with alerts and
 * maintenance into one timeline. */

export type ActivityEvent = {
  id: string
  timestamp: string
  severity: 'critical' | 'warning' | 'info' | 'success'
  icon: ComponentType<{ className?: string }>
  title: string
  subtitle?: string
  kind?: 'trap' | 'status' | 'flapping' | 'reboot' | 'alert' | 'maintenance'
}

function titleCase(s: string) {
  return s.replace(/\b\w/g, (c) => c.toUpperCase())
}

export function normalizeSeverity(s: string | undefined): 'critical' | 'warning' | 'info' | 'success' {
  if (s === 'critical') return 'critical'
  if (s === 'warning') return 'warning'
  if (s === 'success') return 'success'
  return 'info'
}

export function formatEventTimestamp(value: string): string {
  return formatAbsolute(value, { seconds: true })
}

function iconForTrap(t: any): ComponentType<{ className?: string }> {
  const s = (t.trap_name || t.message || '').toLowerCase()
  if (s.includes('config')) return SettingsIcon
  if (s.includes('interface') || s.includes('link')) return Network
  if (s.includes('backup')) return Save
  if (s.includes('poll') || s.includes('snmp')) return Radar
  if (s.includes('health')) return CheckCircle2
  return Info
}

export function buildActivityEvents(
  traps: any[],
  statusHistory: StatusHistoryEvent[],
  metrics: MetricSeriesMap,
  fromISO: string,
  toISO: string,
  episodes: FlapEpisode[] = [],
  collapse = true,
): ActivityEvent[] {
  const trapEvents = traps.map((t, i): ActivityEvent => ({
    id: `trap-${t.timestamp || i}-${t.trap_oid || t.trap_name || i}`,
    timestamp: t.timestamp,
    severity: normalizeSeverity(t.severity),
    icon: iconForTrap(t),
    kind: 'trap',
    title: t.trap_name || 'SNMP trap',
    subtitle: t.message || t.trap_oid || '',
  }))

  // A change that led to an episode was notified as usual (its class was not flapping yet); one during it is marked
  // only as such: whether it was held depends on its rule (notify_while_flapping) and recoveries follow their trigger,
  // so the hold itself is stated once, per kind, on the episode's start row.
  const flapLabel = (phase: FlapPhase | null) => phase === 'lead-in' ? 'Led to flapping' : phase === 'during' ? 'During flapping' : ''
  const transitionEvent = (e: StatusHistoryEvent, i: number, phase: FlapPhase | null): ActivityEvent => {
    const next = (e.new_status || '').toLowerCase()
    const severity =
      next === 'down' ? 'critical'
      : next === 'degraded' ? 'warning'
      : next === 'up' ? 'success'
      : 'info'
    const title =
      next === 'down' ? 'Device went down'
      : next === 'degraded' ? 'Device degraded'
      : next === 'up' ? 'Device recovered'
      : `Status changed to ${titleCase(next || 'unknown')}`
    const previous = e.old_status ? `${titleCase(e.old_status)} → ${titleCase(e.new_status)}` : titleCase(e.new_status)
    const durationSec = e.duration_sec && e.duration_sec > 0
      ? e.duration_sec
      : inferStatusDurationSec(e, statusHistory)
    const duration = statusDurationLabel(e.old_status, e.new_status, durationSec)
    return {
      id: `status-${e.timestamp || i}-${e.old_status}-${e.new_status}`,
      timestamp: e.timestamp,
      severity,
      icon: next === 'up' ? CheckCircle2 : next === 'down' ? AlertTriangle : Activity,
      kind: 'status',
      title,
      subtitle: [flapLabel(phase), previous, e.reason, duration].filter(Boolean).join(' · '),
    }
  }

  // Transitions inside a flap episode become one row in the widget (`collapse`); the full log keeps each of them,
  // marked: the lead-in changes before the episode's start row, the others between its start and stop rows.
  const nowMs = Date.parse(toISO) || serverNow()
  const statusEvents = groupFlapTransitions(statusHistory, episodes, nowMs, collapse).flatMap((g, i): ActivityEvent[] => {
    if (g.kind === 'transition') return [transitionEvent(g.item, i, g.phase)]
    const summary = episodeSummary(g.episode, g.items, nowMs, (ms) => formatEventTimestamp(new Date(ms).toISOString()))
    return [{ id: `flap-${g.episode.id || g.episode.started_at}`, timestamp: summary.timestamp, severity: 'warning', icon: Waves, kind: 'flapping', title: summary.title, subtitle: summary.subtitle }]
  })
  const boundaryEvents = collapse ? [] : episodes.flatMap((ep): ActivityEvent[] => {
    const kind = flapKindOf(ep.kind)
    const rows: ActivityEvent[] = [{
      id: `flap-start-${ep.id || ep.started_at}`,
      timestamp: ep.started_at,
      severity: 'warning',
      icon: Waves,
      kind: 'flapping',
      title: `Flapping started${kind ? ` (${FLAP_KIND_LABEL[kind]})` : ''}`,
      subtitle: [
        ep.start_changes != null && ep.window_minutes != null ? `${ep.start_changes} status changes within ${ep.window_minutes} min` : '',
        `${flapHeldNoun(kind)} held while flapping`,
      ].filter(Boolean).join(' · '),
    }]
    if (ep.ended_at) rows.push({
      id: `flap-stop-${ep.id || ep.started_at}`,
      timestamp: ep.ended_at,
      severity: 'info',
      icon: Waves,
      kind: 'flapping',
      title: 'Flapping stopped',
      subtitle: [
        ep.end_reason ? FLAP_END_REASON_LABEL[ep.end_reason] ?? ep.end_reason : '',
        ep.status_at_end ? `status ${titleCase(ep.status_at_end)}` : '',
        `${flapHeldNoun(kind)} resumed`,
      ].filter(Boolean).join(' · '),
    })
    return rows
  })

  const reboot = inferRebootEvent(metrics, fromISO, toISO)
  return [...trapEvents, ...statusEvents, ...boundaryEvents, ...(reboot ? [reboot] : [])]
    .filter((e) => parseTime(e.timestamp) != null)
    .sort((a, b) => (parseTime(b.timestamp) as number) - (parseTime(a.timestamp) as number))
}

/** GET /devices/{id}/flap-episodes answers `{items}`; anything else (an older server) is no episodes. */
export function flapEpisodesOf(body: unknown): FlapEpisode[] {
  const items = Array.isArray(body) ? body : (body as { items?: unknown } | null)?.items
  return Array.isArray(items) ? items.filter((e): e is FlapEpisode => !!e && typeof (e as FlapEpisode).started_at === 'string') : []
}

export function flapEpisodesUrl(deviceId: string, fromISO: string, toISO: string, limit = 50): string {
  return `/devices/${deviceId}/flap-episodes?from=${encodeURIComponent(fromISO)}&to=${encodeURIComponent(toISO)}&limit=${limit}`
}

function inferStatusDurationSec(event: StatusHistoryEvent, history: StatusHistoryEvent[]): number | null {
  const oldState = (event.old_status || '').toLowerCase()
  const eventTs = parseTime(event.timestamp) ?? NaN
  if (!oldState || !Number.isFinite(eventTs)) return null

  const ordered = [...history]
    .filter((item) => parseTime(item.timestamp) != null)
    .sort((a, b) => (parseTime(a.timestamp) as number) - (parseTime(b.timestamp) as number))

  const currentIndex = ordered.findIndex((item) =>
    item.timestamp === event.timestamp &&
    item.old_status === event.old_status &&
    item.new_status === event.new_status
  )
  if (currentIndex <= 0) return null

  for (let i = currentIndex - 1; i >= 0; i--) {
    const previous = ordered[i]
    if ((previous.new_status || '').toLowerCase() !== oldState) continue
    const previousTs = parseTime(previous.timestamp) ?? NaN
    const seconds = Math.round((eventTs - previousTs) / 1000)
    return seconds > 0 ? seconds : null
  }
  return null
}

function statusDurationLabel(oldStatus?: string, newStatus?: string, durationSec?: number | null): string {
  if (!durationSec || durationSec <= 0) return ''
  const oldState = (oldStatus || '').toLowerCase()
  const nextState = (newStatus || '').toLowerCase()
  const duration = formatDuration(durationSec)
  if (oldState === 'down' && nextState === 'up') return `Down for ${duration}`
  if (oldState === 'up' && nextState === 'down') return `Was up for ${duration} before outage`
  if (oldState === 'degraded' && nextState === 'up') return `Degraded for ${duration}`
  if (oldState === 'up' && nextState === 'degraded') return `Was healthy for ${duration}`
  return `${titleCase(oldState || 'previous state')} lasted ${duration}`
}

function inferRebootEvent(metrics: MetricSeriesMap, fromISO: string, toISO: string): ActivityEvent | null {
  const uptime = metrics.uptime || metrics.sysUpTime || metrics.sys_uptime
  const points = uptime?.points || []
  if (!points.length) return null

  const latest = points.reduce((best, p) => (p.ts > best.ts ? p : best), points[0])
  if (!latest || !Number.isFinite(latest.ts) || !Number.isFinite(latest.value) || latest.value <= 0) return null

  const rebootTs = latest.ts - latest.value * 1000
  const from = Date.parse(fromISO)
  const to = Date.parse(toISO)
  if (!Number.isFinite(rebootTs) || rebootTs < from || rebootTs > to) return null

  const iso = new Date(rebootTs).toISOString()
  return {
    id: `reboot-${Math.round(rebootTs)}`,
    timestamp: iso,
    severity: 'warning',
    icon: Power,
    kind: 'reboot',
    title: 'Device reboot detected',
    subtitle: `SNMP sysUpTime reset; current uptime ${formatDuration(latest.value)}`,
  }
}

