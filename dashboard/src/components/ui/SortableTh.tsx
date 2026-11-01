import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import { Th } from './Table'

export function SortableTh({ column, label, sort, order, onSort }: {
  column: string; label: string; sort: string; order: string; onSort: (column: string) => void
}) {
  const active = sort === column
  const Icon = active ? order === 'desc' ? ArrowDown : ArrowUp : ArrowUpDown
  return <Th aria-sort={active ? order === 'desc' ? 'descending' : 'ascending' : 'none'}>
    <button type="button" onClick={() => onSort(column)} className="inline-flex items-center gap-1 whitespace-nowrap text-left hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary" aria-label={`Sort by ${label}`}>
      {label}<Icon aria-hidden="true" className="h-3 w-3" />
    </button>
  </Th>
}
