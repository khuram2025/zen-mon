import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { SortOrder } from '@/pages/inventory/filters'

/**
 * A column header of a server-sorted table: a `th scope="col"` with `aria-sort` and a button that sorts by the
 * column (the same column again flips the direction). A column the API cannot sort by is a plain header.
 */
export function SortableHeader<K extends string>({
  label, sortKey, sort, order, onSort, align, className,
}: {
  label: string
  sortKey?: K
  /** The sort in force. */
  sort: string
  order: SortOrder
  onSort: (key: K) => void
  align?: 'right'
  className?: string
}) {
  const base = cn('h-9 align-middle', className)
  if (!sortKey) {
    return <th scope="col" className={cn(base, 'px-2 text-[11px] font-semibold uppercase tracking-wider text-muted sm:px-3', align === 'right' ? 'text-right' : 'text-left')}>{label}</th>
  }
  const active = sort === sortKey
  const Icon = !active ? ArrowUpDown : order === 'asc' ? ArrowUp : ArrowDown
  return (
    <th scope="col" aria-sort={active ? (order === 'asc' ? 'ascending' : 'descending') : 'none'} className={cn(base, 'px-0', align === 'right' ? 'text-right' : 'text-left')}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        title={`Sort by ${label}${active ? (order === 'asc' ? ', now ascending' : ', now descending') : ''}`}
        className={cn(
          'flex h-9 w-full items-center gap-1 whitespace-nowrap px-2 text-[11px] sm:px-3 font-semibold uppercase tracking-wider outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary',
          align === 'right' && 'justify-end',
          active ? 'text-text' : 'text-muted',
        )}
      >
        {label}
        <Icon className={cn('h-3 w-3 shrink-0', active ? 'text-primary' : 'text-muted/50')} aria-hidden />
      </button>
    </th>
  )
}
