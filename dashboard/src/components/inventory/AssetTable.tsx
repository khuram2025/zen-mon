import { useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDown, ArrowUp, ArrowUpDown, Network, Server } from 'lucide-react'
import { StatusDot } from '@/components/ui/StatusDot'
import { cn } from '@/lib/utils'
import type { AssetSortKey, SortOrder } from '@/pages/inventory/filters'
import { FLYOUT_STATE } from '@/pages/inventory/urlState'
import type { AssetRow } from '@/pages/inventory/types'
import { addressOf, assetTags, kindLabel, nextRowIndex, osCell, rowSummary, statusTone, type ColumnDef, type Density } from './assetModel'
import { FreshnessBadge } from './FreshnessBadge'
import { ageWords } from './model'
import { SourceBadge } from './SourceBadge'

const MAX_TAGS = 3

/** A cell with nothing to show: a dash with the reason for a screen reader. */
const absent = (reason: string) => <span className="text-muted" title={reason} aria-label={reason}>—</span>

function Cell({ col, row, now }: { col: ColumnDef; row: AssetRow; now: number }): ReactNode {
  switch (col.id) {
    case 'kind':
      return <span className="inline-flex items-center gap-1.5 text-text2">{row.kind === 'server' ? <Server className="h-3.5 w-3.5 text-muted" aria-hidden /> : <Network className="h-3.5 w-3.5 text-muted" aria-hidden />}{row.kind === 'server' ? 'Server' : 'Network'}</span>
    case 'ip':
      return row.ip ? <span className="font-mono text-xs">{row.ip}</span> : absent('No IP address')
    case 'vendor':
      // Unknown is a value on a device (a gap to close); a server has no hardware identity until its agent reports one.
      return row.vendor ?? (row.kind === 'device' ? <span className="italic text-muted">Unknown</span> : absent('Servers report no hardware vendor yet'))
    case 'model':
      return row.model ?? (row.kind === 'device' ? <span className="italic text-muted">Unknown</span> : absent('Servers report no hardware model yet'))
    case 'serial':
      return row.serial ? <span className="font-mono text-xs">{row.serial}</span> : row.kind === 'device' ? <span className="italic text-muted">Unknown</span> : absent('Servers report no serial number yet')
    case 'os': {
      const os = osCell(row)
      return os.unknown ? (
        <span className="italic text-muted" title={os.title}>Unknown</span>
      ) : (
        <span className="flex min-w-0 items-baseline gap-1.5" title={os.title}>
          <span className="truncate">{os.label}</span>
          {os.version && <span className="shrink-0 font-mono text-[11px] text-text2">{os.version}</span>}
        </span>
      )
    }
    case 'site':
      return row.site ?? absent('No site or location')
    case 'sources':
      return row.sources.length > 0 ? (
        <span className="flex flex-wrap gap-1">{row.sources.map((s) => <SourceBadge key={s} source={s} />)}</span>
      ) : absent('No collection source')
    case 'inventory':
      return <FreshnessBadge freshness={row.freshness} kind={row.kind} inventoryAt={row.inventory_at} lastSeen={row.last_seen} now={now} />
    case 'type':
      return row.asset_type ?? absent('No type')
    case 'status': {
      if (!row.status) return absent('No status')
      const tone = statusTone(row.status)
      return (
        <span className="inline-flex items-center gap-1.5">
          <StatusDot status={tone === 'ok' ? 'up' : tone === 'bad' ? 'down' : tone === 'warn' ? 'warn' : 'idle'} />
          {row.status}
        </span>
      )
    }
    case 'tags': {
      const tags = assetTags(row.tags)
      if (tags.length === 0) return absent('No tags')
      return (
        <span className="flex flex-wrap gap-1">
          {tags.slice(0, MAX_TAGS).map((t) => <span key={t} className="rounded-full border border-border bg-surface2 px-1.5 py-px text-[10.5px] text-text2">{t}</span>)}
          {tags.length > MAX_TAGS && <span className="text-[10.5px] text-muted" title={tags.slice(MAX_TAGS).join(', ')}>+{tags.length - MAX_TAGS}</span>}
        </span>
      )
    }
    case 'last_seen':
      return row.last_seen ? <span className="text-text2" title={row.last_seen}>{ageWords(row.last_seen, now)}</span> : absent('Never seen')
    default:
      return null
  }
}

