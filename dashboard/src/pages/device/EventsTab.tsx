import { useEffect, useMemo, useState, type ComponentType, type ReactNode } from 'react'
import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Info, Layers, Loader2, OctagonAlert, RefreshCw, Search, X } from 'lucide-react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { FlapEpisode } from '@/components/devices/deviceStatus'
import { rangePhrase } from '@/components/TimeRangePicker'
import { api } from '@/lib/api'
import { serverNow } from '@/lib/serverClock'
import { cn, timeAxisTickFormatter, timeTicks } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { buildActivityEvents, flapEpisodesOf, flapEpisodesUrl } from './activity'
import { AvailabilityCard } from './AvailabilityCard'
import { Legend, Panel, SERIES_COLORS } from './charts'
import { formatAbsolute, formatRelative, plural } from './format'
import {
  activityToTimeline, alertEvents, bucketEvents, chipCounts, countInRange, EMPTY_FILTER, EVENT_KINDS, EVENT_SEVERITIES,
  EVENTS_PAGE_SIZE, filterEvents, groupEvents, groupingEnabled, isGroup, KIND_LABEL, KIND_SINGULAR, maintenanceEvents,
  mergeEvents, parseEventFilter, SEVERITY_LABEL, toggleValue, trapsInRange, writeEventFilter, writeGrouping,
  type AlertRow, type EventFilter, type EventGroup, type EventSeverity, type MaintenanceWindow, type TimelineEvent,
} from './eventsModel'
import type { DeviceRange, DeviceRecord, DeviceSummary, MetricSeriesMap, StatusHistoryEvent, TrapRow } from './types'

/* Events — what happened, and when. Availability and confirmed outages for
 * the range, a per-bucket count of events by severity, then one timeline that
 * merges alerts, confirmed status changes, flapping, SNMP traps, an inferred
 * reboot and maintenance windows. Every source loads and fails on its own;
 * a failed source is named, the others stay visible. Filters live in the URL.
 * Repeats of one alert condition from one rule show as one expandable row
 * ("Degraded (packet loss) ×5 in 3 days"); the chart and the chip counts still
 * count every alert. */

const SEVERITY_ICON: Record<EventSeverity, ComponentType<{ className?: string }>> = {
  critical: OctagonAlert, warning: AlertTriangle, info: Info, success: CheckCircle2,
}
const SEVERITY_TONE: Record<EventSeverity, string> = {
  critical: 'bg-danger/15 text-danger', warning: 'bg-warning/15 text-warning', info: 'bg-info/15 text-info', success: 'bg-success/15 text-success',
}
const SEVERITY_COLOR: Record<EventSeverity, string> = {
  critical: SERIES_COLORS.danger, warning: SERIES_COLORS.warning, info: SERIES_COLORS.info, success: SERIES_COLORS.success,
}
const SEVERITY_DOT: Record<EventSeverity, string> = {
  critical: 'bg-danger', warning: 'bg-warning', info: 'bg-info', success: 'bg-success',
}
const ALERT_STATUS_TONE: Record<string, string> = {
  active: 'border-danger/40 text-danger', acknowledged: 'border-warning/40 text-warning', resolved: 'border-border text-muted',
}

const ALERT_LIMIT = 200
const STATUS_LIMIT = 500
const FLAP_LIMIT = 200
const TRAP_LIMIT = 500

type Source = { key: string; label: string; enabled: boolean; query: UseQueryResult<unknown>; truncated?: boolean }

