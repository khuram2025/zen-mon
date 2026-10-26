import { Link } from 'react-router-dom'
import { History } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ChangesFilters, ChangesWindowKey } from '@/pages/inventory/osHardwareChanges'
import type { ChangesResponse } from '@/pages/inventory/types'
import {
  CHANGE_WINDOWS, applyChip, changesPageHref, pairedNote, statChips, windowOf, windowText, withWindow,
} from './changesModel'
import { fmtInt } from './model'
import { Panel } from './PanelCard'

/**
 * The window switch (24 h / 7 d / 30 d / 90 d). Each is a link that rewrites `since` (the default 7 d has none);
 * a window that is no preset (a clicked day, an old bookmark) shows as its own marked segment and says what it is.
 */
export function ChangesWindowSwitch({
  filters, now, tz, pageSize,
}: {
  filters: ChangesFilters
  now: number
  tz: string
  pageSize: number
}) {
  const current = windowOf(filters.since, filters.until, now)
  const seg = 'rounded px-2.5 py-1 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-primary'
  return (
    <div className="flex flex-wrap items-center gap-2" data-window-switch="">
      <div role="group" aria-label="Time window" className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface2/60 p-0.5">
        {CHANGE_WINDOWS.map((w) => {
          const active = current === w.key
          return (
            <Link
              key={w.key}
              to={changesPageHref(withWindow(filters, w.key as ChangesWindowKey, now), { page_size: pageSize })}
              aria-current={active ? 'true' : undefined}
              title={`Changes in the last ${w.words}`}
              className={cn(seg, active ? 'bg-primary text-on-primary shadow-sm' : 'text-text2 hover:bg-surface3 hover:text-text')}
            >
              {w.label}
            </Link>
          )
        })}
        {current === 'custom' && (
          <span aria-current="true" className={cn(seg, 'bg-primary text-on-primary shadow-sm')} title={windowText(filters.since, filters.until, now, tz)}>Custom</span>
        )}
      </div>
      <span className="text-xs text-muted" data-window-text="">{windowText(filters.since, filters.until, now, tz)}</span>
    </div>
  )
}

/**
 * The stats strip: eight chips, each a filter. The number on a chip is the total of the list it opens with the
 * same window and filters (05-P1-API-CONTRACT.md section 4); choosing a chip keeps everything else and sets only
 * category, action and label. Upgrades recorded as an installation and a removal are not in Upgraded or Downgraded
 * (those count version-change rows, so they equal their lists); the line under the strip says how many there are.
 */
export function ChangesStatsStrip({
  stats, filters, now, tz, pageSize,
}: {
  stats: ChangesResponse['stats']
  filters: ChangesFilters
  now: number
  tz: string
  pageSize: number
}) {
  const chips = statChips(stats, filters)
  const note = pairedNote(stats)
  return (
    <Panel
      title="Changes"
      icon={<History className="h-4 w-4" />}
      right={<ChangesWindowSwitch filters={filters} now={now} tz={tz} pageSize={pageSize} />}
    >
      <ul role="list" aria-label="Filter by kind of change" className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-8" data-stat-chips="">
        {chips.map((c) => (
          <li key={c.id}>
            <Link
              to={changesPageHref(applyChip(filters, c.id), { page_size: pageSize })}
              aria-current={c.active ? 'true' : undefined}
              data-chip={c.id}
              data-drill-count={c.count}
              title={`${c.hint}. ${c.active ? 'Showing these.' : 'Show only these.'}`}
              className={cn(
                'block rounded-lg border px-3 py-2 outline-none transition-colors focus-visible:ring-2 focus-visible:ring-primary',
                c.active ? 'border-primary bg-primary/10' : 'border-border bg-surface hover:border-primary/50 hover:bg-surface2/60',
              )}
            >
              <span className={cn('block truncate text-xs font-semibold uppercase tracking-wider', c.active ? 'text-text' : 'text-muted')}>{c.label}</span>
              <span className="mt-0.5 block text-xl font-bold tabular-nums leading-tight text-text">{fmtInt(c.count)}</span>
            </Link>
          </li>
        ))}
      </ul>
      {note && <p className="mt-2.5 text-xs text-muted" data-paired-note="">{note}</p>}
    </Panel>
  )
}
