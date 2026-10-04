/**
 * NetFlow time windows (contract §7 and §10).
 *
 * A window is either relative ("last 24h", resolved by the API against the appliance clock at
 * every request) or fixed (explicit bounds, never shifted). Calendar periods ("last month") are
 * sent as fixed bounds, so the page queries the period its label names instead of a trailing
 * window of similar length. A request never carries `hours` together with custom bounds: the
 * API's preset limit used to reject every custom range over 30 days with a 422 (X-20).
 *
 * Hour-of-day and weekday buckets are evaluated in `tz`, the viewer's display timezone.
 */
import { useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { TIME_RANGE_OPTIONS, useTimeRange, type TimeRange, type TimeRangeKey } from '@/components/TimeRangePicker'
import { serverNow } from '@/lib/serverClock'
import { formatDateTime, isValidTimeZone, zonedDate } from './format'
import type { SavedQueryWindow } from './filters'

export type NfWindowMode = 'relative' | 'fixed'

export interface NfWindow {
  /** Picker key: a rolling preset ('1h' … '1M'), a calendar period, or 'custom'. */
  key: TimeRangeKey
  mode: NfWindowMode
  /** Rolling presets only: the API resolves [now − hours, now) on the appliance clock. */
  hours: number | null
  /**
   * ISO UTC bounds. For a relative window these are this browser's estimate on the calibrated
   * appliance clock — for display and freezing only; the API's own bounds are in `meta.window`.
   */
  from: string
  to: string
  /** IANA zone for hour/day buckets and labels. */
  tz: string
  /** Picker label, e.g. "Last 24h", "Last month", "9/1, 10:00 AM → 9/2, 10:00 AM". */
  label: string
  /**
   * The bounds are not final: a calendar period waiting for the appliance clock (see
   * `TimeRange.pending`). Queries built from this window wait (`windowParams` marks them).
   */
  pending?: boolean
}

/** The window block of the response envelope (`meta.window`, contract §9.3). */
export interface NfMetaWindow {
  from: string
  to: string
  mode: NfWindowMode
  tz: string
  effective_from?: string | null
  effective_to?: string | null
  source?: 'raw' | 'rollup' | 'hybrid' | 'interface_rollup' | 'pg' | null
  granularity_s?: number | null
}

const ROLLING_KEYS = new Set<string>(TIME_RANGE_OPTIONS.map((o) => o.key))

export function isRollingKey(key: string | null | undefined): key is (typeof TIME_RANGE_OPTIONS)[number]['key'] {
  return !!key && ROLLING_KEYS.has(key)
}

/** The viewer's display timezone (browser `Intl`), 'UTC' when the engine does not report one. */
export function displayTimeZone(): string {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
    return isValidTimeZone(tz) ? tz : 'UTC'
  } catch {
    return 'UTC'
  }
}

const MINUTE = 60_000
const HOUR = 3_600_000

/**
 * Calendar periods that run to "now" (this month, this quarter) move their end every minute,
 * which would make every panel a new query each minute. The end is snapped to 5 minutes for
 * spans over two days (the same cadence relative windows of that size poll at); the labels show
 * the resolved end, so the snapped edge is disclosed rather than hidden.
 */
function snapOpenEnd(fromMs: number, toMs: number): number {
  const step = toMs - fromMs > 48 * HOUR ? 5 * MINUTE : MINUTE
  return Math.max(fromMs + MINUTE, Math.floor(toMs / step) * step)
}

/** An `NfWindow` from the shared `useTimeRange` state. */
export function nfWindowFromRange(range: TimeRange, tz: string = displayTimeZone()): NfWindow {
  const zone = isValidTimeZone(tz) ? tz : 'UTC'
  if (isRollingKey(range.key)) {
    return { key: range.key, mode: 'relative', hours: range.hours, from: range.fromISO, to: range.toISO, tz: zone, label: range.label }
  }
  const from = range.fromISO
  let to = range.toISO
  if (range.key === 'month' || range.key === 'quarter') {
    to = new Date(snapOpenEnd(Date.parse(from), Date.parse(to))).toISOString()
  }
  const w: NfWindow = { key: range.key, mode: 'fixed', hours: null, from, to, tz: zone, label: range.label }
  return range.pending ? { ...w, pending: true } : w
}

/**
 * Marks request parameters built from a pending window (a symbol key: never sent, never part of a
 * query key). `useNfQuery` does not run such a request; the final window brings new parameters.
 */
export const WINDOW_PENDING: unique symbol = Symbol('nf.window.pending')

/** Whether parameters (from `windowParams`/`nfParams`) belong to a window that is not final yet. */
export function isPendingParams(params: object | null | undefined): boolean {
  return !!params && (params as { [WINDOW_PENDING]?: boolean })[WINDOW_PENDING] === true
}

/**
 * Window request parameters: relative → `{hours, tz}`, fixed → `{from, to, tz}`. Never both
 * `hours` and bounds.
 */
export function windowParams(w: NfWindow): Record<string, string> {
  if (w.mode === 'relative' && w.hours != null) return { hours: String(w.hours), tz: w.tz }
  const out: Record<string, string> = { from: w.from, to: w.to, tz: w.tz }
  if (w.pending) Object.defineProperty(out, WINDOW_PENDING, { value: true, enumerable: true })
  return out
}

