import { regionalInputMs } from '@/lib/regionalTime'
import { periodRange } from '@/features/reports/periods'
import { toZonedInput } from '@/features/reports/viewer/range'
import { regionalTimezone, regionalZoneLabel } from '@/lib/regionalTime'
import { regionalDate } from '@/lib/regionalTime'
import { useEffect, useId, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Clock } from 'lucide-react'
import { isClockCalibrated, serverNow, useClockOffset } from '@/lib/serverClock'

export const TIME_RANGE_OPTIONS = [
  { key: '1h', label: '1h', hours: 1 },
  { key: '24h', label: '24h', hours: 24 },
  { key: '7d', label: '7d', hours: 168 },
  { key: '1M', label: '1M', hours: 720 },
] as const

/**
 * Calendar-anchored presets (configured timezone). Opt-in per page through
 * `TimeRangePicker calendarPresets`; `useTimeRange` resolves them for any
 * page so a shared link keeps its period.
 */
export const CALENDAR_RANGE_OPTIONS = [
  { key: 'month', label: 'This month', title: 'Calendar month to date' },
  { key: 'last_month', label: 'Last month', title: 'Previous complete calendar month' },
  { key: 'quarter', label: 'This quarter', title: 'Calendar quarter to date' },
] as const

export type CalendarRangeKey = typeof CALENDAR_RANGE_OPTIONS[number]['key']

export type TimeRangeKey = typeof TIME_RANGE_OPTIONS[number]['key'] | CalendarRangeKey | 'custom'

function calendarBounds(key: CalendarRangeKey, nowMs: number): { from: Date; to: Date } {
  const period = key === 'month' ? 'month_to_date' : key === 'last_month' ? 'previous_month' : 'quarter_to_date'
  return periodRange(period, regionalTimezone(), 'monday', new Date(nowMs))
}

function isCalendarKey(key: string | null): key is CalendarRangeKey {
  return CALENDAR_RANGE_OPTIONS.some((option) => option.key === key)
}

/** Both bounds present, parseable and ordered (from < to). */
export function isValidRange(from: string | null | undefined, to: string | null | undefined): boolean {
  if (!from || !to) return false
  const a = Date.parse(from)
  const b = Date.parse(to)
  return Number.isFinite(a) && Number.isFinite(b) && a < b
}

