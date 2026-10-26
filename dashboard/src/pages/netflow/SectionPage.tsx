import { Fragment, useEffect, useId, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import {
  ArrowDown,
  ArrowDownUp,
  ArrowUp,
  Boxes,
  Cable,
  ChevronLeft,
  ChevronRight,
  Database,
  Filter,
  Gauge,
  Globe2,
  Landmark,
  Layers,
  Network,
  RadioTower,
  Route,
  Search,
  ShieldAlert,
  Smartphone,
  SquareStack,
  type LucideIcon,
} from 'lucide-react'
import { Card } from '@/components/ui/Card'
import { TimeRangePicker } from '@/components/TimeRangePicker'
import { NetflowPageHeader } from '@/components/netflow/NetflowPageHeader'
import { PanelMessage, QueryPanel } from '@/components/netflow/QueryPanel'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { FilterChips } from '@/components/netflow/FilterChips'
import { StatusTile } from '@/components/netflow/StatusTile'
import { cn } from '@/lib/utils'
import { mergeFlowFilter, removeFlowFilterKeys, savedPageParams, TCP_FLAG_LABELS, writeFlowFilterKeepingInvalid, type FlowFilter, type FlowFilterKey, type TcpFlagFilter } from '@/lib/netflow/filters'
import { useFlowFilter } from '@/lib/netflow/useFlowFilter'
import { useNfWindow, type NfMetaWindow } from '@/lib/netflow/window'
import { buildNetflowLink } from '@/lib/netflow/links'
import {
  dscpLabel,
  formatBps,
  formatBytes,
  formatBytesExact,
  formatCount,
  formatCountExact,
  formatPercent,
  ipDisplay,
  protocolLabel,
} from '@/lib/netflow/format'
import type { NfMeta } from '@/lib/netflow/types'
import {
  defaultRankBy,
  drillFilter,
  dscpShareItems,
  forensicsLink,
  hostLink,
  orientationLabels,
  PAGE_SIZE,
  pageCount,
  pageSlice,
  parsePage,
  parseRankBy,
  parseRankMetric,
  RANK_BY_OPTIONS,
  RANK_METRIC_OPTIONS,
  rankPhrase,
  seriesClass,
  serviceText,
  shareRows,
  showingText,
  sortRowsBy,
  type DscpShareItem,
  type SortDir,
} from './cards/model'
import { Chip, ConfidenceChip, Figure, HostLabel, Segmented, ShareStrip, useBreakdown } from './cards/parts'
import { AS_SOURCE_TEXT } from './cards/AsnCard'
import { flagEmoji, SIDE_OPTIONS, type Side } from './cards/CountriesCard'
import { interfaceTitle, UTILIZATION_LINK_TITLE } from './cards/InterfacesCard'
import { capacityLink } from '@/lib/netflow/interfaceLinks'
import type {
  ApplicationRow,
  AsnRow,
  AsnsPage,
  Conversation,
  CountriesPage,
  CountryRow,
  DscpPage,
  InterfaceRow,
  InterfacesPage,
  IpGroupRow,
  IpGroupsPage,
  NetClassesPage,
  NetClassRow,
  NextHopRow,
  NextHopsPage,
  Paged,
  ProtocolRow,
  RankBy,
  RankMetric,
  ServiceRow,
  Talker,
  TalkersPage,
  TcpFlags,
} from './cards/shapes'
import { IpTrafficDetailPanel, type DetailContext } from './details/IpTrafficDetailPanel'
import { ApplicationDetailPanel } from './details/ApplicationDetailPanel'
import { ProtocolDetailPanel } from './details/ProtocolDetailPanel'
import { ConversationDetailPanel } from './details/ConversationDetailPanel'
import { SaveCurrentViewButton } from './SaveCurrentViewButton'
import { pageSettingsSummary, sectionViewParams } from './forensics/model'

/* ─── Section registry ─────────────────────────────────────────────── */

type Paging = 'server' | 'client' | 'none'
type Control = 'by' | 'metric' | 'side' | 'resolve'

interface SectionDef {
  title: string
  icon: LucideIcon
  endpoint: string
  description: string
  paging: Paging
  controls: Control[]
}

export const SECTIONS: Record<string, SectionDef> = {
  'top-talkers': {
    title: 'Top talkers',
    icon: RadioTower,
    endpoint: '/netflow/top-talkers',
    description: 'Hosts ranked by the traffic they sent, received, or took part in.',
    paging: 'server',
    controls: ['by', 'metric', 'resolve'],
  },
  'top-endpoints': {
    title: 'Top endpoints',
    icon: Smartphone,
    endpoint: '/netflow/top-endpoints',
    description: 'Hosts by all traffic they took part in, with the sent / received split. A flow counts for both of its endpoints.',
    paging: 'server',
    controls: ['by', 'metric', 'resolve'],
  },
  conversations: {
    title: 'Conversations',
    icon: ArrowDownUp,
    endpoint: '/netflow/top-conversations',
    description: 'Client → server per service, request and response directions in one row.',
    paging: 'server',
    controls: ['metric', 'resolve'],
  },
  applications: {
    title: 'Applications',
    icon: Layers,
    endpoint: '/netflow/applications',
    description: 'Traffic by inferred application: exporter App-ID, operator rules, IP protocol and service ports (not deep packet inspection).',
    paging: 'client',
    controls: [],
  },
  protocols: {
    title: 'IP protocols',
    icon: Database,
    endpoint: '/netflow/protocols',
    description: 'Traffic by IP protocol with the services each carries.',
    paging: 'client',
    controls: [],
  },
  ports: {
    title: 'Services',
    icon: SquareStack,
    endpoint: '/netflow/ports',
    description: 'Services by traffic: both directions of each service port, ICMP by message type, portless protocols once.',
    paging: 'server',
    controls: ['metric'],
  },
  dscp: {
    title: 'QoS / DSCP',
    icon: Gauge,
    endpoint: '/netflow/dscp',
    description: 'Traffic by DSCP code point (top six bits of ToS / traffic class).',
    paging: 'client',
    controls: [],
  },
  'tcp-flags': {
    title: 'TCP flags',
    icon: ShieldAlert,
    endpoint: '/netflow/tcp-flags',
    description: 'TCP flows by control bits, counted only where the exporter sent the flags.',
    paging: 'none',
    controls: [],
  },
  'network-classes': {
    title: 'Network classes',
    icon: Network,
    endpoint: '/netflow/network-classes',
    description: 'Address classes on each side of the traffic (the classes the network-class filter uses).',
    paging: 'client',
    controls: [],
  },
  interfaces: {
    title: 'Interfaces',
    icon: Cable,
    endpoint: '/netflow/interfaces',
    description: 'Exporter interfaces by the traffic that crossed them, within every page filter except the interface selection.',
    paging: 'server',
    controls: [],
  },
  countries: {
    title: 'Countries',
    icon: Globe2,
    endpoint: '/netflow/countries',
    description: 'Traffic by country of the source or destination address (IPv4 and IPv6), from the GeoIP database.',
    paging: 'client',
    controls: ['side'],
  },
  asns: {
    title: 'Autonomous systems',
    icon: Landmark,
    endpoint: '/netflow/asns',
    description: 'Source and destination autonomous systems: router-exported AS numbers, else the GeoIP ASN database.',
    paging: 'client',
    controls: ['side'],
  },
  'next-hops': {
    title: 'Next hops',
    icon: Route,
    endpoint: '/netflow/next-hops',
    description: 'Traffic by the next hop the exporter routed it to, with the egress interfaces (raw records only).',
    paging: 'server',
    controls: ['resolve'],
  },
  'ip-groups': {
    title: 'IP groups & sites',
    icon: Boxes,
    endpoint: '/netflow/ip-groups',
    description: 'Traffic sent from, received into and kept inside each managed IP group. Groups can overlap.',
    paging: 'client',
    controls: [],
  },
}

/* ─── Table ────────────────────────────────────────────────────────── */

interface Col<T> {
  key: string
  header: ReactNode
  /** Plain text of the header for aria labels. */
  label: string
  align?: 'right'
  /** Client-side sort value. */
  sort?: (row: T) => number | string | null | undefined
  /** Server-side ranking this header selects (server-paged tables). */
  server?: () => void
  serverActive?: boolean
  emphasis?: boolean
  cell: (row: T) => ReactNode
}

function SortIcon({ dir }: { dir: SortDir | null }) {
  if (dir === 'asc') return <ArrowUp className="h-3 w-3" aria-hidden />
  if (dir === 'desc') return <ArrowDown className="h-3 w-3" aria-hidden />
  return <ArrowDown className="h-3 w-3 opacity-30" aria-hidden />
}

function DataTable<T>({
  rows,
  cols,
  rowKey,
  caption,
  detail,
  selected,
  sortKey,
  sortDir,
  onSort,
  minWidth = '44rem',
}: {
  rows: T[]
  cols: Col<T>[]
  rowKey: (row: T) => string
  caption: string
  detail?: (row: T) => ReactNode
  selected?: (row: T) => boolean
  sortKey?: string | null
  sortDir?: SortDir
  onSort?: (key: string) => void
  minWidth?: string
}) {
  const [open, setOpen] = useState<string | null>(null)
  const baseId = useId()
  const colSpan = cols.length + (detail ? 1 : 0)
  // The decision value (ranked/total column) and its share, repeated under the first cell on
  // phones so it is readable without scrolling the table sideways (NF-30).
  // Only when that column sits beyond what a phone shows first (the fourth column or later).
  const emphasised = cols.findIndex((c) => c.emphasis)
  const decision = emphasised >= 3 ? cols[emphasised] : null
  const share = cols.find((c) => c.key === 'share') ?? null
  const scrollerRef = useRef<HTMLDivElement>(null)
  const viewWidth = useScrollerWidth(scrollerRef)
  return (
    // `relative`: screen-reader-only (absolute) labels inside cells must be clipped by this
    // scroller too, or they widen the page on a phone.
    <div ref={scrollerRef} className="relative -mx-3 overflow-x-auto sm:mx-0">
      <table className="w-full text-xs" style={{ minWidth }}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-border text-xs text-text2">
            {cols.map((c) => {
              const clientSortable = !!c.sort && !!onSort
              const active = clientSortable ? sortKey === c.key : !!c.serverActive
              const sortable = clientSortable || !!c.server
              const ariaSort = active ? (clientSortable && sortDir === 'asc' ? 'ascending' : 'descending') : sortable ? 'none' : undefined
              return (
                <th key={c.key} scope="col" aria-sort={ariaSort} className={cn('whitespace-nowrap px-3 py-2 font-medium', c.align === 'right' ? 'text-right' : 'text-left', active && 'text-text')}>
                  {sortable ? (
                    <button
                      type="button"
                      onClick={() => (clientSortable ? onSort?.(c.key) : c.server?.())}
                      className={cn('inline-flex items-center gap-1 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50', active && 'text-text')}
                      aria-label={`Sort by ${c.label}`}
                    >
                      {c.header}
                      <SortIcon dir={active ? (clientSortable ? sortDir ?? 'desc' : 'desc') : null} />
                    </button>
                  ) : (
                    <span className="inline-flex items-center gap-1">
                      {c.header}
                      {active && <SortIcon dir="desc" />}
                    </span>
                  )}
                </th>
              )
            })}
            {detail && <th scope="col" className="px-3 py-2"><span className="sr-only">Details</span></th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const key = rowKey(row)
            const isOpen = open === key
            const detailId = `${baseId}-${key.replace(/[^a-zA-Z0-9_-]/g, '_')}`
            return (
              <Fragment key={key}>
                <tr className={cn('border-b border-border/60 align-top hover:bg-surface2/50', (isOpen || selected?.(row)) && 'bg-primary/5')}>
                  {cols.map((c, i) => (
                    <td key={c.key} className={cn('px-3 py-2', c.align === 'right' && 'text-right', c.emphasis && 'font-semibold')}>
                      {c.cell(row)}
                      {i === 0 && decision && decision !== c && (
                        <div className="mt-1 flex flex-wrap items-baseline gap-x-2 text-xs sm:hidden" aria-hidden>
                          <span className="text-xs text-text2">{decision.label}</span>
                          <span className="font-semibold">{decision.cell(row)}</span>
                          {share && share !== c && <span>{share.cell(row)}</span>}
                        </div>
                      )}
                    </td>
                  ))}
                  {detail && (
                    <td className="px-3 py-2 text-right">
                      <button
                        type="button"
                        aria-expanded={isOpen}
                        aria-controls={isOpen ? detailId : undefined}
                        onClick={() => setOpen(isOpen ? null : key)}
                        className="rounded px-1.5 py-0.5 text-xs font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
                      >
                        {isOpen ? 'Hide' : 'Details'}
                      </button>
                    </td>
                  )}
                </tr>
                {detail && isOpen && (
                  <tr id={detailId} className="border-b border-border/60">
                    <td colSpan={colSpan} className="bg-surface2/30 p-0">
                      {/* Pinned to the visible part of the scroller: on a phone the table is wider
                          than the screen, the panel is not. */}
                      <div className="sticky left-0 p-2 sm:p-3" style={viewWidth ? { width: viewWidth } : undefined}>
                        {detail(row)}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** Visible width of a horizontal scroller (for panels pinned inside a wide table). */
function useScrollerWidth(ref: RefObject<HTMLDivElement>): number | null {
  const [width, setWidth] = useState<number | null>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => setWidth(el.clientWidth || null)
    update()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref])
  return width
}

function Bytes({ value }: { value: number | null | undefined }) {
  return <Figure value={value} className="text-text" />
}

const DIRECTION_UNKNOWN = 'Direction unknown: no service port identifies the server side (portless protocol or two unidentified ports)'

/**
 * Request or response bytes of a row. When none of the row's traffic has a known direction the
 * split is unknown, not zero; a partly known split names the unknown part under the responses.
 */
function DirBytes({ row, side }: { row: { request_bytes: number; response_bytes: number; bytes: number }; side: 'req' | 'resp' }) {
  const known = row.request_bytes + row.response_bytes
  if (known <= 0 && row.bytes > 0) return <span className="text-text2" title={DIRECTION_UNKNOWN}>—<span className="sr-only"> {DIRECTION_UNKNOWN}</span></span>
  const unknown = row.bytes - known
  return (
    <div>
      <Bytes value={side === 'req' ? row.request_bytes : row.response_bytes} />
      {side === 'resp' && unknown > 0.5 && <div className="text-xs text-text2" title={DIRECTION_UNKNOWN}>+{formatBytes(unknown)} unknown</div>}
    </div>
  )
}

/** Swatch tying a table row to its slice of the share strip above the table. */
function Swatch({ slot }: { slot: number | undefined }) {
  return <span className={cn('mt-1 h-2.5 w-2.5 shrink-0 rounded-sm', seriesClass(slot))} aria-hidden />
}

function Count({ value }: { value: number | null | undefined }) {
  return (
    <span className="tabular-nums text-text" title={formatCountExact(value)}>
      {formatCountExact(value)}
    </span>
  )
}

function Pct({ value }: { value: number | null | undefined }) {
  return <span className="tabular-nums text-text2">{formatPercent(value)}</span>
}

function RowAction({ to, label, children }: { to: string; label: string; children: ReactNode }) {
  return (
    <Link to={to} aria-label={label} title={label} className="inline-flex items-center gap-1 whitespace-nowrap rounded px-1 text-xs font-medium text-primary hover:underline">
      {children}
    </Link>
  )
}

function FilterButton({ label, onClick, active }: { label: string; onClick: () => void; active?: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      aria-label={label}
      title={label}
      className={cn('shrink-0 rounded p-0.5 hover:bg-surface2 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50', active ? 'text-primary' : 'text-text2')}
    >
      <Filter className="h-3 w-3" aria-hidden />
    </button>
  )
}

/* ─── Paging ───────────────────────────────────────────────────────── */

function Pager({ page, pages, offset, returned, total, onPage }: { page: number; pages: number; offset: number; returned: number; total: number | null | undefined; onPage: (p: number) => void }) {
  return (
    <nav aria-label="Pages" className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-3 py-2 text-xs text-text2">
      <span role="status">{showingText(offset, returned, total)}</span>
      {pages > 1 && (
        <span className="flex items-center gap-1">
          <button type="button" onClick={() => onPage(page - 1)} disabled={page <= 0} aria-label="Previous page" className="inline-flex items-center rounded border border-border px-2 py-1 font-medium text-text hover:bg-surface2 disabled:opacity-40">
            <ChevronLeft className="h-3 w-3" aria-hidden /> Prev
          </button>
          <span className="px-1 tabular-nums">Page {page + 1} of {pages.toLocaleString('en-US')}</span>
          <button type="button" onClick={() => onPage(page + 1)} disabled={page >= pages - 1} aria-label="Next page" className="inline-flex items-center rounded border border-border px-2 py-1 font-medium text-text hover:bg-surface2 disabled:opacity-40">
            Next <ChevronRight className="h-3 w-3" aria-hidden />
          </button>
        </span>
      )}
    </nav>
  )
}

/* ─── Page ─────────────────────────────────────────────────────────── */

interface PageState {
  filter: FlowFilter
  apply: (patch: Partial<Record<FlowFilterKey, unknown>>) => void
  toggle: (key: FlowFilterKey, value: unknown, current: unknown) => void
  dctx: DetailContext
  by: RankBy
  metric: RankMetric
  side: Side
  page: number
  setParam: (key: string, value: string | null) => void
  sortKey: string | null
  sortDir: SortDir
  onSort: (key: string) => void
  meta: NfMeta | null
}

export function NetflowSectionPage() {
  const { section = 'top-talkers' } = useParams<{ section: string }>()
  const def = SECTIONS[section]
  const { window: w, rangeIdx, isCustom, setPreset, setCustom, range } = useNfWindow()
  const { filter, errors, invalid, removeKeys, clearAll } = useFlowFilter()
  const [sp, setSp] = useSearchParams()

  const by = parseRankBy(sp.get('by'), defaultRankBy(section))
  const metric = parseRankMetric(sp.get('sort'))
  const side: Side = sp.get('side') === 'src' ? 'src' : 'dst'
  const page = parsePage(sp.get('page'))
  const resolve = sp.get('resolve') === '1'
  const sortKey = sp.get('col')
  const sortDir: SortDir = sp.get('dir') === 'asc' ? 'asc' : 'desc'

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(sp)
    if (value === null) next.delete(key)
    else next.set(key, value)
    if (key !== 'page') next.delete('page')
    setSp(next, { replace: true })
  }
  // Filter changes start again at page 1 (a later page may no longer exist).
  const apply = (patch: Partial<Record<FlowFilterKey, unknown>>) => {
    const next = writeFlowFilterKeepingInvalid(sp, mergeFlowFilter(filter, patch), invalid)
    next.delete('page')
    setSp(next, { replace: true })
  }
  const toggle = (key: FlowFilterKey, value: unknown, current: unknown) => apply({ [key]: current === value ? undefined : value })
  const onSort = (key: string) => {
    const next = new URLSearchParams(sp)
    next.set('dir', sortKey === key && sortDir === 'desc' ? 'asc' : 'desc')
    next.set('col', key)
    next.delete('page')
    setSp(next, { replace: true })
  }

  const server = def?.paging === 'server'
  const extra: Record<string, string | number | null> = {}
  if (server) {
    extra.limit = PAGE_SIZE
    extra.offset = page * PAGE_SIZE
  }
  if (def?.controls.includes('by')) extra.by = by
  if (def?.controls.includes('metric')) extra.sort = metric
  if (def?.controls.includes('resolve') && resolve) extra.resolve = 1
  if (section === 'asns' || section === 'countries') extra.limit = 2000
  const { query, coverage } = useBreakdown<unknown>(def?.endpoint ?? '/netflow/top-talkers', filter, w, extra, !!def)
  const meta = query.data?.meta ?? null

  if (!def) {
    return (
      <div className="space-y-4">
        <NetflowPageHeader title="Unknown NetFlow section" crumb={section} />
        <Card className="p-5">
          <PanelMessage
            icon={Search}
            title={`There is no NetFlow section called "${section}"`}
            detail={
              <span className="flex flex-wrap justify-center gap-2">
                {Object.entries(SECTIONS).map(([k, s]) => (
                  <Link key={k} to={buildNetflowLink(`/netflow/${k}`, { filter, window: w })} className="text-primary hover:underline">{s.title}</Link>
                ))}
              </span>
            }
          />
        </Card>
      </div>
    )
  }

  const state: PageState = {
    filter,
    apply,
    toggle,
    dctx: { filter, window: w, meta: meta?.window ?? null },
    by,
    metric,
    side,
    page,
    setParam,
    sortKey,
    sortDir,
    onSort,
    meta,
  }
  const Icon = def.icon
  const description = section === 'top-talkers' || section === 'top-endpoints' ? `${def.description} ${rankPhrase(by, metric)}.` : def.description
  const selfLink = buildNetflowLink(`/netflow/${section}`, { filter, window: w, extra: Object.fromEntries(Array.from(sp.entries()).filter(([k]) => ['by', 'sort', 'side', 'page', 'resolve', 'col', 'dir'].includes(k))) })
  // A saved view keeps what the page ranks and shows (FE-10): the ranking side (`by`) and the
  // address side are stored explicitly, so "top receivers" never reopens as the default senders.
  const viewParams = sectionViewParams(def.controls, { by, side, resolve, sortKey, sortDir })
  const viewMetric = def.controls.includes('metric') ? metric : null

  return (
    <div className="space-y-4">
      <NetflowPageHeader
        title={def.title}
        icon={Icon}
        description={description}
        parents={filter.exporter ? [{ label: `Exporter ${ipDisplay(filter.exporter)}`, to: buildNetflowLink(`/netflow/devices/${encodeURIComponent(filter.exporter)}`, { filter: removeFlowFilterKeys(filter, ['exporter']), window: w }) }] : []}
        actions={
          <>
            <TimeRangePicker rangeIdx={rangeIdx} isCustom={isCustom} customFrom={range.fromISO} customTo={range.toISO} onPreset={setPreset} onCustom={setCustom} />
            {/* The ranked metric reopens through `sort`; the page path names the section. */}
            <SaveCurrentViewButton
              kind="section"
              filter={filter}
              window={w}
              metaWindow={meta?.window ?? null}
              sort={def.controls.includes('metric') && metric !== 'bytes' ? metric : null}
              params={viewParams}
              settingsSummary={pageSettingsSummary(savedPageParams(viewParams), viewMetric)}
            />
          </>
        }
      />
      <FilterChips filter={filter} invalid={invalid} errors={errors} tz={w.tz} onRemove={(keys) => removeKeys(keys)} onClearAll={clearAll} copyLink={selfLink} />

      {def.controls.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {def.controls.includes('by') && (
            <Segmented label="Rank hosts by" value={by} options={RANK_BY_OPTIONS} onChange={(v) => setParam('by', v)} size="md" />
          )}
          {def.controls.includes('metric') && (
            <Segmented label="Ranked metric" value={metric} options={RANK_METRIC_OPTIONS} onChange={(v) => setParam('sort', v === 'bytes' ? null : v)} size="md" />
          )}
          {def.controls.includes('side') && (
            <Segmented label="Address side" value={side} options={SIDE_OPTIONS} onChange={(v) => setParam('side', v)} size="md" />
          )}
          {def.controls.includes('resolve') && (
            <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-text">
              <input type="checkbox" checked={resolve} onChange={(e) => setParam('resolve', e.target.checked ? '1' : null)} className="h-3.5 w-3.5 accent-[rgb(var(--primary))]" />
              Show reverse-DNS names
            </label>
          )}
        </div>
      )}

      <Card className="min-w-0 overflow-hidden">
        <QueryPanel
          query={query}
          label={def.title.toLowerCase()}
          skeleton="rows"
          className="p-3"
          coverage={coverage}
          unsupported={unsupportedReason(section, query.data?.data)}
          isEmpty={(r) => isEmptySection(section, r.data)}
          empty={emptyText(section, page)}
        >
          {(r) => <SectionBody section={section} data={r.data} s={state} />}
        </QueryPanel>
      </Card>
      <DataQualityBar meta={meta} window={w} filter={filter} />
    </div>
  )
}

function unsupportedReason(section: string, data: unknown): string | null {
  const d = data as Record<string, unknown> | undefined
  if (!d) return null
  if (section === 'countries' && d.available === false) return (d.reason as string) ?? 'No GeoIP country database is installed'
  if (section === 'tcp-flags' && (d.support === 'unsupported' || d.support === 'not_applicable')) return (d.reason as string) ?? 'TCP flags are not exported'
  if (section === 'ip-groups' && d.configured === false) return 'No IP groups are configured. Define them in NetFlow settings.'
  return null
}

function isEmptySection(section: string, data: unknown): boolean {
  const d = data as Record<string, unknown> | undefined
  if (!d) return true
  if (section === 'tcp-flags') return (d.total_tcp as number) === 0
  if (section === 'next-hops') return false // "without next hop" traffic is still shown
  if (section === 'dscp' && d.not_exported) return false // traffic without ToS is still shown
  return Array.isArray(d.items) && d.items.length === 0
}

function emptyText(section: string, page: number): string {
  if (page > 0) return 'This page is past the end of the results (the data has changed); go back to page 1'
  if (section === 'tcp-flags') return 'No TCP flows in this window and filter'
  return 'No flows matched this window and filters'
}

/* ─── Section bodies ───────────────────────────────────────────────── */

function SectionBody({ section, data, s }: { section: string; data: unknown; s: PageState }) {
  switch (section) {
    case 'top-talkers':
    case 'top-endpoints':
      return <TalkersTable data={data as TalkersPage} s={s} />
    case 'conversations':
      return <ConversationsTable data={data as Paged<Conversation>} s={s} />
    case 'applications':
      return <ApplicationsTable data={data as Paged<ApplicationRow>} s={s} />
    case 'protocols':
      return <ProtocolsTable data={data as Paged<ProtocolRow>} s={s} />
    case 'ports':
      return <ServicesTable data={data as Paged<ServiceRow>} s={s} />
    case 'dscp':
      return <DscpTable data={data as DscpPage} s={s} />
    case 'tcp-flags':
      return <TcpFlagsView data={data as TcpFlags} s={s} />
    case 'network-classes':
      return <NetClassesTable data={data as NetClassesPage} s={s} />
    case 'interfaces':
      return <InterfacesTable data={data as InterfacesPage} s={s} />
    case 'countries':
      return <CountriesTable data={data as CountriesPage} s={s} />
    case 'asns':
      return <AsnsTable data={data as AsnsPage} s={s} />
    case 'next-hops':
      return <NextHopsTable data={data as NextHopsPage} s={s} />
    case 'ip-groups':
      return <IpGroupsTable data={data as IpGroupsPage} s={s} />
    default:
      return null
  }
}

/** Scope line above a table: the denominator and what the listed rows cover. */
function ScopeLine({ children }: { children: ReactNode }) {
  return <p className="mb-2 text-xs leading-snug text-text2">{children}</p>
}

function serverPager<T>(data: Paged<T>, s: PageState) {
  const pages = pageCount(data.distinct, PAGE_SIZE)
  // Pinned rows (the selected interface appended to page 1) are not part of the ranked page.
  const returned = data.items.filter((x) => !(x as { pinned?: boolean }).pinned).length
  return <Pager page={s.page} pages={pages} offset={data.offset} returned={returned} total={data.distinct} onPage={(p) => s.setParam('page', p > 0 ? String(p + 1) : null)} />
}

function clientSorted<T>(rows: T[], cols: Col<T>[], s: PageState): T[] {
  const col = cols.find((c) => c.key === s.sortKey && c.sort)
  return col?.sort ? sortRowsBy(rows, col.sort, s.sortDir) : rows
}

function clientPaged<T>(rows: T[], s: PageState): { shown: T[]; pager: ReactNode } {
  const pages = pageCount(rows.length)
  const page = Math.min(s.page, pages - 1)
  return {
    shown: pageSlice(rows, page),
    pager: <Pager page={page} pages={pages} offset={page * PAGE_SIZE} returned={pageSlice(rows, page).length} total={rows.length} onPage={(p) => s.setParam('page', p > 0 ? String(p + 1) : null)} />,
  }
}

/* Talkers / endpoints */

function TalkersTable({ data, s }: { data: TalkersPage; s: PageState }) {
  const by = data.rank_by ?? s.by
  const metric = data.rank_metric ?? s.metric
  const total = data.total
  const listed = data.items.reduce((acc, t) => acc + (by === 'src' ? t.src_bytes : by === 'dst' ? t.dst_bytes : t.bytes), 0)
  const rank = (b: RankBy) => ({ server: () => s.setParam('by', b), serverActive: by === b, emphasis: by === b })
  const cols: Col<Talker>[] = [
    {
      key: 'ip',
      label: 'Host',
      header: 'Host',
      cell: (t) => (
        <HostLabel
          ip={t.ip}
          hostname={t.hostname}
          resolvedAt={t.hostname_resolved_at}
          pending={t.hostname_pending}
          profileHref={hostLink(t.ip, s.filter, s.dctx.window, s.dctx.meta)}
          onFilter={() => s.toggle('talker', t.ip, s.filter.talker)}
          extra={t.group ? <div className="text-xs text-text2">Group {t.group}</div> : null}
        />
      ),
    },
    { key: 'src', label: 'Sent', header: by === 'src' ? 'Sent (ranked)' : 'Sent', align: 'right', ...rank('src'), cell: (t) => <MetricCell t={t} side="src_" metric={metric} /> },
    { key: 'dst', label: 'Received', header: by === 'dst' ? 'Received (ranked)' : 'Received', align: 'right', ...rank('dst'), cell: (t) => <MetricCell t={t} side="dst_" metric={metric} /> },
    { key: 'total', label: 'Total', header: by === 'total' ? 'Total (ranked)' : 'Total', align: 'right', ...rank('total'), cell: (t) => <MetricCell t={t} side="" metric={metric} /> },
    { key: 'share', label: 'Share of scope', header: by === 'total' ? 'Involvement' : 'Share', align: 'right', cell: (t) => <Pct value={t.share_pct} /> },
    {
      key: 'raw',
      label: 'Raw flows',
      header: <span className="sr-only">Raw flows</span>,
      align: 'right',
      cell: (t) => <RowAction to={forensicsLink(s.filter, { talker: t.ip }, s.dctx.window, s.dctx.meta)} label={`Raw flows for ${ipDisplay(t.ip)} (either side)`}>Flows</RowAction>,
    },
  ]
  return (
    <div>
      <ScopeLine>
        {rankPhrase(by, metric)}.{' '}
        {total ? (
          by === 'total' ? (
            <>Involvement is each host's share of all {formatBytes(total.bytes)} in scope; a flow counts for both of its ends, so shares overlap and do not add up.</>
          ) : (
            <>
              Scope: {formatBytes(total.bytes)} from {formatCountExact(data.distinct)} {by === 'src' ? 'senders' : 'receivers'}.
              {data.offset === 0 && total.bytes > 0 && <> The hosts on this page account for {formatPercent((100 * listed) / total.bytes)}.</>}
            </>
          )
        ) : null}
      </ScopeLine>
      <DataTable
        rows={data.items}
        cols={cols}
        rowKey={(t) => t.ip}
        caption={`${rankPhrase(by, metric)}`}
        selected={(t) => s.filter.talker === t.ip}
        detail={(t) => <IpTrafficDetailPanel row={t} by={by} ctx={s.dctx} />}
      />
      {serverPager(data, s)}
    </div>
  )
}

function MetricCell({ t, side, metric }: { t: Talker; side: '' | 'src_' | 'dst_'; metric: RankMetric }) {
  const rec = t as unknown as Record<string, number>
  const b = rec[`${side}bytes`]
  const flows = rec[`${side}flows`]
  const packets = rec[`${side}packets`] ?? t.packets
  return (
    <div>
      {metric === 'bytes' ? <Bytes value={b} /> : <Count value={metric === 'flows' ? flows : packets} />}
      <div className="text-xs tabular-nums text-text2">{metric === 'bytes' ? `${formatCount(flows)} flows` : formatBytes(b)}</div>
    </div>
  )
}

/* Conversations */

function ConversationsTable({ data, s }: { data: Paged<Conversation>; s: PageState }) {
  const cols: Col<Conversation>[] = [
    {
      key: 'pair',
      label: 'Client and server',
      header: 'Client → server',
      cell: (c) => {
        const o = orientationLabels(c.orientation)
        return (
          <div className="space-y-1">
            <div className="flex items-baseline gap-1.5">
              <span className="shrink-0 whitespace-nowrap text-2xs uppercase tracking-wide text-text2 sm:w-[5.5rem]">{o.a}</span>
              <HostLabel ip={c.client} hostname={c.client_hostname} resolvedAt={c.client_hostname_resolved_at} pending={c.client_hostname_pending} profileHref={hostLink(c.client, s.filter, s.dctx.window, s.dctx.meta)} onFilter={() => s.toggle('talker', c.client, s.filter.talker)} />
            </div>
            <div className="flex items-baseline gap-1.5">
              <span className="shrink-0 whitespace-nowrap text-2xs uppercase tracking-wide text-text2 sm:w-[5.5rem]">{o.b}</span>
              <HostLabel ip={c.server} hostname={c.server_hostname} resolvedAt={c.server_hostname_resolved_at} pending={c.server_hostname_pending} profileHref={hostLink(c.server, s.filter, s.dctx.window, s.dctx.meta)} onFilter={() => s.toggle('talker', c.server, s.filter.talker)} />
            </div>
          </div>
        )
      },
    },
    {
      key: 'service',
      label: 'Service',
      header: 'Service',
      cell: (c) => (
        <button type="button" onClick={() => s.apply({ ...c.drill })} className="text-left font-medium text-text hover:text-primary hover:underline" aria-label={`Filter the page to service ${serviceText(c)}`}>
          {serviceText(c)}
        </button>
      ),
    },
    {
      key: 'app',
      label: 'Application',
      header: 'Application (inferred)',
      cell: (c) => (
        <div className="space-y-0.5">
          <div className="text-text">{c.application}</div>
          <ConfidenceChip confidence={c.confidence} title={c.basis_text} />
        </div>
      ),
    },
    { key: 'proto', label: 'IP protocol', header: 'IP protocol', cell: (c) => <span className="text-text">{c.ip_protocol}</span> },
    { key: 'c2s', label: 'Client to server', header: 'C → S', align: 'right', cell: (c) => <Bytes value={c.bytes_c2s} /> },
    { key: 's2c', label: 'Server to client', header: 'S → C', align: 'right', cell: (c) => <Bytes value={c.bytes_s2c} /> },
    {
      key: 'total',
      label: s.metric === 'bytes' ? 'Total' : s.metric === 'flows' ? 'Flows' : 'Packets',
      header: s.metric === 'bytes' ? 'Total' : s.metric === 'flows' ? 'Flows' : 'Packets',
      align: 'right',
      serverActive: true,
      emphasis: true,
      cell: (c) => (s.metric === 'bytes' ? <Bytes value={c.bytes} /> : <Count value={c[s.metric]} />),
    },
    { key: 'share', label: 'Share', header: 'Share', align: 'right', cell: (c) => <Pct value={c.share_pct} /> },
  ]
  return (
    <div>
      <ScopeLine>
        One row per exchange; request and response halves are counted together.
        {data.total && <> Scope: {formatBytes(data.total.bytes)} in {formatCountExact(data.distinct)} conversations.</>}
      </ScopeLine>
      <DataTable
        rows={data.items}
        cols={cols}
        rowKey={(c) => `${c.client}|${c.server}|${c.protocol}|${c.service_port ?? c.icmp_type_code ?? 0}`}
        caption="Top conversations"
        minWidth="56rem"
        detail={(c) => <ConversationDetailPanel row={c} ctx={s.dctx} />}
      />
      {serverPager(data, s)}
    </div>
  )
}

/* Applications */

function ApplicationsTable({ data, s }: { data: Paged<ApplicationRow>; s: PageState }) {
  const strip = shareRows(data.items, { key: (a) => a.name, label: (a) => a.name, value: (a) => a.bytes, total: data.total?.bytes ?? null, max: 8, colors: true })
  const slotOf = new Map(strip.rows.map((r) => [r.key, r.slot]))
  const cols: Col<ApplicationRow>[] = [
    {
      key: 'name',
      label: 'Application',
      header: 'Application',
      sort: (a) => a.name,
      cell: (a) => (
        <div className="flex items-start gap-1.5">
          <Swatch slot={slotOf.get(a.name)} />
          <div className="min-w-0">
            <div className="font-medium text-text">{a.name}</div>
            <div className="text-xs text-text2">{a.category}</div>
            {a.visibility_note && <div className="text-xs text-warning-fg">{a.visibility_note}</div>}
          </div>
          <FilterButton label={`Filter the page to application ${a.name}`} active={s.filter.app === a.name} onClick={() => s.toggle('app', a.name, s.filter.app)} />
        </div>
      ),
    },
    { key: 'conf', label: 'Confidence', header: 'Confidence', sort: (a) => a.confidence_bytes.high / Math.max(1, a.bytes), cell: (a) => <ConfidenceChip confidence={a.confidence} /> },
    { key: 'req', label: 'Requests', header: 'Requests', align: 'right', sort: (a) => a.request_bytes, cell: (a) => <DirBytes row={a} side="req" /> },
    { key: 'resp', label: 'Responses', header: 'Responses', align: 'right', sort: (a) => a.response_bytes, cell: (a) => <DirBytes row={a} side="resp" /> },
    { key: 'bytes', label: 'Total', header: 'Total', align: 'right', emphasis: true, sort: (a) => a.bytes, cell: (a) => <Bytes value={a.bytes} /> },
    { key: 'share', label: 'Share', header: 'Share', align: 'right', sort: (a) => a.share_pct ?? 0, cell: (a) => <Pct value={a.share_pct} /> },
    { key: 'flows', label: 'Flows', header: 'Flows', align: 'right', sort: (a) => a.flows, cell: (a) => <Count value={a.flows} /> },
  ]
  const sorted = clientSorted(data.items, cols, s)
  const { shown, pager } = clientPaged(sorted, s)
  return (
    <div>
      <ScopeLine>
        Every application is in the denominator: {formatBytes(data.total?.bytes)} in scope. The application filter is not applied to this list; the selected application is marked.
        {strip.rows.length < data.items.length ? ` The strip colours the ${strip.rows.length} largest; the grey end is the other ${data.items.length - strip.rows.length}.` : ''}
      </ScopeLine>
      <ShareStrip result={strip} className="mb-3" />
      <DataTable rows={shown} cols={cols} rowKey={(a) => a.name} caption="Applications" selected={(a) => a.selected} sortKey={s.sortKey} sortDir={s.sortDir} onSort={s.onSort} detail={(a) => <ApplicationDetailPanel row={a} ctx={s.dctx} />} />
      {pager}
    </div>
  )
}

/* Protocols */

function ProtocolsTable({ data, s }: { data: Paged<ProtocolRow>; s: PageState }) {
  const cols: Col<ProtocolRow>[] = [
    {
      key: 'name',
      label: 'Protocol',
      header: 'Protocol',
      sort: (p) => p.name,
      cell: (p) => (
        <div className="flex items-center gap-1">
          <span className="font-medium text-text">{protocolLabel(p.protocol)}</span>
          <FilterButton label={`Filter the page to ${protocolLabel(p.protocol)}`} active={s.filter.protocol === p.protocol} onClick={() => s.toggle('protocol', p.protocol, s.filter.protocol)} />
        </div>
      ),
    },
    { key: 'top', label: 'Top service', header: 'Top service', cell: (p) => <span className="text-text2">{p.port_applicable ? (p.services[0] ? serviceText(p.services[0]) : '—') : p.protocol === 1 || p.protocol === 58 ? p.services[0]?.service ?? '—' : 'Not applicable'}</span> },
    { key: 'req', label: 'Requests', header: 'Requests', align: 'right', sort: (p) => p.request_bytes, cell: (p) => <DirBytes row={p} side="req" /> },
    { key: 'resp', label: 'Responses', header: 'Responses', align: 'right', sort: (p) => p.response_bytes, cell: (p) => <DirBytes row={p} side="resp" /> },
    { key: 'bytes', label: 'Total', header: 'Total', align: 'right', emphasis: true, sort: (p) => p.bytes, cell: (p) => <Bytes value={p.bytes} /> },
    { key: 'share', label: 'Share', header: 'Share', align: 'right', sort: (p) => p.share_pct ?? 0, cell: (p) => <Pct value={p.share_pct} /> },
    { key: 'packets', label: 'Packets', header: 'Packets', align: 'right', sort: (p) => p.packets, cell: (p) => <Count value={p.packets} /> },
    { key: 'flows', label: 'Flows', header: 'Flows', align: 'right', sort: (p) => p.flows, cell: (p) => <Count value={p.flows} /> },
  ]
  const { shown, pager } = clientPaged(clientSorted(data.items, cols, s), s)
  return (
    <div>
      <ScopeLine>All protocols in scope: {formatBytes(data.total?.bytes)}.</ScopeLine>
      <DataTable rows={shown} cols={cols} rowKey={(p) => String(p.protocol)} caption="IP protocols" selected={(p) => s.filter.protocol === p.protocol} sortKey={s.sortKey} sortDir={s.sortDir} onSort={s.onSort} detail={(p) => <ProtocolDetailPanel row={p} ctx={s.dctx} />} />
      {pager}
    </div>
  )
}

/* Services */

function ServicesTable({ data, s }: { data: Paged<ServiceRow>; s: PageState }) {
  const cols: Col<ServiceRow>[] = [
    {
      key: 'service',
      label: 'Service',
      header: 'Service',
      cell: (r) => (
        <div className="flex items-center gap-1">
          <span className="font-medium text-text">{serviceText(r)}</span>
          <FilterButton label={`Filter the page to ${serviceText(r)}`} onClick={() => s.apply({ ...r.drill })} />
        </div>
      ),
    },
    { key: 'app', label: 'Application', header: 'Application (inferred)', cell: (r) => <div className="space-y-0.5"><div className="text-text">{r.application}</div><ConfidenceChip confidence={r.confidence} title={r.basis_text} /></div> },
    { key: 'req', label: 'Requests', header: 'Requests', align: 'right', cell: (r) => <DirBytes row={r} side="req" /> },
    { key: 'resp', label: 'Responses', header: 'Responses', align: 'right', cell: (r) => <DirBytes row={r} side="resp" /> },
    { key: 'total', label: s.metric, header: s.metric === 'bytes' ? 'Total' : s.metric === 'flows' ? 'Flows' : 'Packets', align: 'right', serverActive: true, emphasis: true, cell: (r) => (s.metric === 'bytes' ? <Bytes value={r.bytes} /> : <Count value={r[s.metric]} />) },
    { key: 'share', label: 'Share', header: 'Share', align: 'right', cell: (r) => <Pct value={r.share_pct} /> },
    { key: 'raw', label: 'Raw flows', header: <span className="sr-only">Raw flows</span>, align: 'right', cell: (r) => <RowAction to={forensicsLink(s.filter, { ...r.drill }, s.dctx.window, s.dctx.meta)} label={`Raw flows for ${serviceText(r)}`}>Flows</RowAction> },
  ]
  return (
    <div>
      <ScopeLine>Both directions of a service count under its service port. Scope: {formatBytes(data.total?.bytes)} in {formatCountExact(data.distinct)} services.</ScopeLine>
      <DataTable rows={data.items} cols={cols} rowKey={(r) => `${r.protocol}-${r.port}-${r.icmp_type_code ?? ''}`} caption="Services" />
      {serverPager(data, s)}
    </div>
  )
}

/* DSCP */

// Records whose exporter did not send ToS: their DSCP is unknown (never Best Effort) and no DSCP
// filter can select them, so their row has no filter button and no raw-flow link.
const dscpKey = (d: DscpShareItem) => (d.notExported ? 'dscp-not-exported' : `dscp${d.dscp}`)
const dscpName = (d: DscpShareItem) => (d.notExported ? 'Not exported (DSCP unknown)' : dscpLabel(d.dscp))

function DscpTable({ data, s }: { data: DscpPage; s: PageState }) {
  const strip = shareRows(dscpShareItems(data, 8), { key: dscpKey, label: dscpName, value: (d) => d.bytes, total: data.total?.bytes ?? null, max: 8, colors: true })
  const slotOf = new Map(strip.rows.map((r) => [r.key, r.slot]))
  const rows: DscpShareItem[] = dscpShareItems(data).map((d) => (d.notExported ? { ...d, share_pct: data.not_exported?.share_pct ?? null } : d))
  const cols: Col<DscpShareItem>[] = [
    {
      key: 'label',
      label: 'DSCP class',
      header: 'DSCP class',
      sort: (d) => d.dscp,
      cell: (d) => (
        <div className="flex items-start gap-1.5">
          <Swatch slot={slotOf.get(dscpKey(d))} />
          <span className={cn('font-medium', d.notExported ? 'text-text2' : 'text-text')} title={d.notExported ? 'The exporter did not send the ToS field for these records' : undefined}>{dscpName(d)}</span>
          {!d.notExported && <FilterButton label={`Filter the page to ${dscpLabel(d.dscp)}`} active={s.filter.dscp === d.dscp} onClick={() => s.toggle('dscp', d.dscp, s.filter.dscp)} />}
        </div>
      ),
    },
    { key: 'bytes', label: 'Bytes', header: 'Bytes', align: 'right', emphasis: true, sort: (d) => d.bytes, cell: (d) => <Bytes value={d.bytes} /> },
    { key: 'share', label: 'Share', header: 'Share', align: 'right', sort: (d) => d.share_pct ?? 0, cell: (d) => <Pct value={d.share_pct} /> },
    { key: 'packets', label: 'Packets', header: 'Packets', align: 'right', sort: (d) => d.packets, cell: (d) => <Count value={d.packets} /> },
    { key: 'flows', label: 'Flows', header: 'Flows', align: 'right', sort: (d) => d.flows, cell: (d) => <Count value={d.flows} /> },
    { key: 'raw', label: 'Raw flows', header: <span className="sr-only">Raw flows</span>, align: 'right', cell: (d) => (d.notExported ? null : <RowAction to={forensicsLink(s.filter, { dscp: d.dscp }, s.dctx.window, s.dctx.meta)} label={`Raw flows marked ${dscpLabel(d.dscp)}`}>Flows</RowAction>) },
  ]
  const { shown, pager } = clientPaged(clientSorted(rows, cols, s), s)
  return (
    <div>
      <ScopeLine>
        Every class present is in the denominator ({formatBytes(data.total?.bytes)}). The DSCP filter is not applied to this list; the selected class is marked.
        {data.not_exported ? ' Traffic whose exporter did not send the ToS field is listed as "Not exported": its DSCP is unknown, not Best Effort, and a DSCP filter does not match it.' : ''}
      </ScopeLine>
      <ShareStrip result={strip} className="mb-3" />
      <DataTable rows={shown} cols={cols} rowKey={(d) => dscpKey(d)} caption="DSCP classes" minWidth="32rem" selected={(d) => d.selected} sortKey={s.sortKey} sortDir={s.sortDir} onSort={s.onSort} />
      {pager}
    </div>
  )
}

/* TCP flags */

const FLAG_ORDER: TcpFlagFilter[] = ['syn_only', 'ack_only', 'rst', 'fin', 'psh', 'urg', 'no_flags']

function TcpFlagsView({ data, s }: { data: TcpFlags; s: PageState }) {
  const base = data.flagged_tcp
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-3">
        <StatusTile tone="neutral" label="TCP flows" value={formatCount(data.total_tcp)} title={formatCountExact(data.total_tcp)} />
        <StatusTile
          tone={data.support === 'supported' ? 'success' : data.support === 'partial' ? 'warning' : 'unknown'}
          word={data.support === 'supported' ? 'Exported' : data.support === 'partial' ? 'Partial' : 'Unknown'}
          label="Flags known"
          value={formatPercent(data.flag_coverage_pct)}
          caption={`${formatCountExact(data.flagged_tcp)} flows with exported flags`}
        />
        <StatusTile tone="neutral" label="Flags unknown" value={formatCount(data.unknown_tcp)} title={formatCountExact(data.unknown_tcp)} caption={data.legacy_tcp ? `${formatCountExact(data.legacy_tcp)} records predate field tracking` : 'Template without the TCP-flags field'} />
      </div>
      {data.reason && <ScopeLine>{data.reason}</ScopeLine>}
      <DataTable
        rows={FLAG_ORDER}
        rowKey={(k) => k}
        caption="TCP flows by control flag"
        minWidth="28rem"
        selected={(k) => s.filter.tcpflag === k}
        cols={[
          {
            key: 'flag',
            label: 'Flag',
            header: 'Flag',
            cell: (k) => (
              <div className="flex items-center gap-1">
                <span className="font-medium text-text">{TCP_FLAG_LABELS[k]}</span>
                <FilterButton label={`Filter the page to ${TCP_FLAG_LABELS[k]}`} active={s.filter.tcpflag === k} onClick={() => s.toggle('tcpflag', k, s.filter.tcpflag)} />
              </div>
            ),
          },
          { key: 'flows', label: 'Flows', header: 'Flows', align: 'right', emphasis: true, cell: (k) => <Count value={data[k]} /> },
          { key: 'pct', label: 'Share of flagged TCP', header: 'Of flagged TCP', align: 'right', cell: (k) => <Pct value={base ? (100 * data[k]) / base : null} /> },
          { key: 'raw', label: 'Raw flows', header: <span className="sr-only">Raw flows</span>, align: 'right', cell: (k) => <RowAction to={forensicsLink(s.filter, { tcpflag: k, protocol: 6 }, s.dctx.window, s.dctx.meta)} label={`Raw flows: ${TCP_FLAG_LABELS[k]}`}>Flows</RowAction> },
        ]}
      />
    </div>
  )
}

/* Network classes */

function NetClassesTable({ data, s }: { data: NetClassesPage; s: PageState }) {
  const cols: Col<NetClassRow>[] = [
    {
      key: 'name',
      label: 'Class',
      header: 'Class',
      sort: (c) => c.name,
      cell: (c) => (
        <div className="flex items-center gap-1">
          <span className="font-medium text-text">{c.name}</span>
          <FilterButton label={`Filter the page to ${c.name} (either side)`} active={s.filter.netclass === c.name} onClick={() => s.toggle('netclass', c.name, s.filter.netclass)} />
        </div>
      ),
    },
    { key: 'src', label: 'As source', header: 'As source', align: 'right', sort: (c) => c.src_bytes, cell: (c) => <div><Bytes value={c.src_bytes} /><div><Pct value={c.src_share_pct} /></div></div> },
    { key: 'dst', label: 'As destination', header: 'As destination', align: 'right', sort: (c) => c.dst_bytes, cell: (c) => <div><Bytes value={c.dst_bytes} /><div><Pct value={c.dst_share_pct} /></div></div> },
    { key: 'flows', label: 'Flows (either side)', header: 'Flows (src + dst)', align: 'right', sort: (c) => c.flows, cell: (c) => <Count value={c.flows} /> },
  ]
  const { shown, pager } = clientPaged(clientSorted(data.items, cols, s), s)
  return (
    <div className="space-y-3">
      <ScopeLine>Each side is a share of all {formatBytes(data.total?.bytes)} in scope (a flow has one source and one destination).</ScopeLine>
      <DataTable rows={shown} cols={cols} rowKey={(c) => c.name} caption="Traffic by network class and side" minWidth="30rem" selected={(c) => c.selected} sortKey={s.sortKey} sortDir={s.sortDir} onSort={s.onSort} />
      {pager}
      {data.pairs.length > 0 && (
        <section>
          <h2 className="mb-1 text-xs font-semibold text-text">Source class → destination class</h2>
          <ul role="list" className="grid gap-1 sm:grid-cols-2">
            {data.pairs.slice(0, 12).map((p) => (
              <li key={`${p.src}>${p.dst}`} className="flex items-center justify-between gap-2 rounded border border-border/60 px-2 py-1 text-xs">
                <span className="text-text">{p.src} → {p.dst}</span>
                <span className="text-right"><Bytes value={p.bytes} /> <Pct value={data.total?.bytes ? (100 * p.bytes) / data.total.bytes : null} /></span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

/* Interfaces */

function InterfacesTable({ data, s }: { data: InterfacesPage; s: PageState }) {
  const cols: Col<InterfaceRow>[] = [
    {
      key: 'iface',
      label: 'Interface',
      header: 'Interface',
      cell: (i) => (
        <div className="flex items-start gap-1">
          <div className="min-w-0">
            <div className="break-words font-medium text-text">{i.display_name}</div>
            <div className="break-all text-xs text-text2">{i.device_hostname || ipDisplay(i.exporter_ip)} · ifIndex {i.ifindex}{i.role ? ` · ${i.role}` : ''}</div>
            {i.pinned && <div className="text-xs font-medium text-primary">Selected interface (not among the rows ranked on this page)</div>}
          </div>
          <FilterButton label={`${i.selected ? 'Clear' : 'Filter the page to'} interface ${interfaceTitle(i)}`} active={i.selected} onClick={() => s.apply({ iface: i.selected ? undefined : i.iface })} />
        </div>
      ),
    },
    { key: 'in', label: 'In', header: 'In', align: 'right', cell: (i) => <div><Bytes value={i.in_bytes} /><div className="text-xs tabular-nums text-text2">{formatBps(i.in_bps_avg)} avg</div></div> },
    { key: 'out', label: 'Out', header: 'Out', align: 'right', cell: (i) => <div><Bytes value={i.out_bytes} /><div className="text-xs tabular-nums text-text2">{formatBps(i.out_bps_avg)} avg</div></div> },
    { key: 'bytes', label: 'Total', header: 'Total', align: 'right', emphasis: true, serverActive: true, cell: (i) => <Bytes value={i.bytes} /> },
    {
      key: 'speed',
      label: 'Speed',
      header: 'Speed',
      align: 'right',
      cell: (i) => (
        <div>
          <span className="tabular-nums text-text" title={i.speed_in_bps != null ? undefined : 'Set the speed on the device or as an interface override in NetFlow settings'}>{i.speed_in_bps != null ? formatBps(i.speed_in_bps) : 'Unknown'}</span>
          <div className="text-xs text-text2">{i.speed_in_bps != null ? i.speed_source : 'not configured'}</div>
          <Link to={capacityLink(i.iface, s.dctx.window)} className="text-xs text-primary hover:underline" title={UTILIZATION_LINK_TITLE}>Utilization</Link>
        </div>
      ),
    },
    { key: 'flows', label: 'Flows', header: 'Flows', align: 'right', cell: (i) => <Count value={i.flows} /> },
    { key: 'raw', label: 'Raw flows', header: <span className="sr-only">Raw flows</span>, align: 'right', cell: (i) => <RowAction to={forensicsLink(s.filter, { iface: i.iface }, s.dctx.window, s.dctx.meta)} label={`Raw flows on ${interfaceTitle(i)}`}>Flows</RowAction> },
  ]
  return (
    <div>
      <ScopeLine>
        {data.scope.interface_excluded ? 'The selected interface is highlighted; other interfaces are listed for comparison within the other filters. ' : 'Within every page filter. '}
        {data.scope.selected && data.offset === 0 && !data.items.some((i) => i.selected) && (
          <strong className="font-medium text-text">The selected interface ({data.scope.selected}) carried no traffic in this scope. </strong>
        )}
        Interface volumes overlap (a flow crosses an ingress and an egress interface, on each exporter that saw it). Average rates are over {Math.round(data.window_seconds / 60).toLocaleString('en-US')} minutes.
      </ScopeLine>
      <DataTable rows={data.items} cols={cols} rowKey={(i) => i.iface} caption="Interfaces by traffic" minWidth="52rem" selected={(i) => i.selected} />
      {serverPager(data, s)}
    </div>
  )
}

/* Countries and ASNs */

function sideCells<T extends { src_bytes: number; dst_bytes: number; src_share_pct: number | null; dst_share_pct: number | null; flows: number }>(s: PageState): Col<T>[] {
  const active = (side: Side) => s.side === side
  return [
    { key: 'src', label: 'As source', header: 'As source', align: 'right', emphasis: active('src'), sort: (r) => r.src_bytes, cell: (r) => <div><Bytes value={r.src_bytes} /><div><Pct value={r.src_share_pct} /></div></div> },
    { key: 'dst', label: 'As destination', header: 'As destination', align: 'right', emphasis: active('dst'), sort: (r) => r.dst_bytes, cell: (r) => <div><Bytes value={r.dst_bytes} /><div><Pct value={r.dst_share_pct} /></div></div> },
    { key: 'flows', label: 'Flows (src + dst)', header: 'Flows (src + dst)', align: 'right', sort: (r) => r.flows, cell: (r) => <Count value={r.flows} /> },
  ]
}

function sideRows<T extends { src_bytes: number; dst_bytes: number }>(items: T[], s: PageState): T[] {
  const f = s.side === 'src' ? 'src_bytes' : 'dst_bytes'
  return [...items].sort((a, b) => b[f] - a[f]).filter((r) => r.src_bytes > 0 || r.dst_bytes > 0)
}

function RemainderLine({ remainder, s }: { remainder: { src_bytes: number; dst_bytes: number } | null | undefined; s: PageState }) {
  if (!remainder) return null
  const v = s.side === 'src' ? remainder.src_bytes : remainder.dst_bytes
  if (!v) return null
  return <ScopeLine>Not resolved (addresses beyond the top 5,000 networks looked up): {formatBytes(v)} of {s.side === 'src' ? 'source' : 'destination'} traffic.</ScopeLine>
}

function CountriesTable({ data, s }: { data: CountriesPage; s: PageState }) {
  const cols: Col<CountryRow>[] = [
    {
      key: 'name',
      label: 'Country',
      header: 'Country',
      sort: (c) => c.country_name,
      cell: (c) => (
        <div className="flex flex-wrap items-center gap-1">
          {c.country && <span aria-hidden>{flagEmoji(c.country)}</span>}
          <span className="font-medium text-text">{c.country_name}</span>
          {c.country && <span className="text-xs text-text2">{c.country}</span>}
          {c.kind !== 'country' && <Chip title={c.hint}>no country</Chip>}
        </div>
      ),
    },
    ...sideCells<CountryRow>(s),
  ]
  const rows = clientSorted(sideRows(data.items, s), cols, s)
  const { shown, pager } = clientPaged(rows, s)
  return (
    <div>
      <ScopeLine>
        Each side is a share of all {formatBytes(data.total?.bytes)} in scope. GeoIP country database {data.geoip.country_db_version ?? '(version unknown)'}; addresses are placed by /24 (IPv4) or /48 (IPv6) network. Country is not a filter dimension, so rows do not drill down.
      </ScopeLine>
      <RemainderLine remainder={data.remainder} s={s} />
      <DataTable rows={shown} cols={cols} rowKey={(c) => c.country ?? c.kind} caption="Traffic by country and side" minWidth="30rem" sortKey={s.sortKey} sortDir={s.sortDir} onSort={s.onSort} />
      {pager}
    </div>
  )
}

function AsnsTable({ data, s }: { data: AsnsPage; s: PageState }) {
  const cols: Col<AsnRow>[] = [
    {
      key: 'name',
      label: 'Autonomous system',
      header: 'Autonomous system',
      sort: (a) => a.asn ?? -1,
      cell: (a) => {
        const filterable = a.asn != null && a.source !== 'geoip'
        return (
          <div className="flex items-start gap-1">
            <div className="min-w-0">
              <div className="font-medium text-text">{a.asn != null ? `AS${a.asn}` : a.as_name}</div>
              {a.asn != null && <div className="break-words text-xs text-text2">{a.as_name ?? 'Name unknown'}</div>}
              {a.source && <Chip title={AS_SOURCE_TEXT[a.source]}>{a.source === 'geoip' ? 'GeoIP' : a.source === 'flow' ? 'Router' : 'Router + GeoIP'}</Chip>}
            </div>
            {filterable && <FilterButton label={`Filter the page to AS${a.asn}`} active={s.filter.asn === a.asn} onClick={() => s.toggle('asn', a.asn, s.filter.asn)} />}
          </div>
        )
      },
    },
    ...sideCells<AsnRow>(s),
  ]
  const rows = clientSorted(sideRows(data.items, s), cols, s)
  const { shown, pager } = clientPaged(rows, s)
  const b = data.basis_bytes
  const sum = b.flow + b.geoip + b.none
  return (
    <div>
      <ScopeLine>
        Each side is a share of all {formatBytes(data.total?.bytes)} in scope.
        {sum > 0 && <> AS numbers exported by routers cover {formatPercent((100 * b.flow) / sum)} of attributed traffic; GeoIP {formatPercent((100 * b.geoip) / sum)}; none {formatPercent((100 * b.none) / sum)}.</>}{' '}
        The AS filter matches router-exported AS numbers only.
      </ScopeLine>
      <RemainderLine remainder={data.remainder} s={s} />
      <DataTable rows={shown} cols={cols} rowKey={(a) => (a.asn != null ? `AS${a.asn}` : a.kind)} caption="Traffic by autonomous system and side" minWidth="30rem" selected={(a) => a.selected} sortKey={s.sortKey} sortDir={s.sortDir} onSort={s.onSort} />
      {pager}
    </div>
  )
}

/* Next hops */

function NextHopsTable({ data, s }: { data: NextHopsPage; s: PageState }) {
  const cols: Col<NextHopRow>[] = [
    {
      key: 'nh',
      label: 'Next hop',
      header: 'Next hop',
      cell: (h) => <HostLabel ip={h.next_hop} hostname={h.hostname} resolvedAt={h.hostname_resolved_at} pending={h.hostname_pending} profileHref={hostLink(h.next_hop, s.filter, s.dctx.window, s.dctx.meta)} />,
    },
    { key: 'bytes', label: 'Bytes', header: 'Bytes', align: 'right', emphasis: true, serverActive: true, cell: (h) => <Bytes value={h.bytes} /> },
    { key: 'share', label: 'Share', header: 'Share', align: 'right', cell: (h) => <Pct value={h.share_pct} /> },
    { key: 'flows', label: 'Flows', header: 'Flows', align: 'right', cell: (h) => <Count value={h.flows} /> },
    {
      key: 'out',
      label: 'Egress interfaces',
      header: 'Egress interfaces',
      cell: (h) => (
        <ul role="list" className="space-y-0.5 text-xs">
          {h.out_interfaces.slice(0, 4).map((i) => (
            <li key={i.iface}>
              <button type="button" className="text-left text-text hover:text-primary hover:underline" onClick={() => s.apply({ iface: i.iface })} aria-label={`Filter the page to interface ${interfaceTitle(i)}`}>
                {interfaceTitle(i)}
              </button>
            </li>
          ))}
          {h.out_interfaces.length === 0 && <li className="text-text2">Not exported</li>}
        </ul>
      ),
    },
  ]
  const w = data.without_next_hop
  const total = data.total?.bytes ?? 0
  return (
    <div className="space-y-3">
      <ScopeLine>
        Next hops as the exporters reported them (raw records only).
        {data.next_hop_coverage_pct != null && <> Traffic with a next hop: {formatPercent(data.next_hop_coverage_pct)} of {formatBytes(total)}.</>}
      </ScopeLine>
      <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-3">
        <StatusTile tone="neutral" label="No next hop (local / connected)" value={formatBytes(w.connected.bytes)} title={formatBytesExact(w.connected.bytes)} caption={total ? formatPercent((100 * w.connected.bytes) / total) : undefined} />
        <StatusTile tone="unknown" word="Unknown" label="Next hop not exported" value={formatBytes(w.not_exported.bytes)} title={formatBytesExact(w.not_exported.bytes)} caption={total ? formatPercent((100 * w.not_exported.bytes) / total) : undefined} />
        <StatusTile tone="unknown" word="Unknown" label="Legacy records" value={formatBytes(w.legacy.bytes)} title={formatBytesExact(w.legacy.bytes)} caption="Recorded before field tracking" />
      </div>
      {data.items.length > 0 ? (
        <>
          <DataTable rows={data.items} cols={cols} rowKey={(h) => h.next_hop} caption="Traffic by next hop" minWidth="40rem" />
          {serverPager(data, s)}
        </>
      ) : (
        <p className="py-4 text-center text-xs text-text2">No record in this window carries a routed next hop.</p>
      )}
    </div>
  )
}

/* IP groups */

function IpGroupsTable({ data, s }: { data: IpGroupsPage; s: PageState }) {
  const cols: Col<IpGroupRow>[] = [
    {
      key: 'name',
      label: 'Group',
      header: 'Group',
      sort: (g) => g.name,
      cell: (g) => (
        <div className="flex items-start gap-1">
          <div className="min-w-0">
            <div className="font-medium text-text">{g.name}</div>
            <div className="break-all text-xs text-text2">{g.cidrs.slice(0, 3).join(', ')}{g.cidrs.length > 3 ? ` +${g.cidrs.length - 3}` : ''}</div>
          </div>
          <FilterButton label={`Filter the page to IP group ${g.name}`} active={s.filter.group === g.name} onClick={() => s.toggle('group', g.name, s.filter.group)} />
        </div>
      ),
    },
    { key: 'src', label: 'Sent from group', header: 'Sent from', align: 'right', sort: (g) => g.src_bytes, cell: (g) => <Bytes value={g.src_bytes} /> },
    { key: 'dst', label: 'Received into group', header: 'Received into', align: 'right', sort: (g) => g.dst_bytes, cell: (g) => <Bytes value={g.dst_bytes} /> },
    { key: 'internal', label: 'Internal', header: 'Internal', align: 'right', sort: (g) => g.internal_bytes, cell: (g) => <Bytes value={g.internal_bytes} /> },
    { key: 'bytes', label: 'Involved', header: 'Involved', align: 'right', emphasis: true, sort: (g) => g.bytes, cell: (g) => <div><Bytes value={g.bytes} /><div><Pct value={g.share_pct} /></div></div> },
    { key: 'flows', label: 'Flows', header: 'Flows', align: 'right', sort: (g) => g.flows, cell: (g) => <Count value={g.flows} /> },
  ]
  const { shown, pager } = clientPaged(clientSorted(data.items, cols, s), s)
  return (
    <div>
      <ScopeLine>
        Shares of all {formatBytes(data.total?.bytes)} in scope. Groups can overlap and a flow between two groups counts for both, so shares do not add up.
        {data.ungrouped && <> Traffic touching no group: {formatBytes(data.ungrouped.bytes)} ({formatPercent(data.ungrouped.share_pct)}).</>}
      </ScopeLine>
      <DataTable rows={shown} cols={cols} rowKey={(g) => g.name} caption="Traffic by IP group" minWidth="40rem" selected={(g) => g.selected} sortKey={s.sortKey} sortDir={s.sortDir} onSort={s.onSort} />
      {pager}
    </div>
  )
}

