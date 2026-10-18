import type { ComponentType } from 'react'

/* The Events tab's model: one range-aware timeline merged from alerts,
 * confirmed status transitions, flap episodes, SNMP traps, an inferred reboot
 * and maintenance windows. Pure functions only (no React, no API), so the
 * merging, filtering, URL state and mini-chart bucketing are unit tested by
 * scripts/test-device-events.mjs. */

export type EventKind = 'alert' | 'status' | 'flapping' | 'trap' | 'maintenance' | 'reboot'
export type EventSeverity = 'critical' | 'warning' | 'info' | 'success'

export const EVENT_KINDS: EventKind[] = ['alert', 'status', 'flapping', 'trap', 'maintenance', 'reboot']
export const EVENT_SEVERITIES: EventSeverity[] = ['critical', 'warning', 'info', 'success']

export const KIND_LABEL: Record<EventKind, string> = {
  alert: 'Alerts', status: 'Status', flapping: 'Flapping', trap: 'Traps', maintenance: 'Maintenance', reboot: 'Reboots',
}
export const KIND_SINGULAR: Record<EventKind, string> = {
  alert: 'Alert', status: 'Status change', flapping: 'Flapping', trap: 'SNMP trap', maintenance: 'Maintenance', reboot: 'Reboot',
}
export const SEVERITY_LABEL: Record<EventSeverity, string> = {
  critical: 'Critical', warning: 'Warning', info: 'Info', success: 'Recovered',
}

/** Rows shown per page, and added by "Show 50 more". */
export const EVENTS_PAGE_SIZE = 50

export type TimelineEvent = {
  id: string
  kind: EventKind
  severity: EventSeverity
  /** Event time as received (UTC). */
  timestamp: string
  /** Parsed `timestamp`, epoch ms. */
  ts: number
  title: string
  subtitle?: string
  /** In-app link (alerts open their detail page). */
  href?: string
  /** Alert workflow state. */
  status?: 'active' | 'acknowledged' | 'resolved'
  /** Short qualifier shown as a chip: "In progress", "Upcoming". */
  tag?: string
  /** Maintenance windows: their end. */
  endsAt?: string
  icon?: ComponentType<{ className?: string }>
  /** Alerts: what makes two alerts repeats of one condition (see alertGroupKey). */
  group?: { key: string; ruleId?: string; ruleName?: string; metadata?: AlertGroupMeta | null }
}

/** Backend timestamps are UTC; a naive value (no offset) is UTC, not local time (same rule as format.parseTime). */
export function eventTime(value: string | number | null | undefined): number | null {
  if (value == null || value === '') return null
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  let s = String(value)
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) s = s.replace(' ', 'T') + 'Z'
  const ms = Date.parse(s)
  return Number.isFinite(ms) ? ms : null
}

/* ── Sources → timeline rows ────────────────────────────────────────────── */

/** Legacy alert rows stored the rendered SMS template: drop the "[ZenPlus CRITICAL] " transport prefix. */
export function stripAlertPrefix(message: string | null | undefined): string {
  return (message || '').replace(/^\s*\[ZenPlus\s+[A-Za-z_-]+\]\s*/, '').trim()
}

/** An alert's title on its own device's page: the triggering condition without the repeated hostname and IP
 *  ("Packet loss 33% (limit 10%)"); the same rules as the summary API's device_insights.alert_title.
 *  Status alerts raised before the condition was recorded ("<rule>: <host> (<ip>) is DEGRADED") become
 *  "<rule> · Degraded". */
export function deviceAlertTitle(message: string | null | undefined, condition?: string | null, hostname?: string | null, ip?: string | null): string {
  if (condition && condition.trim()) return condition.trim()
  const text = stripAlertPrefix(message)
  const legacy = /^(.+?):\s+(.+?)\s+(?:is\s+([A-Z]+)|recovered\s+—\s+UP)\s*$/.exec(text)
  if (legacy && hostname && legacy[2].split(' (')[0] === hostname) {
    const status = legacy[3] ? legacy[3][0] + legacy[3].slice(1).toLowerCase() : 'Recovered'
    return `${legacy[1]} · ${status}`
  }
  if (hostname) {
    for (const suffix of [` — ${hostname} (${ip})`, ` — ${hostname}`]) if (text.endsWith(suffix)) return text.slice(0, -suffix.length)
  }
  return text
}

