/** What the library shows: the "All" overview (shelves + category sections), or a flat result list. */
import type { ReactNode } from 'react'
import { ChevronRight, History, Sparkles, Star } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import type { RecentEntry, ReportRef } from '../storage'
import {
  favoriteItems, featuredItems, groupByCategory, recentItems,
  type LibraryItem, type LibrarySort, type LibraryViewId, type RailCategory,
} from './model'
import type { ReportCategory } from '../types'
import { CategoryIcon } from './parts'
import { ShelfCard, TemplateCard } from './TemplateCard'
import { TemplateTable } from './TemplateTable'

export const CARD_GRID = 'grid gap-3 grid-cols-[repeat(auto-fill,minmax(min(100%,20rem),1fr))]'

/** Cards shown per category on the overview before "View all". */
export const OVERVIEW_PER_CATEGORY = 6

export function CardGrid({ items, label }: { items: LibraryItem[]; label: string }) {
  return (
    <ul role="list" aria-label={label} className={CARD_GRID}>
      {items.map((item) => (
        <li key={item.ref} className="min-w-0">
          <TemplateCard item={item} />
        </li>
      ))}
    </ul>
  )
}

function ViewAllButton({ label, count, onClick }: { label: string; count?: number; onClick: () => void }) {
  return (
    <Button type="button" variant="ghost" size="sm" className="h-7 shrink-0 gap-1 px-2 text-xs text-primary [&_svg]:size-3.5" onClick={onClick}>
      View all{count !== undefined ? ` ${count}` : ''}
      <ChevronRight aria-hidden />
      <span className="sr-only"> {label}</span>
    </Button>
  )
}

function Shelf({ id, icon, title, hint, action, children }: {
  id: string
  icon: ReactNode
  title: string
  hint?: string
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <section aria-labelledby={id} className="space-y-2">
      <div className="flex min-h-7 items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <h2 id={id} className="flex items-center gap-1.5 text-sm font-semibold text-text">
            {icon}
            {title}
          </h2>
          {hint && <span className="hidden truncate text-xs text-muted sm:inline">{hint}</span>}
        </div>
        {action}
      </div>
      <ul role="list" aria-labelledby={id} className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(min(100%,17rem),1fr))]">
        {children}
      </ul>
    </section>
  )
}

/**
 * Unfiltered "All reports": the viewer's Favourites and Recently viewed
 * shelves (when they have any), the Featured templates, then one section per
 * category.
 */
export function LibraryOverview({ items, categories, favorites, recent, onSelectCategory, onSelectView }: {
  items: LibraryItem[]
  categories: ReportCategory[]
  favorites: ReportRef[]
  recent: RecentEntry[]
  onSelectCategory: (id: string) => void
  onSelectView: (view: LibraryViewId) => void
}) {
  const starred = favoriteItems(items, favorites)
  const starredTotal = items.filter((i) => favorites.includes(i.ref)).length
  const seen = recentItems(items, recent)
  const seenTotal = items.filter((i) => recent.some((r) => r.ref === i.ref)).length
  const featured = featuredItems(items)
  const groups = groupByCategory(items, categories)
  return (
    <div className="space-y-7">
      {starred.length > 0 && (
        <Shelf
          id="rl-favorites"
          icon={<Star className="h-4 w-4 fill-current text-warning-fg" aria-hidden />}
          title="Favourites"
          action={starredTotal > starred.length ? <ViewAllButton label="favourites" count={starredTotal} onClick={() => onSelectView('favorites')} /> : undefined}
        >
          {starred.map((item) => (
            <li key={item.ref}><ShelfCard item={item} /></li>
          ))}
        </Shelf>
      )}
      {seen.length > 0 && (
        <Shelf
          id="rl-recent"
          icon={<History className="h-4 w-4 text-muted" aria-hidden />}
          title="Recently viewed"
          action={seenTotal > seen.length ? <ViewAllButton label="recently viewed reports" count={seenTotal} onClick={() => onSelectView('recent')} /> : undefined}
        >
          {seen.map(({ item, at }) => (
            <li key={item.ref}><ShelfCard item={item} viewedAt={at} /></li>
          ))}
        </Shelf>
      )}
      {featured.length > 0 && (
        <Shelf id="rl-featured" icon={<Sparkles className="h-4 w-4 text-primary" aria-hidden />} title="Featured" hint="Good places to start">
          {featured.map((item) => (
            <li key={item.ref}><ShelfCard item={item} /></li>
          ))}
        </Shelf>
      )}
      {groups.map(({ category, items: groupItems }) => (
        <CategorySection
          key={category.id}
          category={category}
          items={groupItems}
          onViewAll={() => onSelectCategory(category.id)}
        />
      ))}
    </div>
  )
}

function CategorySection({ category, items, onViewAll }: { category: RailCategory; items: LibraryItem[]; onViewAll: () => void }) {
  const headingId = `rl-cat-${category.id}`
  const more = items.length > OVERVIEW_PER_CATEGORY
  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <div className="flex items-center justify-between gap-3 border-b border-border/70 pb-2">
        <div className="flex min-w-0 items-center gap-2.5">
          <CategoryIcon icon={category.icon} category={category.id} size="sm" />
          <div className="min-w-0">
            <h2 id={headingId} className="text-sm font-semibold text-text">
              {category.title} <span className="ml-0.5 font-normal tabular-nums text-muted">{items.length}</span>
            </h2>
            {category.description && <p className="hidden truncate text-xs text-muted sm:block">{category.description}</p>}
          </div>
        </div>
        {more && <ViewAllButton label={`in ${category.title}`} count={items.length} onClick={onViewAll} />}
      </div>
      <CardGrid items={more ? items.slice(0, OVERVIEW_PER_CATEGORY) : items} label={category.title} />
    </section>
  )
}

export function LibraryList({ items, layout, sort, onSortByName, label, showCategory }: {
  items: LibraryItem[]
  layout: 'grid' | 'table'
  sort: LibrarySort
  onSortByName: () => void
  label: string
  /** False when the list is one category. */
  showCategory: boolean
}) {
  return layout === 'table'
    ? <TemplateTable items={items} sort={sort} onSortByName={onSortByName} caption={label} showCategory={showCategory} />
    : <CardGrid items={items} label={label} />
}
