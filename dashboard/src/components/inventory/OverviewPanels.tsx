import { useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { UseQueryResult } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowRight, Boxes, Bot, CheckCircle2, Cpu, Fingerprint, History, Info, Network, Package, Radar, Server, ShieldCheck, Sparkles,
} from 'lucide-react'
import { serverNow } from '@/lib/serverClock'
import { cn } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { CATEGORICAL } from '@/pages/dashboards/shared'
import { assetsHref } from '@/pages/inventory/filters'
import type { ChangesResponse, HardwareResponse, InventorySummary, OsResponse } from '@/pages/inventory/types'
import { changeAssetAddress, changeText, CATEGORY_LABELS } from './changeModel'
import { historyText } from './changesModel'
import { Donut } from './Donut'
import { DrillTarget } from './DrillTarget'
import { InventoryEmpty } from './InventoryEmpty'
import { ageWords, fmtInt, fmtPct, fmtShare, plural, toMillis } from './model'
import {
  PATHS, agentCoverage, assetsDrill, changesDrill, changesHref, healthAllClear, healthItems, memoryRows, networkOsModel, pageDrill,
  serverOsModel, softwareRows, sprawlNote, topModels, unknownServersText, UNKNOWN_SERVERS_HINT, type AgentCoverage, type SoftwareRow,
} from './overviewModel'
import { categoryLabel } from './softwareModel'
import { Panel, QueryPanel } from './PanelCard'
import { VersionDistributionBar } from './VersionDistributionBar'
import { ViewToggle, type ChartView } from './ViewToggle'

type Q<T> = UseQueryResult<T, Error>

const rowLink = 'hover:no-underline'
const th = 'px-2 py-1.5 text-left text-[10.5px] font-semibold uppercase tracking-wider text-muted'
const thRight = cn(th, 'text-right')

/** A hatched swatch for "unknown", a coloured one for a family, a neutral one for the rest. */
function Swatch({ color, unknown, other }: { color?: number; unknown?: boolean; other?: boolean }) {
  if (unknown) {
    return <span aria-hidden className="h-2.5 w-2.5 shrink-0 rounded-sm border border-border-strong" style={{ background: 'repeating-linear-gradient(45deg, rgb(var(--muted) / 0.7) 0 2px, rgb(var(--surface2)) 2px 5px)' }} />
  }
  return <span aria-hidden className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: color === undefined ? 'rgb(var(--muted))' : CATEGORICAL[color % CATEGORICAL.length] }} />
}

/** A thin proportional bar (decorative: the number beside it is the information). */
function MiniBar({ pct, color, unknown }: { pct: number; color?: string; unknown?: boolean }) {
  return (
    <span aria-hidden className="block h-1.5 w-full overflow-hidden rounded-full bg-surface2">
      <span
        className="block h-full rounded-full"
        style={{
          width: `${Math.max(pct > 0 ? 2 : 0, Math.min(100, pct))}%`,
          background: unknown ? 'repeating-linear-gradient(45deg, rgb(var(--muted) / 0.7) 0 2px, rgb(var(--surface2)) 2px 5px)' : color ?? 'rgb(var(--primary) / 0.75)',
        }}
      />
    </span>
  )
}

function FooterLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link to={to} className="inline-flex items-center gap-1 rounded text-xs font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">
      {children} <ArrowRight className="h-3 w-3" aria-hidden />
    </Link>
  )
}

/* ── Servers: operating systems ──────────────────────────────────────────── */

export function ServerOsPanel({ query }: { query: Q<OsResponse> }) {
  const [view, setView] = useState<ChartView>('chart')
  return (
    <QueryPanel
      title="Operating systems: servers"
      hint="ring by family, rows by release"
      icon={<Server className="h-4 w-4" />}
      right={<ViewToggle value={view} onChange={setView} label="Operating systems view" />}
      query={query}
      what="the server operating systems"
      rows={6}
    >
      {(os) => <ServerOsBody os={os} view={view} />}
    </QueryPanel>
  )
}

