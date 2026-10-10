import { useId, useRef } from 'react'
import { Download, Loader2, Search, X } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Switch } from '@/components/ui/Switch'
import { cn } from '@/lib/utils'
import { useSlashFocus } from './AssetToolbar'
import { FilterChipList } from './FilterChipList'
import { FilterSelect, type FilterSelectOption } from './SoftwareFilterSelect'
import { fmtInt } from './model'
import {
  categoryLabel, hiddenCategoryNames, hideEnabled, hideSwitchLabel, SOFTWARE_CATEGORIES, type CatalogChip, type CatalogFilters,
} from './softwareModel'
import { useChipFocus } from './useChipFocus'
import { useDebouncedSearch } from './useDebouncedSearch'

/** What a control changes: a patch of the filters, applied by the page to the filters in the address NOW (`undefined` removes a key). */
export type CatalogPatch = { [K in keyof CatalogFilters]?: CatalogFilters[K] | undefined }

/**
 * Search (debounced, `/` focuses it), the category and publisher selects, the hide switch (the API's `hidden`
 * parameter; its label names the categories the settings hide), the active-filter chips (each removable,
 * "Clear all") and the Export button, which only exists for a user who holds `inventory.export`.
 *
 * Every control reports a PATCH, never the whole filter object of this render: a facet or the switch changed while
 * the search text waited its 300 ms must survive the search landing (FE-07), and the box must not be overwritten by
 * the echo of its own commit (FE-02): see useDebouncedSearch.
 */
export function SoftwareToolbar({
  filters, onPatch, chips, hrefWith, onClear, publishers, publisherNote, hiddenCategories, canExport, onExport, exporting, total, loading,
}: {
  filters: CatalogFilters
  onPatch: (patch: CatalogPatch) => void
  chips: readonly CatalogChip[]
  /** URL of the page with one chip removed. */
  hrefWith: (chip: CatalogChip) => string
  onClear: () => void
  /** Options of the publisher select; empty while the facet loads. */
  publishers: readonly FilterSelectOption[]
  publisherNote: string | null
  /** The categories hidden by default, once the catalog has answered. */
  hiddenCategories: readonly string[] | undefined
  canExport: boolean
  onExport: () => void
  exporting: boolean
  total: number | undefined
  loading: boolean
}) {
  const input = useRef<HTMLInputElement>(null)
  const chipList = useRef<HTMLUListElement>(null)
  useSlashFocus(input)
  const q = filters.q ?? ''
  // The box and the address bar: the commit patches `q` alone against the live address.
  const search = useDebouncedSearch({ value: q, onCommit: (next) => onPatch({ q: next || undefined }) })
  // A removed chip or "Clear all" unmounts the focused element: focus goes to the next chip or the box (UI-08, FV1-N1).
  const focus = useChipFocus(chipList, input, `${chips.map((c) => `${c.key}:${c.value}`).join('|')}#${q}`)

  const switchId = useId()
  const hiding = hideEnabled(filters)
  const nothingHidden = hiddenCategories !== undefined && hiddenCategories.length === 0
  const hiddenList = hiddenCategories ? hiddenCategoryNames(hiddenCategories) : undefined
  const categories = filters.category ?? []
  const categoryOptions: FilterSelectOption[] = SOFTWARE_CATEGORIES.map((c) => ({
    value: c,
    label: categoryLabel(c),
    hint: hiddenCategories?.includes(c) ? 'hidden by default' : undefined,
  }))
  const publisherText = chips.find((c) => c.key === 'publisher')?.text
  const showClear = chips.length > 0 || !!q || !!search.text

  return (
    <div className="space-y-2" data-software-toolbar="">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[12rem] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden />
          <input
            ref={input}
            type="search"
            value={search.text}
            onChange={(e) => search.onChange(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') search.flush() }}
            placeholder="Search software"
            aria-label="Search software"
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
        <FilterSelect
          label="Category"
          allLabel="All categories"
          value={categories.length === 1 ? categories[0] : categories.length > 1 ? '__several__' : ''}
          valueText={categories.length > 1 ? `${categories.length} chosen` : undefined}
          options={categoryOptions}
          onChange={(v) => onPatch({ category: v ? [v] : undefined })}
          className="w-[11.5rem]"
        />
        <FilterSelect
          label="Publisher"
          allLabel="All publishers"
          value={filters.publisher ?? ''}
          valueText={publisherText}
          options={publishers.map((p) => ({ ...p, hint: p.hint }))}
          onChange={(v) => onPatch({ publisher: v || undefined })}
          className="w-[13.5rem]"
          contentClassName="max-h-80"
        />
        {canExport && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-9"
            onClick={onExport}
            aria-label="Export CSV"
            disabled={exporting || total === 0}
            title={total ? `Export all ${fmtInt(total)} matching products as CSV` : 'Nothing to export'}
          >
            {exporting ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Download className="h-4 w-4" aria-hidden />}
            <span className="hidden sm:inline">Export</span>
          </Button>
        )}
      </div>
      {publisherNote && <p className="text-[11px] text-muted">{publisherNote}</p>}
      <div className="flex min-h-[28px] flex-wrap items-center gap-x-4 gap-y-2">
        <div className="flex items-center gap-2">
          <Switch
            id={switchId}
            checked={hiding && !nothingHidden}
            disabled={nothingHidden}
            onCheckedChange={(on) => onPatch({ hidden: on ? undefined : 'include' })}
          />
          <label
            htmlFor={switchId}
            className={cn('cursor-pointer text-xs text-text2', nothingHidden && 'cursor-not-allowed text-muted')}
            title={nothingHidden ? 'No category is hidden by default (Inventory settings).' : hiddenList ? `Hides products in: ${hiddenList}. Named in the category filter, they are always listed.` : undefined}
          >
            {hideSwitchLabel(hiddenCategories)}
          </label>
        </div>
        <p className="text-xs text-muted" role="status" aria-live="polite" data-software-count="">
          {total === undefined ? (loading ? 'Loading products' : '') : <><strong className="font-semibold tabular-nums text-text2">{fmtInt(total)}</strong> {total === 1 ? 'product' : 'products'}{chips.length > 0 || q ? ' match' : ''}</>}
        </p>
        <FilterChipList
          items={chips.map((c) => ({ id: `${c.key}:${c.value}`, label: c.label, text: c.text, href: hrefWith(c) }))}
          listRef={chipList}
          onRemove={(index, e) => focus.removed(index, e)}
        />
        {showClear && (
          <button type="button" onClick={() => { search.reset(); focus.cleared(); onClear() }} className="rounded text-xs font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">
            Clear all
          </button>
        )}
      </div>
    </div>
  )
}
