/** Small shared pieces of the Report Library: icon tiles, chips, the favourite star. */
import type { ReactNode } from 'react'
import { AlertTriangle, CalendarClock, Lock, Star, Users } from 'lucide-react'
import { cn } from '@/lib/utils'
import { reportIcon } from '../icons'
import type { LibraryItem, RunTone } from './model'
import { categoryTone, TONE_TILE } from './tones'

export function CategoryIcon({ icon, category, size = 'md', className }: {
  icon?: string | null
  category: string | null
  size?: 'sm' | 'md' | 'lg'
  className?: string
}) {
  const Icon = reportIcon(icon)
  const box = size === 'sm' ? 'h-7 w-7 rounded-md' : size === 'lg' ? 'h-11 w-11 rounded-lg' : 'h-9 w-9 rounded-lg'
  const glyph = size === 'sm' ? 'h-3.5 w-3.5' : size === 'lg' ? 'h-5 w-5' : 'h-[18px] w-[18px]'
  return (
    <span className={cn('flex shrink-0 items-center justify-center', box, TONE_TILE[categoryTone(category)], className)} aria-hidden>
      <Icon className={glyph} />
    </span>
  )
}

export type ChipTone = 'neutral' | 'primary' | 'info' | 'success' | 'warning' | 'danger'

const CHIP: Record<ChipTone, string> = {
  neutral: 'border-border bg-surface2 text-text2',
  primary: 'border-primary/30 bg-primary/10 text-primary',
  info: 'border-info/30 bg-info/10 text-info-fg',
  success: 'border-success/30 bg-success/10 text-success-fg',
  warning: 'border-warning/30 bg-warning/10 text-warning-fg',
  danger: 'border-danger/30 bg-danger/10 text-danger-fg',
}

export function Chip({ tone = 'neutral', icon, children, title, className }: {
  tone?: ChipTone
  icon?: ReactNode
  children: ReactNode
  title?: string
  className?: string
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex max-w-full items-center gap-1 whitespace-nowrap rounded border px-1.5 py-px text-xs font-medium leading-4',
        CHIP[tone],
        className,
      )}
    >
      {icon}
      <span className="truncate">{children}</span>
    </span>
  )
}

/** Built-in / Custom, visibility, owner, schedules and access warnings. */
export function OriginBadges({ item, compact }: { item: LibraryItem; compact?: boolean }) {
  return (
    <>
      {item.origin === 'builtin' ? (
        <Chip>Built-in</Chip>
      ) : (
        <>
          <Chip tone="info">Custom</Chip>
          {item.visibility === 'shared' ? (
            <Chip icon={<Users className="h-3 w-3" aria-hidden />}>Shared</Chip>
          ) : (
            <Chip icon={<Lock className="h-3 w-3" aria-hidden />}>Private</Chip>
          )}
          {!compact && (
            <span className="truncate text-xs text-muted">
              by {item.isOwner ? 'you' : item.ownerName || 'a former user'}
            </span>
          )}
        </>
      )}
      {item.schedules > 0 && (
        <Chip tone="primary" icon={<CalendarClock className="h-3 w-3" aria-hidden />} title={`${item.schedules} schedule${item.schedules === 1 ? '' : 's'} deliver this template`}>
          Scheduled ×{item.schedules}
        </Chip>
      )}
      {item.restricted ? (
        <Chip tone="danger" icon={<Lock className="h-3 w-3" aria-hidden />} title="Your role cannot see any of this template's sections; you can still edit or delete it.">
          No visible sections
        </Chip>
      ) : item.partial ? (
        <Chip tone="warning" icon={<AlertTriangle className="h-3 w-3" aria-hidden />} title="Some sections read data your role cannot see; they are left out when you open the report.">
          Some sections hidden
        </Chip>
      ) : null}
    </>
  )
}

export function FavoriteButton({ item, pressed, onToggle, className }: {
  item: Pick<LibraryItem, 'title'>
  pressed: boolean
  onToggle: () => void
  className?: string
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      aria-label={`Favourite ${item.title}`}
      title={pressed ? 'Remove from favourites' : 'Add to favourites'}
      onClick={(e) => {
        e.stopPropagation()
        onToggle()
      }}
      className={cn(
        'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
        pressed ? 'text-warning-fg hover:bg-warning/10' : 'text-muted hover:bg-surface2 hover:text-text',
        className,
      )}
    >
      <Star className={cn('h-4 w-4', pressed && 'fill-current')} aria-hidden />
    </button>
  )
}

const RUN_TONE: Record<RunTone, ChipTone> = { success: 'success', warning: 'warning', danger: 'danger', neutral: 'neutral' }

export function RunStatusChip({ status, tone }: { status: string | null | undefined; tone: RunTone }) {
  if (!status) return <span className="text-xs text-muted">—</span>
  const label = status.replace(/_/g, ' ')
  return <Chip tone={RUN_TONE[tone]} className="capitalize">{label}</Chip>
}

/** Centered message for empty views and no-result searches. */
export function EmptyState({ icon, title, children, action }: {
  icon: ReactNode
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed border-border bg-surface/60 px-6 py-12 text-center">
      <span className="flex h-11 w-11 items-center justify-center rounded-full bg-surface2 text-muted" aria-hidden>
        {icon}
      </span>
      <h3 className="text-sm font-semibold text-text">{title}</h3>
      {children && <div className="max-w-md text-xs text-muted">{children}</div>}
      {action && <div className="mt-2 flex flex-wrap items-center justify-center gap-2">{action}</div>}
    </div>
  )
}
