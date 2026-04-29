import { useState, useMemo } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface Top10Column<T> {
  key: keyof T | string
  header: string
  align?: 'left' | 'right' | 'center'
  width?: string
  render?: (row: T) => React.ReactNode
  sortValue?: (row: T) => number | string
}

interface Top10TableProps<T> {
  rows: T[]
  columns: Top10Column<T>[]
  defaultSortKey?: string
  defaultSortDir?: 'asc' | 'desc'
  emptyMessage?: string
  rowKey?: (row: T, idx: number) => string
}

export function Top10Table<T>({
  rows,
  columns,
  defaultSortKey,
  defaultSortDir = 'desc',
  emptyMessage = 'No data',
  rowKey,
}: Top10TableProps<T>) {
  const [sortKey, setSortKey] = useState<string | undefined>(defaultSortKey)
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>(defaultSortDir)

  const sortedRows = useMemo(() => {
    if (!sortKey) return rows
    const col = columns.find((c) => String(c.key) === sortKey)
    if (!col) return rows
    const value = (row: T): number | string => {
      if (col.sortValue) return col.sortValue(row)
      const v = (row as Record<string, unknown>)[String(col.key)]
      if (typeof v === 'number') return v
      if (typeof v === 'string') return v.toLowerCase()
      return v == null ? -Infinity : String(v)
    }
    const sorted = [...rows].sort((a, b) => {
      const va = value(a)
      const vb = value(b)
      if (va < vb) return sortDir === 'asc' ? -1 : 1
      if (va > vb) return sortDir === 'asc' ? 1 : -1
      return 0
    })
    return sorted
  }, [rows, columns, sortKey, sortDir])

  if (rows.length === 0) {
    return <div className="py-8 text-center text-sm text-muted">{emptyMessage}</div>
  }

  function clickSort(key: string) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-[11px] font-semibold uppercase tracking-wider text-muted">
            {columns.map((c) => {
              const k = String(c.key)
              const active = sortKey === k
              const SortIcon = !active ? ArrowUpDown : sortDir === 'asc' ? ArrowUp : ArrowDown
              return (
                <th
                  key={k}
                  className={cn(
                    'select-none px-3 py-2',
                    c.align === 'right' ? 'text-right' : c.align === 'center' ? 'text-center' : 'text-left',
                    'cursor-pointer hover:text-text',
                  )}
                  style={c.width ? { width: c.width } : undefined}
                  onClick={() => clickSort(k)}
                >
                  <span className="inline-flex items-center gap-1">
                    {c.header}
                    <SortIcon className={cn('h-3 w-3', active ? 'text-primary' : 'opacity-40')} />
                  </span>
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {sortedRows.map((row, i) => (
            <tr
              key={rowKey ? rowKey(row, i) : i}
              className="border-b border-border/50 transition-colors last:border-b-0 hover:bg-surface2/50"
            >
              {columns.map((c) => {
                const k = String(c.key)
                const cellValue = c.render
                  ? c.render(row)
                  : (row as Record<string, unknown>)[String(c.key)] ?? '—'
                return (
                  <td
                    key={k}
                    className={cn(
                      'px-3 py-2 align-middle',
                      c.align === 'right' ? 'text-right' : c.align === 'center' ? 'text-center' : 'text-left',
                    )}
                  >
                    {cellValue as React.ReactNode}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