export function EventsTab({ device, summary, range }: { device: DeviceRecord; summary?: DeviceSummary; range: DeviceRange }) {
  const id = device.id
  const snmp = !!device.snmp_enabled
  const canAlerts = useCan()('alerts.view')
  const fromTs = Date.parse(range.fromISO)
  const toTs = Date.parse(range.toISO)
  const fromQ = encodeURIComponent(new Date(fromTs).toISOString())
  const toQ = encodeURIComponent(new Date(toTs).toISOString())
  // Preset ranges follow the clock; a custom range is a fixed window and is not polled.
  const poll = { placeholderData: keepPreviousData, refetchInterval: range.isCustom ? (false as const) : 30_000 }
  const trapHours = Math.min(720, Math.max(1, Math.ceil(range.hours)))
  const metricHours = Math.max(1, Math.round(range.hours))

  const alertsQ = useQuery<{ data: AlertRow[]; meta?: { total?: number } }>({
    queryKey: ['device', id, 'events', 'alerts', range.fromISO, range.toISO],
    queryFn: async () => (await api.get(`/alerts?device_id=${id}&from=${fromQ}&to=${toQ}&limit=${ALERT_LIMIT}`)).data,
    enabled: canAlerts,
    ...poll,
  })
  const statusQ = useQuery<StatusHistoryEvent[]>({
    queryKey: ['device', id, 'events', 'status-history', range.fromISO, range.toISO],
    queryFn: async () => (await api.get(`/devices/${id}/status-history?from=${fromQ}&to=${toQ}&limit=${STATUS_LIMIT}`)).data,
    ...poll,
  })
  const flapsQ = useQuery<{ items: FlapEpisode[]; truncated: boolean }>({
    queryKey: ['device', id, 'events', 'flap-episodes', range.fromISO, range.toISO],
    queryFn: async () => {
      const body = (await api.get(flapEpisodesUrl(id, range.fromISO, range.toISO, FLAP_LIMIT))).data
      return { items: flapEpisodesOf(body), truncated: !!(body as { truncated?: boolean } | null)?.truncated }
    },
    ...poll,
  })
  const trapsQ = useQuery<TrapRow[]>({
    queryKey: ['device', id, 'events', 'traps', trapHours],
    queryFn: async () => (await api.get(`/devices/${id}/traps?hours=${trapHours}&limit=${TRAP_LIMIT}`)).data,
    enabled: snmp,
    ...poll,
  })
  // Same key as the shared SNMP metrics hook, so the Summary tab's samples are reused; only the
  // uptime series is read here (an inferred reboot).
  const metricsQ = useQuery<MetricSeriesMap>({
    queryKey: ['device', id, 'snmp-metrics', metricHours],
    queryFn: async () => (await api.get(`/devices/${id}/snmp-metrics?hours=${metricHours}`)).data,
    enabled: snmp,
    ...poll,
  })
  const maintenanceQ = useQuery<{ active: MaintenanceWindow[]; upcoming: MaintenanceWindow[] }>({
    queryKey: ['device-maintenance', id],
    queryFn: async () => (await api.get(`/devices/${id}/maintenance`)).data,
    ...poll,
  })

  // The traps endpoint answers "the last N hours": keep only those inside the selected window.
  const traps = useMemo(() => trapsQ.data ? trapsInRange(trapsQ.data, fromTs, toTs) : undefined, [trapsQ.data, fromTs, toTs])
  const statusHistory = statusQ.data
  const metrics = metricsQ.data
  const episodes = flapsQ.data?.items
  const events = useMemo(() => {
    // The full log keeps every status change; those made while flapping are marked, between the start and stop rows.
    const activity = buildActivityEvents(traps || [], statusHistory || [], metrics || {}, range.fromISO, range.toISO, episodes || [], false)
    return mergeEvents(
      alertEvents(alertsQ.data?.data || [], device),
      activityToTimeline(activity),
      maintenanceEvents(maintenanceQ.data),
    )
  }, [traps, statusHistory, metrics, range.fromISO, range.toISO, episodes, alertsQ.data, maintenanceQ.data, device])

  const sources: Source[] = [
    { key: 'alerts', label: 'Alerts', enabled: canAlerts, query: alertsQ, truncated: (alertsQ.data?.data?.length ?? 0) >= ALERT_LIMIT },
    { key: 'status', label: 'Status changes', enabled: true, query: statusQ, truncated: (statusQ.data?.length ?? 0) >= STATUS_LIMIT },
    { key: 'flapping', label: 'Flapping episodes', enabled: true, query: flapsQ, truncated: !!flapsQ.data?.truncated },
    { key: 'traps', label: 'Traps', enabled: snmp, query: trapsQ, truncated: (trapsQ.data?.length ?? 0) >= TRAP_LIMIT },
    { key: 'reboot', label: 'Uptime samples (reboot detection)', enabled: snmp, query: metricsQ },
    { key: 'maintenance', label: 'Maintenance windows', enabled: true, query: maintenanceQ },
  ]
  const enabledSources = sources.filter((s) => s.enabled)
  const loading = enabledSources.filter((s) => s.query.isPending)
  const failed = enabledSources.filter((s) => s.query.isError)
  const allLoading = loading.length === enabledSources.length

  // Active alerts that started before the range are not in this window's list; say so instead of hiding them.
  const openInRange = (alertsQ.data?.data || []).filter((a) => a.status === 'active' || a.status === 'acknowledged').length
  const openTotal = summary?.alerts ? summary.alerts.active + summary.alerts.acknowledged : null
  const openBefore = canAlerts && alertsQ.data && openTotal != null ? Math.max(0, openTotal - openInRange) : 0

  return (
    <div className="space-y-4">
      {device.ping_enabled ? <AvailabilityCard device={device} range={range} /> : (
        <Panel title="Availability & outages">
          <p className="text-sm text-muted">Ping monitoring is off for this device, so there is no availability record. Enable ping in the device settings to measure reachability.</p>
        </Panel>
      )}

      <Panel title="Events over time" meta={`${range.label} · by severity`}
        actions={<Legend items={EVENT_SEVERITIES.map((s) => ({ name: SEVERITY_LABEL[s], color: SEVERITY_COLOR[s] }))} />}>
        <EventsChart events={events} fromTs={fromTs} toTs={toTs} rangeHours={range.hours} loading={allLoading && !events.length} />
      </Panel>

      <Panel title="Timeline" meta={`${range.label} · newest first · times in your browser’s timezone`}>
        <SourceNotices loading={loading} failed={failed} truncated={enabledSources.filter((s) => s.truncated)} />
        {openBefore > 0 && (
          <p className="mb-3 rounded-lg border border-border bg-surface2/40 px-3 py-2 text-xs text-muted">
            {plural(openBefore, 'open alert')} started before this range and {openBefore === 1 ? 'is' : 'are'} not listed here.{' '}
            <Link className="text-primary hover:underline" to={`/alerts?device_id=${id}`}>Open the device’s alerts</Link>
          </p>
        )}
        <EventList events={events} range={range} canAlerts={canAlerts} snmp={snmp} loading={allLoading} anyFailed={failed.length > 0} />
      </Panel>
    </div>
  )
}

