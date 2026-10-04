/**
 * Building blocks of the breakdown cards and section pages (WP B2): the card frame, the
 * whole-scope share strip + legend, ranked bars, a segmented ranking control, host labels and
 * the per-card data footnote. Every coloured mark sits next to a text row that names it and
 * states its value, so identity and magnitude never depend on colour alone.
 */
import { useId, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, Filter, Info, UserSearch, type LucideIcon } from 'lucide-react'
import { Card } from '@/components/ui/Card'
import { cn, relativeTime } from '@/lib/utils'
import { useNfCoverage, useNfQuery, nfParams, type NfParams } from '@/lib/netflow/api'
import { FLOW_FILTER_LABELS, unappliedFilterKeys, type FlowFilter, type FlowFilterKey } from '@/lib/netflow/filters'
import { formatBytes, formatBytesExact, formatCount, formatCountExact, formatInterval, formatPercent, ipDisplay } from '@/lib/netflow/format'
import { coverageState } from '@/lib/netflow/status'
import type { NfMeta, NfResult } from '@/lib/netflow/types'
import type { NfWindow } from '@/lib/netflow/window'
import { BAR_CLASS, BAR2_CLASS, isUnspecifiedIp, seriesClass, UNRECORDED_ADDRESS_HINT, UNRECORDED_ADDRESS_LABEL, type RankRow, type ShareResult, type ShareRow } from './model'
import type { RankMetric } from './shapes'

/* ─── Data ─────────────────────────────────────────────────────────── */

/**
 * A breakdown endpoint for the page window and filter, with `with_totals=1` (whole-scope
 * denominators) inside the NetFlow envelope, plus the window's collection coverage (shared with
 * every other panel through the query cache) so "no records" is not shown as "no traffic".
 */
export function useBreakdown<T>(path: string, filter: FlowFilter, w: NfWindow, extra?: NfParams | null, enabled = true) {
  const params = nfParams(w, filter, { with_totals: 1, ...(extra ?? {}) })
  const query = useNfQuery<T>(path, params, { window: w, enabled })
  const coverage = useNfCoverage(w, filter, enabled)
  return { query, coverage: coverageState(coverage.data?.data) }
}

/* ─── Values ───────────────────────────────────────────────────────── */

export function metricText(value: number | null | undefined, metric: RankMetric): string {
  return metric === 'bytes' ? formatBytes(value) : formatCount(value)
}

export function metricExact(value: number | null | undefined, metric: RankMetric): string {
  return metric === 'bytes' ? formatBytesExact(value) : `${formatCountExact(value)} ${metric}`
}

/** A figure whose exact value is one hover (and one screen-reader label) away. */
export function Figure({ value, metric = 'bytes', className }: { value: number | null | undefined; metric?: RankMetric; className?: string }) {
  const exact = metricExact(value, metric)
  return (
    <span className={cn('tabular-nums', className)} title={exact}>
      <span aria-hidden>{metricText(value, metric)}</span>
      <span className="sr-only">{exact}</span>
    </span>
  )
}

/* ─── Card frame ───────────────────────────────────────────────────── */

export interface CardShellProps {
  title: string
  icon?: LucideIcon
  subtitle?: ReactNode
  /** Controls in the header (ranking selector). */
  controls?: ReactNode
  /** "View all" target. */
  viewAll?: string
  viewAllLabel?: string
  footer?: ReactNode
  children: ReactNode
  className?: string
}