export function alertSeverity(severity: string | null | undefined): EventSeverity {
  const s = (severity || '').toLowerCase()
  if (s === 'critical' || s === 'high' || s === 'emergency' || s === 'alert') return 'critical'
  if (s === 'warning' || s === 'major' || s === 'minor' || s === 'medium' || s === 'error') return 'warning'
  return 'info'
}

export type AlertRow = {
  id: string
  status?: string | null
  severity?: string | null
  message?: string | null
  rule_name?: string | null
  triggered_at?: string | null
  acknowledged_at?: string | null
  resolved_at?: string | null
  rule_id?: string | null
  metadata?: ({ condition?: string | null } & AlertGroupMeta) | null
}

export function alertEvents(alerts: AlertRow[], device?: { hostname?: string | null; ip_address?: string | null }): TimelineEvent[] {
  return alerts.flatMap((a): TimelineEvent[] => {
    const ts = eventTime(a.triggered_at)
    if (ts == null || !a.id) return []
    const status = a.status === 'active' || a.status === 'acknowledged' || a.status === 'resolved' ? a.status : undefined
    const message = deviceAlertTitle(a.message, a.metadata?.condition, device?.hostname, device?.ip_address)
    const title = message || a.rule_name || 'Alert'
    return [{
      id: `alert-${a.id}`,
      kind: 'alert',
      severity: alertSeverity(a.severity),
      timestamp: String(a.triggered_at),
      ts,
      title,
      subtitle: a.rule_name && a.rule_name !== title ? `Rule: ${a.rule_name}` : undefined,
      href: `/alerts/${a.id}`,
      status,
      ...groupOf(a),
    }]
  })
}

function groupOf(a: AlertRow): Pick<TimelineEvent, 'group'> {
  const key = alertGroupKey(a.rule_id, a.rule_name, a.metadata)
  return key == null ? {} : { group: { key, ruleId: a.rule_id || undefined, ruleName: a.rule_name || undefined, metadata: a.metadata } }
}

/* ── Repeated alerts (field review D-09) ────────────────────────────────── *
 * Presentation only: repeats of one condition from one rule show as one row
 * ("Degraded (packet loss) ×5 in 3 days") that expands to every alert. Alert
 * rows, rules and the confirmed status are untouched. The key and label rules
 * match app.services.device_insights; both replay
 * scripts/dev/device-alert-groups/vectors.json. */

export type AlertGroupMeta = { condition_metric?: string | null; new_status?: string | null; if_index?: string | number | null }

const GROUP_STATUS_LABEL: Record<string, string> = { down: 'Down', degraded: 'Degraded', up: 'Recovered' }
const GROUP_METRIC_LABEL: Record<string, string> = { packet_loss: 'packet loss', rtt: 'latency' }

/** The rule (id, else name), the condition metric (absent on legacy rows), the new status and, for per-interface
 *  rules, the interface. An alert without a rule is never grouped (null). */
export function alertGroupKey(ruleId: string | null | undefined, ruleName: string | null | undefined, metadata: AlertGroupMeta | null | undefined): string | null {
  const meta = metadata && typeof metadata === 'object' ? metadata : {}
  const rule = ruleId ? String(ruleId) : (ruleName || '').trim()
  if (!rule) return null
  const ifIndex = meta.if_index
  return [rule, String(meta.condition_metric || ''), String(meta.new_status || '').toLowerCase(), ifIndex == null ? '' : String(ifIndex)].join('|')
}

/** A group's name without the count: one shared title; differing readings name the status and what was measured
 *  ("Degraded (packet loss)"); otherwise the rule, then the newest title. `titles` are newest first. */
