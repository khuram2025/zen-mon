import { useId, useMemo, useState, type RefObject } from 'react'
import { ChevronDown, Info, Layers, Plus, Search, SearchX, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { reportIcon } from '../icons'
import type { ReportCategory, SectionDef } from '../types'
import { catalogCategoryFor, groupSections, MAX_SECTIONS, permissionLabel } from './model'
import { DND_SECTION, Hint, SectionBadges } from './ui'

export interface SectionLibraryProps {
  sections: SectionDef[]
  categories: ReportCategory[]
  /** How many times each section id is already in the template. */
  usage: Record<string, number>
  /** The template is at the section limit. */
  full: boolean
  onAdd: (def: SectionDef) => void
  /** Offer drag-to-add (the outline is visible next to the library). */
  draggable?: boolean
  searchRef?: RefObject<HTMLInputElement>
  className?: string
}

function describeScope(def: SectionDef): string {
  if (def.scope === 'device') return 'Follows the device scope chosen in the viewer or a schedule.'
  if (def.scope === 'netflow') return 'Follows the NetFlow scope chosen in the viewer or a schedule.'
  return 'Fleet-wide: covers everything the viewer may see.'
}

/**
 * The section library: searchable, grouped by category. Click a section (or
 * drag it into the outline) to add it; sections the user's role cannot see
 * stay addable but are flagged.
 */
export function SectionLibrary({ sections, categories, usage, full, onAdd, draggable, searchRef, className }: SectionLibraryProps) {
  const [query, setQuery] = useState('')
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set())
  const [expanded, setExpanded] = useState<string | null>(null)
  const baseId = useId()
  const groups = useMemo(() => groupSections(sections, query), [sections, query])
  const shown = groups.reduce((n, g) => n + g.sections.length, 0)
  const searching = query.trim().length > 0

  const toggleGroup = (cat: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(cat)) next.delete(cat)
      else next.add(cat)
      return next
    })

  return (
    <div className={cn('flex min-h-0 flex-1 flex-col', className)}>
      <div className="space-y-2 border-b border-border px-3 py-2.5">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
          <input
            ref={searchRef}
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Escape' && query) { e.preventDefault(); e.stopPropagation(); setQuery('') } }}
            aria-label="Search sections"
            aria-controls={`${baseId}-list`}
            placeholder="Search sections"
            className="h-8 w-full rounded-md border border-border bg-surface pl-8 pr-7 text-xs text-text shadow-sm placeholder:text-muted focus-visible:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 [&::-webkit-search-cancel-button]:hidden"
          />
          {query && (
            <button
              type="button"
              onClick={() => setQuery('')}
              aria-label="Clear search"
              className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          )}
        </div>
        <p className="text-[11px] text-muted" aria-live="polite">
          {searching ? (
            <><span className="tabular-nums">{shown}</span> of <span className="tabular-nums">{sections.length}</span> sections match</>
          ) : (
            <><span className="tabular-nums">{sections.length}</span> sections · click to add{draggable ? ' or drag into the outline' : ''}</>
          )}
        </p>
      </div>

      {full && (
        <p role="note" className="mx-3 mt-2.5 rounded-md border border-warning/40 bg-warning/5 px-2.5 py-2 text-[11px] leading-relaxed text-text2">
          This template has the maximum of {MAX_SECTIONS} sections. Remove one to add another.
        </p>
      )}

      <div id={`${baseId}-list`} className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
        {groups.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
            <SearchX className="h-6 w-6 text-muted" aria-hidden />
            <p className="text-sm font-medium text-text">No sections match “{query.trim()}”</p>
            <p className="text-xs text-muted">Try a shorter word, a data source such as “netflow”, or a metric such as “cpu”.</p>
            <Button type="button" size="sm" variant="outline" onClick={() => setQuery('')}>Clear search</Button>
          </div>
        ) : (
          groups.map((g) => {
            const cat = catalogCategoryFor(g.category, categories)
            const Icon = reportIcon(cat?.icon, Layers)
            const open = searching || !collapsed.has(g.category)
            const listId = `${baseId}-g-${g.category.replace(/\W+/g, '-')}`
            return (
              <section key={g.category} className="mb-1">
                <h3>
                  <button
                    type="button"
                    aria-expanded={open}
                    aria-controls={listId}
                    disabled={searching}
                    onClick={() => toggleGroup(g.category)}
                    className="flex w-full items-center gap-2 rounded-md px-1.5 py-1.5 text-left text-[11px] font-semibold uppercase tracking-wider text-text2 transition-colors hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:cursor-default disabled:hover:bg-transparent"
                  >
                    <ChevronDown className={cn('h-3.5 w-3.5 shrink-0 text-muted transition-transform', !open && '-rotate-90')} aria-hidden />
                    <Icon className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />
                    <span className="truncate">{g.category}</span>
                    <span className="ml-auto rounded-full bg-surface2 px-1.5 text-[10px] font-medium tabular-nums text-muted">
                      {g.sections.length}
                    </span>
                  </button>
                </h3>
                {open && (
                  <ul id={listId} className="mt-0.5 space-y-0.5 pb-1.5">
                    {g.sections.map((s) => (
                      <LibraryItem
                        key={s.id}
                        def={s}
                        used={usage[s.id] ?? 0}
                        full={full}
                        expanded={expanded === s.id}
                        onToggle={() => setExpanded((cur) => (cur === s.id ? null : s.id))}
                        onAdd={() => onAdd(s)}
                        draggable={draggable}
                        baseId={baseId}
                      />
                    ))}
                  </ul>
                )}
              </section>
            )
          })
        )}
      </div>
    </div>
  )
}

