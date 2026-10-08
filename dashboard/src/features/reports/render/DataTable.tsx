import { useId, useMemo, useState } from 'react'
import {
  ArrowDown, ArrowUp, ArrowUpDown, CircleAlert, CircleCheck, CircleMinus, CircleX, Download, Info, OctagonAlert,
  Search, TriangleAlert, X,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { toast } from '@/components/ui/Toast'
import type { ColStyle, RenderTable } from '../types'
import { slug } from './format'
import {
  cellText, filterRows, firstSortDir, inferStyles, isEmptyCell, isNumericStyle, pctTone, pctValue, severityTone,
  sortRows, statusTone, toCsv, type Cell, type SortDir, type Tone,
} from './table'

const COLLAPSE_AT = 12
const SEARCH_FROM = 9

const PILL: Record<Tone, string> = {
  success: 'border-success/30 bg-success/10 text-success-fg',
  warning: 'border-warning/30 bg-warning/10 text-warning-fg',
  danger: 'border-danger/30 bg-danger/10 text-danger-fg',
  info: 'border-info/30 bg-info/10 text-info-fg',
  neutral: 'border-border bg-surface2 text-text2',
}

const STATUS_ICON: Record<Tone, typeof CircleCheck> = {
  success: CircleCheck,
  warning: TriangleAlert,
  danger: CircleX,
  info: Info,
  neutral: CircleMinus,
}

const SEVERITY_ICON: Record<Tone, typeof CircleCheck> = {
  success: CircleCheck,
  warning: TriangleAlert,
  danger: OctagonAlert,
  info: CircleAlert,
  neutral: CircleMinus,
}

const BAR: Record<Tone | 'primary', string> = {
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  info: 'bg-info',
  neutral: 'bg-muted2',
  primary: 'bg-primary',
}

function Pill({ value, kind }: { value: string; kind: 'status' | 'severity' }) {
  if (isEmptyCell(value)) return <span className="text-muted">—</span>
  const tone = kind === 'status' ? statusTone(value) : severityTone(value)
  const Icon = (kind === 'status' ? STATUS_ICON : SEVERITY_ICON)[tone]
  return (
    <span className={cn('inline-flex max-w-full items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium', PILL[tone])}>
      <Icon className="h-3 w-3 shrink-0" aria-hidden />
      <span className="truncate">{value}</span>
    </span>
  )
}

function PctBar({ value, header }: { value: string; header: string }) {
  const num = pctValue(value)
  if (num == null || isEmptyCell(value)) return <span className="text-muted">{value || '—'}</span>
  const tone = pctTone(num, header)
  return (
    <span className="flex items-center justify-end gap-2">
      <span aria-hidden className="h-1.5 w-16 shrink-0 overflow-hidden rounded-full bg-surface3">
        <span className={cn('block h-full rounded-full', BAR[tone])} style={{ width: `${Math.max(0, Math.min(100, num))}%` }} />
      </span>
      <span className="min-w-[3.5rem] text-right tabular-nums">{value}</span>
    </span>
  )
}

function renderCell(v: Cell, style: ColStyle | undefined, header: string) {
  const text = cellText(v)
  if (style === 'status' || style === 'severity') return <Pill value={text} kind={style} />
  if (style === 'pct-bar') return <PctBar value={text} header={header} />
  if (isEmptyCell(v)) return <span className="text-muted">—</span>
  return text
}

function downloadCsv(name: string, csv: string) {
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

export interface DataTableProps {
  table: RenderTable
  /** Smaller paddings; no search (designer preview, half-width sections). */
  compact?: boolean
  /** Offer "Download CSV" of the current view (needs reports.export). */
  canExport?: boolean
  /** File-name stem for CSV downloads, e.g. the report title. */
  filenameBase?: string
  /** Accessible fallback caption when the table has no title. */
  label?: string
}

/**
 * Report table: sortable headers (aria-sort, keyboard buttons), per-table
 * search, "Top 12 · show all N" expansion (all rows print), column styles
 * from the server's hints, and CSV export of exactly what is shown.
 */
export function DataTable({ table, compact, canExport, filenameBase = 'report', label }: DataTableProps) {
  const id = useId()
  const styles = useMemo(() => inferStyles(table), [table])
  const [sort, setSort] = useState<{ col: number; dir: SortDir } | null>(null)
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState(false)

  const rows = useMemo(() => {
    const filtered = filterRows(table.rows, query)
    return sort ? sortRows(filtered, sort.col, sort.dir) : filtered
  }, [table.rows, query, sort])

  const total = table.rows.length
  const searchable = !compact && total >= SEARCH_FROM
  const collapsible = rows.length > COLLAPSE_AT
  const showRank = total > 4 && !table.headers.some((h) => h.trim() === '#')
  const rankOf = useMemo(() => new Map(table.rows.map((r, i) => [r, i + 1])), [table.rows])
  const caption = table.title || label

  const toggleSort = (col: number) => {
    setSort((cur) => {
      const first = firstSortDir(styles[col])
      if (!cur || cur.col !== col) return { col, dir: first }
      if (cur.dir === first) return { col, dir: first === 'asc' ? 'desc' : 'asc' }
      return null
    })
  }

  const exportCsv = () => {
    const name = `${slug(filenameBase)}-${slug(table.title || 'table', 'table')}.csv`
    downloadCsv(name, toCsv(table.headers, rows))
    toast.success('Table exported', `${rows.length} row${rows.length === 1 ? '' : 's'} saved to ${name}`)
  }

  const cellPad = compact ? 'px-2.5 py-1' : 'px-3 py-1.5'

  return (
    <div className="min-w-0">
      {(table.title || searchable || (canExport && total > 0)) && (
        <div className="mb-2 flex flex-wrap items-center gap-2">
          {table.title && (
            <p className="mr-auto text-[11px] font-semibold uppercase tracking-wide text-muted">{table.title}</p>
          )}
          {searchable && (
            <div className="relative ml-auto print:hidden">
              <label htmlFor={`${id}-q`} className="sr-only">
                Search {table.title || 'this table'}
              </label>
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
              <input
                id={`${id}-q`}
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape' && query) {
                    e.stopPropagation()
                    setQuery('')
                  }
                }}
                placeholder="Filter rows…"
                className="h-7 w-44 rounded-md border border-border bg-surface pl-7 pr-2 text-xs text-text placeholder:text-muted focus-visible:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
              />
            </div>
          )}
          {canExport && total > 0 && (
            <button
              type="button"
              onClick={exportCsv}
              className={cn(
                'inline-flex h-7 items-center gap-1 rounded-md border border-border bg-surface px-2 text-[11px] font-medium text-text2 hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 print:hidden',
                !searchable && 'ml-auto',
              )}
              aria-label={`Download ${table.title || 'table'} as CSV${query || sort ? ' (current filter and sort)' : ''}`}
            >
              <Download className="h-3 w-3" aria-hidden /> CSV
            </button>
          )}
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full border-collapse text-xs">
          {caption && <caption className="sr-only">{caption}</caption>}
          <thead>
            <tr className="border-b border-border bg-surface2/70 text-left">
              {showRank && (
                <th scope="col" className={cn('w-8 font-semibold text-muted', cellPad)}>
                  <span aria-hidden>#</span>
                  <span className="sr-only">Rank</span>
                </th>
              )}
              {table.headers.map((h, i) => {
                const active = sort?.col === i
                const numeric = isNumericStyle(styles[i])
                const SortIcon = !active ? ArrowUpDown : sort!.dir === 'asc' ? ArrowUp : ArrowDown
                return (
                  <th
                    key={i}
                    scope="col"
                    aria-sort={active ? (sort!.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
                    className={cn(
                      'whitespace-nowrap p-0 font-semibold text-text2',
                      numeric && 'text-right',
                      // Narrow screens scroll the table sideways rather than breaking names at every hyphen.
                      !numeric && (i === 0 ? 'min-w-[8rem]' : 'min-w-[5.5rem]'),
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => toggleSort(i)}
                      className={cn(
                        'group inline-flex w-full items-center gap-1 rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/50 print:pointer-events-none',
                        cellPad,
                        numeric ? 'justify-end' : 'justify-start',
                        active ? 'text-text' : 'hover:text-text',
                      )}
                    >
                      <span>{h}</span>
                      <SortIcon
                        className={cn('h-3 w-3 shrink-0 print:hidden', active ? 'text-primary' : 'text-muted2 opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100')}
                        aria-hidden
                      />
                    </button>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody id={`${id}-rows`}>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={table.headers.length + (showRank ? 1 : 0)} className="px-3 py-6 text-center text-muted">
                  {total === 0 ? (
                    'No data for this window'
                  ) : (
                    <span className="inline-flex items-center gap-2">
                      No rows match “{query}”.
                      <button type="button" className="font-medium text-primary hover:underline" onClick={() => setQuery('')}>
                        Clear filter
                      </button>
                    </span>
                  )}
                </td>
              </tr>
            ) : (
              rows.map((row, ri) => {
                const hidden = collapsible && !expanded && ri >= COLLAPSE_AT
                return (
                  <tr
                    key={ri}
                    className={cn(
                      'border-b border-border/50 last:border-0 even:bg-surface2/30',
                      hidden && 'hidden print:table-row',
                    )}
                  >
                    {showRank && <td className={cn('tabular-nums text-muted', cellPad)}>{rankOf.get(row) ?? ri + 1}</td>}
                    {table.headers.map((h, ci) => {
                      const s = styles[ci]
                      return (
                        <td
                          key={ci}
                          className={cn(
                            'text-text2',
                            cellPad,
                            ci === 0 && 'font-medium text-text',
                            s === 'mono' && 'font-mono text-[11px]',
                            (s === 'num' || s === 'pct') && 'whitespace-nowrap text-right tabular-nums',
                            s === 'pct-bar' && 'text-right',
                          )}
                        >
                          {renderCell(row[ci], s, h)}
                        </td>
                      )
                    })}
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      {(collapsible || (query && rows.length !== total)) && (
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted print:hidden">
          {query && <span role="status">{rows.length} of {total} rows match</span>}
          {collapsible && (
            <button
              type="button"
              aria-expanded={expanded}
              aria-controls={`${id}-rows`}
              onClick={() => setExpanded((v) => !v)}
              className="font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
            >
              {expanded ? `Show top ${COLLAPSE_AT}` : `Top ${COLLAPSE_AT} · show all ${rows.length}`}
            </button>
          )}
          {query && rows.length > 0 && (
            <button type="button" onClick={() => setQuery('')} className="inline-flex items-center gap-0.5 font-medium text-primary hover:underline">
              <X className="h-3 w-3" aria-hidden /> Clear filter
            </button>
          )}
        </div>
      )}
    </div>
  )
}
