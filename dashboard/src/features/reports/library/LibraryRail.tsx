/**
 * Library navigation: views (All, Favourites, Recently viewed, My templates,
 * Shared, Built-in, Delivered) and every catalog category with its count.
 * A sticky rail from `lg`; a grouped select below it.
 */
import { useEffect, useRef, type ReactNode, type RefObject } from 'react'
import * as SelectPrimitive from '@radix-ui/react-select'
import {
  History, Inbox, LayoutGrid, Package, Star, UserRound, Users, type LucideIcon,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { reportIcon } from '../icons'
import { LIBRARY_VIEWS, type LibraryViewId, type RailCategory } from './model'
import { categoryTone, TONE_TILE } from './tones'

export const VIEW_ICONS: Record<LibraryViewId, LucideIcon> = {
  all: LayoutGrid,
  favorites: Star,
  recent: History,
  mine: UserRound,
  shared: Users,
  builtin: Package,
  delivered: Inbox,
}

export interface RailSelection {
  view: LibraryViewId
  category: string | null
}

interface RailProps {
  selection: RailSelection
  viewCounts: Partial<Record<LibraryViewId, number>>
  categories: RailCategory[]
  onSelect: (s: RailSelection) => void
  loading?: boolean
}

function RailButton({ icon, label, count, active, onClick, dim }: {
  icon: ReactNode
  label: string
  count?: number
  active: boolean
  onClick: () => void
  dim?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? 'true' : undefined}
      className={cn(
        'group flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left text-[13px] transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
        active ? 'bg-primary/10 font-semibold text-primary' : dim ? 'text-muted hover:bg-surface2 hover:text-text' : 'text-text2 hover:bg-surface2 hover:text-text',
      )}
    >
      {icon}
      <span className="min-w-0 flex-1 leading-snug">{label}</span>
      {count !== undefined && (
        <span className={cn('shrink-0 text-[11px] tabular-nums', active ? 'text-primary' : 'text-muted')}>{count}</span>
      )}
    </button>
  )
}

/** Space kept above the rail: the app header (44px) plus a gap. */
const RAIL_TOP = 60

/**
 * Keeps the rail in view while the page scrolls. The app's <main> is an
 * overflow container that does not scroll itself (the document does), so
 * `position: sticky` inside it never engages; the rail follows the window
 * with a transform instead, clamped to its column. If <main> ever becomes
 * the scroller, `sticky` takes over and the window never scrolls.
 */
function useFollowWindow(ref: RefObject<HTMLDivElement>) {
  useEffect(() => {
    const el = ref.current
    const column = el?.parentElement
    if (!el || !column) return
    let frame = 0
    const place = () => {
      frame = 0
      if (column.offsetParent === null) { el.style.transform = ''; return } // hidden below lg
      const box = column.getBoundingClientRect()
      const room = Math.max(0, box.height - el.offsetHeight)
      const shift = Math.min(room, Math.max(0, RAIL_TOP - box.top))
      el.style.transform = shift > 0.5 ? `translate3d(0, ${Math.round(shift)}px, 0)` : ''
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(place) }
    window.addEventListener('scroll', schedule, { passive: true })
    window.addEventListener('resize', schedule)
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(schedule) : null
    ro?.observe(column)
    ro?.observe(el)
    place()
    return () => {
      window.removeEventListener('scroll', schedule)
      window.removeEventListener('resize', schedule)
      ro?.disconnect()
      if (frame) cancelAnimationFrame(frame)
    }
  }, [ref])
}

export function LibraryRail({ selection, viewCounts, categories, onSelect, loading }: RailProps) {
  const catActive = (id: string) => selection.view === 'all' && selection.category === id
  const inner = useRef<HTMLDivElement>(null)
  useFollowWindow(inner)
  return (
    <nav aria-label="Report library" className="hidden w-60 shrink-0 lg:block">
      <div ref={inner} className="sticky top-[60px] max-h-[calc(100vh-76px)] space-y-5 overflow-y-auto overscroll-contain pb-4 pr-1 will-change-transform">
        <div>
          <h2 className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">Views</h2>
          <ul className="space-y-px" role="list">
            {LIBRARY_VIEWS.map((v) => {
              const Icon = VIEW_ICONS[v.id]
              const active = selection.view === v.id && !selection.category
              return (
                <li key={v.id}>
                  {v.id === 'delivered' && <div className="mx-2 my-1.5 h-px bg-border" aria-hidden />}
                  <RailButton
                    icon={<Icon className={cn('h-4 w-4 shrink-0', !active && 'text-muted group-hover:text-text2')} aria-hidden />}
                    label={v.label}
                    count={loading ? undefined : viewCounts[v.id]}
                    active={active}
                    onClick={() => onSelect({ view: v.id, category: null })}
                  />
                </li>
              )
            })}
          </ul>
        </div>
        <div>
          <h2 className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">Categories</h2>
          {loading ? (
            <div className="space-y-1.5 px-2 pt-1" aria-hidden>
              {Array.from({ length: 8 }).map((_, i) => <div key={i} className="h-5 animate-shimmer rounded" />)}
            </div>
          ) : (
            <ul className="space-y-px" role="list">
              {categories.map((c) => {
                const Icon = reportIcon(c.icon)
                const active = catActive(c.id)
                return (
                  <li key={c.id}>
                    <RailButton
                      icon={
                        <span className={cn('flex h-5 w-5 shrink-0 items-center justify-center rounded', TONE_TILE[categoryTone(c.id)], c.count === 0 && 'opacity-60')} aria-hidden>
                          <Icon className="h-3 w-3" />
                        </span>
                      }
                      label={c.title}
                      count={c.count}
                      active={active}
                      dim={c.count === 0}
                      onClick={() => onSelect({ view: 'all', category: c.id })}
                    />
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </div>
    </nav>
  )
}

/** The rail as one grouped select, for screens below `lg`. */
export function RailSelect({ selection, viewCounts, categories, onSelect }: RailProps) {
  const value = selection.view === 'all' && selection.category ? `cat:${selection.category}` : `view:${selection.view}`
  return (
    <Select
      value={value}
      onValueChange={(v) => {
        if (v.startsWith('cat:')) onSelect({ view: 'all', category: v.slice(4) })
        else onSelect({ view: v.slice(5) as LibraryViewId, category: null })
      }}
    >
      <SelectTrigger aria-label="Library view or category" className="lg:hidden">
        <SelectValue />
      </SelectTrigger>
      <SelectContent className="max-h-[60vh] overflow-y-auto">
        <SelectGroup>
          <SelectPrimitive.Label className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted">Views</SelectPrimitive.Label>
          {LIBRARY_VIEWS.map((v) => (
            <SelectItem key={v.id} value={`view:${v.id}`}>
              {v.label}
              {viewCounts[v.id] !== undefined ? ` (${viewCounts[v.id]})` : ''}
            </SelectItem>
          ))}
        </SelectGroup>
        <SelectPrimitive.Separator className="-mx-1 my-1 h-px bg-border" />
        <SelectGroup>
          <SelectPrimitive.Label className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted">Categories</SelectPrimitive.Label>
          {categories.map((c) => (
            <SelectItem key={c.id} value={`cat:${c.id}`}>
              {c.title} ({c.count})
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}
