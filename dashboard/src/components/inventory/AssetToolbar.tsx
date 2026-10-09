import { useEffect, useRef } from 'react'
import * as Menu from '@radix-ui/react-dropdown-menu'
import { Check, Columns3, Download, Loader2, PanelLeftClose, PanelLeftOpen, Search, SlidersHorizontal, X } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { DropdownMenuContent, DropdownMenuLabel, DropdownMenuSeparator } from '@/components/ui/DropdownMenu'
import { cn } from '@/lib/utils'
import type { activeFilterChips } from '@/pages/inventory/filters'
import { ASSET_COLUMNS, type ColumnId, type Density } from './assetModel'
import { fmtInt } from './model'
import { FilterChipList } from './FilterChipList'
import { useChipFocus } from './useChipFocus'
import { useDebouncedSearch } from './useDebouncedSearch'

type Chip = ReturnType<typeof activeFilterChips>[number]

/** `/` focuses the search box, the way the rest of the product's list pages do; ignored while typing or in a dialog. */
export function useSlashFocus(target: React.RefObject<HTMLInputElement>) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return
      const el = e.target as HTMLElement | null
      if (el && (el.isContentEditable || /^(input|textarea|select)$/i.test(el.tagName))) return
      if (document.querySelector('[role="dialog"]')) return
      e.preventDefault()
      target.current?.focus()
      target.current?.select()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [target])
}

const menuItem = 'flex cursor-pointer select-none items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] outline-none data-[highlighted]:bg-surface2 data-[disabled]:opacity-50'

/**
 * Search, the filter pane toggle, the active-filter chips (each removable, with "Clear all"), the column
 * and density chooser and the Export button. Saved views are Phase 2 and have no button yet. Export is
 * only rendered for a user who holds `inventory.export`.
 */