function ServerOsBody({ os, view }: { os: OsResponse; view: ChartView }) {
  const m = useMemo(() => serverOsModel(os), [os])
  if (m.total === 0) {
    return <InventoryEmpty kind="no-data" compact title="No servers yet">Servers appear here once their agent reports an operating system.</InventoryEmpty>
  }
  const summary = `${plural(m.total, 'server')} by operating system family: ${m.slices.map((s) => `${s.label} ${fmtInt(s.count)} (${fmtPct(s.pct)})`).join(', ')}`
  if (view === 'table') {
    return (
      <table className="w-full text-xs">
        <caption className="sr-only">{summary}</caption>
        <thead>
          <tr><th scope="col" className={th}>Operating system</th><th scope="col" className={thRight}>Servers</th><th scope="col" className={thRight}>Share</th></tr>
        </thead>
        <tbody>
          {m.releases.map((r) => (
            <tr key={r.key} className="border-t border-border/60">
              <th scope="row" className={cn('px-2 py-1.5 text-left font-normal', r.unknown && 'italic text-muted')}>{r.label}</th>
              <td className="px-2 py-1.5 text-right"><DrillTarget drill={r.drill} className="font-medium">{fmtInt(r.count)}</DrillTarget></td>
              <td className="px-2 py-1.5 text-right tabular-nums text-muted">{fmtPct(r.pct)}</td>
            </tr>
          ))}
          {m.more && (
            <tr className="border-t border-border/60">
              <th scope="row" className="px-2 py-1.5 text-left font-normal text-muted">{fmtInt(m.more.releases)} more releases</th>
              <td className="px-2 py-1.5 text-right"><Link to={m.more.href} className="tabular-nums hover:underline">{fmtInt(m.more.assets)}</Link></td>
              <td className="px-2 py-1.5 text-right tabular-nums text-muted">{fmtPct((m.more.assets / m.total) * 100)}</td>
            </tr>
          )}
        </tbody>
        <tfoot>
          <tr className="border-t border-border">
            <th scope="row" className="px-2 py-1.5 text-left font-semibold">All servers</th>
            <td className="px-2 py-1.5 text-right"><DrillTarget drill={assetsDrill({ kind: 'server' }, m.total)} className="font-semibold">{fmtInt(m.total)}</DrillTarget></td>
            <td className="px-2 py-1.5" />
          </tr>
        </tfoot>
      </table>
    )
  }
  return (
    <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-start">
      <Donut
        slices={m.slices}
        label={summary}
        centre={<><span className="text-xl font-bold tabular-nums leading-none text-text">{fmtInt(m.total)}</span><span className="mt-0.5 text-[10px] uppercase tracking-wider text-muted">servers</span></>}
      />
      <ul role="list" aria-label="Operating system releases" className="w-full min-w-0 flex-1 space-y-0.5">
        {m.releases.map((r) => (
          <li key={r.key}>
            <DrillTarget drill={r.drill} className={cn('flex items-center gap-2 rounded-md px-1.5 py-1 text-[13px] hover:bg-surface2', rowLink)} ariaLabel={`${r.label}: ${plural(r.count, 'server')}, ${fmtPct(r.pct)}`}>
              <Swatch color={r.color} unknown={r.unknown} other={r.color === undefined && !r.unknown} />
              <span className={cn('min-w-0 flex-1 truncate', r.unknown && 'italic text-muted')}>{r.label}</span>
              <span className="hidden w-16 sm:block"><MiniBar pct={r.pct} unknown={r.unknown} color={r.color === undefined ? undefined : CATEGORICAL[r.color % CATEGORICAL.length]} /></span>
              <span className="w-9 text-right font-medium tabular-nums">{fmtInt(r.count)}</span>
              <span className="w-11 text-right text-[11px] tabular-nums text-muted">{fmtPct(r.pct)}</span>
            </DrillTarget>
          </li>
        ))}
        {m.more && (
          <li>
            <Link to={m.more.href} className="flex items-center gap-2 rounded-md px-1.5 py-1 text-xs text-muted outline-none hover:bg-surface2 hover:text-text focus-visible:ring-2 focus-visible:ring-primary">
              <span className="h-2.5 w-2.5 shrink-0" aria-hidden />
              <span className="flex-1">{plural(m.more.releases, 'more release')} on {plural(m.more.assets, 'server')}</span>
              <ArrowRight className="h-3 w-3" aria-hidden />
            </Link>
          </li>
        )}
      </ul>
    </div>
  )
}

/* ── Network devices: OS and firmware ────────────────────────────────────── */

export function NetworkOsPanel({ query }: { query: Q<OsResponse> }) {
  const [view, setView] = useState<ChartView>('chart')
  return (
    <QueryPanel
      title="Network OS and firmware"
      icon={<Network className="h-4 w-4" />}
      right={<ViewToggle value={view} onChange={setView} label="Network OS view" />}
      query={query}
      what="the network OS and firmware"
      rows={6}
    >
      {(os) => <NetworkOsBody os={os} view={view} />}
    </QueryPanel>
  )
}

