import { useMemo, useState } from 'react'
import { Gauge, PieChart } from 'lucide-react'
import { cn } from '@/lib/utils'
import { CATEGORICAL } from '@/pages/dashboards/shared'
import type { OsResponse } from '@/pages/inventory/types'
import { Donut } from './Donut'
import { DrillTarget } from './DrillTarget'
import { InventoryEmpty } from './InventoryEmpty'
import { fmtInt, fmtPct } from './model'
import {
  CURRENCY_NOTE, currencyRows, currencySentence, donutSummary, familyDonut, nounFor, type CurrencyRow, type OsKindChoice,
} from './osTreeModel'
import { Panel } from './PanelCard'
import { ViewToggle, type ChartView } from './ViewToggle'

const th = 'px-2 py-1.5 text-left text-xs font-semibold uppercase tracking-wider text-muted'
const thRight = cn(th, 'text-right')

/** A hatched swatch for "unknown", a coloured one for the five biggest families, a neutral one for the rest. */
function Swatch({ color, unknown }: { color?: number; unknown?: boolean }) {
  if (unknown) {
    return <span aria-hidden className="h-2.5 w-2.5 shrink-0 rounded-sm border border-border-strong" style={{ background: 'repeating-linear-gradient(45deg, rgb(var(--muted) / 0.7) 0 2px, rgb(var(--surface2)) 2px 5px)' }} />
  }
  return <span aria-hidden className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: color === undefined ? 'rgb(var(--muted))' : CATEGORICAL[color % CATEGORICAL.length] }} />
}

/* ── Family donut ────────────────────────────────────────────────────────── */

/**
 * Assets by OS family: a donut whose slices are links, the legend that lists EVERY family (the donut folds the
 * small ones into one neutral slice, the legend never does), and "View as table" for the same numbers.
 */
