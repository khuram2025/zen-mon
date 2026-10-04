import { Cable, Gauge } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { StatusTile } from '@/components/netflow/StatusTile'
import { cn } from '@/lib/utils'
import { formatCount, formatPercent } from '@/lib/netflow/format'
import {
  coverageSentence,
  RESOLUTION_LABELS,
  stateTone,
  STATE_LABELS,
  type CapacityPermissions,
  type CapacityState,
  type CapacitySummary,
  type CapacityMethod,
  type Resolution,
} from './model'

const UNRESOLVED: Exclude<Resolution, 'resolved'>[] = ['exporter_unmapped', 'no_interface_inventory', 'interface_not_found', 'speed_unknown']

/**
 * Known capacity coverage first (NF-16), then the utilization states, both counted over every
 * interface in scope (not the loaded page).
 */
export function CoverageCard({
  summary,
  method,
  permissions,
  activeResolution,
  onResolution,
  onLinkExporters,
}: {
  summary: CapacitySummary
  method: CapacityMethod
  permissions: CapacityPermissions
  activeResolution: string | null
  onResolution: (r: string | null) => void
  onLinkExporters: () => void
}) {
  const total = summary.interfaces_total
  const known = summary.speed_known
  const pct = total ? (100 * known) / total : 0
  return (
    <section aria-labelledby="cap-coverage" className="rounded-lg border border-border bg-surface p-4 shadow-card">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0 space-y-2">
          <h2 id="cap-coverage" className="flex items-center gap-2 text-sm font-semibold text-text">
            <Gauge className="h-4 w-4 text-primary" aria-hidden />
            Capacity coverage
          </h2>
          <p className="text-sm text-text">
            <span className="text-2xl font-semibold tabular-nums">{formatCount(known)}</span>
            <span className="text-text2"> of {formatCount(total)} interfaces have a known capacity</span>
          </p>
          <div
            className="h-2 w-full max-w-md overflow-hidden rounded-full bg-surface3"
            role="meter"
            aria-label="Interfaces with a known capacity"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(pct)}
          >
            <div className="h-full rounded-full bg-primary" style={{ width: `${pct}%` }} />
          </div>
          <p className="text-xs text-text2">{coverageSentence(summary)}. Utilization stays unknown until a speed is known.</p>
        </div>
        {summary.by_resolution.exporter_unmapped > 0 && (
          <Button
            size="sm"
            variant="outline"
            onClick={onLinkExporters}
            disabled={!permissions.manage}
            title={permissions.manage ? undefined : 'Linking exporters needs the NetFlow manage permission'}
          >
            <Cable aria-hidden />
            Link exporters ({summary.exporters_unmapped.length})
          </Button>
        )}
      </div>
      {total > known && (
        <ul className="mt-3 flex flex-wrap gap-2" aria-label="Why capacity is unknown">
          {UNRESOLVED.filter((r) => summary.by_resolution[r] > 0).map((r) => {
            const active = activeResolution === r
            return (
              <li key={r}>
                <button
                  type="button"
                  onClick={() => onResolution(active ? null : r)}
                  aria-pressed={active}
                  className={cn(
                    'inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs text-text hover:bg-surface2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
                    active ? 'border-primary bg-primary/10' : 'border-border bg-surface2/50',
                  )}
                >
                  <span className="font-semibold tabular-nums">{formatCount(summary.by_resolution[r])}</span>
                  {RESOLUTION_LABELS[r]}
                </button>
              </li>
            )
          })}
        </ul>
      )}
      <p className="sr-only">Thresholds: warning at {formatPercent(method.thresholds.warn_pct)}, critical at {formatPercent(method.thresholds.crit_pct)}.</p>
    </section>
  )
}

const TILE_STATES: CapacityState[] = ['over', 'critical', 'warning', 'healthy', 'unknown']

export function StateTiles({
  summary,
  method,
  active,
  onToggle,
}: {
  summary: CapacitySummary
  method: CapacityMethod
  active: string | null
  onToggle: (s: CapacityState) => void
}) {
  const t = method.thresholds
  const captions: Record<CapacityState, string> = {
    over: 'Above the interface speed: check the data',
    critical: `${statText(method)} at or above ${formatPercent(t.crit_pct)}`,
    warning: `${formatPercent(t.warn_pct)} to ${formatPercent(t.crit_pct)}`,
    healthy: `Below ${formatPercent(t.warn_pct)}`,
    insufficient: 'No samples, or too little coverage to call it healthy',
    unknown: 'No speed: utilization unknown',
  }
  const states = summary.by_state.insufficient > 0 ? [...TILE_STATES, 'insufficient' as CapacityState] : TILE_STATES
  return (
    <div className={cn('grid grid-cols-2 gap-2 sm:gap-3 md:grid-cols-3', states.length > 5 ? 'xl:grid-cols-6' : 'xl:grid-cols-5')}>
      {states.map((s) => (
        <StatusTile
          key={s}
          tone={stateTone(s)}
          word={s === 'over' ? 'Over' : s === 'unknown' ? 'Unknown' : s === 'insufficient' ? 'No data' : undefined}
          label={STATE_LABELS[s]}
          value={formatCount(summary.by_state[s] ?? 0)}
          caption={captions[s]}
          onClick={() => onToggle(s)}
          active={active === s}
          title={`Show only ${STATE_LABELS[s].toLowerCase()} interfaces`}
        />
      ))}
    </div>
  )
}

function statText(method: CapacityMethod): string {
  return method.statistic
}
