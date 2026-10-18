/**
 * Shared escalation widgets: the priority badge, target chips, the searchable
 * target picker, the step timeline and the dry-run recipients list.
 */
import { useMemo, useState, type KeyboardEvent } from 'react'
import * as Popover from '@radix-ui/react-popover'
import { AlertTriangle, CalendarClock, Check, Hash, Mail, MessageSquare, Plus, Search, User as UserIcon, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  PRIORITY_META, TARGET_TYPE_LABEL, describeTarget, fmtOffset, isChannelRecipient, normalizePriority, pickerOptions, recipientLabel, viaLabel,
  type Directory,
} from './model'
import type { PreviewRecipient, PreviewStep, Priority, Target, TargetType } from './types'

const focusRing = 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60 focus-visible:ring-offset-1 focus-visible:ring-offset-surface'

const PRIORITY_CLS: Record<Priority, string> = {
  P1: 'border-danger/50 bg-danger text-white',
  P2: 'border-danger/35 bg-danger/10 text-danger',
  P3: 'border-warning/40 bg-warning/10 text-warning',
  P4: 'border-border bg-surface2 text-text2',
}

/** "P2 High" pill. `compact` shows the code only (with the name as its title). */
export function PriorityBadge({ priority, compact, className }: { priority: string | null | undefined; compact?: boolean; className?: string }) {
  const p = normalizePriority(priority)
  if (!p) return null
  const m = PRIORITY_META[p]
  return (
    <span
      className={cn('inline-flex items-center whitespace-nowrap rounded-md border px-1.5 py-0.5 text-xs font-semibold tabular-nums', PRIORITY_CLS[p], className)}
      title={`${m.label} — ${m.trigger}`}
      aria-label={`Priority ${m.label}`}
    >
      {compact ? p : m.label}
    </span>
  )
}

export const TARGET_ICON: Record<TargetType, typeof UserIcon> = { schedule: CalendarClock, user: UserIcon, channel: Hash }

export function TargetChip({ target, dir, onRemove }: { target: Target; dir: Directory; onRemove?: () => void }) {
  const info = describeTarget(target, dir)
  const Icon = TARGET_ICON[target.type]
  const bad = info.missing || info.disabled || info.detail === 'No members'
  return (
    <span
      className={cn(
        'inline-flex max-w-full items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs',
        bad ? 'border-warning/40 bg-warning/10 text-warning' : 'border-border bg-surface2/60 text-text',
      )}
      title={`${info.kindLabel}: ${info.label}${info.detail ? ` (${info.detail})` : ''}${info.disabled ? ' — disabled' : ''}`}
    >
      <Icon className="h-3 w-3 shrink-0" aria-hidden />
      <span className="truncate">{info.label}</span>
      {info.disabled && <span className="text-[10px] uppercase">off</span>}
      {info.detail === 'No members' && <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden />}
      {onRemove && (
        <button type="button" onClick={onRemove} aria-label={`Remove ${info.kindLabel.toLowerCase()} ${info.label}`} className={cn('-mr-0.5 rounded p-0.5 text-muted hover:bg-surface hover:text-danger', focusRing)}>
          <X className="h-3 w-3" aria-hidden />
        </button>
      )}
    </span>
  )
}

