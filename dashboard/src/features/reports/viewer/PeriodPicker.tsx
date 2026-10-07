import { useEffect, useId, useMemo, useState, type FormEvent } from 'react'
import * as Popover from '@radix-ui/react-popover'
import { CalendarRange, Check, ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatPeriod, zoneLabel, type Branding } from '@/lib/branding'
import { Button } from '@/components/ui/Button'
import { bucketNow, PERIODS, periodRange } from '../periods'
import type { PeriodId } from '../types'
import { shortDate, shortTime } from '../render/format'
import {
  customRangeError, fromZonedInput, selectionLabel, toZonedInput, type PeriodSelection,
} from './range'

export interface PeriodPickerProps {
  value: PeriodSelection
  onChange: (sel: PeriodSelection) => void
  /** The resolved window of `value` (shown in the trigger and seeded into the custom inputs). */
  window: { from: Date; to: Date }
  branding: Branding
  /** The template's default period, marked in the list. */
  defaultPeriod?: PeriodId | null
  className?: string
}

function rangeHint(id: PeriodId, b: Branding, now: Date): string {
  const { from, to } = periodRange(id, b.timezone, b.week_start, now)
  const toDate = id.endsWith('_to_date') || id === 'today' ? null : new Date(to.getTime() - 1)
  if (id === 'today' || id === 'yesterday') return shortDate(from.getTime(), b)
  return toDate ? `${shortDate(from.getTime(), b)} – ${shortDate(toDate.getTime(), b)}` : `${shortDate(from.getTime(), b)} – now`
}

/**
 * Report period: rolling presets, calendar presets computed in the
 * organisation timezone (with its week start), and a custom range whose
 * inputs are wall times in that same timezone.
 */
