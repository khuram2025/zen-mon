/** What the library shows: the "All" overview (shelves + category sections), or a flat result list. */
import type { ReactNode } from 'react'
import { ChevronRight, History, Sparkles } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import type { RecentEntry } from '../storage'
import {
  featuredItems, groupByCategory, recentItems, type LibraryItem, type LibrarySort, type RailCategory,
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

function Shelf({ id, icon, title, hint, children }: { id: string; icon: ReactNode; title: string; hint?: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="space-y-2">
      <div className="flex items-baseline gap-2">
        <h2 id={id} className="flex items-center gap-1.5 text-sm font-semibold text-text">
          {icon}
          {title}
        </h2>
        {hint && <span className="text-xs text-muted">{hint}</span>}
      </div>
      <ul role="list" className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(min(100%,17rem),1fr))]">
        {children}
      </ul>
    </section>
  )
}

/** Unfiltered "All reports": Featured and Recently viewed shelves, then one section per category. */
export function LibraryOverview({ items, categories, recent, onSelectCategory }: {
  items: LibraryItem[]
  categories: ReportCategory[]
  recent: RecentEntry[]
  onSelectCategory: (id: string) => void
}) {
  const featured = featuredItems(items)
  const seen = recentItems(items, recent)
  const groups = groupByCategory(items, categories)
  return (
    <div className="space-y-7">
      {featured.length > 0 && (
        <Shelf id="rl-featured" icon={<Sparkles className="h-4 w-4 text-primary" aria-hidden />} title="Featured" hint="Good places to start">
          {featured.map((item) => (
            <li key={item.ref}><ShelfCard item={item} /></li>
          ))}
        </Shelf>
      )}
      {seen.length > 0 && (
        <Shelf id="rl-recent" icon={<History className="h-4 w-4 text-muted" aria-hidden />} title="Recently viewed">
          {seen.map(({ item, at }) => (
            <li key={item.ref}><ShelfCard item={item} viewedAt={at} /></li>
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
        {more && (
          <Button type="button" variant="ghost" size="sm" className="h-7 shrink-0 gap-1 px-2 text-xs text-primary [&_svg]:size-3.5" onClick={onViewAll}>
            View all {items.length}
            <ChevronRight aria-hidden />
            <span className="sr-only"> in {category.title}</span>
          </Button>
        )}
      </div>
      <CardGrid items={more ? items.slice(0, OVERVIEW_PER_CATEGORY) : items} label={category.title} />
    </section>
  )
}

export function LibraryList({ items, layout, sort, onSortByName, label }: {
  items: LibraryItem[]
  layout: 'grid' | 'table'
  sort: LibrarySort
  onSortByName: () => void
  label: string
}) {
  return layout === 'table'
    ? <TemplateTable items={items} sort={sort} onSortByName={onSortByName} caption={label} />
    : <CardGrid items={items} label={label} />
}