/** The URL form of a window for links: `range=<key>` or `range=custom&from&to`. */
export function windowSearchParams(w: NfWindow | SavedQueryWindow): Record<string, string> {
  if ('key' in w) {
    if (w.key !== 'custom') return { range: w.key }
    return { range: 'custom', from: w.from, to: w.to }
  }
  if (w.mode === 'relative') return { range: w.range }
  return { range: 'custom', from: w.from, to: w.to }
}

/**
 * Pin a window to explicit bounds, for evidence links and fixed saved views. Pass the response's
 * `meta.window` when available: those are the exact bounds of the data on screen. Otherwise a
 * relative window is resolved on the calibrated appliance clock at this moment.
 */
export function freezeWindow(w: NfWindow, meta?: Pick<NfMetaWindow, 'from' | 'to'> | null, now: number = serverNow()): NfWindow {
  let from: string
  let to: string
  if (meta?.from && meta?.to && Date.parse(meta.from) < Date.parse(meta.to)) {
    from = new Date(Date.parse(meta.from)).toISOString()
    to = new Date(Date.parse(meta.to)).toISOString()
  } else if (w.mode === 'relative' && w.hours != null) {
    const end = Math.floor(now / 1000) * 1000
    from = new Date(end - w.hours * HOUR).toISOString()
    to = new Date(end).toISOString()
  } else {
    from = w.from
    to = w.to
  }
  return { key: 'custom', mode: 'fixed', hours: null, from, to, tz: w.tz, label: rangeText(from, to, w.tz) }
}

/** The saved-query window for a page window in the chosen time mode. */
export function savedQueryWindow(w: NfWindow, mode: 'relative' | 'fixed', meta?: Pick<NfMetaWindow, 'from' | 'to'> | null): SavedQueryWindow {
  if (mode === 'relative' && w.key !== 'custom') return { mode: 'relative', range: w.key }
  const frozen = freezeWindow(w, meta)
  return { mode: 'fixed', from: frozen.from, to: frozen.to }
}

/** Whether the window can be saved as relative (re-resolved when the view is opened). */
export function canSaveRelative(w: NfWindow): boolean {
  return w.key !== 'custom'
}

function rangeText(fromIso: string, toIso: string, tz: string): string {
  const sameDay = zonedDate(fromIso, tz) === zonedDate(toIso, tz)
  const withSeconds = new Date(fromIso).getUTCSeconds() !== 0 || new Date(toIso).getUTCSeconds() !== 0
  const a = formatDateTime(fromIso, tz, { zone: 'none', seconds: withSeconds })
  const b = formatDateTime(toIso, tz, { zone: 'none', seconds: withSeconds, timeOnly: sameDay })
  return `${a} → ${b} (${tz})`
}

export interface WindowLabelOptions {
  /** Label the effective (widened/clamped) bounds of a response instead of the requested ones. */
  effective?: boolean
}

/**
 * "2026-09-30 14:00 → 15:00 (Asia/Riyadh)". Accepts a page window (relative windows are prefixed
 * with their preset, "Last 24h · …") or a response's `meta.window`.
 */
export function windowLabel(w: NfWindow | NfMetaWindow, opts: WindowLabelOptions = {}): string {
  const tz = isValidTimeZone(w.tz) ? w.tz : 'UTC'
  let from = w.from
  let to = w.to
  if (opts.effective && 'effective_from' in w) {
    from = w.effective_from || from
    to = w.effective_to || to
  }
  if (!Number.isFinite(Date.parse(from)) || !Number.isFinite(Date.parse(to))) return 'Unknown period'
  const text = rangeText(from, to, tz)
  if ('key' in w && w.key !== 'custom') return `${w.label} · ${text}`
  return text
}

/** True when a response's effective bounds differ from the ones requested (rollup widening, clamps). */
export function hasEffectiveShift(w: NfMetaWindow): boolean {
  const ef = w.effective_from ? Date.parse(w.effective_from) : NaN
  const et = w.effective_to ? Date.parse(w.effective_to) : NaN
  return (Number.isFinite(ef) && ef !== Date.parse(w.from)) || (Number.isFinite(et) && et !== Date.parse(w.to))
}

/**
 * Auto-refresh cadence. Fixed windows never poll (their data does not change); relative windows
 * poll less as they grow, because multi-day windows are multi-second database scans.
 */
export function pollIntervalFor(w: NfWindow | null | undefined): number | false {
  if (!w || w.mode !== 'relative' || w.hours == null) return false
  if (w.hours <= 6) return 15_000
  if (w.hours <= 48) return 60_000
  return 300_000
}

/**
 * The page window from the URL (`range`, `from`, `to`, and an optional `tz` override that must
 * be a valid IANA zone), plus the picker setters.
 */
export function useNfWindow() {
  const tr = useTimeRange()
  const [sp] = useSearchParams()
  const tzParam = sp.get('tz')
  const tz = isValidTimeZone(tzParam) ? tzParam : displayTimeZone()
  const { key, fromISO, toISO, hours, label, isCustom, pending } = tr.range
  // `tr.range` is a new object every render; memoise on its values so query keys stay stable.
  const w = useMemo(
    () => nfWindowFromRange({ key, fromISO, toISO, hours, label, isCustom, pending }, tz),
    [key, fromISO, toISO, hours, label, isCustom, pending, tz],
  )
  return { window: w, ...tr }
}
