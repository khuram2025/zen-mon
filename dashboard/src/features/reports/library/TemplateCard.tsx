/** Library cards: the full template card and the compact shelf card. */
import { useId } from 'react'
import { Link } from 'react-router-dom'
import { Info } from 'lucide-react'
import { formatDate } from '@/lib/branding'
import { cn } from '@/lib/utils'
import { useReportBranding } from '../api'
import { QuickActions, TemplateMenu, useLibraryActions } from './actions'
import { metaLine, openLink, periodLabel, plural, viewedAgo, type LibraryItem } from './model'
import { CategoryIcon, FavoriteButton, OriginBadges } from './parts'
import { categoryTone, TONE_BAR } from './tones'

/**
 * A template card. The title is a button stretched over the card (opens the
 * details drawer); the star and the action bar sit above it, so every control
 * stays a separate tab stop.
 */
export function TemplateCard({ item }: { item: LibraryItem }) {
  const a = useLibraryActions()
  const titleId = useId()
  const descId = useId()
  return (
    <article
      aria-labelledby={titleId}
      className={cn(
        'group relative flex h-full min-h-[184px] flex-col overflow-hidden rounded-lg border border-border bg-surface',
        'shadow-card transition-[border-color,box-shadow] dark:shadow-card-dark',
        'hover:border-primary/40 hover:shadow-elevated focus-within:border-primary/40',
      )}
    >
      <span className={cn('absolute inset-x-0 top-0 h-0.5', TONE_BAR[categoryTone(item.category)])} aria-hidden />
      <div className="flex items-start gap-3 px-4 pb-2 pt-4">
        <CategoryIcon icon={item.icon} category={item.category} />
        <div className="min-w-0 flex-1">
          <h3 id={titleId} className="text-sm font-semibold leading-snug text-text">
            <button
              type="button"
              onClick={() => a.showDetails(item)}
              aria-haspopup="dialog"
              aria-describedby={descId}
              className={cn(
                'line-clamp-1 text-left transition-colors group-hover:text-primary focus-visible:outline-none',
                // Stretch the hit area over the card; the ring follows the card outline.
                'after:absolute after:inset-0 after:rounded-lg after:content-[""]',
                'focus-visible:after:ring-2 focus-visible:after:ring-inset focus-visible:after:ring-primary/50',
              )}
            >
              {item.title}
            </button>
          </h3>
          <p className="mt-0.5 truncate text-xs text-muted">{item.categoryTitle}</p>
        </div>
        <FavoriteButton
          item={item}
          pressed={a.isFavorite(item)}
          onToggle={() => a.toggleFavorite(item)}
          className="relative z-10 -mr-1.5 -mt-1"
        />
      </div>

      <p id={descId} className="line-clamp-2 min-h-[2rem] px-4 text-xs leading-4 text-text2">
        {item.description || <span className="text-muted">No description.</span>}
      </p>
      <p className="mt-2 truncate px-4 text-xs tabular-nums text-muted">{metaLine(item)}</p>
      <div className="mb-3 mt-2 flex min-w-0 flex-wrap items-center gap-1.5 px-4">
        <OriginBadges item={item} />
      </div>

      <div className="relative z-10 mt-auto flex items-center gap-1 border-t border-border/70 bg-surface2/30 px-2 py-1.5">
        <QuickActions item={item} className="min-w-0 flex-1" />
        <TemplateMenu item={item} />
      </div>
    </article>
  )
}

/**
 * A compact shelf card (Featured, Recently viewed): the body opens the
 * report; the info button opens its details.
 */
export function ShelfCard({ item, viewedAt }: { item: LibraryItem; viewedAt?: number }) {
  const a = useLibraryActions()
  const { branding } = useReportBranding()
  const sub = viewedAt
    ? `Viewed ${viewedAgo(viewedAt) ?? formatDate(viewedAt, branding)}`
    : [periodLabel(item.defaultPeriod), plural(item.entries.length, 'section')].filter(Boolean).join(' · ')
  return (
    <div className="group relative flex h-full w-full min-w-0 items-center gap-3 rounded-lg border border-border bg-surface p-3 shadow-card transition-colors hover:border-primary/40 focus-within:border-primary/40 dark:shadow-card-dark">
      <CategoryIcon icon={item.icon} category={item.category} size="sm" />
      <div className="min-w-0 flex-1">
        <Link
          to={openLink(item)}
          onClick={() => a.opened(item)}
          className={cn(
            'block truncate text-sm font-medium text-text group-hover:text-primary focus-visible:outline-none',
            'after:absolute after:inset-0 after:rounded-lg after:content-[""] focus-visible:after:ring-2 focus-visible:after:ring-inset focus-visible:after:ring-primary/50',
          )}
          aria-label={`Open ${item.title}`}
        >
          {item.title}
        </Link>
        <p className="truncate text-xs tabular-nums text-muted">{sub}</p>
      </div>
      <button
        type="button"
        onClick={() => a.showDetails(item)}
        aria-haspopup="dialog"
        aria-label={`Details for ${item.title}`}
        title="Details"
        className="relative z-10 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
      >
        <Info className="h-4 w-4" aria-hidden />
      </button>
    </div>
  )
}