/* ── Source state ───────────────────────────────────────────────────────── */

function SourceNotices({ loading, failed, truncated }: { loading: Source[]; failed: Source[]; truncated: Source[] }) {
  if (!loading.length && !failed.length && !truncated.length) return null
  return (
    <div className="mb-3 space-y-1.5" aria-live="polite">
      {failed.map((s) => (
        <div key={s.key} role="alert" className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning/5 px-3 py-1.5 text-xs">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning" aria-hidden />
          <span className="flex-1">
            {s.label} could not be loaded{s.query.data !== undefined ? '; showing the last data that loaded, which may be out of date' : '; they are missing from the timeline below'}.
          </span>
          <button type="button" onClick={() => void s.query.refetch()} disabled={s.query.isFetching}
            className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs hover:bg-surface2 disabled:opacity-50">
            {s.query.isFetching ? <Loader2 className="h-3 w-3 animate-spin" aria-hidden /> : <RefreshCw className="h-3 w-3" aria-hidden />}Retry
          </button>
        </div>
      ))}
      {loading.length > 0 && (
        <div className="flex items-center gap-2 text-xs text-muted">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />Loading {loading.map((s) => s.label.toLowerCase()).join(', ')}…
        </div>
      )}
      {truncated.map((s) => (
        <div key={`t-${s.key}`} className="flex items-center gap-2 text-xs text-muted">
          <Info className="h-3.5 w-3.5 shrink-0" aria-hidden />{s.label}: only the most recent are shown; narrow the range to see all of them.
        </div>
      ))}
    </div>
  )
}

/* ── Mini chart ─────────────────────────────────────────────────────────── */

