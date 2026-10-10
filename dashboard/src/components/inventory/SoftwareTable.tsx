import { useRef, useState, type KeyboardEvent, type MouseEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { assetsHref, type SortOrder } from '@/pages/inventory/filters'
import type { CatalogRow } from '@/pages/inventory/softwareSettings'
import { nextRowIndex } from './assetModel'
import { fmtInt } from './model'
import { SortableHeader } from './SoftwareSortHeader'
import { SoftwareVersionBar } from './SoftwareVersionBar'
import {
  categoryLabel, firstSeenView, hostsLinkLabel, installsLinkLabel, normStatusMeta, productPath, rowSummary, versionBarLabel,
  type CatalogSort,
} from './softwareModel'

interface Column {
  id: 'product' | 'publisher' | 'category' | 'hosts' | 'installs' | 'versions' | 'latest' | 'first_seen'
  label: string
  sort?: CatalogSort
  /** Responsive visibility of the cell and its header. */
  show: string
  align?: 'right'
}

// Below these widths a column moves into the line under the product name (publisher, category) or is dropped.
const COLUMNS: readonly Column[] = [
  { id: 'product', label: 'Product', sort: 'name', show: '' },
  { id: 'publisher', label: 'Publisher', sort: 'publisher', show: 'hidden md:table-cell' },
  { id: 'category', label: 'Category', sort: 'category', show: 'hidden lg:table-cell' },
  { id: 'hosts', label: 'Hosts', sort: 'hosts', show: '', align: 'right' },
  { id: 'installs', label: 'Installs', show: 'hidden xl:table-cell', align: 'right' },
  { id: 'versions', label: 'Versions', sort: 'version_count', show: '' },
  { id: 'latest', label: 'Latest', sort: 'latest_version', show: 'hidden sm:table-cell' },
  { id: 'first_seen', label: 'First seen', sort: 'first_seen', show: 'hidden lg:table-cell' },
]

/** The first-seen cell: a date, or `before <date>` (with the reason in its tooltip) for software that was there when recording began. */
export function FirstSeen({ iso, historyStart }: { iso: string | null | undefined; historyStart?: string | null }) {
  const v = firstSeenView(iso, historyStart)
  return <span title={v.title} data-first-seen={v.before ? 'before' : undefined} className={v.before ? 'italic' : undefined}>{v.text}</span>
}

const linkClass = 'rounded-sm tabular-nums outline-none hover:text-primary hover:underline focus-visible:ring-2 focus-visible:ring-primary'

/**
 * The catalog as a real table: `scope` headers, `aria-sort`, sortable columns as buttons, one row per product. The
 * product name is a link to its page; every host count opens the assets that have it installed, every installs
 * count the Installed on list, every version count the Versions tab (principle 1). One Tab stop per table: the
 * links of the current row are reachable, the arrow keys move between rows (Home and End jump), and the segments
 * of the version bar are pointer targets whose lists the product page offers to the keyboard too.
 */
export function SoftwareTable({
  rows, sort, order, onSort, caption, historyStart,
}: {
  rows: readonly CatalogRow[]
  sort: string
  order: SortOrder
  onSort: (key: CatalogSort) => void
  caption: string
  /** When change capture began (`history_start`): a first-seen at or before it reads "before <date>". */
  historyStart?: string | null
}) {
  const navigate = useNavigate()
  const [focusIndex, setFocusIndex] = useState(0)
  const body = useRef<HTMLTableSectionElement>(null)
  const current = Math.min(focusIndex, Math.max(0, rows.length - 1))

  const move = (e: KeyboardEvent<HTMLElement>, index: number) => {
    const next = nextRowIndex(index, e.key, rows.length)
    if (next === null) return
    e.preventDefault()
    setFocusIndex(next)
    body.current?.querySelectorAll<HTMLElement>('[data-product-link]')[next]?.focus()
  }
  const open = (e: MouseEvent<HTMLTableRowElement>, row: CatalogRow) => {
    // A click on a link or button inside the row keeps its own meaning; elsewhere it opens the product.
    if ((e.target as HTMLElement).closest('a,button')) return
    navigate(productPath(row.product_key))
  }

  return (
    <table className="w-full border-separate border-spacing-0 text-[13px]" data-software-table="">
      <caption className="sr-only">{caption}</caption>
      <thead className="sticky top-0 z-10 bg-surface">
        <tr className="[&>th]:border-b [&>th]:border-border">
          {COLUMNS.map((c) => (
            <SortableHeader<CatalogSort>
              key={c.id}
              label={c.label}
              sortKey={c.sort}
              sort={sort}
              order={order}
              onSort={onSort}
              align={c.align}
              className={cn(c.show, c.id === 'product' && 'sticky left-0 z-[1] bg-surface')}
            />
          ))}
        </tr>
      </thead>
      <tbody ref={body}>
        {rows.map((row, i) => {
          const status = normStatusMeta(row.norm_status)
          const tab = i === current ? 0 : -1
          const roving = { tabIndex: tab, onFocus: () => setFocusIndex(i), onKeyDown: (e: KeyboardEvent<HTMLElement>) => move(e, i) }
          return (
            <tr
              key={row.product_key}
              data-product-row={row.product_key}
              onClick={(e) => open(e, row)}
              className="group cursor-pointer transition-colors hover:bg-surface2/60 [&>td]:border-b [&>td]:border-border/60 [&>th]:border-b [&>th]:border-border/60 [&>td]:py-1.5 [&>th]:py-1.5"
            >
              <th scope="row" className="sticky left-0 z-[1] max-w-[10.5rem] bg-surface px-2 text-left align-middle font-normal group-hover:bg-surface2 sm:max-w-[22rem] sm:px-3">
                <div className="flex min-w-0 flex-wrap items-center gap-x-2">
                  <Link
                    to={productPath(row.product_key)}
                    data-product-link={row.product_key}
                    {...roving}
                    aria-label={rowSummary(row)}
                    className="min-w-0 truncate rounded-sm font-medium text-text outline-none hover:text-primary focus-visible:ring-2 focus-visible:ring-primary"
                  >
                    {row.name}
                  </Link>
                  {row.norm_status !== 'normalized' && (
                    <span title={status.meaning} className={cn('shrink-0 rounded-full border px-1.5 py-px text-[10px] font-medium', status.tone)}>{status.label}</span>
                  )}
                </div>
                <div className="truncate text-[11px] leading-4 text-muted lg:hidden">
                  <span className="md:hidden">{row.publisher ?? 'Unknown publisher'} · </span>
                  {categoryLabel(row.category)}
                </div>
              </th>
              <td className="hidden max-w-[14rem] truncate px-3 align-middle md:table-cell">
                {row.publisher ?? <span className="italic text-muted">Unknown</span>}
              </td>
              <td className="hidden px-3 align-middle lg:table-cell">
                <span className="whitespace-nowrap rounded-full border border-border bg-surface2 px-1.5 py-px text-[10.5px] text-text2">{categoryLabel(row.category)}</span>
              </td>
              <td className="px-2 text-right align-middle sm:px-3">
                <Link to={assetsHref({ product: row.product_key })} data-drill="assets" data-drill-count={row.hosts} aria-label={hostsLinkLabel(row)} {...roving} className={cn(linkClass, 'font-medium text-text')}>
                  {fmtInt(row.hosts)}
                </Link>
              </td>
              <td className="hidden px-3 text-right align-middle xl:table-cell">
                <Link to={productPath(row.product_key, { tab: 'installed' })} data-drill="page" data-drill-count={row.installs} aria-label={installsLinkLabel(row)} {...roving} className={cn(linkClass, 'text-text2')}>
                  {fmtInt(row.installs)}
                </Link>
              </td>
              <td className="px-2 align-middle sm:px-3">
                <div className="flex min-w-0 items-center gap-2">
                  <SoftwareVersionBar
                    productKey={row.product_key}
                    bar={row.distribution}
                    label={versionBarLabel(row)}
                    className="w-16 shrink-0 sm:w-36"
                    focusable={false}
                  />
                  {row.version_count > 0 && (
                    <Link
                      to={productPath(row.product_key)}
                      data-drill="page"
                      data-drill-count={row.version_count}
                      aria-label={`${fmtInt(row.version_count)} ${row.version_count === 1 ? 'version' : 'versions'} of ${row.name}`}
                      {...roving}
                      className={cn(linkClass, 'shrink-0 text-text2')}
                    >
                      {fmtInt(row.version_count)}
                    </Link>
                  )}
                </div>
              </td>
              <td className="hidden max-w-[10rem] truncate px-3 align-middle sm:table-cell">
                {row.latest_version
                  ? <span className="font-mono text-xs" title={row.latest_version}>{row.latest_version}</span>
                  : <span className="text-muted" title="No version can be ordered" aria-label="No version can be ordered">—</span>}
              </td>
              <td className="hidden whitespace-nowrap px-3 align-middle text-text2 lg:table-cell">
                <FirstSeen iso={row.first_seen} historyStart={historyStart} />
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