/** A date with the month spelled out ("27 Oct 2026, 15:21"), so a day/month mix-up is visible. */
export function longDateTime(ms: number): string {
  return regionalDate(new Date(ms), 'datetime', [], { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/** A datetime-local input value ("2026-10-27T15:21") for an instant, in configured time. */
export function toLocalInput(ms: number): string {
  return Number.isFinite(ms) ? toZonedInput(new Date(ms), regionalTimezone()) : ''
}

export type CustomRangeCheck =
  | { ok: true; fromISO: string; toISO: string; clipped: boolean }
  | { ok: false; field: 'from' | 'to' | 'both'; message: string }

/**
 * Validate the two custom inputs against the appliance clock. A start in the future can hold no data: the inputs open
 * on today's date and show day/month, so changing only the day to "27" asks for the 27th of this month, which may not
 * have happened yet. That used to apply silently and the page then blamed the agent. An end after now is cut to now.
 */
export function checkCustomRange(fromInput: string, toInput: string, nowMs: number): CustomRangeCheck {
  if (!fromInput && !toInput) return { ok: false, field: 'both', message: 'Enter a start and an end time.' }
  if (!fromInput) return { ok: false, field: 'from', message: 'Enter a start time.' }
  if (!toInput) return { ok: false, field: 'to', message: 'Enter an end time.' }
  const from = regionalInputMs(fromInput)
  const to = regionalInputMs(toInput)
  if (Number.isNaN(from)) return { ok: false, field: 'from', message: 'The start time is not a valid date.' }
  if (Number.isNaN(to)) return { ok: false, field: 'to', message: 'The end time is not a valid date.' }
  if (from >= to) return { ok: false, field: 'both', message: 'The end time must be after the start time.' }
  if (from >= nowMs) {
    return { ok: false, field: 'from', message: `The start time (${longDateTime(from)}) is in the future. Choose a time before now.` }
  }
  const end = Math.min(to, nowMs)
  return { ok: true, fromISO: new Date(from).toISOString(), toISO: new Date(end).toISOString(), clipped: end < to }
}

/** The configured timezone shown next to custom inputs, e.g. "Asia/Riyadh (UTC+03:00)". */
export function timezoneLabel(at: number = Date.now()): string {
  return `${regionalTimezone()} (${regionalZoneLabel(at)})`
}

/**
 * Mid-sentence form of a range label ("No events in {…}").
 *
 * Never lowercase the whole label: "Last 1M" (one month) would become
 * "last 1m", which reads as one minute. Only the leading "Last" is folded.
 */
export function rangePhrase(label: string): string {
  return label.startsWith('Last ') ? `the last ${label.slice(5)}` : label
}

export interface TimeRange {
  hours: number
  fromISO: string
  toISO: string
  isCustom: boolean
  /** Active preset key ('1h', '7d', 'month', 'custom', …). */
  key: TimeRangeKey
  /** Human-readable label, e.g. "Last 1h" or "4/28, 12:00 PM → 1:00 PM". */
  label: string
  /**
   * A calendar period whose bounds still come from the uncalibrated browser clock (no appliance
   * time sample yet): a browser hours off would query the wrong month. Data queries wait until it
   * clears (the first API response, or `CALIBRATION_WAIT_MS` without any).
   */
  pending?: boolean
  /** A custom range (e.g. from a shared link) that starts after now: there is no data for it yet. */
  future?: boolean
}

/** How long a calendar range waits for the appliance clock before falling back to the browser's. */
export const CALIBRATION_WAIT_MS = 4_000

/**
 * Hook that reads/writes the active time range from the URL query string.
 *
 *   ?range=1h           → preset
 *   ?range=custom&from=ISO&to=ISO  → custom window
 *
 * Default is 1h when nothing is set.
 */
export function useTimeRange(): {
  range: TimeRange
  rangeIdx: number
  isCustom: boolean
  setPreset: (i: number) => void
  setCustom: (fromISO: string, toISO: string) => void
  setCalendar: (key: CalendarRangeKey) => void
} {
  const [searchParams, setSearchParams] = useSearchParams()
  const key = searchParams.get('range')
  const customFromParam = searchParams.get('from')
  const customToParam = searchParams.get('to')
  // A custom URL range must hold two valid, ordered timestamps; anything else
  // falls back to the default preset instead of scoring a different period (B26).
  const isCustom = key === 'custom' && isValidRange(customFromParam, customToParam)
  const calendarKey = !isCustom && isCalendarKey(key) ? key : null

  const idx = (() => {
    const i = TIME_RANGE_OPTIONS.findIndex((r) => r.key === key)
    return i >= 0 ? i : 0
  })()

  // Preset ranges slide with the appliance clock (B01); bucket to the minute so
  // query keys keyed on fromISO/toISO do not change every render (see useReports.ts).
  const { offsetMs, calibrated } = useClockOffset()
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => setTick((n) => n + 1), 60_000)
    return () => clearInterval(id)
  }, [])
  // Recomputed in the very render where the clock offset or calibration changes (not in an effect
  // afterwards), so no request is built from the previous, uncalibrated "now".
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const nowMs = useMemo(() => Math.floor(serverNow() / 60_000) * 60_000, [tick, offsetMs, calibrated])
  // Calendar bounds are only resolved on the appliance clock: until the first response calibrates
  // it, the range is pending (and NetFlow queries wait) rather than querying the browser's month.
  const [gaveUp, setGaveUp] = useState(false)
  const awaitingClock = !!calendarKey && !calibrated && !isClockCalibrated() && !gaveUp
  useEffect(() => {
    if (!awaitingClock) return
    const id = window.setTimeout(() => setGaveUp(true), CALIBRATION_WAIT_MS)
    return () => clearTimeout(id)
  }, [awaitingClock])

  const fromISO = useMemo(() => {
    if (isCustom) return new Date(customFromParam!).toISOString()
    if (calendarKey) return calendarBounds(calendarKey, nowMs).from.toISOString()
    return new Date(nowMs - TIME_RANGE_OPTIONS[idx].hours * 3_600_000).toISOString()
  }, [isCustom, customFromParam, calendarKey, idx, nowMs])

  const toISO = useMemo(() => {
    if (isCustom) return new Date(customToParam!).toISOString()
    if (calendarKey) return calendarBounds(calendarKey, nowMs).to.toISOString()
    return new Date(nowMs).toISOString()
  }, [isCustom, customToParam, calendarKey, nowMs])
  const hours = isCustom || calendarKey
    ? Math.max(1, Math.round((Date.parse(toISO) - Date.parse(fromISO)) / 3_600_000))
    : TIME_RANGE_OPTIONS[idx].hours
  const label = isCustom
    ? `${regionalDate(new Date(fromISO), 'datetime', [], { dateStyle: 'short', timeStyle: 'short' })} → ${regionalDate(new Date(toISO), 'datetime', [], { dateStyle: 'short', timeStyle: 'short' })}`
    : calendarKey
      ? CALENDAR_RANGE_OPTIONS.find((option) => option.key === calendarKey)!.label
      : `Last ${TIME_RANGE_OPTIONS[idx].label}`
  const activeKey: TimeRangeKey = isCustom ? 'custom' : calendarKey ?? TIME_RANGE_OPTIONS[idx].key

  const setPreset = (i: number) => {
    const next = new URLSearchParams(searchParams)
    next.set('range', TIME_RANGE_OPTIONS[i].key)
    next.delete('from')
    next.delete('to')
    setSearchParams(next, { replace: true })
  }
  const setCalendar = (k: CalendarRangeKey) => {
    const next = new URLSearchParams(searchParams)
    next.set('range', k)
    next.delete('from')
    next.delete('to')
    setSearchParams(next, { replace: true })
  }
  const setCustom = (f: string, t: string) => {
    const next = new URLSearchParams(searchParams)
    next.set('range', 'custom')
    next.set('from', f)
    next.set('to', t)
    setSearchParams(next, { replace: true })
  }

  return {
    range: {
      hours, fromISO, toISO, isCustom, label, key: activeKey, pending: awaitingClock,
      future: isCustom && Date.parse(fromISO) >= nowMs,
    },
    // -1 while a calendar preset is active so no rolling preset shows selected.
    rangeIdx: calendarKey ? -1 : idx,
    isCustom,
    setPreset,
    setCustom,
    setCalendar,
  }
}

