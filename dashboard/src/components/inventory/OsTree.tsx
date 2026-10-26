import { ArrowDown, ArrowUp, ArrowUpDown, ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { DrillTarget } from './DrillTarget'
import { fmtInt, fmtPct } from './model'
import {
  expandLabel, nounFor, rowSummary, versionsText, visibleOsRows, type OsSort, type OsSortKey, type OsTreeRow,
} from './osTreeModel'

/** A thin proportional bar (decorative: the number beside it is the information). The unknown node is hatched. */
function CountBar({ pct, unknown, strong }: { pct: number; unknown?: boolean; strong?: boolean }) {
  return (
    <span aria-hidden className="block h-1.5 w-full overflow-hidden rounded-full bg-surface2">
      <span
        className="block h-full rounded-full"
        style={{
          width: `${Math.max(pct > 0 ? 2 : 0, Math.min(100, pct))}%`,
          background: unknown
            ? 'repeating-linear-gradient(45deg, rgb(var(--muted) / 0.7) 0 2px, rgb(var(--surface2)) 2px 5px)'
            : `rgb(var(--primary) / ${strong ? '0.85' : '0.6'})`,
        }}
      />
    </span>
  )
}

const th = 'h-9 px-3 text-left align-middle text-xs font-semibold uppercase tracking-wider text-muted'

function SortHeader({
  label, sortKey, sort, onSort, className, align = 'left',
}: {
  label: string
  sortKey: OsSortKey
  sort: OsSort
  onSort: (key: OsSortKey) => void
  className?: string
  align?: 'left' | 'right'
}) {
  const active = sort.key === sortKey
  const Icon = !active ? ArrowUpDown : sort.order === 'asc' ? ArrowUp : ArrowDown
  return (
    <th scope="col" aria-sort={active ? (sort.order === 'asc' ? 'ascending' : 'descending') : 'none'} className={cn('h-9 px-0 align-middle', align === 'right' ? 'text-right' : 'text-left', className)}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        title={`Sort by ${label.toLowerCase()}${active ? (sort.order === 'asc' ? ', now ascending' : ', now descending') : ''}`}
        className={cn(
          'flex h-9 w-full items-center gap-1 whitespace-nowrap px-3 text-xs font-semibold uppercase tracking-wider outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary',
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

const INDENT_PX = 18

function TreeRow({
  row, open, onToggle, total, noun, showRaw,
}: {
  row: OsTreeRow
  open: boolean
  onToggle: (key: string) => void
  total: number
  noun: string
  showRaw: boolean
}) {
  const isFamily = row.type === 'family'
  const isVersion = row.type === 'version'
  const label = (
    <span title={row.path} className={cn('min-w-0 truncate', isVersion && 'font-mono text-xs', row.unknown && 'italic text-muted', isFamily && 'font-semibold', row.type === 'product' && 'font-medium')}>
      {row.label}
    </span>
  )
  return (
    <tr
      data-os-row={row.key}
      data-type={row.type}
      className={cn(
        'transition-colors hover:bg-surface2/60 [&>td]:border-b [&>td]:border-border/60 [&>th]:border-b [&>th]:border-border/60',
        isFamily && 'bg-surface2/50',
      )}
    >
      <th scope="row" className="min-w-0 px-3 py-1.5 text-left align-middle font-normal" style={{ paddingLeft: 12 + row.depth * INDENT_PX }}>
        <div className="flex min-w-0 items-center gap-1.5">
          {row.expandable ? (
            <button
              type="button"
              aria-expanded={open}
              aria-label={expandLabel(row, open)}
              onClick={() => onToggle(row.key)}
              className="-ml-1 flex min-w-0 items-center gap-1 rounded-sm px-1 py-0.5 text-left outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-primary"
            >
              {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />}
              {label}
            </button>
          ) : (
            <>
              <span className="w-3.5 shrink-0" aria-hidden />
              {label}
            </>
          )}
          {row.type === 'release' && row.versions !== null && row.versions > 1 && (
            <span className="shrink-0 rounded-full border border-border bg-surface px-1.5 text-2xs text-text2" title={`${row.versions} versions of this release are in use`}>{row.versions}</span>
          )}
        </div>
        {/* Below md the latest observed version moves under the name; the raw string of a version too (where it has no column). */}
        {row.type === 'release' && row.latest && (
          <div className="mt-0.5 pl-5 text-xs text-muted md:hidden">Latest <span className="font-mono text-text2">{row.latest}</span></div>
        )}
        {isVersion && row.raw && (
          <div className="mt-0.5 break-all pl-5 font-mono text-xs text-muted lg:hidden" title="As the device reported it">Reported as {row.raw}</div>
        )}
      </th>
      <td className="hidden w-32 px-3 align-middle sm:table-cell"><CountBar pct={row.pct} unknown={row.unknown} strong={isFamily} /></td>
      <td className="w-24 px-3 text-right align-middle">
        <DrillTarget drill={row.drill} className={cn('inline-block min-w-[2.5rem] text-right font-medium', isFamily && 'font-semibold')} title={`${fmtInt(row.count)} ${nounFor(row.count, noun)}, ${fmtPct(row.pct)}: open the list`} ariaLabel={rowSummary(row, total, noun)}>
          {fmtInt(row.count)}
        </DrillTarget>
      </td>
      <td className="hidden w-24 px-3 text-right align-middle tabular-nums text-text2 sm:table-cell">
        {row.versions === null ? '' : (
          <span title={versionsText(row)}>{row.versions === 0 ? <span className="text-muted">none</span> : fmtInt(row.versions)}</span>
        )}
      </td>
      <td className="hidden px-3 align-middle md:table-cell">
        {row.type === 'release' ? (
          row.latest ? <span className="font-mono text-xs">{row.latest}</span> : <span className="text-muted" title="Observed versions of this release cannot all be ordered, or none was identified">—</span>
        ) : null}
      </td>
      {showRaw && (
        <td className="hidden max-w-[16rem] px-3 align-middle lg:table-cell">
          {isVersion && row.raw ? <span className="block truncate font-mono text-xs text-muted" title={`As the device reported it: ${row.raw}`}>{row.raw}</span> : null}
        </td>
      )}
    </tr>
  )
}

/**
 * The family > product > release > version tree as one real table: row headers, `aria-sort` on the sortable
 * columns, an expand button per expandable row (`aria-expanded`), and a count link on every row whose list total
 * equals it. The raw firmware string a version was normalised from has its own column when the tree has any.
 */
export function OsTree({
  rows, expanded, onToggle, sort, onSort, total, noun, caption,
}: {
  /** Every row; the expansion decides which are drawn. */
  rows: readonly OsTreeRow[]
  expanded: ReadonlySet<string>
  onToggle: (key: string) => void
  sort: OsSort
  onSort: (key: OsSortKey) => void
  total: number
  noun: string
  caption: string
}) {
  const visible = visibleOsRows(rows, expanded)
  // The column exists while a version with a raw string is on show; collapsed, an empty column would only confuse.
  const showRaw = visible.some((r) => r.type === 'version' && r.raw)
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-0 border-separate border-spacing-0 text-sm" data-os-tree="">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-surface">
          <tr className="[&>th]:border-b [&>th]:border-border">
            <SortHeader label="OS / release" sortKey="name" sort={sort} onSort={onSort} />
            <th scope="col" className={cn(th, 'hidden sm:table-cell')}><span className="sr-only">Share of all {noun}</span><span aria-hidden>Share</span></th>
            <SortHeader label="Assets" sortKey="count" sort={sort} onSort={onSort} align="right" />
            <SortHeader label="Versions" sortKey="versions" sort={sort} onSort={onSort} align="right" className="hidden sm:table-cell" />
            <th scope="col" className={cn(th, 'hidden md:table-cell')}>Latest observed</th>
            {showRaw && <th scope="col" className={cn(th, 'hidden lg:table-cell')}>Reported as</th>}
          </tr>
        </thead>
        <tbody>
          {visible.map((r) => (
            <TreeRow key={r.key} row={r} open={expanded.has(r.key)} onToggle={onToggle} total={total} noun={noun} showRaw={showRaw} />
          ))}
        </tbody>
      </table>
    </div>
  )
}
