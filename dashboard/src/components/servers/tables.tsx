/** Shared table furniture for the server detail tabs: client-side paging,
 *  CSV export, and honest empty/error states.
 *
 *  These tabs previously rendered every row at once (a Windows host reports
 *  ~250 services and up to 1000 packages) and showed "nothing here yet" when
 *  a request had actually failed, which reads as "the server is clean" when
 *  it means "we don't know". */

import { useMemo, useState } from 'react'
import { AlertTriangle, ChevronLeft, ChevronRight, Database, Download, Inbox } from 'lucide-react'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Td, Tr } from '@/components/ui/Table'

/* ── States ──────────────────────────────────────────────────────── */

export function NoData({ label = 'No data for this window' }: { label?: string }) {
  return (
    <div className="flex h-full min-h-[80px] flex-col items-center justify-center gap-1 text-center">
      <Database className="h-5 w-5 text-muted/40" />
      <span className="text-xs text-muted">{label}</span>
    </div>
  )
}

/** Distinguishes "we asked and there is nothing" from "we could not ask". */
export function QueryError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center gap-2 py-10 text-center">
      <AlertTriangle className="h-6 w-6 text-danger/70" />
      <div className="text-sm font-medium text-danger">Could not load this data</div>
      <div className="max-w-md text-xs text-muted">{apiErrorMessage(error)}</div>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  )
}

export function EmptyState({
  icon, title, hint,
}: {
  icon?: React.ReactNode
  title: string
  hint?: React.ReactNode
}) {
  return (
    <div className="flex flex-col items-center gap-2 py-10 text-center">
      <span className="text-muted/50">{icon ?? <Inbox className="h-7 w-7" />}</span>
      <div className="text-sm font-medium text-text2">{title}</div>
      {hint && <div className="max-w-md text-xs text-muted">{hint}</div>}
    </div>
  )
}

/** Full-width row wrapper so states line up inside a <Table>. */
export function TableStateRow({ colSpan, children }: { colSpan: number; children: React.ReactNode }) {
  return (
    <Tr className="hover:bg-transparent">
      <Td colSpan={colSpan}>{children}</Td>
    </Tr>
  )
}

/* ── Paging ──────────────────────────────────────────────────────── */

export const PAGE_SIZES = [25, 50, 100, 250] as const

export function usePagedRows<T>(rows: T[], initialSize: number = 50) {
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<number>(initialSize)

  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize))
  // Filtering can shrink the list under the current page; clamp rather than
  // render an empty page the user cannot navigate out of.
  const safePage = Math.min(page, pageCount)
  const start = (safePage - 1) * pageSize

  const pageRows = useMemo(
    () => rows.slice(start, start + pageSize),
    [rows, start, pageSize],
  )

  return {
    pageRows,
    page: safePage,
    pageCount,
    pageSize,
    total: rows.length,
    setPage,
    setPageSize: (n: number) => { setPageSize(n); setPage(1) },
    reset: () => setPage(1),
  }
}

export function TablePager({
  page, pageCount, pageSize, total, setPage, setPageSize, noun = 'rows',
}: {
  page: number
  pageCount: number
  pageSize: number
  total: number
  setPage: (n: number) => void
  setPageSize: (n: number) => void
  noun?: string
}) {
  if (total === 0) return null
  const first = (page - 1) * pageSize + 1
  const last = Math.min(total, page * pageSize)

  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/60 px-4 py-2.5 text-xs">
      <span className="text-muted">
        {first.toLocaleString()}–{last.toLocaleString()} of{' '}
        <span className="font-medium text-text2">{total.toLocaleString()}</span> {noun}
      </span>
      <div className="flex items-center gap-2">
        <select
          aria-label={`${noun} per page`}
          className="h-7 rounded-md border border-border bg-surface px-1.5 text-xs text-text2"
          value={pageSize}
          onChange={(e) => setPageSize(Number(e.target.value))}
        >
          {PAGE_SIZES.map((n) => (
            <option key={n} value={n}>{n} / page</option>
          ))}
        </select>
        <div className="flex items-center gap-1">
          <Button
            size="sm" variant="outline" className="h-7 w-7 p-0"
            aria-label="Previous page"
            disabled={page <= 1}
            onClick={() => setPage(page - 1)}
          >
            <ChevronLeft className="h-3.5 w-3.5" />
          </Button>
          <span className="min-w-[64px] text-center tabular-nums text-muted">
            {page} / {pageCount}
          </span>
          <Button
            size="sm" variant="outline" className="h-7 w-7 p-0"
            aria-label="Next page"
            disabled={page >= pageCount}
            onClick={() => setPage(page + 1)}
          >
            <ChevronRight className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
    </div>
  )
}

/* ── CSV export ──────────────────────────────────────────────────── */

function csvCell(value: unknown): string {
  if (value == null) return ''
  const s = Array.isArray(value) ? value.join(' ') : String(value)
  // Quote when the value could otherwise break the row, and double any
  // embedded quotes per RFC 4180.
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

export function toCsv<T>(rows: T[], columns: { header: string; value: (row: T) => unknown }[]) {
  const head = columns.map((c) => csvCell(c.header)).join(',')
  const body = rows.map((r) => columns.map((c) => csvCell(c.value(r))).join(','))
  return [head, ...body].join('\r\n')
}

export function downloadCsv(filename: string, csv: string) {
  // BOM so Excel opens UTF-8 correctly instead of mangling non-ASCII.
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
}

export function ExportCsvButton<T>({
  rows, columns, filename, disabled,
}: {
  rows: T[]
  columns: { header: string; value: (row: T) => unknown }[]
  filename: string
  disabled?: boolean
}) {
  return (
    <Button
      size="sm"
      variant="outline"
      className="h-8"
      // Exports everything currently matched by filters, not just this page.
      onClick={() => downloadCsv(filename, toCsv(rows, columns))}
      disabled={disabled || rows.length === 0}
      title={rows.length ? `Export ${rows.length.toLocaleString()} rows to CSV` : 'Nothing to export'}
    >
      <Download className="h-3.5 w-3.5" /> Export
    </Button>
  )
}

/* ── Sortable header ─────────────────────────────────────────────── */

export function sortIndicator(active: boolean, dir: 'asc' | 'desc') {
  if (!active) return <span className="ml-1 text-muted/30">↕</span>
  return <span className="ml-1 text-primary">{dir === 'asc' ? '↑' : '↓'}</span>
}

export const sortableTh = 'cursor-pointer select-none whitespace-nowrap hover:text-text'

export function cmp(a: unknown, b: unknown): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b
  return String(a ?? '').localeCompare(String(b ?? ''), undefined, { numeric: true })
}