export function AssetToolbar({
  q, onQ, chips, hrefWith, onClear, filtersOpen, onToggleFilters, wide, filterCount,
  columns, onToggleColumn, density, onDensity, canExport, onExport, exporting, total, loading,
}: {
  q: string
  onQ: (q: string) => void
  chips: readonly Chip[]
  /** URL of the page with one filter removed. */
  hrefWith: (chip: Chip) => string
  onClear: () => void
  filtersOpen: boolean
  onToggleFilters: () => void
  /** The facet pane is inline (wide screens) rather than a drawer. */
  wide: boolean
  filterCount: number
  columns: readonly ColumnId[]
  onToggleColumn: (id: ColumnId) => void
  density: Density
  onDensity: (d: Density) => void
  canExport: boolean
  onExport: () => void
  exporting: boolean
  total: number | undefined
  loading: boolean
}) {
  const input = useRef<HTMLInputElement>(null)
  const chipList = useRef<HTMLUListElement>(null)
  useSlashFocus(input)
  // The box and the address bar (see useDebouncedSearch): `onQ` must patch `q` against the LIVE address.
  const search = useDebouncedSearch({ value: q, onCommit: onQ })
  const shown = chips.filter((c) => c.key !== 'q')
  const focus = useChipFocus(chipList, input, `${shown.map((c) => `${c.key}:${c.value}`).join('|')}#${q}`)

  const PaneIcon = wide ? (filtersOpen ? PanelLeftClose : PanelLeftOpen) : SlidersHorizontal
  return (
    <div className="space-y-2" data-asset-toolbar="">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-9"
          onClick={onToggleFilters}
          aria-expanded={wide ? filtersOpen : undefined}
          aria-haspopup={wide ? undefined : 'dialog'}
        >
          <PaneIcon className="h-4 w-4" aria-hidden />
          {wide ? (filtersOpen ? 'Hide filters' : 'Show filters') : 'Filters'}
          {filterCount > 0 && <span className="rounded-full bg-primary/15 px-1.5 text-[10px] font-bold tabular-nums text-primary" aria-label={`${filterCount} active`}>{filterCount}</span>}
        </Button>
        <div className="relative min-w-[12rem] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden />
          <input
            ref={input}
            type="search"
            value={search.text}
            onChange={(e) => search.onChange(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') search.flush() }}
            placeholder="Search name, IP, serial, model, vendor, OS"
            aria-label="Search assets"
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
        <Menu.Root>
          <Menu.Trigger asChild>
            <Button type="button" variant="outline" size="sm" className="h-9" aria-label="Columns and density">
              <Columns3 className="h-4 w-4" aria-hidden /> <span className="hidden sm:inline">Columns</span>
            </Button>
          </Menu.Trigger>
          <DropdownMenuContent className="w-60" align="end">
            <DropdownMenuLabel>Columns</DropdownMenuLabel>
            {ASSET_COLUMNS.map((c) => (
              <Menu.CheckboxItem
                key={c.id}
                checked={columns.includes(c.id)}
                disabled={c.locked}
                // Keep the menu open: several columns are usually changed together.
                onSelect={(e) => { e.preventDefault(); onToggleColumn(c.id) }}
                className={menuItem}
              >
                <span aria-hidden className={cn('flex h-3.5 w-3.5 items-center justify-center rounded-[3px] border', columns.includes(c.id) ? 'border-primary bg-primary text-on-primary' : 'border-border-strong bg-bg')}>
                  {columns.includes(c.id) && <Check className="h-2.5 w-2.5" strokeWidth={3.5} />}
                </span>
                {c.label}
                {c.locked && <span className="ml-auto text-[10px] text-muted">always shown</span>}
              </Menu.CheckboxItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuLabel>Row height</DropdownMenuLabel>
            <Menu.RadioGroup value={density} onValueChange={(v) => onDensity(v === 'compact' ? 'compact' : 'comfortable')}>
              {(['comfortable', 'compact'] as const).map((d) => (
                <Menu.RadioItem key={d} value={d} onSelect={(e) => e.preventDefault()} className={menuItem}>
                  <span aria-hidden className={cn('flex h-3.5 w-3.5 items-center justify-center rounded-full border', density === d ? 'border-primary bg-primary text-on-primary' : 'border-border-strong bg-bg')}>
                    {density === d && <Check className="h-2.5 w-2.5" strokeWidth={3.5} />}
                  </span>
                  {d === 'comfortable' ? 'Comfortable' : 'Compact'}
                </Menu.RadioItem>
              ))}
            </Menu.RadioGroup>
          </DropdownMenuContent>
        </Menu.Root>
        {canExport && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-9"
            onClick={onExport}
            aria-label="Export CSV"
            disabled={exporting || total === 0}
            title={total ? `Export all ${fmtInt(total)} matching assets as CSV` : 'Nothing to export'}
          >
            {exporting ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Download className="h-4 w-4" aria-hidden />}
            <span className="hidden sm:inline">Export</span>
          </Button>
        )}
      </div>
      <div className="flex min-h-[24px] flex-wrap items-center gap-x-3 gap-y-1.5">
        <p className="text-xs text-muted" role="status" aria-live="polite" data-asset-count="">
          {total === undefined ? (loading ? 'Loading assets' : '') : <><strong className="font-semibold tabular-nums text-text2">{fmtInt(total)}</strong> {total === 1 ? 'asset' : 'assets'}{shown.length > 0 || q ? ' match' : ''}</>}
        </p>
        <FilterChipList
          items={shown.map((c) => ({ id: `${c.key}:${c.value}`, label: c.label, text: c.text, href: hrefWith(c) }))}
          listRef={chipList}
          onRemove={(index, e) => focus.removed(index, e)}
        />
        {(shown.length > 0 || q) && (
          <button type="button" onClick={() => { search.reset(); focus.cleared(); onClear() }} className="rounded text-xs font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">
            Clear all
          </button>
        )}
      </div>
    </div>
  )
}
