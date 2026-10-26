import type { ReactNode } from 'react'
import { useEffect, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  CircleHelp,
  Info,
  MinusCircle,
  OctagonAlert,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import type { StatusTone } from '@/components/netflow/StatusTile'
import type { CheckStatus, NfIssue } from './model'

/** Tone classes: tinted fill + border, `*-fg` text (≥ 4.5:1 in both themes, NF-29). */
export const TONE_CHIP: Record<StatusTone, string> = {
  danger: 'border-danger/35 bg-danger/10 text-danger-fg',
  warning: 'border-warning/35 bg-warning/10 text-warning-fg',
  success: 'border-success/35 bg-success/10 text-success-fg',
  info: 'border-info/35 bg-info/10 text-info-fg',
  neutral: 'border-border bg-surface2 text-text2',
  unknown: 'border-border bg-surface2 text-text2',
}

const TONE_ICON: Record<StatusTone, LucideIcon> = {
  danger: OctagonAlert,
  warning: AlertTriangle,
  success: CheckCircle2,
  info: Info,
  neutral: Info,
  unknown: CircleHelp,
}

/** A small status chip: icon + word, never colour alone. */
export function ToneChip({ tone, children, icon, className, title }: {
  tone: StatusTone
  children: ReactNode
  icon?: LucideIcon | null
  className?: string
  title?: string
}) {
  const Icon = icon === null ? null : icon ?? TONE_ICON[tone]
  return (
    <span
      className={cn('inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-semibold', TONE_CHIP[tone], className)}
      title={title}
    >
      {Icon && <Icon className="h-3 w-3 shrink-0" aria-hidden />}
      {children}
    </span>
  )
}

export function issueTone(sev: NfIssue['severity']): StatusTone {
  return sev === 'danger' ? 'danger' : sev === 'warning' ? 'warning' : 'info'
}

const CHECK_META: Record<CheckStatus, { icon: LucideIcon; cls: string; word: string }> = {
  pass: { icon: CheckCircle2, cls: 'text-success-fg', word: 'Pass' },
  warn: { icon: AlertTriangle, cls: 'text-warning-fg', word: 'Check' },
  fail: { icon: XCircle, cls: 'text-danger-fg', word: 'Fail' },
  pending: { icon: CircleDashed, cls: 'text-text2', word: 'Waiting' },
  info: { icon: Info, cls: 'text-info-fg', word: 'Info' },
  na: { icon: MinusCircle, cls: 'text-text2', word: 'Not needed' },
}

export function CheckIcon({ status }: { status: CheckStatus }) {
  const m = CHECK_META[status] ?? CHECK_META.pending
  const Icon = m.icon
  return (
    <span className={cn('inline-flex shrink-0 items-center gap-1 text-xs font-semibold uppercase tracking-wide', m.cls)}>
      <Icon className="h-4 w-4" aria-hidden />
      <span className="w-[4.5rem]">{m.word}</span>
    </span>
  )
}

/** Label / value pair for metric grids; the value never truncates (NF-30). */
export function Metric({ label, value, hint, title, className }: {
  label: string
  value: ReactNode
  hint?: ReactNode
  title?: string
  className?: string
}) {
  return (
    <div className={cn('min-w-0 rounded-md border border-border bg-surface2/40 px-3 py-2', className)} title={title}>
      <div className="text-xs font-medium text-text2">{label}</div>
      <div className="mt-0.5 break-words text-sm font-semibold tabular-nums text-text">{value}</div>
      {hint && <div className="mt-0.5 text-xs leading-snug text-text2">{hint}</div>}
    </div>
  )
}

/** Debounced copy of a value (search boxes). */
export function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const h = window.setTimeout(() => setV(value), ms)
    return () => window.clearTimeout(h)
  }, [value, ms])
  return v
}

/** Horizontal segmented toggle (radio group) used for filters and windows. */
export function Segmented<T extends string>({ value, onChange, options, label, className }: {
  value: T
  onChange: (v: T) => void
  options: { value: T; label: ReactNode; count?: number }[]
  label: string
  className?: string
}) {
  return (
    <div role="radiogroup" aria-label={label} className={cn('flex flex-wrap gap-1', className)}>
      {options.map((o) => {
        const active = o.value === value
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(o.value)}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
              active ? 'border-primary/50 bg-primary/10 text-text' : 'border-border bg-surface text-text2 hover:bg-surface2 hover:text-text',
            )}
          >
            {o.label}
            {o.count != null && <span className="tabular-nums text-text2">{o.count}</span>}
          </button>
        )
      })}
    </div>
  )
}