export function alertGroupLabel(titles: string[], ruleName: string | null | undefined, metadata: AlertGroupMeta | null | undefined): string {
  const present = titles.filter(Boolean)
  if (new Set(present).size === 1) return present[0]
  const meta = metadata && typeof metadata === 'object' ? metadata : {}
  const status = GROUP_STATUS_LABEL[String(meta.new_status || '').toLowerCase()]
  if (status) {
    const measured = GROUP_METRIC_LABEL[String(meta.condition_metric || '')]
    return measured ? `${status} (${measured})` : status
  }
  return (ruleName || '').trim() || present[0] || 'Alert'
}

const HOUR = 3_600_000
const DAY = 24 * HOUR

/** How long a group of repeats spans, rounded up: "12 min", "5 hours", "3 days". */
export function spanPhrase(ms: number): string {
  const span = Math.max(0, ms)
  if (span < HOUR) return `${Math.max(1, Math.ceil(span / 60_000))} min`
  if (span < 2 * DAY) { const h = Math.ceil(span / HOUR); return `${h} hour${h === 1 ? '' : 's'}` }
  return `${Math.ceil(span / DAY)} days`
}

/** "×5 in 3 days" */
export function repeatPhrase(count: number, firstMs: number, lastMs: number): string {
  return `×${count} in ${spanPhrase(lastMs - firstMs)}`
}

/** A timeline row standing for two or more repeated alerts. */
export type EventGroup = TimelineEvent & {
  /** The alerts, newest first. */
  members: TimelineEvent[]
  label: string
  firstTs: number
  /** Members still active or acknowledged. */
  open: number
}

export type TimelineItem = TimelineEvent | EventGroup

export function isGroup(item: TimelineItem): item is EventGroup {
  return Array.isArray((item as EventGroup).members)
}

const SEVERITY_RANK: Record<EventSeverity, number> = { critical: 0, warning: 1, info: 2, success: 3 }

/** Collapse repeated alerts in a newest-first list. A group takes its newest member's place, the worst member's
 *  severity and the most urgent workflow state; a "group" of one stays a plain row. Group after filtering, so a
 *  group holds only the alerts the filters show, and page over the result. `grouped: false` returns the list. */
export function groupEvents(events: TimelineEvent[], grouped = true): TimelineItem[] {
  if (!grouped) return events
  const byKey = new Map<string, TimelineEvent[]>()
  for (const e of events) {
    if (e.kind !== 'alert' || !e.group) continue
    const list = byKey.get(e.group.key)
    if (list) list.push(e)
    else byKey.set(e.group.key, [e])
  }
  const out: TimelineItem[] = []
  for (const e of events) {
    const members = e.kind === 'alert' && e.group ? byKey.get(e.group.key) : undefined
    if (!members || members.length < 2) { out.push(e); continue }
    if (members[0] !== e) continue
    const sorted = [...members].sort((a, b) => b.ts - a.ts || a.id.localeCompare(b.id))
    const newest = sorted[0]
    const firstTs = sorted[sorted.length - 1].ts
    const label = alertGroupLabel(sorted.map((m) => m.title), e.group!.ruleName, e.group!.metadata)
    const open = sorted.filter((m) => m.status === 'active' || m.status === 'acknowledged').length
    const status = sorted.some((m) => m.status === 'active') ? 'active'
      : sorted.some((m) => m.status === 'acknowledged') ? 'acknowledged'
        : sorted.every((m) => m.status === 'resolved') ? 'resolved' : undefined
    const severity = sorted.reduce<EventSeverity>((w, m) => (SEVERITY_RANK[m.severity] < SEVERITY_RANK[w] ? m.severity : w), newest.severity)
    out.push({
      id: `group-${e.group!.key}`,
      kind: 'alert',
      severity,
      timestamp: newest.timestamp,
      ts: newest.ts,
      title: `${label} ${repeatPhrase(sorted.length, firstTs, newest.ts)}`,
      subtitle: [
        newest.title !== label ? `Latest: ${newest.title}` : '',
        e.group!.ruleName && e.group!.ruleName !== label ? `Rule: ${e.group!.ruleName}` : '',
        open ? `${open} open` : '',
      ].filter(Boolean).join(' · ') || undefined,
      status,
      members: sorted,
      label,
      firstTs,
      open,
    })
  }
  return out
}

/** Grouping is on unless the URL says `egroup=off`. */
export function groupingEnabled(params: { get(name: string): string | null }): boolean {
  return params.get('egroup') !== 'off'
}

