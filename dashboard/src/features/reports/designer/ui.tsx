/**
 * Small building blocks for the template designer: side sheets, tooltips,
 * a polite live region, a media-query hook, a radio segmented control,
 * the period select and section badges.
 */
import {
  useCallback, useRef, useState, useSyncExternalStore, type KeyboardEvent, type ReactNode,
} from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import * as SelectPrimitive from '@radix-ui/react-select'
import * as Tooltip from '@radix-ui/react-tooltip'
import { Camera, LockKeyhole, Server, Waypoints, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { PERIODS } from '../periods'
import type { PeriodId, SectionDef } from '../types'
import { permissionLabel } from './model'

/** Drag-and-drop payload types (lower case: browsers normalise them). */
export const DND_ENTRY = 'application/x-zp-report-entry'
export const DND_SECTION = 'application/x-zp-report-section'

/* ------------------------------------------------------------------ */
/*  Media queries                                                       */
/* ------------------------------------------------------------------ */

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window === 'undefined' || !window.matchMedia) return () => undefined
      const m = window.matchMedia(query)
      m.addEventListener('change', onChange)
      return () => m.removeEventListener('change', onChange)
    },
    () => (typeof window !== 'undefined' && !!window.matchMedia && window.matchMedia(query).matches),
    () => false,
  )
}

export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

/* ------------------------------------------------------------------ */
/*  Announcements                                                       */
/* ------------------------------------------------------------------ */

/** A polite live region; repeated messages are still read (alternating no-break space). */
export function useAnnouncer() {
  const [message, setMessage] = useState('')
  const flip = useRef(false)
  const timer = useRef<number>()
  const announce = useCallback((text: string) => {
    window.clearTimeout(timer.current)
    // A short delay lets rapid arrow-key moves settle into one announcement.
    timer.current = window.setTimeout(() => {
      flip.current = !flip.current
      setMessage(flip.current ? text : `${text} `)
    }, 100)
  }, [])
  const region = (
    <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
      {message}
    </div>
  )
  return { announce, region }
}

/* ------------------------------------------------------------------ */
/*  Side sheet                                                          */
/* ------------------------------------------------------------------ */

