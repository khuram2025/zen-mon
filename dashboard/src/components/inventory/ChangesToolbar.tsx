import { useRef } from 'react'
import { Link } from 'react-router-dom'
import { Search, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ChangesFilters } from '@/pages/inventory/osHardwareChanges'
import { useSlashFocus } from './AssetToolbar'
import { changesPageHref, type ActiveFilter } from './changesModel'
import { FilterChipList } from './FilterChipList'
import { isPlainClick, useChipFocus } from './useChipFocus'
import { useDebouncedSearch } from './useDebouncedSearch'

const KINDS: { value: ChangesFilters['kind'] | 'all'; label: string }[] = [
  { value: 'all', label: 'All assets' },
  { value: 'server', label: 'Servers' },
  { value: 'device', label: 'Network devices' },
]

/**
 * Search, the kind switch and the active filters (each removable). The search box is debounced and replaces the
 * history entry while typing; everything else is a link, so Back and a copied address behave. `/` focuses the box.
 */
export function ChangesToolbar({
  filters, active, pageSize, onQ, trailing,
}: {
  filters: ChangesFilters
  active: readonly ActiveFilter[]
  pageSize: number
  onQ: (q: string) => void
  /** The Export button, when the caller may export. */
  trailing?: React.ReactNode
}) {
  const q = filters.q ?? ''
  const input = useRef<HTMLInputElement>(null)
  const chipList = useRef<HTMLUListElement>(null)
  useSlashFocus(input)
  // The box and the address bar (see useDebouncedSearch): `onQ` must patch `q` against the LIVE address.
  const search = useDebouncedSearch({ value: q, onCommit: onQ })
  const focus = useChipFocus(chipList, input, `${active.map((a) => a.id).join('|')}#${q}`)

  const kind = filters.kind ?? 'all'
  return (
    <div className="space-y-2" data-changes-toolbar="">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[12rem] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden />
          <input
            ref={input}
            type="search"
            value={search.text}
            onChange={(e) => search.onChange(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') search.flush() }}
            placeholder="Search asset, product, version"
            aria-label="Search changes"
            aria-keyshortcuts="/"
            maxLength={200}
            className="h-9 w-full rounded-md border border-border bg-surface pl-8 pr-8 text-sm shadow-sm placeholder:text-muted focus-visible:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
          />
          {search.text ? (
            <button
              type="button"
              onClick={() => { search.clear(); input.current?.focus() }}
              aria-label="Clear search"
              className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-primary"
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          ) : (
            <kbd className="pointer-events-none absolute right-2 top-1/2 hidden -translate-y-1/2 rounded border border-border bg-surface2 px-1.5 text-[10px] text-muted sm:block" aria-hidden>/</kbd>
          )}
        </div>
        <div role="group" aria-label="Kind of asset" className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/60 p-0.5">
          {KINDS.map((k) => {
            const on = kind === k.value
            const next: ChangesFilters = { ...filters, kind: k.value === 'all' ? undefined : k.value }
            return (
              <Link
                key={k.value}
                to={changesPageHref(next, { page_size: pageSize })}
                aria-current={on ? 'true' : undefined}
                className={cn('whitespace-nowrap rounded px-2.5 py-1 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-primary', on ? 'bg-primary text-on-primary shadow-sm' : 'text-text2 hover:bg-surface3 hover:text-text')}
              >
                {k.label}
              </Link>
            )
          })}
        </div>
        {trailing}
      </div>
      {(active.length > 0 || q) && (
        <div className="flex min-h-[24px] flex-wrap items-center gap-x-3 gap-y-1.5">
          <FilterChipList
            items={active.map((a) => ({ id: a.id, label: a.label, text: a.text, href: changesPageHref(a.without, { page_size: pageSize }) }))}
            listRef={chipList}
            onRemove={(index, e) => focus.removed(index, e)}
            maxWidth="max-w-[18rem]"
          />
          <Link
            to={changesPageHref({ since: filters.since, until: filters.until }, { page_size: pageSize })}
            // A modified click opens a new tab and leaves this page as it is: the box must keep its text (NF-05).
            onClick={(e) => { if (isPlainClick(e)) { search.reset(); focus.cleared(e) } }}
            className="rounded text-xs font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary"
          >
            Clear all
          </Link>
        </div>
      )}
    </div>
  )
}
