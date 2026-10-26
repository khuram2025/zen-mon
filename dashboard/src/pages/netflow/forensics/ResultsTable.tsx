import { Fragment, useCallback, useSyncExternalStore, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  dscpLabel,
  formatBytes,
  formatBytesExact,
  formatCount,
  formatCountExact,
  formatDateTime,
  formatDuration,
  ipDisplay,
  protocolName,
  tzOffsetLabel,
} from '@/lib/netflow/format'
import { buildNetflowLink, exporterPath, hostPath } from '@/lib/netflow/links'
import type { FlowFilter } from '@/lib/netflow/filters'
import type { NfWindow } from '@/lib/netflow/window'
import { relativeTime } from '@/lib/utils'
import {
  COLUMNS,
  endpointText,
  flagsText,
  icmpTypeCodeText,
  rowEndpoint,
  interfaceText,
  SORT_LABELS,
  SORTS,
  type ForensicsRow,
  type ForensicsSort,
  type SortOrder,
} from './model'

export interface ResultsTableProps {
  rows: ForensicsRow[]
  columns: string[]
  sort: ForensicsSort
  order: SortOrder
  onSort: (col: ForensicsSort) => void
  /** Direct sort choice (the narrow-screen sort menu); falls back to onSort. */
  onSortChange?: (sort: ForensicsSort, order: SortOrder) => void
  expanded: ReadonlySet<string>
  onToggle: (id: string) => void
  tz: string
  window: NfWindow
  /** The applied filter (row actions narrow it further). */
  filter: FlowFilter
  dimmed?: boolean
}

const endpoint = endpointText

function protoShort(n: number): string {
  return protocolName(n) ?? String(n)
}

function cell(row: ForensicsRow, key: string, tz: string): ReactNode {
  switch (key) {
    case 'time':
      return (
        <span className="whitespace-nowrap font-mono text-xs" title={`${row.timestamp ?? ''} · ${relativeTime(row.timestamp)}`}>
          {formatDateTime(row.timestamp, tz, { zone: 'none' })}
        </span>
      )
    case 'received':
      return <span className="whitespace-nowrap font-mono text-xs">{formatDateTime(row.received_at, tz, { zone: 'none' })}</span>
    case 'exporter':
      return <span className="font-mono text-xs">{ipDisplay(row.exporter)}</span>
    case 'src':
      return <span className="whitespace-nowrap font-mono text-xs">{rowEndpoint(row, 'src')}</span>
    case 'dst': {
      // ICMP carries type/code where ports would be: shown as such, never as ":2048".
      const icmp = icmpTypeCodeText(row)
      return <span className="whitespace-nowrap font-mono text-xs" title={icmp ? `ICMP ${icmp}` : undefined}>{rowEndpoint(row, 'dst')}</span>
    }
    case 'protocol':
      return <span className="whitespace-nowrap" title={`IP protocol ${row.protocol}`}>{protoShort(row.protocol)}</span>
    case 'service':
      return (
        <span className="block min-w-[7rem]">
          <span className="block">{row.service ?? '—'}</span>
          {row.application && row.application !== row.service && <span className="block text-2xs text-text2">{row.application}</span>}
        </span>
      )
    case 'flags': {
      const f = flagsText(row)
      return <span className={cn('font-mono text-xs', f.text === 'n/e' && 'text-text2')} title={f.title}>{f.text}</span>
    }
    case 'dscp':
      return row.dscp === null ? <span className="text-text2" title="Not exported">n/e</span> : <span className="whitespace-nowrap">{dscpLabel(row.dscp)}</span>
    case 'interfaces':
      return (
        <span className="block min-w-[8rem] text-xs">
          <span className="block" title="Input interface">{interfaceText(row.input_interface, row.input_if)}</span>
          <span className="block text-text2" title="Output interface">→ {interfaceText(row.output_interface, row.output_if)}</span>
        </span>
      )
    case 'bytes':
      return <span className="whitespace-nowrap font-mono text-xs" title={formatBytesExact(row.bytes)}>{formatBytes(row.bytes)}</span>
    case 'packets':
      return <span className="whitespace-nowrap font-mono text-xs" title={`${formatCountExact(row.packets)} packets`}>{formatCount(row.packets)}</span>
    case 'duration':
      return row.duration_ms === null
        ? <span className="text-text2" title="Start/end times were not exported or are inconsistent">n/e</span>
        : <span className="whitespace-nowrap font-mono text-xs">{formatDuration(row.duration_ms)}</span>
    case 'sampling':
      return row.sampling_interval > 1 ? <span className="font-mono text-xs" title="Volumes are scaled estimates">1:{row.sampling_interval}</span> : <span className="text-text2">1:1</span>
    case 'direction':
      return <span>{row.flow_direction ?? '—'}</span>
    case 'as':
      return row.src_as === null && row.dst_as === null
        ? <span className="text-text2">n/e</span>
        : <span className="whitespace-nowrap font-mono text-xs">AS{row.src_as ?? '?'} → AS{row.dst_as ?? '?'}</span>
    case 'next_hop':
      return <span className="font-mono text-xs">{row.next_hop ?? '—'}</span>
    case 'nat':
      return row.nat
        ? <span className="whitespace-nowrap font-mono text-xs">{endpoint(row.nat.src ?? row.src, row.nat.src_port)} → {endpoint(row.nat.dst ?? row.dst, row.nat.dst_port)}</span>
        : <span className="text-text2">—</span>
    case 'fw_event':
      return <span>{row.fw_event ?? '—'}</span>
    default:
      return null
  }
}