function NetworkOsBody({ os, view }: { os: OsResponse; view: ChartView }) {
  const m = useMemo(() => networkOsModel(os), [os])
  if (m.total === 0) {
    return <InventoryEmpty kind="no-data" compact title="No network devices yet">Devices appear here once the poller has read their firmware.</InventoryEmpty>
  }
  if (view === 'table') {
    return (
      <div className="max-h-[22rem] overflow-auto">
        <table className="w-full text-xs">
          <caption className="sr-only">{fmtInt(m.total)} network devices by operating system and firmware version</caption>
          <thead>
            <tr><th scope="col" className={th}>OS</th><th scope="col" className={th}>Version</th><th scope="col" className={thRight}>Devices</th></tr>
          </thead>
          <tbody>
            {m.rows.flatMap((r) => (r.entries.length === 0
              ? [(
                <tr key={r.key} className="border-t border-border/60">
                  <th scope="row" className="px-2 py-1.5 text-left font-normal italic text-muted">{r.label}</th>
                  <td className="px-2 py-1.5 text-muted">not identified</td>
                  <td className="px-2 py-1.5 text-right"><DrillTarget drill={r.drill} className="font-medium">{fmtInt(r.count)}</DrillTarget></td>
                </tr>
              )]
              : r.entries.map((e, i) => (
                <tr key={`${r.key}:${e.key}`} className="border-t border-border/60">
                  <th scope="row" className="px-2 py-1.5 text-left font-normal">{i === 0 ? r.label : <span className="sr-only">{r.label}</span>}</th>
                  <td className={cn('px-2 py-1.5 font-mono', e.unknown && 'font-sans italic text-muted')}>{e.label}</td>
                  <td className="px-2 py-1.5 text-right">
                    {e.href ? <Link to={e.href} data-drill-count={e.count} className="font-medium tabular-nums hover:underline">{fmtInt(e.count)}</Link> : fmtInt(e.count)}
                  </td>
                </tr>
              ))))}
          </tbody>
        </table>
      </div>
    )
  }
  return (
    <div className="space-y-3.5">
      <ul role="list" className="space-y-3.5">
        {m.rows.map((r) => (
          <li key={r.key}>
            <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
              <DrillTarget drill={r.drill} className={cn('text-[13px] font-semibold text-text', r.unknown && 'italic text-muted')}>
                {r.label} <span className="font-normal tabular-nums text-text2">{fmtInt(r.count)} {r.count === 1 ? 'device' : 'devices'}</span>
              </DrillTarget>
              {r.unknown ? (
                <span className="text-[11px] text-muted">firmware not identified</span>
              ) : (
                <Link to={`${PATHS.os}?kind=device`} className="text-[11px] tabular-nums text-muted hover:text-primary hover:underline">
                  {fmtInt(r.versions)} {r.versions === 1 ? 'version' : 'versions'}
                </Link>
              )}
            </div>
            {r.unknown ? (
              <MiniBar pct={(r.count / m.total) * 100} unknown />
            ) : (
              <VersionDistributionBar entries={r.entries} otherHref={r.drill.href} noun="device" maxSegments={5} showLegend subject={r.label} />
            )}
          </li>
        ))}
      </ul>
      {m.more && (
        <FooterLink to={m.more.href}>{plural(m.more.products, 'more product')} on {plural(m.more.assets, 'device')}</FooterLink>
      )}
    </div>
  )
}

/* ── Software ────────────────────────────────────────────────────────────── */