/** Searchable picker for schedules, users and channels. */
export function TargetPicker({ dir, value, onAdd, label, types, disabled }: {
  dir: Directory
  value: Target[]
  onAdd: (t: Target) => void
  label: string
  types?: TargetType[]
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const kindsKey = (types ?? ['schedule', 'user', 'channel']).join(',')
  const kinds = useMemo(() => kindsKey.split(',') as TargetType[], [kindsKey])
  const options = useMemo(() => pickerOptions(dir, q, kinds, value), [dir, q, kinds, value])
  const pick = (i: number) => {
    const o = options[i]
    if (!o) return
    onAdd({ type: o.type, id: o.id })
    setQ('')
    setActive(0)
  }
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(options.length - 1, a + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(0, a - 1)) }
    else if (e.key === 'Enter') { e.preventDefault(); pick(active) }
  }
  const noChannels = kinds.includes('channel') && dir.channels.length === 0
  return (
    <Popover.Root open={open} onOpenChange={(o) => { setOpen(o); if (!o) { setQ(''); setActive(0) } }}>
      <Popover.Trigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-label={label}
          className={cn('inline-flex h-7 items-center gap-1 rounded-md border border-dashed border-border px-2 text-xs text-muted hover:border-primary/50 hover:text-text disabled:opacity-50', focusRing)}
        >
          <Plus className="h-3.5 w-3.5" aria-hidden /> Add target
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content align="start" sideOffset={4} collisionPadding={12} className="z-50 w-[340px] rounded-md border border-border bg-surface p-1 shadow-elevated">
          <div className="flex items-center gap-2 border-b border-border px-2 pb-1.5 pt-1">
            <Search className="h-3.5 w-3.5 text-muted" aria-hidden />
            <input
              autoFocus
              role="combobox"
              aria-expanded
              aria-controls="target-picker-list"
              aria-label={`Search ${kinds.map((k) => TARGET_TYPE_LABEL[k].toLowerCase()).join(', ')}`}
              value={q}
              onChange={(e) => { setQ(e.target.value); setActive(0) }}
              onKeyDown={onKey}
              placeholder="Search schedules, people, channels"
              className="h-7 w-full bg-transparent text-sm text-text outline-none placeholder:text-muted"
            />
          </div>
          <div id="target-picker-list" role="listbox" aria-label="Targets" className="max-h-72 overflow-y-auto py-1">
            {options.length === 0 && <div className="px-2 py-3 text-center text-xs text-muted">{q ? 'Nothing matches' : 'Everything is already added'}</div>}
            {options.map((o, i) => {
              const Icon = TARGET_ICON[o.type]
              return (
                <button
                  key={`${o.type}:${o.id}`}
                  type="button"
                  role="option"
                  aria-selected={i === active}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => pick(i)}
                  className={cn('flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm', i === active && 'bg-surface2', o.disabled && 'opacity-60')}
                >
                  <Icon className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />
                  <span className="min-w-0 flex-1 truncate">{o.label}</span>
                  <span className="shrink-0 text-[11px] text-muted">{o.disabled ? 'disabled' : o.detail}</span>
                </button>
              )
            })}
          </div>
          {noChannels && <p className="border-t border-border px-2 py-1.5 text-[11px] text-muted">Channels are listed for roles with alerts.manage.</p>}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}

/** Horizontal step timeline: one marker per step at its delay. */
export function StepTimeline({ steps, ackMinutes, className }: {
  steps: Array<{ delay: number; label: string; detail?: string; warn?: boolean }>
  ackMinutes?: number | null
  className?: string
}) {
  if (!steps.length) return <p className={cn('text-xs text-muted', className)}>No escalation steps.</p>
  const max = Math.max(...steps.map((s) => s.delay), ackMinutes ?? 0, 1)
  return (
    <ol className={cn('relative space-y-1.5 border-l border-border pl-3', className)} aria-label="Escalation steps">
      {steps.map((s, i) => (
        <li key={i} className="relative">
          <span className={cn('absolute -left-[17px] top-1 h-2.5 w-2.5 rounded-full border-2 border-surface', s.warn ? 'bg-warning' : i === 0 ? 'bg-primary' : 'bg-text2')} aria-hidden />
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="w-20 shrink-0 text-xs font-semibold tabular-nums text-text2">{fmtOffset(s.delay)}</span>
            <span className="min-w-0 flex-1 text-sm">{s.label}</span>
          </div>
          {s.detail && <div className="ml-[88px] text-xs text-muted">{s.detail}</div>}
          <div className="ml-[88px] mt-0.5 h-1 rounded bg-surface2" aria-hidden>
            <div className="h-1 rounded bg-primary/40" style={{ width: `${Math.max(2, (s.delay / max) * 100)}%` }} />
          </div>
        </li>
      ))}
    </ol>
  )
}

const KIND_ICON: Record<string, typeof Mail> = { email: Mail, sms: MessageSquare }