function SortHeader({ col, sort, order, onSort }: { col: ColumnDef; sort: string; order: SortOrder; onSort: (key: AssetSortKey) => void }) {
  const key = col.sort
  if (!key) return <th scope="col" className="h-9 px-3 text-left align-middle text-[11px] font-semibold uppercase tracking-wider text-muted">{col.label}</th>
  const active = sort === key
  const Icon = !active ? ArrowUpDown : order === 'asc' ? ArrowUp : ArrowDown
  return (
    <th scope="col" aria-sort={active ? (order === 'asc' ? 'ascending' : 'descending') : 'none'} className="h-9 px-0 text-left align-middle">
      <button
        type="button"
        onClick={() => onSort(key)}
        title={`Sort by ${col.label}${active ? (order === 'asc' ? ', now ascending' : ', now descending') : ''}`}
        className={cn(
          'flex h-9 w-full items-center gap-1 whitespace-nowrap px-3 text-[11px] font-semibold uppercase tracking-wider outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary',
          active ? 'text-text' : 'text-muted',
        )}
      >
        {col.label}
        <Icon className={cn('h-3 w-3 shrink-0', active ? 'text-primary' : 'text-muted/50')} aria-hidden />
      </button>
    </th>
  )
}

/**
 * The asset list as a real table: `scope` headers, `aria-sort`, sortable columns as buttons, one row per
 * asset. The name is a link to `?asset=<kind>:<id>` (so Enter, a click, a middle click or a copied link all
 * open the flyout) and the rest of the row is a mouse target for the same thing. Arrow keys, Home and End move
 * between rows (a roving tab stop: one Tab stop for the whole table), Enter opens.
 */
export function AssetTable({
  rows, columns, density, sort, order, onSort, onOpen, hrefFor, activeAddress, now, caption,
}: {
  rows: readonly AssetRow[]
  columns: readonly ColumnDef[]
  density: Density
  sort: string
  order: SortOrder
  onSort: (key: AssetSortKey) => void
  onOpen: (address: string) => void
  /** URL of this page with `address` open in the flyout. */
  hrefFor: (address: string) => string
  /** The asset whose flyout is open: its row is marked. */
  activeAddress: string | null
  now: number
  caption: string
}) {
  const [focusIndex, setFocusIndex] = useState(0)
  const body = useRef<HTMLTableSectionElement>(null)
  const current = Math.min(focusIndex, Math.max(0, rows.length - 1))

  const move = (e: KeyboardEvent<HTMLElement>, index: number) => {
    const next = nextRowIndex(index, e.key, rows.length)
    if (next === null) return
    e.preventDefault()
    setFocusIndex(next)
    body.current?.querySelectorAll<HTMLElement>('[data-asset-link]')[next]?.focus()
  }

  const open = (e: MouseEvent<HTMLTableRowElement>, row: AssetRow) => {
    // A click on a link or button inside the row keeps its own meaning; elsewhere it opens the flyout.
    if ((e.target as HTMLElement).closest('a,button,summary')) return
    onOpen(addressOf(row))
  }

  return (
    <table className="w-full min-w-max border-separate border-spacing-0 text-[13px]" data-asset-table="">
      <caption className="sr-only">{caption}</caption>
      <thead className="sticky top-0 z-10 bg-surface">
        <tr className="[&>th]:border-b [&>th]:border-border">
          {columns.map((c) => <SortHeader key={c.id} col={c} sort={sort} order={order} onSort={onSort} />)}
        </tr>
      </thead>
      <tbody ref={body}>
        {rows.map((row, i) => {
          const address = addressOf(row)
          const selected = activeAddress === address
          const Icon = row.kind === 'server' ? Server : Network
          return (
            <tr
              key={row.key}
              data-asset-row={address}
              aria-selected={selected || undefined}
              onClick={(e) => open(e, row)}
              className={cn(
                'cursor-pointer transition-colors hover:bg-surface2/60 [&>td]:border-b [&>td]:border-border/60',
                selected && 'bg-primary/10 hover:bg-primary/10',
                density === 'compact' ? '[&>td]:py-0.5' : '[&>td]:py-1.5',
              )}
            >
              {columns.map((c) =>
                c.id === 'name' ? (
                  <th key={c.id} scope="row" className="max-w-[18rem] px-3 text-left align-middle font-normal">
                    <Link
                      to={hrefFor(address)}
                      // Marks the entry as "opened from the list": closing the flyout then goes Back instead of leaving a twin entry (FE-09).
                      state={FLYOUT_STATE}
                      data-asset-link={address}
                      tabIndex={i === current ? 0 : -1}
                      onFocus={() => setFocusIndex(i)}
                      onKeyDown={(e) => move(e, i)}
                      aria-label={rowSummary(row)}
                      title={`${kindLabel(row.kind)}: open details`}
                      className="flex items-center gap-2 rounded-sm font-medium text-text outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-primary"
                    >
                      <Icon className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />
                      <span className="truncate">{row.name}</span>
                    </Link>
                  </th>
                ) : (
                  <td key={c.id} className={cn('truncate px-3 align-middle', c.id === 'os' ? 'max-w-[22rem]' : 'max-w-[16rem]')}>
                    <Cell col={c} row={row} now={now} />
                  </td>
                ),
              )}
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