export function writeGrouping(params: URLSearchParams, on: boolean): URLSearchParams {
  const next = new URLSearchParams(params)
  if (on) next.delete('egroup')
  else next.set('egroup', 'off')
  return next
}

/** The device's open alerts from one rule: where an open group of repeats is worked in full. */
export function ruleOpenAlertsHref(deviceId: string, ruleId: string): string {
  return `/alerts?device_id=${encodeURIComponent(deviceId)}&rule_id=${encodeURIComponent(ruleId)}&view=open`
}

export type MaintenanceWindow = { id: string; starts_at: string; ends_at: string; reason?: string | null; scope_label?: string | null; active?: boolean }

/** Active and upcoming maintenance windows (the endpoint returns only those), labelled; not limited to the range. */
export function maintenanceEvents(body: { active?: MaintenanceWindow[]; upcoming?: MaintenanceWindow[] } | null | undefined): TimelineEvent[] {
  const rows = [
    ...(body?.active || []).map((w) => ({ w, active: true })),
    ...(body?.upcoming || []).map((w) => ({ w, active: false })),
  ]
  return rows.flatMap(({ w, active }): TimelineEvent[] => {
    const ts = eventTime(w.starts_at)
    if (ts == null) return []
    return [{
      id: `maint-${w.id}`,
      kind: 'maintenance',
      severity: 'info',
      timestamp: w.starts_at,
      ts,
      title: active ? 'Maintenance in progress' : 'Maintenance scheduled',
      subtitle: [w.reason, w.scope_label ? `Scope: ${w.scope_label}` : '', 'status changes, alerting and SLA impact paused'].filter(Boolean).join(' · '),
      tag: active ? 'In progress' : 'Upcoming',
      endsAt: w.ends_at,
    }]
  })
}

/** Activity rows (traps, status changes, flap boundaries, reboot) from buildActivityEvents. */
export function activityToTimeline(events: Array<{
  id: string; timestamp: string; severity: EventSeverity; title: string; subtitle?: string; kind?: string; icon?: ComponentType<{ className?: string }>
}>): TimelineEvent[] {
  return events.flatMap((e): TimelineEvent[] => {
    const ts = eventTime(e.timestamp)
    const kind = (EVENT_KINDS as string[]).includes(e.kind || '') ? (e.kind as EventKind) : 'status'
    if (ts == null) return []
    return [{ id: e.id, kind, severity: e.severity, timestamp: e.timestamp, ts, title: e.title, subtitle: e.subtitle || undefined, icon: e.icon }]
  })
}

/** Traps come from a "last N hours" endpoint: keep only those inside the selected window. */
export function trapsInRange<T extends { timestamp?: string | null }>(traps: T[], fromMs: number, toMs: number): T[] {
  return traps.filter((t) => {
    const ts = eventTime(t.timestamp)
    return ts != null && ts >= fromMs && ts <= toMs
  })
}

/** All sources into one list, newest first; duplicate ids keep the first. */
export function mergeEvents(...lists: TimelineEvent[][]): TimelineEvent[] {
  const seen = new Set<string>()
  const out: TimelineEvent[] = []
  for (const list of lists) {
    for (const e of list) {
      if (!Number.isFinite(e.ts) || seen.has(e.id)) continue
      seen.add(e.id)
      out.push(e)
    }
  }
  return out.sort((a, b) => b.ts - a.ts || a.id.localeCompare(b.id))
}

/* ── Filters (URL state: etype=alert,trap  esev=critical  eq=text) ──────── */

export type EventFilter = { kinds: EventKind[]; severities: EventSeverity[]; q: string }

export const EMPTY_FILTER: EventFilter = { kinds: [], severities: [], q: '' }

function listParam<T extends string>(raw: string | null, allowed: readonly T[]): T[] {
  if (!raw) return []
  const wanted = new Set(raw.split(',').map((s) => s.trim().toLowerCase()))
  return allowed.filter((v) => wanted.has(v))
}