export function OsFamilyPanel({ os, kind }: { os: OsResponse; kind: OsKindChoice }) {
  const [view, setView] = useState<ChartView>('chart')
  const m = useMemo(() => familyDonut(os, kind), [os, kind])
  return (
    <Panel
      title="By operating system"
      icon={<PieChart className="h-4 w-4" />}
      hint={m.noun}
      right={m.total > 0 ? <ViewToggle value={view} onChange={setView} label="Operating system families view" /> : undefined}
    >
      {m.total === 0 ? (
        <InventoryEmpty kind="no-data" compact title={`No ${m.noun} yet`}>Nothing has reported an operating system, so there is nothing to chart.</InventoryEmpty>
      ) : view === 'table' ? (
        <table className="w-full text-xs">
          <caption className="sr-only">{donutSummary(m)}</caption>
          <thead>
            <tr><th scope="col" className={th}>Family</th><th scope="col" className={thRight}>Assets</th><th scope="col" className={thRight}>Share</th></tr>
          </thead>
          <tbody>
            {m.legend.map((l) => (
              <tr key={l.key} className="border-t border-border/60">
                <th scope="row" className={cn('px-2 py-1.5 text-left font-normal', l.unknown && 'italic text-muted')}>{l.label}</th>
                <td className="px-2 py-1.5 text-right"><DrillTarget drill={l.drill} className="font-medium">{fmtInt(l.count)}</DrillTarget></td>
                <td className="px-2 py-1.5 text-right tabular-nums text-muted">{fmtPct(l.pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-start lg:flex-col lg:items-center xl:flex-row xl:items-start">
          <Donut
            slices={m.slices}
            label={donutSummary(m)}
            centre={<><span className="text-xl font-bold tabular-nums leading-none text-text">{fmtInt(m.total)}</span><span className="mt-0.5 max-w-[5.5rem] text-2xs uppercase leading-tight tracking-wider text-muted">{m.noun}</span></>}
          />
          <ul role="list" aria-label="Operating system families" className="w-full min-w-0 flex-1 space-y-0.5">
            {m.legend.flatMap((l, i) => {
              const row = (
                <li key={l.key}>
                  <DrillTarget
                    drill={l.drill}
                    className={cn('flex items-center gap-2 rounded-md py-1 pr-1.5 text-sm hover:bg-surface2 hover:no-underline', l.tail ? 'pl-5' : 'pl-1.5')}
                    ariaLabel={`${l.label}${l.tail ? ' (in the grey Other slice)' : ''}: ${fmtInt(l.count)} ${nounFor(l.count, m.noun)}, ${fmtPct(l.pct)}`}
                  >
                    <Swatch color={l.color} unknown={l.unknown} />
                    <span className={cn('min-w-0 flex-1 break-words leading-tight', l.unknown && 'italic text-muted', l.tail && 'text-text2')}>{l.label}</span>
                    <span className="w-9 text-right font-medium tabular-nums">{fmtInt(l.count)}</span>
                    <span className="w-12 text-right text-xs tabular-nums text-muted">{fmtPct(l.pct)}</span>
                  </DrillTarget>
                </li>
              )
              // The grey slice has no single filter: its heading row names it, and the families it folds follow, indented.
              const first = l.tail && !m.legend[i - 1]?.tail
              if (!first || !m.other) return [row]
              return [
                <li key="__other__" data-legend-other="">
                  <DrillTarget
                    drill={m.other.drill}
                    className="flex items-center gap-2 rounded-md px-1.5 py-1 text-sm hover:bg-surface2 hover:no-underline"
                    ariaLabel={`${m.other.label}, the grey slice: ${fmtInt(m.other.count)} ${nounFor(m.other.count, m.noun)}, ${fmtPct(m.other.pct)}. Listed below.`}
                  >
                    <Swatch />
                    <span className="min-w-0 flex-1 break-words font-medium leading-tight">{m.other.label}</span>
                    <span className="w-9 text-right font-medium tabular-nums">{fmtInt(m.other.count)}</span>
                    <span className="w-12 text-right text-xs tabular-nums text-muted">{fmtPct(m.other.pct)}</span>
                  </DrillTarget>
                </li>,
                row,
              ]
            })}
          </ul>
        </div>
      )}
    </Panel>
  )
}

/* ── Build currency ──────────────────────────────────────────────────────── */

const SHOWN = 6

/** The stacked bar of one release: latest, N-1, older, no usable version. Decorative; the sentence beside it is the information. */
function CurrencyBar({ r }: { r: CurrencyRow }) {
  const seg = (pct: number, style: React.CSSProperties, key: string) =>
    pct > 0 ? <span key={key} className="block h-full" style={{ width: `${pct}%`, ...style }} /> : null
  return (
    <span aria-hidden className="flex h-2 w-full overflow-hidden rounded-full bg-surface2">
      {seg(r.pct.latest, { background: 'rgb(var(--primary) / 0.9)' }, 'latest')}
      {seg(r.pct.previous, { background: 'rgb(var(--primary) / 0.5)' }, 'previous')}
      {seg(r.pct.older, { background: 'rgb(var(--muted) / 0.6)' }, 'older')}
      {seg(r.pct.unknown, { background: 'repeating-linear-gradient(45deg, rgb(var(--muted) / 0.7) 0 2px, rgb(var(--surface2)) 2px 5px)' }, 'unknown')}
    </span>
  )
}

function CurrencyItem({ r }: { r: CurrencyRow }) {
  return (
    <li className="space-y-1.5" data-currency={r.key} title={currencySentence(r)}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="min-w-0 truncate text-sm font-medium" title={r.label}>{r.label}</span>
        <span className="shrink-0 text-xs tabular-nums text-muted">{fmtInt(r.count)} {r.count === 1 ? 'asset' : 'assets'}</span>
      </div>
      {r.currency && r.latest ? (
        <>
          <CurrencyBar r={r} />
          {/* Each cell is wide enough for a four-part Windows build (10.0.20348.2700): the one thing this card compares. */}
          <dl className="grid grid-cols-[repeat(auto-fit,minmax(11rem,1fr))] gap-x-3 gap-y-1.5 text-xs">
            <div className="min-w-0">
              <dt className="text-xs uppercase tracking-wider text-muted">Latest observed</dt>
              <dd className="flex items-baseline gap-1.5">
                <DrillTarget drill={r.latest.drill} className="text-sm font-semibold text-text" ariaLabel={`${r.label}, latest observed ${r.latest.version}: ${fmtInt(r.latest.count)} ${nounFor(r.latest.count, 'assets')}`}>{fmtInt(r.latest.count)}</DrillTarget>
                <code className="break-all font-mono text-xs leading-4 text-text2" title={r.latest.version}>{r.latest.version}</code>
              </dd>
            </div>
            {r.previous && (
              <div className="min-w-0">
                <dt className="text-xs uppercase tracking-wider text-muted" title="One version behind the latest observed">N-1</dt>
                <dd className="flex items-baseline gap-1.5">
                  <DrillTarget drill={r.previous.drill} className="text-sm font-semibold text-text" ariaLabel={`${r.label}, N-1 ${r.previous.version}: ${fmtInt(r.previous.count)} ${nounFor(r.previous.count, 'assets')}`}>{fmtInt(r.previous.count)}</DrillTarget>
                  <code className="break-all font-mono text-xs leading-4 text-text2" title={r.previous.version}>{r.previous.version}</code>
                </dd>
              </div>
            )}
            {r.older > 0 && (
              <div className="min-w-0">
                <dt className="text-xs uppercase tracking-wider text-muted" title="Two or more versions behind: several versions, so no single list">Older</dt>
                <dd className="text-sm font-semibold tabular-nums text-text">{fmtInt(r.older)}</dd>
              </div>
            )}
            {r.unknown > 0 && (
              <div className="min-w-0">
                <dt className="text-xs uppercase tracking-wider text-muted">No usable version</dt>
                <dd className="text-sm font-semibold tabular-nums text-text">{fmtInt(r.unknown)}</dd>
              </div>
            )}
          </dl>
        </>
      ) : (
        <p className="text-xs leading-5 text-muted">The observed versions cannot be ordered (they come from different trains), so no build is called the latest.</p>
      )}
    </li>
  )
}

/**
 * Build currency per release (doc 03 section 3.3): "41 on the latest observed build, 14 on N-1, 6 older". Latest
 * and N-1 are links to the assets on that exact version; older and unknown are text (several versions have no one
 * filter). The note says honestly that "latest observed" is not the vendor's latest release.
 */
export function OsCurrencyPanel({ os, kind }: { os: OsResponse; kind: OsKindChoice }) {
  const [all, setAll] = useState(false)
  const rows = useMemo(() => currencyRows(os, kind), [os, kind])
  const shown = all ? rows : rows.slice(0, SHOWN)
  return (
    <Panel title="Build currency" icon={<Gauge className="h-4 w-4" />} hint="per release">
      {rows.length === 0 ? (
        <InventoryEmpty kind="no-data" compact title="No versions identified yet">Once assets report a version, each release shows how many run the newest build seen and how many are behind.</InventoryEmpty>
      ) : (
        <div className="space-y-3">
          <ul role="list" aria-label="Build currency per release" className="space-y-3.5">
            {shown.map((r) => <CurrencyItem key={r.key} r={r} />)}
          </ul>
          {rows.length > SHOWN && (
            <button
              type="button"
              onClick={() => setAll((v) => !v)}
              aria-expanded={all}
              className="rounded text-xs font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary"
            >
              {all ? 'Show fewer releases' : `Show all ${fmtInt(rows.length)} releases`}
            </button>
          )}
          <ul role="list" aria-label="Key" className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
            <li className="inline-flex items-center gap-1.5"><span aria-hidden className="h-2 w-2 rounded-sm" style={{ background: 'rgb(var(--primary) / 0.9)' }} />Latest observed</li>
            <li className="inline-flex items-center gap-1.5"><span aria-hidden className="h-2 w-2 rounded-sm" style={{ background: 'rgb(var(--primary) / 0.5)' }} />N-1</li>
            <li className="inline-flex items-center gap-1.5"><span aria-hidden className="h-2 w-2 rounded-sm" style={{ background: 'rgb(var(--muted) / 0.6)' }} />Older</li>
            <li className="inline-flex items-center gap-1.5"><Swatch unknown />No usable version</li>
          </ul>
          <p className="border-t border-border pt-2.5 text-xs leading-4 text-muted" data-currency-note="">{CURRENCY_NOTE}</p>
        </div>
      )}
    </Panel>
  )
}