export function PeriodPicker({ value, onChange, window, branding, defaultPeriod, className }: PeriodPickerProps) {
  const [open, setOpen] = useState(false)
  const id = useId()
  const tz = branding.timezone
  const zone = zoneLabel(tz, window.to)
  const now = useMemo(() => bucketNow(), [open]) // eslint-disable-line react-hooks/exhaustive-deps
  const [fromText, setFromText] = useState('')
  const [toText, setToText] = useState('')
  const [touched, setTouched] = useState(false)

  const fromMs = window.from.getTime()
  const toMs = window.to.getTime()
  useEffect(() => {
    if (!open) return
    setFromText(toZonedInput(new Date(fromMs), tz))
    setToText(toZonedInput(new Date(toMs), tz))
    setTouched(false)
  }, [open, fromMs, toMs, tz])

  const from = fromZonedInput(fromText, tz)
  const to = fromZonedInput(toText, tz)
  const error = customRangeError(from, to)

  const label = value.kind === 'custom'
    ? `${shortDate(value.from.getTime(), branding)} ${shortTime(value.from.getTime(), branding)} – ${shortDate(value.to.getTime(), branding)} ${shortTime(value.to.getTime(), branding)}`
    : selectionLabel(value)
  const full = formatPeriod(window.from, window.to, branding)

  const pick = (pid: PeriodId) => {
    onChange({ kind: 'period', id: pid })
    setOpen(false)
  }

  const applyCustom = (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (error || !from || !to) return
    onChange({ kind: 'custom', from, to })
    setOpen(false)
  }

  const item = (p: (typeof PERIODS)[number]) => {
    const active = value.kind === 'period' && value.id === p.id
    return (
      <li key={p.id}>
        <button
          type="button"
          aria-pressed={active}
          onClick={() => pick(p.id)}
          className={cn(
            'flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
            active ? 'bg-primary/10 font-semibold text-text' : 'text-text2 hover:bg-surface2 hover:text-text',
          )}
        >
          <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center">
            {active && <Check className="h-3.5 w-3.5 text-primary" aria-hidden />}
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate">{p.label}</span>
            {p.kind === 'calendar' && <span className="block truncate text-[10px] font-normal tabular-nums text-muted">{rangeHint(p.id, branding, now)}</span>}
          </span>
          {defaultPeriod === p.id && (
            <span className="shrink-0 rounded border border-border px-1 text-[9px] font-semibold uppercase tracking-wide text-muted">Default</span>
          )}
        </button>
      </li>
    )
  }

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <Button
          variant="outline"
          size="sm"
          className={cn('h-8 max-w-full gap-1.5', className)}
          aria-label={`Report period: ${label} (${full}). Change period`}
          title={full}
        >
          <CalendarRange className="h-3.5 w-3.5 text-primary" aria-hidden />
          <span className="truncate text-xs font-semibold">{label}</span>
          <span className="hidden text-[10px] font-medium text-muted sm:inline">{zone}</span>
          <ChevronDown className={cn('h-3 w-3 opacity-60 transition-transform', open && 'rotate-180')} aria-hidden />
        </Button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={6}
          collisionPadding={12}
          aria-label="Report period"
          className="z-50 w-[min(34rem,calc(100vw-1.5rem))] rounded-lg border border-border bg-surface p-3 text-text shadow-elevated animate-fade-in"
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <p id={`${id}-rolling`} className="px-2.5 pb-1 text-[10px] font-semibold uppercase tracking-wide text-muted">Rolling</p>
              <ul role="list" aria-labelledby={`${id}-rolling`} className="space-y-0.5">
                {PERIODS.filter((p) => p.kind === 'rolling').map(item)}
              </ul>
            </div>
            <div>
              <p id={`${id}-calendar`} className="px-2.5 pb-1 text-[10px] font-semibold uppercase tracking-wide text-muted">
                Calendar <span className="font-normal normal-case tracking-normal">· weeks start {branding.week_start}</span>
              </p>
              <ul role="list" aria-labelledby={`${id}-calendar`} className="grid grid-cols-1 gap-0.5 min-[420px]:grid-cols-2 sm:grid-cols-1">
                {PERIODS.filter((p) => p.kind === 'calendar').map(item)}
              </ul>
            </div>
          </div>

          <form onSubmit={applyCustom} className="mt-3 border-t border-border pt-3" noValidate>
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted">
              Custom range <span className="font-normal normal-case tracking-normal">· times in {tz.replace(/_/g, ' ')} ({zone})</span>
            </p>
            <div className="mt-2 grid gap-2 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
              <div className="space-y-1">
                <label htmlFor={`${id}-from`} className="block text-[11px] font-medium text-text2">From</label>
                <input
                  id={`${id}-from`}
                  type="datetime-local"
                  value={fromText}
                  onChange={(e) => setFromText(e.target.value)}
                  aria-invalid={touched && !!error}
                  aria-describedby={touched && error ? `${id}-err` : undefined}
                  className="h-8 w-full rounded-md border border-border bg-surface px-2 text-xs tabular-nums text-text focus-visible:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                />
              </div>
              <div className="space-y-1">
                <label htmlFor={`${id}-to`} className="block text-[11px] font-medium text-text2">To</label>
                <input
                  id={`${id}-to`}
                  type="datetime-local"
                  value={toText}
                  onChange={(e) => setToText(e.target.value)}
                  aria-invalid={touched && !!error}
                  aria-describedby={touched && error ? `${id}-err` : undefined}
                  className="h-8 w-full rounded-md border border-border bg-surface px-2 text-xs tabular-nums text-text focus-visible:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                />
              </div>
              <Button type="submit" size="sm" className="h-8">Apply</Button>
            </div>
            {touched && error ? (
              <p id={`${id}-err`} role="alert" className="mt-1.5 text-[11px] font-medium text-danger-fg">{error}</p>
            ) : (
              <p className="mt-1.5 text-[11px] text-muted">The end is exclusive; a report covers at most 400 days.</p>
            )}
          </form>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}