export function Sheet({
  open, onOpenChange, side = 'right', title, description, children, footer, className, onOpenAutoFocus, onCloseAutoFocus,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  side?: 'left' | 'right'
  title: ReactNode
  description?: ReactNode
  children: ReactNode
  footer?: ReactNode
  className?: string
  onOpenAutoFocus?: (e: Event) => void
  onCloseAutoFocus?: (e: Event) => void
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/50 data-[state=open]:animate-fade-in" />
        <DialogPrimitive.Content
          {...(description ? {} : { 'aria-describedby': undefined })}
          onOpenAutoFocus={onOpenAutoFocus}
          onCloseAutoFocus={onCloseAutoFocus}
          className={cn(
            'fixed inset-y-0 z-50 flex w-full max-w-[26rem] flex-col bg-surface shadow-xl focus:outline-none data-[state=open]:animate-fade-in',
            side === 'right' ? 'right-0 border-l border-border' : 'left-0 border-r border-border',
            className,
          )}
        >
          <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
            <div className="min-w-0">
              <DialogPrimitive.Title className="text-base font-semibold text-text">{title}</DialogPrimitive.Title>
              {description && (
                <DialogPrimitive.Description className="mt-0.5 text-xs text-text2">
                  {description}
                </DialogPrimitive.Description>
              )}
            </div>
            <DialogPrimitive.Close
              className="rounded-md p-1 text-text2 hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
              aria-label="Close"
            >
              <X className="h-4 w-4" aria-hidden />
            </DialogPrimitive.Close>
          </div>
          <div className="flex min-h-0 flex-1 flex-col">{children}</div>
          {footer && <div className="border-t border-border px-4 py-3">{footer}</div>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

/* ------------------------------------------------------------------ */
/*  Tooltip                                                             */
/* ------------------------------------------------------------------ */

/** Hover/focus hint. Keep the same information available as text for screen readers. */
export function Hint({ content, children, side = 'top', delay = 250 }: { content: ReactNode; children: ReactNode; side?: 'top' | 'right' | 'bottom' | 'left'; delay?: number }) {
  return (
    <Tooltip.Provider delayDuration={delay} skipDelayDuration={100}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content
            side={side}
            sideOffset={6}
            collisionPadding={8}
            className="z-[60] max-w-[18rem] rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs leading-relaxed text-text shadow-elevated data-[state=delayed-open]:animate-fade-in"
          >
            {content}
            <Tooltip.Arrow className="fill-surface" />
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  )
}

/* ------------------------------------------------------------------ */
/*  Segmented radio group                                               */
/* ------------------------------------------------------------------ */

export function SegmentedRadio<T extends string>({
  value, onChange, options, label, labelledBy, disabled, className,
}: {
  value: T
  onChange: (v: T) => void
  options: { value: T; label: string; icon?: typeof Server; hint?: string }[]
  label?: string
  labelledBy?: string
  disabled?: boolean
  className?: string
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const delta = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0
    if (!delta) return
    e.preventDefault()
    const next = (i + delta + options.length) % options.length
    onChange(options[next].value)
    refs.current[next]?.focus()
  }
  return (
    <div
      role="radiogroup"
      aria-label={label}
      aria-labelledby={labelledBy}
      aria-disabled={disabled || undefined}
      className={cn('grid grid-flow-col auto-cols-fr gap-1 rounded-md border border-border bg-surface2 p-1', disabled && 'opacity-60', className)}
    >
      {options.map((o, i) => {
        const Icon = o.icon
        const active = value === o.value
        return (
          <button
            key={o.value}
            ref={(el) => { refs.current[i] = el }}
            type="button"
            role="radio"
            aria-checked={active}
            tabIndex={active ? 0 : -1}
            disabled={disabled}
            title={o.hint}
            onClick={() => onChange(o.value)}
            onKeyDown={(e) => onKey(e, i)}
            className={cn(
              'inline-flex min-w-0 items-center justify-center gap-1.5 rounded px-2 py-1 text-xs font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:cursor-not-allowed',
              active ? 'bg-surface text-text shadow-sm' : 'text-text2 hover:text-text',
            )}
          >
            {Icon && <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />}
            <span className="truncate">{o.label}</span>
          </button>
        )
      })}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Period select                                                       */
/* ------------------------------------------------------------------ */

const NONE = '__none__'

export function PeriodSelect({
  value, onChange, id, labelledBy, allowNone, noneLabel = 'No default (last 24 hours)', className, size = 'default', ariaLabel,
}: {
  value: PeriodId | null
  onChange: (v: PeriodId | null) => void
  id?: string
  labelledBy?: string
  allowNone?: boolean
  noneLabel?: string
  className?: string
  size?: 'default' | 'sm'
  ariaLabel?: string
}) {
  const groups: { label: string; kind: 'rolling' | 'calendar' }[] = [
    { label: 'Rolling', kind: 'rolling' },
    { label: 'Calendar', kind: 'calendar' },
  ]
  return (
    <Select value={value ?? NONE} onValueChange={(v) => onChange(v === NONE ? null : (v as PeriodId))}>
      <SelectTrigger
        id={id}
        aria-labelledby={labelledBy}
        aria-label={ariaLabel}
        className={cn(size === 'sm' && 'h-8 text-xs', className)}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {allowNone && <SelectItem value={NONE}>{noneLabel}</SelectItem>}
        {groups.map((g) => (
          <SelectGroup key={g.kind}>
            <SelectPrimitive.Label className="px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
              {g.label}
            </SelectPrimitive.Label>
            {PERIODS.filter((p) => p.kind === g.kind).map((p) => (
              <SelectItem key={p.id} value={p.id}>{p.label}</SelectItem>
            ))}
          </SelectGroup>
        ))}
      </SelectContent>
    </Select>
  )
}

/* ------------------------------------------------------------------ */
/*  Section badges                                                      */
/* ------------------------------------------------------------------ */

const SCOPE_TEXT: Record<SectionDef['scope'], { label: string; hint: string; icon: typeof Server } | null> = {
  device: { label: 'Device scope', hint: 'Follows the device scope chosen in the viewer or schedule.', icon: Server },
  netflow: { label: 'Flow scope', hint: 'Follows the NetFlow scope chosen in the viewer or schedule.', icon: Waypoints },
  fleet: null,
}

function NoHint({ children }: { content: ReactNode; children: ReactNode }) {
  return <>{children}</>
}

const badge = 'inline-flex items-center gap-1 rounded border px-1.5 py-px text-[10px] font-medium leading-4'

/** Scope / snapshot / permission badges for a section (library, outline, inspector). */
export function SectionBadges({ def, className, showScope = true, tooltips = true }: {
  def: SectionDef
  className?: string
  showScope?: boolean
  /** False inside another hover target (the library rows). */
  tooltips?: boolean
}) {
  const scope = SCOPE_TEXT[def.scope]
  const needs = def.requires.map(permissionLabel).join(', ')
  const Wrap = tooltips ? Hint : NoHint
  return (
    <span className={cn('flex flex-wrap items-center gap-1', className)}>
      {!def.available && (
        <Wrap content={<>Your role cannot see this section's data{needs ? <> (needs {needs})</> : null}. You can still add it: it shows as restricted for anyone without that permission.</>}>
          <span className={cn(badge, 'border-warning/40 bg-warning/10 text-warning-fg')}>
            <LockKeyhole className="h-2.5 w-2.5" aria-hidden />
            Restricted
            <span className="sr-only"> for your role{needs ? `; needs ${needs}` : ''}</span>
          </span>
        </Wrap>
      )}
      {def.semantics === 'snapshot' && (
        <Wrap content="Shows the current state when the report runs, not the report period.">
          <span className={cn(badge, 'border-info/35 bg-info/10 text-info-fg')}>
            <Camera className="h-2.5 w-2.5" aria-hidden />
            Snapshot
          </span>
        </Wrap>
      )}
      {showScope && scope && (
        <Wrap content={scope.hint}>
          <span className={cn(badge, 'border-border bg-surface2 text-text2')}>
            <scope.icon className="h-2.5 w-2.5" aria-hidden />
            {scope.label}
          </span>
        </Wrap>
      )}
    </span>
  )
}