interface PickerProps {
  rangeIdx: number
  isCustom: boolean
  customFrom: string
  customTo: string
  onPreset: (i: number) => void
  onCustom: (fromISO: string, toISO: string) => void
  /** Page-specific button labels for rolling presets, e.g. { '1M': 'Last 30 days' }. */
  presetLabels?: Partial<Record<typeof TIME_RANGE_OPTIONS[number]['key'], string>>
  /** Show calendar month/quarter presets; requires `onCalendar`. */
  calendarPresets?: boolean
  activeKey?: TimeRangeKey
  onCalendar?: (key: CalendarRangeKey) => void
}

export function TimeRangePicker({
  rangeIdx, isCustom, customFrom, customTo, onPreset, onCustom,
  presetLabels, calendarPresets, activeKey, onCalendar,
}: PickerProps) {
  const [open, setOpen] = useState(false)
  const uid = useId()
  const toLocal = (iso: string) => toLocalInput(Date.parse(iso))
  const [fromInput, setFromInput] = useState(() => toLocal(customFrom))
  const [toInput, setToInput] = useState(() => toLocal(customTo))
  const [error, setError] = useState<{ field: 'from' | 'to' | 'both'; message: string } | null>(null)
  // The latest selectable minute (appliance clock), refreshed each time the popover opens.
  const [maxInput, setMaxInput] = useState(() => toLocalInput(serverNow()))
  // Re-seed the inputs only when the popover opens; edits survive a failed Apply.
  useEffect(() => {
    if (open) {
      setFromInput(toLocal(customFrom))
      setToInput(toLocal(customTo))
      setMaxInput(toLocalInput(serverNow()))
      setError(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
  // What the inputs mean with the month spelled out: the inputs show day/month or month/day by locale.
  const preview = (() => {
    const a = Date.parse(fromInput)
    const b = Date.parse(toInput)
    return Number.isFinite(a) && Number.isFinite(b) ? `${longDateTime(a)} → ${longDateTime(b)}` : ''
  })()
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open])

  const apply = () => {
    const check = checkCustomRange(fromInput, toInput, serverNow())
    if (!check.ok) { setError({ field: check.field, message: check.message }); return }
    setError(null)
    onCustom(check.fromISO, check.toISO)
    setOpen(false)
  }

  const btn = (active: boolean) =>
    `whitespace-nowrap rounded px-2 py-1 text-xs font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 ${
      active ? 'bg-primary text-on-primary' : 'text-text2 hover:text-text'
    }`
  const errId = `${uid}-err`
  const tzId = `${uid}-tz`

  return (
    <div className="relative">
      <div
        className="inline-flex flex-wrap items-center gap-0.5 rounded-md border border-border bg-surface2/40 p-0.5"
        role="group"
        aria-label="Time range"
      >
        <Clock className="ml-1 mr-0.5 h-3 w-3 text-muted" aria-hidden />
        {TIME_RANGE_OPTIONS.map((r, i) => {
          const active = !isCustom && rangeIdx === i
          return (
            <button
              key={r.key}
              type="button"
              aria-pressed={active}
              onClick={() => { setOpen(false); onPreset(i) }}
              className={btn(active)}
            >
              {presetLabels?.[r.key] ?? r.label}
            </button>
          )
        })}
        {calendarPresets && onCalendar && CALENDAR_RANGE_OPTIONS.map((r) => {
          const active = activeKey === r.key
          return (
            <button
              key={r.key}
              type="button"
              aria-pressed={active}
              title={r.title}
              onClick={() => { setOpen(false); onCalendar(r.key) }}
              className={btn(active)}
            >
              {r.label}
            </button>
          )
        })}
        <button
          type="button"
          aria-pressed={isCustom}
          aria-expanded={open}
          aria-haspopup="dialog"
          onClick={() => setOpen((v) => !v)}
          className={btn(isCustom)}
        >
          Custom
        </button>
      </div>

      {open && (
        <>
          <div className="fixed inset-0 z-20" onClick={() => setOpen(false)} />
          <div
            role="dialog"
            aria-label="Custom time range"
            className="absolute right-0 top-full z-30 mt-1 w-72 rounded-md border border-border bg-surface p-3 shadow-lg"
          >
            <form
              className="space-y-2"
              noValidate
              onSubmit={(e) => { e.preventDefault(); apply() }}
            >
              <div>
                <label htmlFor={`${uid}-from`} className="text-2xs uppercase tracking-wider text-muted">From</label>
                <input
                  id={`${uid}-from`}
                  type="datetime-local"
                  value={fromInput}
                  max={maxInput}
                  required
                  aria-invalid={error?.field === 'from' || error?.field === 'both'}
                  aria-describedby={error ? `${errId} ${tzId}` : tzId}
                  onChange={(e) => setFromInput(e.target.value)}
                  className="mt-1 w-full rounded border border-border bg-transparent px-2 py-1 text-xs text-text aria-[invalid=true]:border-danger"
                />
              </div>
              <div>
                <label htmlFor={`${uid}-to`} className="text-2xs uppercase tracking-wider text-muted">To</label>
                <input
                  id={`${uid}-to`}
                  type="datetime-local"
                  value={toInput}
                  max={maxInput}
                  required
                  aria-invalid={error?.field === 'to' || error?.field === 'both'}
                  aria-describedby={error ? `${errId} ${tzId}` : tzId}
                  onChange={(e) => setToInput(e.target.value)}
                  className="mt-1 w-full rounded border border-border bg-transparent px-2 py-1 text-xs text-text aria-[invalid=true]:border-danger"
                />
              </div>
              {preview && <p className="text-xs font-medium text-text">{preview}</p>}
              <p id={tzId} className="text-2xs text-muted">Times are in {timezoneLabel()}. A range ending after now stops at now.</p>
              {error && <p id={errId} role="alert" className="text-xs text-danger">{error.message}</p>}
              <div className="flex justify-end gap-2 pt-1">
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  className="rounded px-2 py-1 text-xs text-muted hover:text-text"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="rounded bg-primary px-2 py-1 text-xs font-semibold text-on-primary"
                >
                  Apply
                </button>
              </div>
            </form>
          </div>
        </>
      )}
    </div>
  )
}
