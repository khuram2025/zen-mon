/**
 * Library toolbar: search ("/" focuses it), audience, origin and sort,
 * the grid/table toggle and the result count.
 */
import { useEffect, useRef, type ReactNode, type RefObject } from 'react'
import { LayoutGrid, Rows3, Search, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import type { LibraryView } from '../storage'
import { SORTS, type LibraryFilters, type LibrarySort, type OriginFilter } from './model'

const ORIGINS: { id: OriginFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'builtin', label: 'Built-in' },
  { id: 'custom', label: 'Custom' },
]

function isTypingTarget(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable
}

/**
 * Focus the search box on "/" unless the user is typing or a dialog is open.
 * Listens on window, after the sidebar's document-level "/" (Jump to), so the
 * page search wins on this page — the same convention as the Devices page.
 */
function useSlashFocus(ref: RefObject<HTMLInputElement>) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return
      if (isTypingTarget(e.target)) return
      if (document.querySelector('[role="dialog"][data-state="open"], [role="menu"][data-state="open"]')) return
      e.preventDefault()
      ref.current?.focus()
      ref.current?.select()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [ref])
}

export function SearchBox({ value, onChange, placeholder, label }: {
  value: string
  onChange: (q: string) => void
  placeholder: string
  label: string
}) {
  const ref = useRef<HTMLInputElement>(null)
  useSlashFocus(ref)
  return (
    <div role="search" className="relative w-full min-w-0 sm:w-auto sm:min-w-[220px] sm:flex-1">
      <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden />
      <label htmlFor="report-library-search" className="sr-only">{label}</label>
      <Input
        ref={ref}
        id="report-library-search"
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && value) {
            e.preventDefault()
            onChange('')
          }
        }}
        placeholder={placeholder}
        autoComplete="off"
        aria-keyshortcuts="/"
        className="pl-8 pr-9 [&::-webkit-search-cancel-button]:hidden"
      />
      {value ? (
        <button
          type="button"
          onClick={() => {
            onChange('')
            ref.current?.focus()
          }}
          aria-label="Clear search"
          className="absolute right-1.5 top-1/2 inline-flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded text-muted hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      ) : (
        <kbd className="pointer-events-none absolute right-2 top-1/2 hidden -translate-y-1/2 rounded border border-border bg-surface2 px-1.5 py-px font-mono text-2xs font-medium text-muted sm:inline-block" aria-hidden>
          /
        </kbd>
      )}
    </div>
  )
}

function Segmented<T extends string>({ label, value, options, onChange }: {
  label: string
  value: T
  options: { id: T; label: string }[]
  onChange: (v: T) => void
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex h-9 shrink-0 items-center gap-0.5 rounded-md border border-border bg-surface2/50 p-0.5">
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          aria-pressed={value === o.id}
          onClick={() => onChange(o.id)}
          className={cn(
            'h-full rounded px-2.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
            value === o.id ? 'bg-surface text-text shadow-sm' : 'text-text2 hover:text-text',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

export function LayoutToggle({ value, onChange }: { value: LibraryView; onChange: (v: LibraryView) => void }) {
  const opts: { id: LibraryView; label: string; icon: ReactNode }[] = [
    { id: 'grid', label: 'Cards', icon: <LayoutGrid className="h-4 w-4" aria-hidden /> },
    { id: 'table', label: 'Table', icon: <Rows3 className="h-4 w-4" aria-hidden /> },
  ]
  return (
    <div role="group" aria-label="Layout" className="ml-auto inline-flex h-9 shrink-0 items-center gap-0.5 rounded-md border border-border bg-surface2/50 p-0.5 sm:ml-0">
      {opts.map((o) => (
        <button
          key={o.id}
          type="button"
          aria-pressed={value === o.id}
          aria-label={`${o.label} layout`}
          title={`${o.label} layout`}
          onClick={() => onChange(o.id)}
          className={cn(
            'inline-flex h-full w-8 items-center justify-center rounded transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
            value === o.id ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text',
          )}
        >
          {o.icon}
        </button>
      ))}
    </div>
  )
}

export function LibraryFiltersBar({ filters, audiences, layout, onChange, onLayout }: {
  filters: LibraryFilters
  audiences: string[]
  layout: LibraryView
  onChange: (patch: Partial<LibraryFilters>) => void
  onLayout: (v: LibraryView) => void
}) {
  return (
    <>
      <Select value={filters.audience ?? 'all'} onValueChange={(v) => onChange({ audience: v === 'all' ? null : v })}>
        <SelectTrigger aria-label={`Audience: ${filters.audience ?? 'all audiences'}`} className="w-[calc(50%-0.25rem)] sm:w-44">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All audiences</SelectItem>
          {audiences.map((a) => <SelectItem key={a} value={a}>{a}</SelectItem>)}
          {filters.audience && !audiences.includes(filters.audience) && (
            <SelectItem value={filters.audience}>{filters.audience}</SelectItem>
          )}
        </SelectContent>
      </Select>
      <Select value={filters.sort} onValueChange={(v) => onChange({ sort: v as LibrarySort })}>
        <SelectTrigger
          aria-label={`Sort by: ${SORTS.find((s) => s.id === filters.sort)?.label ?? 'Relevance'}`}
          className="w-[calc(50%-0.25rem)] sm:w-40"
        >
          <span className="truncate"><span className="text-muted">Sort: </span><SelectValue /></span>
        </SelectTrigger>
        <SelectContent>
          {SORTS.map((s) => <SelectItem key={s.id} value={s.id}>{s.label}</SelectItem>)}
        </SelectContent>
      </Select>
      <Segmented label="Origin" value={filters.origin} options={ORIGINS} onChange={(origin) => onChange({ origin })} />
      <LayoutToggle value={layout} onChange={onLayout} />
    </>
  )
}