function LibraryItem({ def, used, full, expanded, onToggle, onAdd, draggable, baseId }: {
  def: SectionDef
  used: number
  full: boolean
  expanded: boolean
  onToggle: () => void
  onAdd: () => void
  draggable?: boolean
  baseId: string
}) {
  const descId = `${baseId}-d-${def.id}`
  const canDrag = draggable && !full
  return (
    <li>
      <div
        draggable={canDrag}
        onDragStart={canDrag ? (e) => {
          e.dataTransfer.setData(DND_SECTION, def.id)
          e.dataTransfer.setData('text/plain', def.title)
          e.dataTransfer.effectAllowed = 'copy'
        } : undefined}
        className={cn(
          'group flex items-start gap-0.5 rounded-lg border transition-colors',
          expanded ? 'border-border bg-surface2/50' : 'border-transparent hover:border-border hover:bg-surface2/60',
          canDrag && 'cursor-grab active:cursor-grabbing',
        )}
      >
        <Hint content={<span className="line-clamp-4">{def.description}</span>} side="right" delay={600}>
          <button
            type="button"
            disabled={full || !def.available}
            onClick={onAdd}
            aria-describedby={expanded ? descId : undefined}
            className="flex min-w-0 flex-1 items-start gap-2 rounded-lg px-2 py-1.5 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <span
              aria-hidden
              className="mt-px flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary transition-colors group-hover:bg-primary group-hover:text-on-primary"
            >
              <Plus className="h-3 w-3" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-[13px] font-medium leading-5 text-text">
                <span className="sr-only">Add </span>
                {def.title}
              </span>
              {(used > 0 || !def.available || def.semantics === 'snapshot') && (
                <span className="mt-0.5 flex flex-wrap items-center gap-1">
                  {used > 0 && (
                    <span className="inline-flex items-center rounded border border-primary/30 bg-primary/10 px-1.5 py-px text-[10px] font-medium leading-4 text-primary">
                      In template{used > 1 ? ` ×${used}` : ''}
                    </span>
                  )}
                  <SectionBadges def={def} showScope={false} tooltips={false} />
                </span>
              )}
            </span>
          </button>
        </Hint>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          aria-controls={descId}
          aria-label={`About ${def.title}`}
          className={cn(
            'mr-1 mt-1 shrink-0 rounded-md p-1 text-muted transition-colors hover:bg-surface3 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
            expanded && 'text-primary',
          )}
        >
          <Info className="h-3.5 w-3.5" aria-hidden />
        </button>
      </div>
      {expanded && (
        <div id={descId} className="space-y-1.5 px-2 pb-2 pl-9 pt-1 text-[11px] leading-relaxed text-text2">
          <p>{def.description}</p>
          <p className="text-muted">
            {def.semantics === 'snapshot' ? 'Snapshot: shows the state when the report runs. ' : 'Covers the report period. '}
            {describeScope(def)}
          </p>
          <p className="text-muted">
            Data access: {def.requires.length ? def.requires.map(permissionLabel).join(', ') : 'report access only'}
            {!def.available && <span className="font-medium text-warning-fg"> — your role lacks this, so you can't add it to a template.</span>}
          </p>
        </div>
      )}
    </li>
  )
}