export function CardShell({ title, icon: Icon, subtitle, controls, viewAll, viewAllLabel, footer, children, className }: CardShellProps) {
  const headingId = useId()
  return (
    <Card className={cn('flex min-w-0 flex-col', className)} role="region" aria-labelledby={headingId}>
      <div className="flex flex-wrap items-start justify-between gap-2 px-4 pb-2 pt-4">
        <div className="min-w-0">
          <h3 id={headingId} className="flex items-center gap-1.5 text-sm font-semibold text-text">
            {Icon && <Icon className="h-4 w-4 shrink-0 text-primary" aria-hidden />}
            {title}
          </h3>
          {subtitle && <div className="mt-0.5 text-[11px] leading-snug text-text2">{subtitle}</div>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {controls}
          {viewAll && (
            <Link
              to={viewAll}
              className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-xs font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
            >
              {viewAllLabel ?? 'View all'}
              <span className="sr-only"> {title.toLowerCase()}</span>
              <ArrowRight className="h-3 w-3" aria-hidden />
            </Link>
          )}
        </div>
      </div>
      <div className="min-w-0 flex-1 px-4 pb-3">{children}</div>
      {footer && <div className="border-t border-border px-4 py-2">{footer}</div>}
    </Card>
  )
}

/* ─── Segmented control ────────────────────────────────────────────── */

export function Segmented<V extends string>({
  label,
  value,
  options,
  onChange,
  size = 'sm',
}: {
  label: string
  value: V
  options: { value: V; label: string; hint?: string }[]
  onChange: (v: V) => void
  size?: 'sm' | 'md'
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-md border border-border bg-surface2/60 p-0.5">
      {options.map((o) => {
        const active = o.value === value
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={active}
            title={o.hint}
            onClick={() => onChange(o.value)}
            onKeyDown={(e) => {
              if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return
              e.preventDefault()
              const i = options.findIndex((x) => x.value === value)
              const next = options[(i + (e.key === 'ArrowRight' ? 1 : options.length - 1)) % options.length]
              onChange(next.value)
            }}
            className={cn(
              'rounded px-2 font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
              size === 'sm' ? 'py-0.5 text-[11px]' : 'py-1 text-xs',
              active ? 'bg-surface text-text shadow-sm' : 'text-text2 hover:text-text',
            )}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}

/* ─── Share strip + legend (additive breakdowns) ───────────────────── */

/** A 100% bar of the whole scope: one segment per listed item plus the remainder (2px gaps). */
export function ShareStrip<T>({ result, className }: { result: ShareResult<T>; className?: string }) {
  const rows: ShareRow<T>[] = [...result.rows, ...(result.remainder ? [result.remainder] : [])]
  if (!(result.denominator > 0)) return null
  return (
    <div className={cn('flex h-3 w-full gap-[2px] overflow-hidden rounded', className)} aria-hidden>
      {rows.map((r) => {
        const pct = r.pct ?? 0
        if (pct <= 0) return null
        return (
          <div
            key={r.key}
            className={cn('h-full min-w-[2px] first:rounded-l last:rounded-r', r.remainder ? 'bg-[repeating-linear-gradient(135deg,transparent_0_3px,rgb(var(--surface))_3px_4px)]' : '', seriesClass(r.remainder ? undefined : r.slot))}
            style={{ flexGrow: pct, flexBasis: 0 }}
            title={`${r.label}: ${formatPercent(pct)}`}
          />
        )
      })}
    </div>
  )
}

export interface LegendRowAction<T> {
  /** Click the row label: e.g. apply as page filter. */
  onSelect?: (item: T) => void
  selectLabel?: (item: T) => string
  /** Secondary link (details, host profile, raw flows). */
  href?: (item: T) => string | null
  hrefLabel?: (item: T) => string
  /** Rows that cannot be applied as a filter (e.g. "Not exported"); default every item. */
  selectable?: (item: T) => boolean
}

/**
 * Every slice of the strip as a row: swatch, name, value and share of the stated denominator.
 * The remainder row closes the list so the rows visibly add up to the total.
 */
export function ShareLegend<T>({
  result,
  metric = 'bytes',
  render,
  action,
  selected,
  sub,
}: {
  result: ShareResult<T>
  metric?: RankMetric
  /** Custom label content for an item row. */
  render?: (row: ShareRow<T>) => ReactNode
  action?: LegendRowAction<T>
  selected?: (item: T) => boolean
  sub?: (item: T) => ReactNode
}) {
  const rows: ShareRow<T>[] = [...result.rows, ...(result.remainder ? [result.remainder] : [])]
  return (
    <ul role="list" className="divide-y divide-border/60">
      {rows.map((r) => {
        const item = r.item
        const isSel = !!item && !!selected?.(item)
        const href = item && action?.href ? action.href(item) : null
        return (
          <li key={r.key} className={cn('flex items-start gap-2 py-1.5', isSel && 'rounded bg-primary/5')}>
            <span className={cn('mt-1 h-2.5 w-2.5 shrink-0 rounded-sm', seriesClass(r.remainder ? undefined : r.slot))} aria-hidden />
            <div className="min-w-0 flex-1">
              {item && action?.onSelect && (action.selectable?.(item) ?? true) ? (
                <button
                  type="button"
                  onClick={() => action.onSelect?.(item)}
                  aria-pressed={isSel}
                  aria-label={action.selectLabel?.(item)}
                  className="max-w-full text-left text-xs font-medium text-text hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
                >
                  {render ? render(r) : <span className="break-words">{r.label}</span>}
                </button>
              ) : (
                <span className={cn('break-words text-xs', r.remainder ? 'italic text-text2' : 'font-medium text-text')}>
                  {render && !r.remainder ? render(r) : r.label}
                </span>
              )}
              {item && sub && <div className="mt-0.5 text-[11px] text-text2">{sub(item)}</div>}
            </div>
            <div className="shrink-0 text-right text-xs">
              <Figure value={r.value} metric={metric} className="font-semibold text-text" />
              <div className="text-[11px] tabular-nums text-text2">{formatPercent(r.pct)}</div>
            </div>
            {href && (
              <Link
                to={href}
                aria-label={action?.hrefLabel?.(item as T) ?? `Details for ${r.label}`}
                title={action?.hrefLabel?.(item as T)}
                className="mt-0.5 shrink-0 rounded p-0.5 text-text2 hover:bg-surface2 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
              >
                <ArrowRight className="h-3.5 w-3.5" aria-hidden />
              </Link>
            )}
          </li>
        )
      })}
    </ul>
  )
}

/* ─── Ranked bars (non-additive rankings, or long lists) ───────────── */

export function RankedBars<T>({
  rows,
  metric = 'bytes',
  label,
  split,
  selected,
  sub,
  trailing,
}: {
  rows: RankRow<T>[]
  metric?: RankMetric
  label: (row: RankRow<T>) => ReactNode
  /** Two-part bar (e.g. sent vs received) as fractions 0..1 of the row value. */
  split?: (item: T) => { a: number; b: number; aLabel: string; bLabel: string } | null
  selected?: (item: T) => boolean
  sub?: (item: T) => ReactNode
  trailing?: (item: T) => ReactNode
}) {
  return (
    <ul role="list" className="space-y-2">
      {rows.map((r) => {
        const parts = split?.(r.item) ?? null
        const isSel = !!selected?.(r.item)
        return (
          <li key={r.key} className={cn('rounded', isSel && 'bg-primary/5 ring-1 ring-primary/30')}>
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1 text-xs">{label(r)}</div>
              <div className="shrink-0 text-right text-xs">
                <Figure value={r.value} metric={metric} className="font-semibold text-text" />
                {r.pct !== null && <span className="ml-1.5 text-[11px] tabular-nums text-text2">{formatPercent(r.pct)}</span>}
              </div>
              {trailing?.(r.item)}
            </div>
            {sub && <div className="mt-0.5 text-[11px] text-text2">{sub(r.item)}</div>}
            <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-surface2" aria-hidden>
              {parts ? (
                <div className="flex h-full gap-[2px]" style={{ width: `${Math.max(1.5, r.width)}%` }}>
                  {parts.a > 0 && <div className={cn('h-full rounded-l-full', BAR_CLASS)} style={{ flexGrow: parts.a, flexBasis: 0 }} title={parts.aLabel} />}
                  {parts.b > 0 && <div className={cn('h-full rounded-r-full', BAR2_CLASS)} style={{ flexGrow: parts.b, flexBasis: 0 }} title={parts.bLabel} />}
                </div>
              ) : (
                <div className={cn('h-full rounded-full', BAR_CLASS)} style={{ width: `${Math.max(1.5, r.width)}%` }} />
              )}
            </div>
          </li>
        )
      })}
    </ul>
  )
}

/** Legend for two-part bars. */
export function SplitLegend({ a, b }: { a: string; b: string }) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-[11px] text-text2">
      <span className="inline-flex items-center gap-1"><span className={cn('h-2 w-2 rounded-sm', BAR_CLASS)} aria-hidden />{a}</span>
      <span className="inline-flex items-center gap-1"><span className={cn('h-2 w-2 rounded-sm', BAR2_CLASS)} aria-hidden />{b}</span>
    </div>
  )
}

/* ─── Hosts ────────────────────────────────────────────────────────── */

/**
 * An address with its reverse-DNS name when one was resolved (and how fresh it is), linked to
 * the host profile, with a named button that applies it as the page's host filter.
 */
export function HostLabel({
  ip,
  hostname,
  resolvedAt,
  pending,
  profileHref,
  onFilter,
  extra,
}: {
  ip: string
  hostname?: string | null
  resolvedAt?: string | null
  pending?: boolean
  profileHref?: string
  onFilter?: () => void
  extra?: ReactNode
}) {
  const shown = ipDisplay(ip)
  const unrecorded = isUnspecifiedIp(ip)
  return (
    <div className="flex min-w-0 items-start gap-1">
      <div className="min-w-0">
        {unrecorded ? (
          <span className="text-xs font-medium italic text-text2" title={UNRECORDED_ADDRESS_HINT}>
            {UNRECORDED_ADDRESS_LABEL} <span className="font-mono not-italic">({shown})</span>
          </span>
        ) : profileHref ? (
          <Link
            to={profileHref}
            className="break-all font-mono text-xs font-medium text-text hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
            title={`Host profile for ${shown}`}
          >
            {shown}
          </Link>
        ) : (
          <span className="break-all font-mono text-xs font-medium text-text">{shown}</span>
        )}
        {hostname && (
          <div className="break-all text-[11px] text-text2" title={resolvedAt ? `Reverse DNS, resolved ${relativeTime(resolvedAt)}` : 'Reverse DNS'}>
            {hostname}
            {resolvedAt && <span className="ml-1 text-text2/80">· rDNS {relativeTime(resolvedAt)}</span>}
          </div>
        )}
        {pending && <div className="text-[11px] italic text-text2">Name lookup pending</div>}
        {extra}
      </div>
      {onFilter && (
        <button
          type="button"
          onClick={onFilter}
          aria-label={`Filter the page to host ${shown}`}
          title={`Filter to ${shown} (source or destination)`}
          className="shrink-0 rounded p-0.5 text-text2 hover:bg-surface2 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
        >
          <Filter className="h-3 w-3" aria-hidden />
        </button>
      )}
    </div>
  )
}

/** A host address as a profile link, or the "address not recorded" label for 0.0.0.0. */
export function HostLink({ ip, href, className }: { ip: string; href: string; className?: string }) {
  if (isUnspecifiedIp(ip)) {
    return (
      <span className={cn('italic text-text2', className)} title={UNRECORDED_ADDRESS_HINT}>
        {UNRECORDED_ADDRESS_LABEL}
      </span>
    )
  }
  return (
    <Link to={href} className={cn('break-all font-mono font-medium text-text hover:text-primary hover:underline', className)} title={`Host profile for ${ipDisplay(ip)}`}>
      {ipDisplay(ip)}
    </Link>
  )
}

export function HostProfileLink({ href, ip }: { href: string; ip: string }) {
  return (
    <Link
      to={href}
      aria-label={`Host profile for ${ipDisplay(ip)}`}
      className="inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline"
    >
      <UserSearch className="h-3 w-3" aria-hidden /> Host profile
    </Link>
  )
}

/* ─── Chips ────────────────────────────────────────────────────────── */

const CONF_TONE: Record<string, string> = {
  high: 'border-success/35 bg-success/10 text-success-fg',
  medium: 'border-info/35 bg-info/10 text-info-fg',
  low: 'border-warning/35 bg-warning/10 text-warning-fg',
  'n/a': 'border-border bg-surface2 text-text2',
}

/** Classification confidence as a word, never colour alone. */
export function ConfidenceChip({ confidence, title }: { confidence: string; title?: string }) {
  const word = confidence === 'n/a' ? 'unclassified' : `${confidence} confidence`
  return (
    <span className={cn('inline-flex items-center rounded border px-1.5 py-px text-[10px] font-semibold', CONF_TONE[confidence] ?? CONF_TONE['n/a'])} title={title}>
      {word}
    </span>
  )
}

export function Chip({ children, title, className }: { children: ReactNode; title?: string; className?: string }) {
  return (
    <span className={cn('inline-flex items-center gap-1 rounded border border-border bg-surface2/70 px-1.5 py-px text-[10px] font-medium text-text2', className)} title={title}>
      {children}
    </span>
  )
}

/* ─── Footnote (what the numbers are based on) ─────────────────────── */

const SOURCE_SHORT: Record<string, string> = {
  raw: 'raw records',
  rollup: 'hourly rollup',
  hybrid: 'rollup + raw edges',
  interface_rollup: '5-min interface rollup',
  pg: 'configuration',
}

/**
 * One line under a card: the denominator the shares use, the data source, filters this card does
 * not apply (and why), truncation and API warnings. The page-level DataQualityBar carries the
 * full coverage and retention detail.
 */
export function CardFootnote({
  meta,
  filter,
  basis,
  intentional,
}: {
  meta: NfMeta | null | undefined
  filter: FlowFilter
  basis?: ReactNode
  /** Filter keys this card leaves out on purpose, with the reason. */
  intentional?: Partial<Record<FlowFilterKey, string>>
}) {
  const parts: ReactNode[] = []
  if (basis) parts.push(<span key="basis">{basis}</span>)
  const src = meta?.window?.source
  if (src) {
    const gran = meta?.window?.granularity_s && meta.window.granularity_s > 1 ? ` (${formatInterval(meta.window.granularity_s)})` : ''
    parts.push(<span key="src">Source: {SOURCE_SHORT[src] ?? src}{gran}</span>)
  }
  const unapplied = meta ? unappliedFilterKeys(filter, meta.filters) : []
  for (const k of unapplied) {
    const why = intentional?.[k]
    parts.push(
      <span key={`u-${k}`} className={why ? undefined : 'font-medium text-warning-fg'}>
        Not filtered by {FLOW_FILTER_LABELS[k] ?? k}{why ? ` (${why})` : ''}
      </span>,
    )
  }
  if (meta?.truncated?.more) parts.push(<span key="trunc">Top {meta.truncated.returned.toLocaleString('en-US')} shown</span>)
  for (const w of meta?.warnings ?? []) {
    parts.push(
      <span key={`w-${w.code}`} className="text-warning-fg" title={w.message}>
        {w.message}
      </span>,
    )
  }
  if (parts.length === 0) return null
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] leading-snug text-text2">
      <Info className="h-3 w-3 shrink-0" aria-hidden />
      {parts.map((p, i) => (
        <span key={i} className="inline-flex items-center gap-2">
          {i > 0 && <span aria-hidden className="text-border-strong">·</span>}
          {p}
        </span>
      ))}
    </div>
  )
}

/** The envelope of a card query (for footnotes), or null while loading. */
export function metaOf<T>(q: { data?: NfResult<T> }): NfMeta | null {
  return q.data?.meta ?? null
}
