import type { ReactNode } from 'react'
import { AlertTriangle, CheckCircle2, CircleHelp, Info, OctagonAlert, type LucideIcon } from 'lucide-react'
import { Skeleton } from '@/components/ui/Skeleton'
import { cn } from '@/lib/utils'

export type StatusTone = 'danger' | 'warning' | 'success' | 'info' | 'neutral' | 'unknown'

/**
 * Tone styling (NF-29). The tone is carried by a tinted fill, a 4px left border, an icon and a
 * visible word — never by coloured text alone — and the word/icon use the `*-fg` tokens, which
 * keep ≥ 4.5:1 on these fills in both themes. Label, value and caption stay neutral text so the
 * number is the most legible thing on the tile.
 */
const TONES: Record<StatusTone, { box: string; fg: string; word: string | null; icon: LucideIcon | null }> = {
  danger: { box: 'border-danger/35 border-l-danger bg-danger/10', fg: 'text-danger-fg', word: 'Critical', icon: OctagonAlert },
  warning: { box: 'border-warning/35 border-l-warning bg-warning/10', fg: 'text-warning-fg', word: 'Warning', icon: AlertTriangle },
  success: { box: 'border-success/35 border-l-success bg-success/10', fg: 'text-success-fg', word: 'Healthy', icon: CheckCircle2 },
  info: { box: 'border-info/35 border-l-info bg-info/10', fg: 'text-info-fg', word: 'Info', icon: Info },
  // Neutral figures (traffic, counts) carry no status, so no icon or word unless one is passed.
  neutral: { box: 'border-border border-l-border-strong bg-surface2/60', fg: 'text-text2', word: null, icon: null },
  unknown: { box: 'border-border border-l-border-strong bg-surface2/60', fg: 'text-text2', word: 'Unknown', icon: CircleHelp },
}

export interface StatusTileProps {
  tone: StatusTone
  label: string
  /** The figure. `null`/`undefined` renders "—" (unknown), never 0. */
  value: ReactNode
  /** Visible status word; defaults per tone (Critical/Warning/Healthy/Info/Unknown). `null` hides it. */
  word?: string | null
  caption?: ReactNode
  icon?: LucideIcon
  /** Exact value or explanation for hover and assistive technology. */
  title?: string
  loading?: boolean
  /** Makes the tile a toggle button (e.g. filter a table by this status). */
  onClick?: () => void
  active?: boolean
  className?: string
}

export function StatusTile({
  tone,
  label,
  value,
  word,
  caption,
  icon,
  title,
  loading,
  onClick,
  active,
  className,
}: StatusTileProps) {
  const t = TONES[tone]
  const Icon = icon ?? t.icon
  const shownWord = word === undefined ? t.word : word
  const shownValue = value === null || value === undefined || value === '' ? '—' : value
  const body = (
    <>
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs font-medium leading-snug text-text2">{label}</span>
        {(Icon || shownWord) && (
          <span className={cn('inline-flex shrink-0 items-center gap-1 text-xs font-semibold uppercase tracking-wide', t.fg)}>
            {Icon && <Icon className="h-3.5 w-3.5" aria-hidden />}
            {shownWord}
          </span>
        )}
      </div>
      {loading ? (
        <Skeleton className="mt-2 h-7 w-20" />
      ) : (
        <div className="mt-1 break-words text-2xl font-semibold leading-tight tabular-nums text-text">{shownValue}</div>
      )}
      {caption && <div className="mt-1 text-xs leading-snug text-text2">{caption}</div>}
    </>
  )
  const boxClass = cn(
    'block w-full rounded-lg border border-l-4 p-3 text-left',
    t.box,
    onClick && 'transition-shadow hover:shadow-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
    active && 'ring-2 ring-primary/60',
    className,
  )
  if (onClick) {
    return (
      <button type="button" className={boxClass} onClick={onClick} aria-pressed={active ?? false} title={title} aria-busy={loading || undefined}>
        {body}
      </button>
    )
  }
  return (
    <div role="group" aria-label={label} className={boxClass} title={title} aria-busy={loading || undefined}>
      {body}
    </div>
  )
}