function EventsChart({ events, fromTs, toTs, rangeHours, loading }: { events: TimelineEvent[]; fromTs: number; toTs: number; rangeHours: number; loading: boolean }) {
  const buckets = useMemo(() => bucketEvents(events, fromTs, toTs), [events, fromTs, toTs])
  const ticks = useMemo(() => timeTicks(fromTs, toTs, rangeHours), [fromTs, toTs, rangeHours])
  const tickFormatter = useMemo(() => timeAxisTickFormatter(rangeHours), [rangeHours])
  const inRange = countInRange(events, fromTs, toTs)
  if (loading) return <div className="flex h-28 items-center justify-center rounded-lg border border-dashed border-border text-sm text-muted">Loading events…</div>
  if (!inRange) return <div className="flex h-28 items-center justify-center rounded-lg border border-dashed border-border text-sm text-muted">No events in this period</div>
  const withSeconds = buckets.length > 0 && buckets[0].end - buckets[0].start < 60_000
  return (
    <div className="h-28" role="img" aria-label={`${plural(inRange, 'event')} in the selected range, counted per interval by severity`}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={buckets} margin={{ top: 4, right: 40, bottom: 0, left: 24 }} barCategoryGap={1}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border) / 0.4)" vertical={false} />
          <XAxis dataKey="mid" type="number" scale="time" domain={[fromTs, toTs]} ticks={ticks} interval={0}
            tickFormatter={tickFormatter} tick={{ fontSize: 12, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
          <YAxis allowDecimals={false} width={32} tick={{ fontSize: 12, fill: 'rgb(var(--muted))' }} axisLine={false} tickLine={false} />
          <Tooltip
            cursor={{ fill: 'rgb(var(--surface2))' }}
            contentStyle={{ background: 'rgb(var(--surface))', border: '1px solid rgb(var(--border))', borderRadius: 6, fontSize: 12, color: 'rgb(var(--text))' }}
            labelFormatter={(_, payload) => {
              const b = payload?.[0]?.payload as { start: number; end: number } | undefined
              return b ? `${formatAbsolute(b.start, { seconds: withSeconds })} – ${formatAbsolute(b.end, { seconds: withSeconds })}` : ''
            }}
          />
          {EVENT_SEVERITIES.map((s) => (
            <Bar key={s} dataKey={s} name={SEVERITY_LABEL[s]} stackId="sev" fill={SEVERITY_COLOR[s]} isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

/* ── Timeline ───────────────────────────────────────────────────────────── */

function Chip({ active, onClick, children, label }: { active: boolean; onClick: () => void; children: ReactNode; label?: string }) {
  return (
    <button type="button" aria-pressed={active} aria-label={label} onClick={onClick}
      className={cn('inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary',
        active ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted hover:text-text')}>
      {children}
    </button>
  )
}

function EventList({ events, range, canAlerts, snmp, loading, anyFailed }: {
  events: TimelineEvent[]; range: DeviceRange; canAlerts: boolean; snmp: boolean; loading: boolean; anyFailed: boolean
}) {
  const [params, setParams] = useSearchParams()
  const filter = parseEventFilter(params)
  const [query, setQuery] = useState(filter.q)
  const grouped = groupingEnabled(params)
  const [shown, setShown] = useState(EVENTS_PAGE_SIZE)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const filterKey = `${params.get('etype') || ''}|${params.get('esev') || ''}|${params.get('eq') || ''}|${grouped}|${range.fromISO}|${range.toISO}`
  useEffect(() => { setShown(EVENTS_PAGE_SIZE) }, [filterKey])
  const toggleGroup = (groupId: string) => setExpanded((prev) => {
    const next = new Set(prev)
    if (!next.delete(groupId)) next.add(groupId)
    return next
  })

  const update = (next: EventFilter) => setParams((prev) => writeEventFilter(prev, next), { replace: true })
  // Debounce typing into the URL.
  useEffect(() => {
    if (query === filter.q) return
    const t = window.setTimeout(() => update({ ...filter, q: query }), 250)
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query])

  const filtered = useMemo(() => filterEvents(events, filter), [events, filter.kinds.join(), filter.severities.join(), filter.q]) // eslint-disable-line react-hooks/exhaustive-deps
  const counts = useMemo(() => chipCounts(events, filter), [events, filter.kinds.join(), filter.severities.join(), filter.q]) // eslint-disable-line react-hooks/exhaustive-deps
  // Filters apply to each alert first, so a group holds only the alerts they show; paging counts rows.
  const items = useMemo(() => groupEvents(filtered, grouped), [filtered, grouped])
  const kinds = EVENT_KINDS.filter((k) => (k !== 'alert' || canAlerts) && (k !== 'trap' || snmp) && (k !== 'reboot' || snmp))
  const filtering = filter.kinds.length > 0 || filter.severities.length > 0 || !!filter.q.trim()
  const visible = items.slice(0, shown)
  const repeats = items.length < filtered.length
  const now = serverNow()

  return (
    <div>
      <div className="mb-3 space-y-2">
        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Event type">
          <Chip active={!filter.kinds.length} onClick={() => update({ ...filter, kinds: [] })}>All <span className="tabular-nums">{counts.all}</span></Chip>
          {kinds.map((k) => (
            <Chip key={k} active={filter.kinds.includes(k)} onClick={() => update({ ...filter, kinds: toggleValue(filter.kinds, k) })}>
              {KIND_LABEL[k]} <span className="tabular-nums">{counts.kinds[k]}</span>
            </Chip>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Severity">
            {EVENT_SEVERITIES.map((s) => {
              const Icon = SEVERITY_ICON[s]
              return (
                <Chip key={s} active={filter.severities.includes(s)} onClick={() => update({ ...filter, severities: toggleValue(filter.severities, s) })}>
                  <Icon className={cn('h-3.5 w-3.5', SEVERITY_TONE[s].split(' ')[1])} aria-hidden />{SEVERITY_LABEL[s]} <span className="tabular-nums">{counts.severities[s]}</span>
                </Chip>
              )
            })}
          </div>
          <label className="relative ml-auto flex min-w-[200px] flex-1 items-center sm:max-w-xs">
            <span className="sr-only">Search events</span>
            <Search className="pointer-events-none absolute left-2 h-3.5 w-3.5 text-muted" aria-hidden />
            <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search events"
              className="h-8 w-full rounded-md border border-border bg-surface pl-7 pr-2 text-sm outline-none focus:border-primary" />
          </label>
          <Chip active={grouped}
            onClick={() => setParams((prev) => writeGrouping(prev, !grouped), { replace: true })}>
            <Layers className="h-3.5 w-3.5" aria-hidden />Group repeats
          </Chip>
          {filtering && (
            <button type="button" onClick={() => { setQuery(''); update(EMPTY_FILTER) }}
              className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-muted hover:text-text">
              <X className="h-3.5 w-3.5" aria-hidden />Clear filters
            </button>
          )}
        </div>
      </div>

      {loading && !events.length ? (
        <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" aria-hidden />Loading events…</div>
      ) : !events.length ? (
        <div className="flex flex-col items-center gap-1 py-10 text-center">
          <Info className="h-6 w-6 text-muted" aria-hidden />
          <div className="text-sm font-medium">No events in {rangePhrase(range.label)}{anyFailed ? ' from the sources that loaded' : ''}</div>
          <div className="text-xs text-muted">Alerts, status changes, flapping, traps, reboots and maintenance appear here. Widen the range in the bar above to see earlier events.</div>
        </div>
      ) : !filtered.length ? (
        <div className="flex flex-col items-center gap-2 py-10 text-center">
          <div className="text-sm font-medium">No events match these filters</div>
          <div className="text-xs text-muted">{plural(events.length, 'event')} in {rangePhrase(range.label)} are hidden by the filters.</div>
          <button type="button" onClick={() => { setQuery(''); update(EMPTY_FILTER) }} className="text-xs text-primary hover:underline">Clear filters</button>
        </div>
      ) : (
        <>
          <ol className="divide-y divide-border">
            {visible.map((e) => isGroup(e)
              ? <GroupRow key={e.id} group={e} now={now} open={expanded.has(e.id)} onToggle={() => toggleGroup(e.id)} />
              : <EventRow key={e.id} event={e} now={now} />)}
          </ol>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
            <span>
              {repeats
                ? <>Showing {visible.length.toLocaleString()} of {plural(items.length, 'row')} · {plural(filtered.length, 'event')}, repeated alerts grouped</>
                : <>Showing {visible.length.toLocaleString()} of {plural(filtered.length, 'event')}</>}
            </span>
            {items.length > shown && (
              <button type="button" onClick={() => setShown((n) => n + EVENTS_PAGE_SIZE)}
                className="rounded-md border border-border px-3 py-1.5 text-xs font-medium text-text hover:bg-surface2">
                Show {Math.min(EVENTS_PAGE_SIZE, items.length - shown)} more
              </button>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function EventRow({ event: e, now }: { event: TimelineEvent; now: number }) {
  const Icon = SEVERITY_ICON[e.severity]
  const title = e.href
    ? <Link to={e.href} className="font-medium text-text hover:text-primary hover:underline">{e.title}</Link>
    : <span className="font-medium">{e.title}</span>
  const future = e.ts > now
  return (
    <li className="flex items-start gap-3 py-2.5">
      <span className={cn('mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full', SEVERITY_TONE[e.severity])}>
        <Icon className="h-4 w-4" aria-hidden />
        <span className="sr-only">{SEVERITY_LABEL[e.severity]}</span>
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
          <span className="min-w-0 break-words">{title}</span>
          <span className="rounded border border-border px-1.5 text-xs text-muted">{KIND_SINGULAR[e.kind]}</span>
          {e.status && <span className={cn('rounded border px-1.5 text-xs capitalize', ALERT_STATUS_TONE[e.status])}>{e.status}</span>}
          {e.tag && <span className="rounded border border-info/40 px-1.5 text-xs text-info">{e.tag}</span>}
        </div>
        {(e.subtitle || e.endsAt) && (
          <div className="mt-0.5 break-words text-xs text-muted">
            {[e.subtitle, e.endsAt ? `until ${formatAbsolute(e.endsAt)}` : ''].filter(Boolean).join(' · ')}
          </div>
        )}
      </div>
      <div className="shrink-0 text-right text-xs">
        <div className="tabular-nums">{formatAbsolute(e.timestamp, { seconds: true })}</div>
        <div className="text-muted">{future ? 'starts later' : formatRelative(e.timestamp)}</div>
      </div>
    </li>
  )
}

/** Repeated alerts as one row; expanding lists every alert with its own link. */
const GROUP_MEMBERS_SHOWN = 10

function GroupRow({ group: g, now, open, onToggle }: { group: EventGroup; now: number; open: boolean; onToggle: () => void }) {
  const [all, setAll] = useState(false)
  const Icon = SEVERITY_ICON[g.severity]
  const members = all ? g.members : g.members.slice(0, GROUP_MEMBERS_SHOWN)
  const listId = `${g.id}-alerts`
  return (
    <li className="py-2.5">
      <div className="flex items-start gap-3">
        <span className={cn('mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full', SEVERITY_TONE[g.severity])}>
          <Icon className="h-4 w-4" aria-hidden />
          <span className="sr-only">{SEVERITY_LABEL[g.severity]}</span>
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
            <button type="button" onClick={onToggle} aria-expanded={open} aria-controls={listId}
              className="inline-flex min-w-0 items-center gap-1 text-left font-medium text-text hover:text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
              {open ? <ChevronDown className="h-4 w-4 shrink-0" aria-hidden /> : <ChevronRight className="h-4 w-4 shrink-0" aria-hidden />}
              <span className="break-words">{g.title}</span>
            </button>
            <span className="rounded border border-border px-1.5 text-xs text-muted">{KIND_LABEL.alert}</span>
            {g.status && <span className={cn('rounded border px-1.5 text-xs capitalize', ALERT_STATUS_TONE[g.status])}>{g.status}</span>}
          </div>
          {g.subtitle && <div className="mt-0.5 break-words text-xs text-muted">{g.subtitle}</div>}
        </div>
        <div className="shrink-0 text-right text-xs">
          <div className="tabular-nums">{formatAbsolute(g.timestamp, { seconds: true })}</div>
          <div className="text-muted">latest · {formatRelative(g.timestamp)}</div>
        </div>
      </div>
      {open && (
        <div id={listId} className="ml-10 mt-2 border-l border-border pl-3">
          <ol className="space-y-1.5">
            {members.map((m) => (
              <li key={m.id} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm">
                <span className={cn('h-2 w-2 shrink-0 rounded-full', SEVERITY_DOT[m.severity])} aria-hidden />
                <span className="sr-only">{SEVERITY_LABEL[m.severity]}</span>
                {m.href ? <Link to={m.href} className="min-w-0 break-words text-text hover:text-primary hover:underline">{m.title}</Link>
                  : <span className="min-w-0 break-words">{m.title}</span>}
                {m.status && <span className={cn('rounded border px-1.5 text-xs capitalize', ALERT_STATUS_TONE[m.status])}>{m.status}</span>}
                <span className="ml-auto shrink-0 text-xs tabular-nums text-muted" title={formatRelative(m.timestamp)}>
                  {m.ts > now ? 'later' : formatAbsolute(m.timestamp, { seconds: true })}
                </span>
              </li>
            ))}
          </ol>
          {g.members.length > GROUP_MEMBERS_SHOWN && (
            <button type="button" onClick={() => setAll((v) => !v)} className="mt-1.5 text-xs font-medium text-primary hover:underline">
              {all ? 'Show fewer' : `Show all ${g.members.length}`}
            </button>
          )}
        </div>
      )}
    </li>
  )
}
