/** Table layout of the library: the same data and actions as the cards, easier to compare. */
import { ArrowDownAZ, ArrowUpDown, Lock, Users } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { QuickActions, TemplateMenu, useLibraryActions } from './actions'
import { periodLabel, type LibraryItem, type LibrarySort } from './model'
import { CategoryIcon, Chip, FavoriteButton } from './parts'

export function TemplateTable({ items, sort, onSortByName, caption }: {
  items: LibraryItem[]
  sort: LibrarySort
  /** Toggle between name order and the toolbar's default order. */
  onSortByName: () => void
  caption: string
}) {
  const a = useLibraryActions()
  const byName = sort === 'name'
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface shadow-card dark:shadow-card-dark">
      <Table className="text-[13px]">
        <caption className="sr-only">{caption}</caption>
        <THead className="bg-surface2/50">
          <Tr className="hover:bg-transparent">
            <Th className="w-10 px-2"><span className="sr-only">Favourite</span></Th>
            <Th aria-sort={byName ? 'ascending' : 'none'} className="min-w-[240px]">
              <button
                type="button"
                onClick={onSortByName}
                className="inline-flex items-center gap-1 rounded uppercase tracking-wider hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
                title={byName ? 'Sorted by name — click for the default order' : 'Sort by name'}
              >
                Name
                {byName ? <ArrowDownAZ className="h-3.5 w-3.5" aria-hidden /> : <ArrowUpDown className="h-3 w-3 opacity-60" aria-hidden />}
              </button>
            </Th>
            <Th className="hidden lg:table-cell">Category</Th>
            <Th>Origin</Th>
            <Th className="text-right">Sections</Th>
            <Th className="hidden xl:table-cell">Default period</Th>
            <Th className="hidden 2xl:table-cell">Audience</Th>
            <Th className="text-right">Schedules</Th>
            <Th className="text-right"><span className="sr-only">Actions</span></Th>
          </Tr>
        </THead>
        <TBody>
          {items.map((item) => (
            <Tr key={item.ref} className="align-top">
              <Td className="px-2 py-2.5">
                <FavoriteButton item={item} pressed={a.isFavorite(item)} onToggle={() => a.toggleFavorite(item)} />
              </Td>
              <Td className="py-2.5">
                <div className="flex min-w-0 items-start gap-2.5">
                  <CategoryIcon icon={item.icon} category={item.category} size="sm" className="mt-0.5" />
                  <div className="min-w-0">
                    <button
                      type="button"
                      onClick={() => a.showDetails(item)}
                      aria-haspopup="dialog"
                      className="text-left font-medium text-text hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 rounded-sm"
                    >
                      {item.title}
                    </button>
                    {item.description && <p className="line-clamp-1 max-w-[52ch] text-xs text-muted">{item.description}</p>}
                  </div>
                </div>
              </Td>
              <Td className="hidden py-2.5 text-xs text-text2 lg:table-cell xl:whitespace-nowrap">{item.categoryTitle}</Td>
              <Td className="py-2.5">
                <div className="flex flex-wrap items-center gap-1">
                  {item.origin === 'builtin' ? (
                    <Chip>Built-in</Chip>
                  ) : (
                    <>
                      <Chip tone="info">Custom</Chip>
                      <Chip icon={item.visibility === 'shared' ? <Users className="h-3 w-3" aria-hidden /> : <Lock className="h-3 w-3" aria-hidden />}>
                        {item.visibility === 'shared' ? 'Shared' : 'Private'}
                      </Chip>
                    </>
                  )}
                </div>
                {item.origin === 'custom' && (
                  <p className="mt-0.5 truncate text-[11px] text-muted">by {item.isOwner ? 'you' : item.ownerName || 'a former user'}</p>
                )}
              </Td>
              <Td className="py-2.5 text-right tabular-nums text-text2">{item.entries.length}</Td>
              <Td className="hidden whitespace-nowrap py-2.5 text-xs text-text2 xl:table-cell">{periodLabel(item.defaultPeriod) || '—'}</Td>
              <Td className="hidden py-2.5 text-xs text-text2 2xl:table-cell">{item.audience.join(', ') || '—'}</Td>
              <Td className={cn('py-2.5 text-right tabular-nums', item.schedules ? 'text-text' : 'text-muted')}>
                {item.origin === 'custom' ? item.schedules : '—'}
              </Td>
              <Td className="py-2">
                <div className="flex items-center justify-end gap-0.5">
                  <QuickActions item={item} iconsOnly className="flex-nowrap justify-end" />
                  <TemplateMenu item={item} />
                </div>
              </Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  )
}