export function parseEventFilter(params: { get(name: string): string | null }): EventFilter {
  return {
    kinds: listParam(params.get('etype'), EVENT_KINDS),
    severities: listParam(params.get('esev'), EVENT_SEVERITIES),
    q: (params.get('eq') || '').slice(0, 200),
  }
}

/** A copy of `params` with the filter written (empty values removed); other params are kept. */
export function writeEventFilter(params: URLSearchParams, filter: EventFilter): URLSearchParams {
  const next = new URLSearchParams(params)
  const set = (key: string, value: string) => (value ? next.set(key, value) : next.delete(key))
  set('etype', EVENT_KINDS.filter((k) => filter.kinds.includes(k)).join(','))
  set('esev', EVENT_SEVERITIES.filter((s) => filter.severities.includes(s)).join(','))
  set('eq', filter.q.trim() ? filter.q : '')
  return next
}

export function toggleValue<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value]
}

function matchesText(e: TimelineEvent, q: string): boolean {
  const needle = q.trim().toLowerCase()
  if (!needle) return true
  return [e.title, e.subtitle, e.status, e.tag, KIND_SINGULAR[e.kind], SEVERITY_LABEL[e.severity]]
    .some((s) => !!s && s.toLowerCase().includes(needle))
}

export function filterEvents(events: TimelineEvent[], f: EventFilter): TimelineEvent[] {
  return events.filter((e) =>
    (!f.kinds.length || f.kinds.includes(e.kind))
    && (!f.severities.length || f.severities.includes(e.severity))
    && matchesText(e, f.q))
}

/** Chip counts: each chip row counts what the other filters leave, so a count is what selecting it shows. */
export function chipCounts(events: TimelineEvent[], f: EventFilter): { kinds: Record<EventKind, number>; severities: Record<EventSeverity, number>; all: number } {
  const kinds = Object.fromEntries(EVENT_KINDS.map((k) => [k, 0])) as Record<EventKind, number>
  const severities = Object.fromEntries(EVENT_SEVERITIES.map((s) => [s, 0])) as Record<EventSeverity, number>
  let all = 0
  for (const e of filterEvents(events, { ...f, kinds: [] })) { kinds[e.kind]++; all++ }
  for (const e of filterEvents(events, { ...f, severities: [] })) severities[e.severity]++
  return { kinds, severities, all }
}

/* ── Mini chart: events per bucket, stacked by severity ─────────────────── */

export type EventBucket = { start: number; end: number; mid: number } & Record<EventSeverity, number>

const MINUTE = 60_000
const BUCKET_STEPS = [1, 5, 10, 15, 30, 60, 180, 360, 720, 1440, 2880, 10080].map((m) => m * MINUTE)

/** The smallest "round" bucket width giving at most `maxBuckets` buckets across the range. */
export function bucketWidth(fromMs: number, toMs: number, maxBuckets = 60): number {
  const span = Math.max(1, toMs - fromMs)
  return BUCKET_STEPS.find((w) => span / w <= maxBuckets) ?? Math.ceil(span / maxBuckets)
}

/** Counts per bucket from `fromMs`; events outside [from, to] are not counted. */
export function bucketEvents(events: TimelineEvent[], fromMs: number, toMs: number, maxBuckets = 60): EventBucket[] {
  if (!Number.isFinite(fromMs) || !Number.isFinite(toMs) || toMs <= fromMs) return []
  const width = bucketWidth(fromMs, toMs, maxBuckets)
  const count = Math.max(1, Math.ceil((toMs - fromMs) / width))
  const buckets: EventBucket[] = Array.from({ length: count }, (_, i) => {
    const start = fromMs + i * width
    const end = Math.min(toMs, start + width)
    return { start, end, mid: (start + end) / 2, critical: 0, warning: 0, info: 0, success: 0 }
  })
  for (const e of events) {
    if (e.ts < fromMs || e.ts > toMs) continue
    buckets[Math.min(count - 1, Math.floor((e.ts - fromMs) / width))][e.severity]++
  }
  return buckets
}

/** Events whose time falls inside the selected range (maintenance windows may lie outside it). */
export function countInRange(events: TimelineEvent[], fromMs: number, toMs: number): number {
  return events.filter((e) => e.ts >= fromMs && e.ts <= toMs).length
}