export function RecipientList({ recipients, fmt }: { recipients: PreviewRecipient[]; fmt?: (iso: string) => string }) {
  if (!recipients.length) return <p className="text-xs text-danger">Nobody would be notified.</p>
  return (
    <ul className="space-y-0.5">
      {recipients.map((r, i) => {
        const channel = isChannelRecipient(r)
        const Icon = channel ? Hash : KIND_ICON[r.kind] ?? Hash
        return (
          <li key={`${r.kind}-${r.address ?? r.via}-${i}`} className="flex flex-wrap items-center gap-1.5 text-xs">
            <Icon className="h-3 w-3 shrink-0 text-muted" aria-hidden />
            <span className="min-w-0 break-all font-medium">{recipientLabel(r)}</span>
            {channel ? <span className="text-muted">· {r.kind.toUpperCase()} channel</span> : r.via && <span className="text-muted">· {viaLabel(r.via)}</span>}
            {r.notified_at && fmt && <span className="inline-flex items-center gap-0.5 text-success"><Check className="h-3 w-3" aria-hidden />notified {fmt(r.notified_at)}</span>}
          </li>
        )
      })}
    </ul>
  )
}

/** Dry-run / route steps with recipients and gaps. `current` highlights the step the incident is at. */
export function PreviewSteps({ steps, current, fmt }: { steps: PreviewStep[]; current?: number | null; fmt?: (iso: string) => string }) {
  if (!steps.length) return <p className="text-sm text-muted">No steps.</p>
  return (
    <ol className="space-y-2">
      {steps.map((s, i) => {
        const isCurrent = s.current === true || (current != null && s.position === current)
        return (
          <li key={`${s.position}-${i}`} className={cn('rounded-md border p-2.5', isCurrent ? 'border-primary/50 bg-primary/5' : 'border-border bg-surface2/30')}>
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <span className="text-xs font-semibold tabular-nums text-text2">{fmtOffset(s.at_offset_minutes)}</span>
              <span className="text-sm font-medium">{s.name || `Step ${i + 1}`}</span>
              {isCurrent && <span className="rounded bg-primary/15 px-1.5 py-px text-[10px] font-semibold uppercase text-primary">Current step</span>}
              {s.notified_at && fmt && <span className="ml-auto text-xs text-success">Notified {fmt(s.notified_at)}</span>}
            </div>
            {s.uses_fallback && !s.recipients.length ? (
              <div className="space-y-0.5">
                <p className="text-xs text-warning">Reaches nobody — the fallback route would be used:</p>
                {s.fallback_recipients?.length ? <RecipientList recipients={s.fallback_recipients} fmt={fmt} /> : <p className="text-xs text-danger">The fallback reaches nobody either.</p>}
              </div>
            ) : (
              <RecipientList recipients={s.recipients} fmt={fmt} />
            )}
            {s.gaps.length > 0 && (
              <ul className="mt-1 space-y-0.5">
                {s.gaps.map((g) => (
                  <li key={g} className="flex items-start gap-1 text-xs text-warning"><AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />{g}</li>
                ))}
              </ul>
            )}
          </li>
        )
      })}
    </ol>
  )
}

/** Minutes input that keeps the typed text (empty = none). */
export function MinutesField({ id, value, onChange, placeholder, disabled, invalid, ariaLabel, max = 10080, min = 1 }: {
  id?: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  disabled?: boolean
  invalid?: boolean
  ariaLabel?: string
  max?: number
  min?: number
}) {
  return (
    <div className="flex items-center gap-1.5">
      <input
        id={id}
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        value={value}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-invalid={invalid || undefined}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={cn(
          'h-9 w-24 rounded-md border bg-surface px-2 text-sm tabular-nums shadow-sm placeholder:text-muted disabled:opacity-50',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
          invalid ? 'border-danger' : 'border-border',
        )}
      />
      <span className="text-xs text-muted">min</span>
    </div>
  )
}

export const USER_COLOR_CLS = [
  'bg-primary/75', 'bg-success/75', 'bg-info/75', 'bg-accent/75', 'bg-warning/75', 'bg-primary/45', 'bg-success/45', 'bg-text2/60',
]
