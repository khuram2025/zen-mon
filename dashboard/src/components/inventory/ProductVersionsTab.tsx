import { useState, type CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import { Card } from '@/components/ui/Card'
import { cn } from '@/lib/utils'
import { NO_VERSION, type ProductResponse } from '@/pages/inventory/softwareSettings'
import { InventoryEmpty } from './InventoryEmpty'
import { fmtInt, plural } from './model'
import {
  installedHref, SCHEME_LABELS, versionGroups, versionRows, type GenerationMeta, type VersionBarRow, type VersionGroup,
} from './productModel'
import { ViewToggle, type ChartView } from './ViewToggle'

const focus = 'outline-none focus-visible:ring-2 focus-visible:ring-primary'
const HATCH = 'repeating-linear-gradient(45deg, rgb(var(--muted) / 0.6) 0 2px, rgb(var(--surface2)) 2px 5px)'

export function GenerationPill({ meta, className }: { meta: GenerationMeta; className?: string }) {
  return (
    <span title={meta.title} className={cn('inline-flex shrink-0 items-center rounded-full border px-1.5 py-px text-xs font-semibold leading-4', meta.tone, className)}>
      {meta.label}
    </span>
  )
}

/** The bar's tint follows the generation: the newest strongest, older ones fade, a version that cannot be ordered is hatched. */
function fillStyle(generation: string | null): CSSProperties {
  if (generation === 'N') return { background: 'rgb(var(--primary) / 0.95)' }
  if (generation === 'N-1') return { background: 'rgb(var(--primary) / 0.7)' }
  if (generation === 'older') return { background: 'rgb(var(--primary) / 0.45)' }
  return { background: HATCH }
}

const rowGrid = 'grid grid-cols-[minmax(0,1fr)_3.25rem] items-center gap-x-3 gap-y-1 sm:grid-cols-[minmax(7.5rem,14rem)_minmax(0,1fr)_3.5rem]'

function ChartRow({ row }: { row: VersionBarRow }) {
  const text = `${row.version}: ${row.generation.label === 'Unordered' ? 'cannot be ordered' : `generation ${row.generation.label}`}, ${plural(row.hosts, 'host')}`
  return (
    <li>
      <Link
        to={row.hostsHref}
        data-drill="assets"
        data-drill-count={row.hosts}
        aria-label={text}
        title={`${plural(row.hosts, 'host')} run ${row.version}${row.installs !== row.hosts ? ` (${plural(row.installs, 'installation')})` : ''}. Open the list.`}
        className={cn(rowGrid, 'group rounded-md px-2 py-1.5 hover:bg-surface2/70', focus)}
      >
        <span className="flex min-w-0 items-center gap-2">
          <GenerationPill meta={row.generation} />
          <span className="truncate font-mono text-xs text-text" title={row.version}>{row.version}</span>
        </span>
        <span className="order-last col-span-2 h-3 overflow-hidden rounded bg-surface2 sm:order-none sm:col-span-1" aria-hidden>
          <span className="block h-full rounded" style={{ width: `${Math.max(row.width, 1.5)}%`, ...fillStyle(row.generationKey) }} />
        </span>
        <span className="text-right text-sm font-medium tabular-nums text-text group-hover:underline">{fmtInt(row.hosts)}</span>
      </Link>
    </li>
  )
}

function GroupHeading({ group, visible }: { group: VersionGroup; visible: boolean }) {
  const label = SCHEME_LABELS[group.scheme]
  return (
    <h3 className={cn('flex flex-wrap items-baseline gap-x-2 px-2 pb-1 pt-3 text-xs font-semibold text-text first:pt-0', !visible && 'sr-only')}>
      {label.title}
      <span className="text-xs font-normal text-muted">{label.note} · {plural(group.versions.length, 'version')}</span>
    </h3>
  )
}

function Unversioned({ productKey, unversioned, maxHosts }: { productKey: string; unversioned: ProductResponse['unversioned']; maxHosts: number }) {
  if (unversioned.installs === 0) return null
  const width = Math.max(1.5, (unversioned.hosts / Math.max(1, maxHosts)) * 100)
  return (
    <li data-unversioned="">
      <div className={cn(rowGrid, 'px-2 py-1.5')}>
        <span className="flex min-w-0 items-center gap-2">
          <span className="inline-flex shrink-0 items-center rounded-full border border-border bg-surface2 px-1.5 py-px text-xs font-semibold leading-4 text-text2" title="These rows report no version, so they are not a version and have no generation">Unknown</span>
          <span className="truncate text-xs italic text-muted">No version reported</span>
        </span>
        <span className="order-last col-span-2 h-3 overflow-hidden rounded bg-surface2 sm:order-none sm:col-span-1" aria-hidden>
          <span className="block h-full rounded" style={{ width: `${width}%`, background: HATCH }} />
        </span>
        <span className="text-right text-sm tabular-nums text-text2" title="Hosts with a row that reports no version. There is no list of these hosts; the installations are listed.">{fmtInt(unversioned.hosts)}</span>
      </div>
      <p className="px-2 pb-1 text-xs text-muted">
        <Link to={installedHref(productKey, unversioned.address || NO_VERSION)} data-drill="page" data-drill-count={unversioned.installs} className={cn('rounded font-medium text-primary hover:underline', focus)}>
          {plural(unversioned.installs, 'installation')}
        </Link>{' '}
        report no version. They are not counted as a version.
      </p>
    </li>
  )
}

function VersionTable({ rows, caption, hideCaption }: { rows: VersionBarRow[]; caption: string; hideCaption: boolean }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full table-fixed border-separate border-spacing-0 text-sm" data-versions-table="">
        <colgroup><col className="w-24 sm:w-32" /><col /><col className="w-20 sm:w-32" /><col className="w-24 sm:w-36" /></colgroup>
        <caption className={cn('px-2 pb-1 pt-3 text-left text-xs font-semibold text-text', hideCaption && 'sr-only')}>{caption}</caption>
        <thead>
          <tr className="[&>th]:border-b [&>th]:border-border">
            <th scope="col" className="h-8 px-3 text-left text-xs font-semibold uppercase tracking-wider text-muted">Generation</th>
            <th scope="col" className="h-8 px-3 text-left text-xs font-semibold uppercase tracking-wider text-muted">Version</th>
            <th scope="col" className="h-8 px-3 text-right text-xs font-semibold uppercase tracking-wider text-muted">Hosts</th>
            <th scope="col" className="h-8 px-3 text-right text-xs font-semibold uppercase tracking-wider text-muted">Installs</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="[&>td]:border-b [&>td]:border-border/60 [&>td]:py-1.5 [&>th]:border-b [&>th]:border-border/60 [&>th]:py-1.5">
              <td className="px-3 align-middle"><GenerationPill meta={r.generation} /></td>
              <th scope="row" className="truncate px-3 text-left align-middle font-mono text-xs font-normal" title={r.version}>{r.version}</th>
              <td className="px-3 text-right align-middle">
                <Link to={r.hostsHref} data-drill="assets" data-drill-count={r.hosts} aria-label={r.hostsLabel} className={cn('rounded-sm font-medium tabular-nums hover:text-primary hover:underline', focus)}>{fmtInt(r.hosts)}</Link>
              </td>
              <td className="px-3 text-right align-middle">
                <Link to={r.installsHref} data-drill="page" data-drill-count={r.installs} aria-label={r.installsLabel} className={cn('rounded-sm tabular-nums text-text2 hover:text-primary hover:underline', focus)}>{fmtInt(r.installs)}</Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * The Versions tab: a bar per version, newest first within each scheme group, with its generation (N, N-1, Older)
 * as a word as well as a tint. Each bar is one link: its hosts open the assets that run exactly that version
 * (principle 1). "View as table" is the same numbers as a real table, where the installation counts are links too.
 */
export function ProductVersionsTab({ productKey, data }: { productKey: string; data: Pick<ProductResponse, 'header' | 'versions' | 'unversioned'> }) {
  const [view, setView] = useState<ChartView>('chart')
  const groups = versionGroups(data.versions)
  const rows = versionRows(productKey, data.versions)
  const maxHosts = Math.max(1, ...data.versions.map((v) => v.hosts))
  // The rows follow the server's order, and so do the groups: each group takes the next rows.
  let offset = 0
  const byGroup = groups.map((g) => {
    const part = rows.slice(offset, offset + g.versions.length)
    offset += g.versions.length
    return { group: g, rows: part }
  })
  const showHeadings = groups.length > 1

  return (
    <Card data-tab="versions">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
          <h2 className="text-sm font-semibold tracking-tight">Hosts per version</h2>
          <span className="text-2xs uppercase tracking-wider text-muted">newest first · N is the newest seen on a visible server</span>
        </div>
        {data.versions.length > 0 && <ViewToggle value={view} onChange={setView} label="Versions view" />}
      </div>
      <div className="p-3 sm:p-4">
        {data.versions.length === 0 && data.unversioned.installs === 0 ? (
          <InventoryEmpty kind="no-data" title="No installations in view" compact>
            Nothing reports this product on the servers you can see.
          </InventoryEmpty>
        ) : data.versions.length === 0 ? (
          <>
            <InventoryEmpty kind="no-data" title="No version is reported" compact>
              Every installation of this product reports no version, so there is nothing to rank.
            </InventoryEmpty>
            <ul role="list" className="mt-2"><Unversioned productKey={productKey} unversioned={data.unversioned} maxHosts={1} /></ul>
          </>
        ) : view === 'chart' ? (
          <div className="space-y-1">
            {byGroup.map(({ group, rows: groupRows }) => (
              <div key={group.scheme}>
                <GroupHeading group={group} visible={showHeadings} />
                <ul role="list" aria-label={`${SCHEME_LABELS[group.scheme].title}: hosts per version`}>
                  {groupRows.map((r) => <ChartRow key={r.key} row={r} />)}
                </ul>
              </div>
            ))}
            {data.unversioned.installs > 0 && <ul role="list" className="pt-2"><Unversioned productKey={productKey} unversioned={data.unversioned} maxHosts={maxHosts} /></ul>}
          </div>
        ) : (
          <div className="space-y-2">
            {byGroup.map(({ group, rows: groupRows }) => (
              <VersionTable key={group.scheme} rows={groupRows} caption={`${SCHEME_LABELS[group.scheme].title}: hosts and installations per version`} hideCaption={!showHeadings} />
            ))}
            {data.unversioned.installs > 0 && (
              <p className="px-2 text-xs text-muted">
                <Link to={installedHref(productKey, data.unversioned.address || NO_VERSION)} data-drill="page" data-drill-count={data.unversioned.installs} className={cn('rounded font-medium text-primary hover:underline', focus)}>{plural(data.unversioned.installs, 'installation')}</Link>{' '}
                on {plural(data.unversioned.hosts, 'host')} report no version and are not a version.
              </p>
            )}
          </div>
        )}
        {data.header.mixed_schemes && (
          <p className="mt-3 border-t border-border/60 px-2 pt-3 text-xs text-muted">
            Versions that follow different numbering schemes are ranked apart: one scheme&apos;s version is never compared with another&apos;s, so each group has its own N.
          </p>
        )}
      </div>
    </Card>
  )
}
