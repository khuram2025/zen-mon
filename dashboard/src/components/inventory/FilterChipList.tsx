import { Link } from 'react-router-dom'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { isPlainClick } from './useChipFocus'

export interface FilterChipItem {
  id: string
  /** The filter's name ("Kind"). */
  label: string
  /** The value in words ("Server"). */
  text: string
  /** The page with this one filter removed. */
  href: string
}

/**
 * The active-filter chips of an Inventory list: one removable link per filter. Assets, Changes and Software
 * render them through this component, so the markup, the accessible name, the focus hook (`data-chip-remove`) and
 * the contrast are the same everywhere. The filter's name is `text-text2` (the 11 px prefix on the pill's
 * `surface2` fill is below 4.5:1 in `text-muted`: review finding NF-2); the value is the stronger weight.
 *
 * `onRemove(index, event)` is the toolbar's chance to arm the focus move (`useChipFocus().removed`); it decides
 * for itself whether the click was a plain one.
 */
export function FilterChipList({
  items, listRef, onRemove, maxWidth = 'max-w-[16rem]',
}: {
  items: readonly FilterChipItem[]
  listRef?: React.Ref<HTMLUListElement>
  onRemove?: (index: number, event: React.MouseEvent<HTMLAnchorElement>) => void
  maxWidth?: string
}) {
  if (items.length === 0) return null
  return (
    <ul ref={listRef} role="list" aria-label="Active filters" className="flex flex-wrap items-center gap-1.5">
      {items.map((c, index) => (
        <li key={c.id}>
          <Link
            to={c.href}
            data-chip-remove=""
            onClick={(e) => onRemove?.(index, e)}
            aria-label={`Remove filter ${c.label}: ${c.text}`}
            className={cn('inline-flex items-center gap-1 rounded-full border border-border bg-surface2 px-2 py-0.5 text-[11px] font-medium text-text2 outline-none hover:border-primary/50 hover:text-text focus-visible:ring-2 focus-visible:ring-primary', maxWidth)}
          >
            <span className="shrink-0 font-normal text-text2" data-chip-label="">{c.label}:</span>
            <span className="truncate text-text">{c.text}</span>
            <X className="h-3 w-3 shrink-0" aria-hidden />
          </Link>
        </li>
      ))}
    </ul>
  )
}

export { isPlainClick }