const NARROW = '(max-width: 639px)'

/** True below the `sm` breakpoint (phones); false where media queries are unavailable (SSR, tests). */
export function useNarrowScreen(query = NARROW): boolean {
  const subscribe = useCallback(
    (cb: () => void) => {
      try {
        const m = window.matchMedia(query)
        m.addEventListener('change', cb)
        return () => m.removeEventListener('change', cb)
      } catch {
        return () => {}
      }
    },
    [query],
  )
  return useSyncExternalStore(
    subscribe,
    () => {
      try {
        return window.matchMedia(query).matches
      } catch {
        return false
      }
    },
    () => false,
  )
}

/**
 * Raw records with frozen headers, a column chooser's columns, and expandable detail rows. On a
 * phone the same rows render as cards so the decision values (endpoints, volume, duration) are
 * never pushed off-screen (NF-30).
 */
export function ResultsTable(props: ResultsTableProps) {
  const narrow = useNarrowScreen()
  return narrow ? <ResultsCards {...props} /> : <ResultsGrid {...props} />
}

/** Columns every card shows; other chosen columns are listed under them. */
const CARD_CORE = new Set(['time', 'src', 'dst', 'protocol', 'service', 'bytes', 'packets', 'duration', 'exporter'])

export function ResultsCards({ rows, columns, sort, order, onSort, onSortChange, expanded, onToggle, tz, window: w, filter, dimmed }: ResultsTableProps) {
  const extra = COLUMNS.filter((c) => columns.includes(c.key) && !CARD_CORE.has(c.key))
  const offset = tzOffsetLabel(tz, rows[0]?.timestamp ?? Date.now())
  const choose = (value: string) => {
    const [s, o] = value.split(':') as [ForensicsSort, SortOrder]
    if (onSortChange) onSortChange(s, o)
    else if (s !== sort || o !== order) onSort(s)
  }
  return (
    <div className={cn('space-y-2', dimmed && 'opacity-60')}>
      <div className="flex items-center justify-between gap-2 text-xs text-text2">
        <span>Times in {offset}</span>
        <label className="flex items-center gap-1.5">
          <span>Sort</span>
          <select
            value={`${sort}:${order}`}
            onChange={(e) => choose(e.target.value)}
            className="h-8 rounded-md border border-border bg-surface px-2 text-xs text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
          >
            {SORTS.flatMap((s) =>
              (['desc', 'asc'] as SortOrder[]).map((o) => (
                <option key={`${s}:${o}`} value={`${s}:${o}`}>
                  {SORT_LABELS[s]} {s === 'timestamp' ? (o === 'desc' ? '(newest first)' : '(oldest first)') : o === 'desc' ? '(largest first)' : '(smallest first)'}
                </option>
              )),
            )}
          </select>
        </label>
      </div>
      <ul aria-label="Flow records" className="space-y-2">
        {rows.map((r) => {
          const open = expanded.has(r.id)
          return (
            <li key={r.id} className={cn('rounded-md border border-border bg-surface', open && 'border-primary/40')}>
              <button
                type="button"
                onClick={() => onToggle(r.id)}
                aria-expanded={open}
                aria-label={`${open ? 'Hide' : 'Show'} details for ${ipDisplay(r.src)} to ${ipDisplay(r.dst)} at ${r.timestamp ?? 'unknown time'}`}
                className="block w-full rounded-md px-3 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
              >
                <span className="flex items-baseline justify-between gap-2">
                  <span className="flex min-w-0 items-center gap-1 font-mono text-xs text-text2">
                    {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5 shrink-0" aria-hidden />}
                    {formatDateTime(r.timestamp, tz, { zone: 'none' })}
                  </span>
                  <span className="shrink-0 font-mono text-xs font-semibold text-text" title={formatBytesExact(r.bytes)}>
                    {formatBytes(r.bytes)}
                  </span>
                </span>
                <span className="mt-1 block break-all font-mono text-sm text-text">
                  {rowEndpoint(r, 'src')} <span className="text-text2">→</span> {rowEndpoint(r, 'dst')}
                </span>
                <span className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-xs text-text2">
                  <span className="text-text">{protoShort(r.protocol)}</span>
                  {r.service && <span>{r.service}</span>}
                  <span>{formatCountExact(r.packets)} pkt</span>
                  <span>{r.duration_ms === null ? 'duration n/e' : formatDuration(r.duration_ms)}</span>
                  <span className="font-mono">via {ipDisplay(r.exporter)}</span>
                </span>
                {extra.length > 0 && (
                  <span className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-xs">
                    {extra
                      // TCP flags only mean something on TCP records.
                      .filter((c) => c.key !== 'flags' || r.protocol === 6)
                      .map((c) => (
                        <span key={c.key} className="min-w-0">
                          <span className="text-text2">{c.label}: </span>
                          {c.key === 'interfaces'
                            ? `${interfaceText(r.input_interface, r.input_if)} → ${interfaceText(r.output_interface, r.output_if)}`
                            : cell(r, c.key, tz)}
                        </span>
                      ))}
                  </span>
                )}
              </button>
              {open && (
                <div className="border-t border-border bg-surface2/40 px-3 py-3">
                  <RowDetail row={r} tz={tz} window={w} filter={filter} />
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function ResultsGrid({ rows, columns, sort, order, onSort, expanded, onToggle, tz, window: w, filter, dimmed }: ResultsTableProps) {
  const cols = COLUMNS.filter((c) => columns.includes(c.key))
  const offset = tzOffsetLabel(tz, rows[0]?.timestamp ?? Date.now())
  return (
    <div
      role="region"
      aria-label="Flow records"
      tabIndex={0}
      className={cn('max-h-[70vh] overflow-auto rounded-md border border-border focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40', dimmed && 'opacity-60')}
    >
      <table className="w-full min-w-[56rem] border-separate border-spacing-0 text-xs">
        <thead className="sticky top-0 z-10">
          <tr>
            <th scope="col" className="sticky top-0 w-8 border-b border-border bg-surface2 px-2 py-2">
              <span className="sr-only">Details</span>
            </th>
            {cols.map((c) => {
              const active = c.sort === sort
              const label = c.key === 'time' ? `${c.label} (${offset})` : c.key === 'received' ? `${c.label} (${offset})` : c.label
              return (
                <th
                  key={c.key}
                  scope="col"
                  aria-sort={c.sort ? (active ? (order === 'asc' ? 'ascending' : 'descending') : 'none') : undefined}
                  className={cn('border-b border-border bg-surface2 px-2 py-2 text-left text-xs font-semibold text-text2', c.numeric && 'text-right')}
                >
                  {c.sort ? (
                    <button
                      type="button"
                      onClick={() => onSort(c.sort as ForensicsSort)}
                      className={cn('inline-flex items-center gap-1 rounded hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40', active && 'text-text')}
                    >
                      {label}
                      {active ? (order === 'asc' ? <ArrowUp className="h-3 w-3" aria-hidden /> : <ArrowDown className="h-3 w-3" aria-hidden />) : <ArrowUpDown className="h-3 w-3 opacity-40" aria-hidden />}
                      <span className="sr-only">{active ? `, sorted ${order === 'asc' ? 'ascending' : 'descending'}` : ', sortable'}</span>
                    </button>
                  ) : (
                    label
                  )}
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const open = expanded.has(r.id)
            return (
              <Fragment key={r.id}>
                <tr className={cn('hover:bg-surface2/60', open && 'bg-primary/5')}>
                  <td className="border-b border-border px-2 py-1.5 align-top">
                    <button
                      type="button"
                      onClick={() => onToggle(r.id)}
                      aria-expanded={open}
                      aria-label={`${open ? 'Hide' : 'Show'} details for ${ipDisplay(r.src)} to ${ipDisplay(r.dst)} at ${r.timestamp ?? 'unknown time'}`}
                      className="rounded p-0.5 text-text2 hover:bg-surface3 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                    >
                      {open ? <ChevronDown className="h-3.5 w-3.5" aria-hidden /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden />}
                    </button>
                  </td>
                  {cols.map((c) => (
                    <td key={c.key} className={cn('border-b border-border px-2 py-1.5 align-top text-text', c.numeric && 'text-right')}>
                      {cell(r, c.key, tz)}
                    </td>
                  ))}
                </tr>
                {open && (
                  <tr>
                    <td colSpan={cols.length + 1} className="border-b border-border bg-surface2/40 px-3 py-3">
                      <RowDetail row={r} tz={tz} window={w} filter={filter} />
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

function Item({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs font-medium uppercase tracking-wide text-text2">{label}</dt>
      <dd className="break-words text-sm text-text">{children}</dd>
    </div>
  )
}

const BITS: [number, string][] = [
  [0, 'TCP flags'], [1, 'ToS'], [2, 'ports'], [3, 'input ifIndex'], [4, 'output ifIndex'], [5, 'flow times'],
  [6, 'AS'], [7, 'masks'], [8, 'next hop'], [9, 'direction'], [10, 'application ID'], [11, 'ICMP type'],
  [12, 'NAT'], [13, 'firewall event'], [14, 'sampling'], [15, 'VLAN'],
]

function presentFields(fp: number): string {
  if (!fp) return 'Unknown (recorded before field tracking)'
  const names = BITS.filter(([bit]) => (fp >>> bit) & 1).map(([, n]) => n)
  return names.length ? names.join(', ') : 'none of the optional fields'
}

function RowDetail({ row, tz, window: w, filter }: { row: ForensicsRow; tz: string; window: NfWindow; filter: FlowFilter }) {
  const link = (f: FlowFilter) => buildNetflowLink('/netflow/forensics', { filter: f, window: w, extra: { run: true } })
  const conv: FlowFilter = { ...filter, talker: undefined, src: row.src, dst: row.dst, protocol: row.protocol }
  if (row.dst_port !== null && row.protocol !== 1 && row.protocol !== 58) conv.dst_port = row.dst_port
  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-4">
        <Item label="Flow end">{formatDateTime(row.timestamp, tz)}{row.clock_clamped && ' (exporter clock out of range; set to receive time)'}</Item>
        <Item label="Received by the appliance">{formatDateTime(row.received_at, tz)}</Item>
        <Item label="Duration">{row.duration_ms === null ? 'Not exported or inconsistent' : formatDuration(row.duration_ms)}</Item>
        <Item label="Exporter">
          {ipDisplay(row.exporter)} · {row.export_protocol ?? `v${row.flow_version}`}
          {row.observation_domain ? ` · domain ${row.observation_domain}` : ''}
        </Item>
        <Item label="Source">{rowEndpoint(row, 'src')}{row.src_mask !== null ? ` (/${row.src_mask})` : ''}</Item>
        <Item label="Destination">{rowEndpoint(row, 'dst')}{row.dst_mask !== null ? ` (/${row.dst_mask})` : ''}</Item>
        {icmpTypeCodeText(row) && <Item label="ICMP type / code">{icmpTypeCodeText(row)}</Item>}
        <Item label="Protocol">{protoShort(row.protocol)} ({row.protocol}) · IPv{row.ip_version}</Item>
        <Item label="Service">
          {row.service ?? '—'}
          {row.application ? ` · ${row.application}` : ''}
          {row.class_direction && row.class_direction !== 'n/a' ? ` · ${row.class_direction}` : ''}
          {row.class_basis ? <span className="text-text2"> (basis: {row.class_basis.replace('_', ' ')})</span> : null}
        </Item>
        <Item label="Bytes / packets">{formatBytesExact(row.bytes)} · {formatCountExact(row.packets)} {row.packets === 1 ? 'packet' : 'packets'}</Item>
        <Item label="Sampling">{row.sampling_interval > 1 ? `1:${row.sampling_interval} (volumes scaled)` : 'Unsampled'}</Item>
        <Item label="TCP flags">{flagsText(row).title}</Item>
        <Item label="DSCP / ToS">{row.dscp === null ? 'Not exported' : `${dscpLabel(row.dscp)} · ToS ${row.tos}`}</Item>
        <Item label="Input interface">
          {interfaceText(row.input_interface, row.input_if)}
          {row.input_if ? ` (ifIndex ${row.input_if})` : ''}
          {row.input_interface?.device_hostname ? ` on ${row.input_interface.device_hostname}` : ''}
        </Item>
        <Item label="Output interface">
          {interfaceText(row.output_interface, row.output_if)}
          {row.output_if ? ` (ifIndex ${row.output_if})` : ''}
        </Item>
        <Item label="Flow direction">{row.flow_direction ?? 'Not exported'}</Item>
        <Item label="AS">{row.src_as === null && row.dst_as === null ? 'Not exported' : `AS${row.src_as ?? '?'} → AS${row.dst_as ?? '?'}`}</Item>
        <Item label="Next hop">{row.next_hop ?? 'Not exported'}</Item>
        <Item label="NAT (post-translation)">{row.nat ? `${endpoint(row.nat.src ?? row.src, row.nat.src_port)} → ${endpoint(row.nat.dst ?? row.dst, row.nat.dst_port)}` : 'None'}</Item>
        <Item label="Firewall event">{row.fw_event ?? 'None'}</Item>
        <Item label="Application ID">{row.app_id ?? 'None'}</Item>
        <Item label="End reason">{row.flow_end_reason ?? 'Not exported'}</Item>
        <Item label="Fields the exporter sent">{presentFields(row.fields_present)}{row.reverse ? ' · reverse direction of a bidirectional record' : ''}</Item>
      </dl>
      <div className="flex flex-wrap gap-2 text-xs">
        <Link className="rounded-md border border-border bg-surface px-2 py-1 hover:bg-surface2" to={link(conv)}>Only this conversation</Link>
        <Link className="rounded-md border border-border bg-surface px-2 py-1 hover:bg-surface2" to={link({ talker: row.src })}>All flows of {ipDisplay(row.src)}</Link>
        <Link className="rounded-md border border-border bg-surface px-2 py-1 hover:bg-surface2" to={link({ talker: row.dst })}>All flows of {ipDisplay(row.dst)}</Link>
        <Link className="rounded-md border border-border bg-surface px-2 py-1 hover:bg-surface2" to={buildNetflowLink(hostPath(ipDisplay(row.src)), { window: w })}>Host profile: source</Link>
        <Link className="rounded-md border border-border bg-surface px-2 py-1 hover:bg-surface2" to={buildNetflowLink(exporterPath(ipDisplay(row.exporter)), { window: w })}>Exporter overview</Link>
      </div>
    </div>
  )
}