function SoftwareTable({ rows, by, caption }: { rows: SoftwareRow[]; by: 'hosts' | 'versions'; caption: string }) {
  if (rows.length === 0) return <InventoryEmpty kind="no-data" compact title="No software reported yet">Titles appear once an agent has uploaded its software list.</InventoryEmpty>
  return (
    // Fixed layout: the product name takes what the two number columns leave and truncates, so the table never outgrows a phone.
    <table className="w-full table-fixed text-[13px]">
      <caption className="sr-only">{caption}</caption>
      <colgroup><col /><col className="w-20" /><col className="w-20" /></colgroup>
      <thead>
        <tr>
          <th scope="col" className={th}>Product</th>
          <th scope="col" className={thRight}>{by === 'hosts' ? 'Hosts' : 'Versions'}</th>
          <th scope="col" className={thRight}>{by === 'hosts' ? 'Versions' : 'Hosts'}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const first = by === 'hosts' ? r.hostsDrill : r.versionsDrill
          const second = by === 'hosts' ? r.versionsDrill : r.hostsDrill
          return (
            <tr key={r.key} className="border-t border-border/60 align-top">
              <th scope="row" className="min-w-0 overflow-hidden px-2 py-1.5 text-left font-normal">
                <span className="flex items-baseline gap-2">
                  <Link to={r.versionsDrill.href} className="min-w-0 truncate rounded-sm font-medium text-text outline-none hover:text-primary hover:underline focus-visible:ring-2 focus-visible:ring-primary" title={r.name}>{r.name}</Link>
                  {r.category && <span className="shrink-0 text-[10.5px] text-muted" data-software-category="">{categoryLabel(r.category)}</span>}
                </span>
                <span className="mt-1 block w-full max-w-[12rem]"><MiniBar pct={r.barPct} /></span>
              </th>
              <td className="px-2 py-1.5 text-right font-medium"><DrillTarget drill={first} ariaLabel={`${r.name}: ${by === 'hosts' ? plural(r.hosts, 'host') : plural(r.versions, 'version')}`}>{fmtInt(by === 'hosts' ? r.hosts : r.versions)}</DrillTarget></td>
              <td className="px-2 py-1.5 text-right text-text2"><DrillTarget drill={second} ariaLabel={`${r.name}: ${by === 'hosts' ? plural(r.versions, 'version') : plural(r.hosts, 'host')}`}>{fmtInt(by === 'hosts' ? r.versions : r.hosts)}</DrillTarget></td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function hiddenNote(summary: InventorySummary): string | null {
  const hidden = summary.software.hidden_categories
  return hidden.length > 0 ? `Not counted: ${hidden.map(categoryLabel).join(', ')} entries (a setting).` : null
}

export function TopSoftwarePanel({ query }: { query: Q<InventorySummary> }) {
  return (
    <QueryPanel title="Top software" icon={<Package className="h-4 w-4" />} hint="by hosts" query={query} what="the software summary" rows={6} quietStale>
      {(s) => {
        const rows = softwareRows(s.software.top, 'hosts')
        const agent = agentCoverage(s)
        return (
          <div className="space-y-2">
            <SoftwareTable rows={rows} by="hosts" caption="Most widely installed software, by number of servers" />
            {agent && <AgentCoverageLine coverage={agent} />}
            <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
              <span className="text-[11px] text-muted">{hiddenNote(s)}</span>
              <FooterLink to={PATHS.software}>All {plural(s.software.products, 'title')}</FooterLink>
            </div>
          </div>
        )
      }}
    </QueryPanel>
  )
}

export function SprawlPanel({ query }: { query: Q<InventorySummary> }) {
  return (
    <QueryPanel title="Version sprawl" icon={<Boxes className="h-4 w-4" />} hint="most distinct versions" query={query} what="the software summary" rows={6} quietStale>
      {(s) => (
        <div className="space-y-2">
          <SoftwareTable rows={softwareRows(s.software.sprawl, 'versions')} by="versions" caption="Products installed in the most distinct versions: a sign of upgrade drift" />
          <p className="pt-1 text-[11px] text-muted" data-sprawl-note="">{sprawlNote(s.software.sprawl_basis)}</p>
        </div>
      )}
    </QueryPanel>
  )
}

/**
 * "Endpoint protection on 39 of 52 servers with a complete software list (75.0%) · 13 without", and, as its own line,
 * "3 servers unknown: no complete software or service list to judge". The three figures sum to every server and each
 * opens the list it counts; a server whose list is truncated or not yet normalised is unknown, never "without".
 */
export function AgentCoverageLine({ coverage, compact }: { coverage: AgentCoverage; compact?: boolean }) {
  const { covered, missing, answerable, unknown } = coverage
  const without = missing > 0 ? (
    <DrillTarget drill={coverage.gapDrill} className="font-semibold text-warning-fg">{fmtInt(missing)} without</DrillTarget>
  ) : (
    <span className="font-medium text-success-fg">every one covered</span>
  )
  return (
    <div className={cn('space-y-0.5', !compact && 'rounded-md border border-border bg-surface2/50 px-2.5 py-1.5')} data-agent-coverage="">
      {answerable > 0 ? (
        <p className={cn('text-xs text-text2', compact ? 'text-[11px] leading-4' : 'flex flex-wrap items-center gap-x-1.5 gap-y-0.5')}>
          {!compact && <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />}
          <span>
            Endpoint protection on <DrillTarget drill={coverage.coveredDrill} className="font-semibold text-text" ariaLabel={`Servers with endpoint protection: ${fmtInt(covered)}`}>{fmtInt(covered)}</DrillTarget> of {plural(answerable, 'server')} with a complete software list
            {!compact && <span className="text-muted"> ({fmtShare(covered, answerable)})</span>}
          </span>
          <span aria-hidden className="text-muted/60">·</span>
          {without}
        </p>
      ) : (
        <p className="text-[11px] leading-4 text-text2" data-agent-unassessed="">Endpoint protection cannot be assessed: no server has a complete software list.</p>
      )}
      {unknown > 0 && (
        <p className="text-[11px] leading-4 text-text2" data-agent-unknown="">
          <DrillTarget drill={coverage.unknownDrill} className="font-medium text-text2 hover:text-primary" title={UNKNOWN_SERVERS_HINT}>{unknownServersText(unknown)}</DrillTarget>
        </p>
      )}
    </div>
  )
}

/* ── Hardware ────────────────────────────────────────────────────────────── */

const MAX_MODELS = 6

export function HardwarePanel({ summary, hardware }: { summary: Q<InventorySummary>; hardware: Q<HardwareResponse> }) {
  return (
    <QueryPanel title="Hardware" icon={<Cpu className="h-4 w-4" />} hint="network models, server memory" query={summary} what="the hardware summary" rows={6} quietStale>
      {(s) => <HardwareBody summary={s} hardware={hardware} />}
    </QueryPanel>
  )
}

function HardwareBody({ summary, hardware }: { summary: InventorySummary; hardware: Q<HardwareResponse> }) {
  const models = topModels(summary).slice(0, MAX_MODELS)
  return (
    <div className="space-y-4">
      <div>
        <h3 className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-muted">Most common network models</h3>
        {models.length === 0 ? (
          <InventoryEmpty kind="no-data" compact title="No models identified yet">Models appear once the poller has read the hardware of a device.</InventoryEmpty>
        ) : (
          <ul role="list" className="space-y-0.5">
            {models.map((r) => (
              <li key={r.key}>
                <DrillTarget drill={r.drill} className={cn('flex items-center gap-2 rounded-md px-1.5 py-1 text-[13px] hover:bg-surface2', rowLink)} ariaLabel={`${r.vendor} ${r.model}: ${plural(r.count, 'device')}`}>
                  <span className="min-w-0 flex-1 truncate"><span className="text-muted">{r.vendor}</span> <span className="font-medium">{r.model}</span></span>
                  <span className="hidden w-20 sm:block"><MiniBar pct={r.barPct} /></span>
                  <span className="w-8 text-right font-medium tabular-nums">{fmtInt(r.count)}</span>
                </DrillTarget>
              </li>
            ))}
          </ul>
        )}
      </div>
      <ServerMemory query={hardware} />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[11px] text-muted">
          <DrillTarget drill={assetsDrill({ kind: 'device' }, summary.hardware.devices)} className="font-medium text-text2">{fmtInt(summary.hardware.devices)} network devices</DrillTarget>
          {' · '}<DrillTarget drill={pageDrill(PATHS.hardware, summary.hardware.models)} className="text-muted hover:text-primary">{plural(summary.hardware.models, 'model')} from {plural(summary.hardware.vendors, 'vendor')}</DrillTarget>
        </span>
        <FooterLink to={PATHS.hardware}>Hardware</FooterLink>
      </div>
    </div>
  )
}

function ServerMemory({ query }: { query: Q<HardwareResponse> }) {
  const rows = query.data ? memoryRows(query.data) : []
  return (
    <div>
      <h3 className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-wider text-muted">Servers by installed memory</h3>
      {query.isLoading ? (
        <div className="h-16 animate-shimmer rounded-md" aria-busy="true" aria-label="Loading server memory" />
      ) : !query.data ? (
        <p className="flex items-center gap-2 text-xs text-muted">
          <AlertTriangle className="h-3.5 w-3.5 text-warning-fg" aria-hidden /> Server memory could not be loaded.
          <button type="button" onClick={() => void query.refetch()} className="rounded font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">Retry</button>
        </p>
      ) : query.data.servers.total === 0 ? (
        <p className="text-xs text-muted">No servers have reported their hardware yet.</p>
      ) : (
        <ul role="list" className="grid grid-cols-1 gap-x-4 gap-y-0.5 sm:grid-cols-2">
          {rows.map((r) => (
            <li key={r.value}>
              <DrillTarget drill={r.drill} className={cn('flex items-center gap-2 rounded-md px-1.5 py-0.5 text-xs hover:bg-surface2', rowLink)} ariaLabel={`${r.label}: ${plural(r.count, 'server')}`}>
                <Swatch unknown={r.unknown} color={r.unknown ? undefined : 0} />
                <span className={cn('min-w-0 flex-1 truncate', r.unknown && 'italic text-muted')}>{r.label}</span>
                <span className="font-medium tabular-nums">{fmtInt(r.count)}</span>
              </DrillTarget>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/* ── Recent changes ──────────────────────────────────────────────────────── */

const RECENT_COUNT = 8

export function RecentChangesPanel({ changes, summary }: { changes: Q<ChangesResponse>; summary: Q<InventorySummary> }) {
  const total = summary.data?.changes_7d
  const href = total ? changesHref({ since: total.since }) : PATHS.changes
  return (
    <QueryPanel
      title="Recent changes"
      icon={<History className="h-4 w-4" />}
      hint="last 7 days"
      right={total && <DrillTarget drill={changesDrill({ since: total.since }, total.total)} className="text-xs text-text2 hover:text-primary"><strong className="font-semibold text-text">{fmtInt(total.total)}</strong> in 7 days</DrillTarget>}
      query={changes}
      what="the recent changes"
      rows={6}
    >
      {(c) => <RecentChangesBody data={c} href={href} />}
    </QueryPanel>
  )
}

function RecentChangesBody({ data, href }: { data: ChangesResponse; href: string }) {
  const now = serverNow()
  const history = historyText(data, now, data.window.tz)
  const rows = data.items.slice(0, RECENT_COUNT)
  if (rows.length === 0) {
    return (
      <div className="space-y-2">
        <InventoryEmpty kind="no-data" compact title="No changes in the last 7 days">Installs, removals, upgrades and firmware changes appear here as they are reported.</InventoryEmpty>
        <p className="text-center text-[11px] text-muted">{history.text}</p>
      </div>
    )
  }
  return (
    <div className="space-y-2">
      <ol className="divide-y divide-border/60">
        {rows.map((r) => {
          const t = changeText(r)
          const address = changeAssetAddress(r)
          return (
            <li key={r.id} className="flex items-start gap-2.5 py-1.5" data-change={r.id}>
              <time dateTime={r.observed_at} title={new Date(toMillis(r.observed_at) ?? 0).toLocaleString()} className="w-14 shrink-0 pt-px text-[11px] tabular-nums text-muted">
                {ageWords(r.observed_at, now)}
              </time>
              <div className="min-w-0 flex-1 text-[13px] leading-5">
                {t.marker ? (
                  <span className="italic text-muted">{t.sentence}</span>
                ) : (
                  <>
                    {address ? (
                      <Link to={assetsHref({}, { asset: address })} className="rounded-sm font-medium text-text outline-none hover:text-primary hover:underline focus-visible:ring-2 focus-visible:ring-primary">{r.asset_name ?? address}</Link>
                    ) : (
                      <span className="font-medium text-text" title="This asset has been deleted; its history is kept">{r.asset_name ?? 'Deleted asset'}</span>
                    )}{' '}
                    <span className="text-text2"><span className="font-medium">{t.subject}</span> {t.verb}{t.detail && <> <span className="break-words font-mono text-xs">{t.detail}</span></>}</span>
                  </>
                )}
              </div>
              {!t.marker && (
                <span className="shrink-0 rounded border border-border bg-surface2 px-1.5 py-px text-[10.5px] font-medium text-text2">{CATEGORY_LABELS[r.category]}</span>
              )}
            </li>
          )
        })}
      </ol>
      <FooterLink to={href}>View all changes</FooterLink>
    </div>
  )
}

/* ── Inventory health ────────────────────────────────────────────────────── */

export function HealthPanel({ query }: { query: Q<InventorySummary> }) {
  return (
    <QueryPanel title="Inventory health" icon={<Fingerprint className="h-4 w-4" />} hint="gaps to close" query={query} what="the inventory health" rows={3} quietStale>
      {(s) => <HealthBody summary={s} />}
    </QueryPanel>
  )
}

function HealthBody({ summary }: { summary: InventorySummary }) {
  const items = healthItems(summary)
  const byDesign = summary.health.not_collected_by_design?.count ?? 0
  if (healthAllClear(summary)) {
    return (
      <div className="space-y-1.5" data-health="clear">
        <p className="flex items-center gap-2 text-sm font-medium text-success-fg">
          <CheckCircle2 className="h-5 w-5 shrink-0" aria-hidden />
          Inventory is complete: every asset that can report did so within its targets and nothing is missing.
        </p>
        {byDesign > 0 && (
          <p className="pl-7 text-xs text-text2">
            <DrillTarget drill={assetsDrill({ freshness: 'not_applicable' }, byDesign)} className="hover:text-primary">
              <strong className="font-semibold text-text">{fmtInt(byDesign)}</strong> {byDesign === 1 ? 'asset is' : 'assets are'} not reported by design and not counted above.
            </DrillTarget>
          </p>
        )}
      </div>
    )
  }
  return (
    <ul role="list" className="grid gap-x-6 gap-y-2 sm:grid-cols-2 xl:grid-cols-3" data-health="gaps">
      {items.map((i) => (
        <li key={i.key} className="flex items-start gap-2 text-[13px]" data-health-item={i.key}>
          {i.info ? (
            <Info className="mt-0.5 h-4 w-4 shrink-0 text-muted" aria-hidden />
          ) : i.count > 0 ? (
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning-fg" aria-hidden />
          ) : (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success-fg" aria-hidden />
          )}
          <span className="min-w-0">
            {i.count > 0 ? (
              <DrillTarget drill={i.drill} className="text-text2 hover:text-primary">
                <strong className="font-semibold text-text">{fmtInt(i.count)}</strong> {i.text}
              </DrillTarget>
            ) : (
              <span className="text-muted">{i.none}</span>
            )}
            {i.count > 0 && i.hint && <span className="mt-0.5 block text-[11px] leading-4 text-muted">{i.hint}</span>}
          </span>
        </li>
      ))}
    </ul>
  )
}

/* ── First run ───────────────────────────────────────────────────────────── */

export function OnboardingCards() {
  const can = useCan()
  const step = (icon: ReactNode, title: string, body: string, to: string | null, action: string, reason: string) => (
    <li className="flex flex-col gap-2 rounded-lg border border-border bg-surface p-4 shadow-card dark:shadow-card-dark">
      <span aria-hidden className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">{icon}</span>
      <h3 className="text-sm font-semibold">{title}</h3>
      <p className="flex-1 text-xs text-text2">{body}</p>
      {to ? (
        <FooterLink to={to}>{action}</FooterLink>
      ) : (
        <span className="text-xs text-muted" title={reason}>{reason}</span>
      )}
    </li>
  )
  return (
    <section aria-labelledby="inventory-onboarding" className="space-y-3">
      <div>
        <h2 id="inventory-onboarding" className="text-base font-semibold">Nothing in the inventory yet</h2>
        <p className="mt-0.5 max-w-2xl text-xs text-text2">
          Inventory is built from what ZenPlus already collects. Connect a source below and this page fills in by itself; there is nothing to configure here.
        </p>
      </div>
      <ul role="list" className="grid gap-3 md:grid-cols-3">
        {step(<Bot className="h-5 w-5" />, 'Install the Windows agent', 'Servers report their operating system, installed software and hardware through the agent.', can('servers.view') ? '/server-agents' : null, 'Open Agent Fleet', 'Needs the servers.view permission')}
        {step(<Radar className="h-5 w-5" />, 'Add network devices', 'Discovery finds switches, firewalls and load balancers and reads their model and firmware over SNMP.', can('discovery.view') ? '/discovery' : null, 'Open Discovery', 'Needs the discovery.view permission')}
        {step(<Sparkles className="h-5 w-5" />, 'Inventory fills in automatically', 'Once a source reports, OS releases, software titles, hardware models and every change appear here within minutes.', null, '', 'No action needed')}
      </ul>
    </section>
  )
}

/** The note that stands where trend lines would be for a user whose scope hides fleet-wide history. */
export function TrendsNote({ children }: { children: ReactNode }) {
  return <p className="text-[11px] text-muted" data-trends-note="">{children}</p>
}
