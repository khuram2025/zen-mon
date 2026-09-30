import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Clock } from 'lucide-react'

export const TIME_RANGE_OPTIONS = [
  { key: '1h', label: '1h', hours: 1 },
  { key: '24h', label: '24h', hours: 24 },
  { key: '7d', label: '7d', hours: 168 },
  { key: '1M', label: '1M', hours: 720 },
] as const

/**
 * Calendar-anchored presets (browser timezone). Opt-in per page through
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
  const now = new Date(nowMs)
  if (key === 'month') {
    return { from: new Date(now.getFullYear(), now.getMonth(), 1), to: now }
  }
  if (key === 'last_month') {
    return {
      from: new Date(now.getFullYear(), now.getMonth() - 1, 1),
      to: new Date(now.getFullYear(), now.getMonth(), 1),
    }
  }
  const quarterStart = Math.floor(now.getMonth() / 3) * 3
  return { from: new Date(now.getFullYear(), quarterStart, 1), to: now }
}

function isCalendarKey(key: string | null): key is CalendarRangeKey {
  return CALENDAR_RANGE_OPTIONS.some((option) => option.key === key)
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
}

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
  const isCustom = key === 'custom' && !!customFromParam && !!customToParam
  const calendarKey = !isCustom && isCalendarKey(key) ? key : null

  const idx = (() => {
    const i = TIME_RANGE_OPTIONS.findIndex((r) => r.key === key)
    return i >= 0 ? i : 0
  })()

  // Preset ranges slide with wall clock; bucket to the minute so query keys
  // keyed on fromISO/toISO do not change every render (see useReports.ts).
  const minuteFloor = () => Math.floor(Date.now() / 60_000) * 60_000
  const [nowMs, setNowMs] = useState(minuteFloor)
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(minuteFloor()), 60_000)
    return () => clearInterval(id)
  }, [])

  const fromISO = useMemo(() => {
    if (isCustom) return customFromParam!
    if (calendarKey) return calendarBounds(calendarKey, nowMs).from.toISOString()
    return new Date(nowMs - TIME_RANGE_OPTIONS[idx].hours * 3_600_000).toISOString()
  }, [isCustom, customFromParam, calendarKey, idx, nowMs])

  const toISO = useMemo(() => {
    if (isCustom) return customToParam!
    if (calendarKey) return calendarBounds(calendarKey, nowMs).to.toISOString()
    return new Date(nowMs).toISOString()
  }, [isCustom, customToParam, calendarKey, nowMs])
  const hours = isCustom || calendarKey
    ? Math.max(1, Math.round((Date.parse(toISO) - Date.parse(fromISO)) / 3_600_000))
    : TIME_RANGE_OPTIONS[idx].hours
  const label = isCustom
    ? `${new Date(fromISO).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })} → ${new Date(toISO).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })}`
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
    range: { hours, fromISO, toISO, isCustom, label, key: activeKey },
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
  const toLocal = (iso: string) => {
    const d = new Date(iso)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
  }
  const [fromInput, setFromInput] = useState(() => toLocal(customFrom))
  const [toInput, setToInput] = useState(() => toLocal(customTo))
  useEffect(() => {
    if (open) {
      setFromInput(toLocal(customFrom))
      setToInput(toLocal(customTo))
    }
  }, [open, customFrom, customTo])

  return (
    <div className="relative">
      <div
        className="inline-flex flex-wrap items-center gap-0.5 rounded-md border border-border bg-surface2/40 p-0.5"
        role="tablist"
        aria-label="Time range"
      >
        <Clock className="ml-1 mr-0.5 h-3 w-3 text-muted" />
        {TIME_RANGE_OPTIONS.map((r, i) => {
          const active = !isCustom && rangeIdx === i
          return (
            <button
              key={r.key}
              role="tab"
              aria-selected={active}
              onClick={() => { setOpen(false); onPreset(i) }}
              className={`whitespace-nowrap rounded px-2 py-1 text-[11px] font-semibold transition-colors ${
                active ? 'bg-primary text-black' : 'text-muted hover:text-text'
              }`}
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
              role="tab"
              aria-selected={active}
              title={r.title}
              onClick={() => { setOpen(false); onCalendar(r.key) }}
              className={`whitespace-nowrap rounded px-2 py-1 text-[11px] font-semibold transition-colors ${
                active ? 'bg-primary text-black' : 'text-muted hover:text-text'
              }`}
            >
              {r.label}
            </button>
          )
        })}
        <button
          role="tab"
          aria-selected={isCustom}
          onClick={() => setOpen((v) => !v)}
          className={`whitespace-nowrap rounded px-2 py-1 text-[11px] font-semibold transition-colors ${
            isCustom ? 'bg-primary text-black' : 'text-muted hover:text-text'
          }`}
        >
          Custom
        </button>
      </div>

      {open && (
        <>
          <div className="fixed inset-0 z-20" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full z-30 mt-1 w-72 rounded-md border border-border bg-surface p-3 shadow-lg">
            <div className="space-y-2">
              <label className="block">
                <span className="text-[10px] uppercase tracking-wider text-muted">From</span>
                <input
                  type="datetime-local"
                  value={fromInput}
                  onChange={(e) => setFromInput(e.target.value)}
                  className="mt-1 w-full rounded border border-border bg-transparent px-2 py-1 text-xs text-text"
                />
              </label>
              <label className="block">
                <span className="text-[10px] uppercase tracking-wider text-muted">To</span>
                <input
                  type="datetime-local"
                  value={toInput}
                  onChange={(e) => setToInput(e.target.value)}
                  className="mt-1 w-full rounded border border-border bg-transparent px-2 py-1 text-xs text-text"
                />
              </label>
              <div className="flex justify-end gap-2 pt-1">
                <button
                  onClick={() => setOpen(false)}
                  className="rounded px-2 py-1 text-[11px] text-muted hover:text-text"
                >
                  Cancel
                </button>
                <button
                  onClick={() => {
                    const fromISO = new Date(fromInput).toISOString()
                    const toISO = new Date(toInput).toISOString()
                    if (Date.parse(fromISO) >= Date.parse(toISO)) return
                    onCustom(fromISO, toISO)
                    setOpen(false)
                  }}
                  className="rounded bg-primary px-2 py-1 text-[11px] font-semibold text-black"
                >
                  Apply
                </button>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
