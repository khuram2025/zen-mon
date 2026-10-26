/**
 * Selected-window SLA results (B18, B19) and the SLO / error-budget card.
 * Nulls read "No data"; MTTR states its population; the ongoing incident is
 * reported separately from completed recovery statistics.
 */
import type { ReactNode } from 'react'
import { Target } from 'lucide-react'
import { cn } from '@/lib/utils'
import { BAND_TEXT } from '@/components/services/uptime'
import {
  NO_DATA,
  availabilityBand,
  exactTime,
  formatAvailability,
  formatDur,
  formatDurMeasured,
  formatMs,
  pct,
} from './format'
import type { SlaStats } from './types'

function Shell({ title, subtitle, children, actions }: { title: string; subtitle?: ReactNode; children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-col overflow-hidden rounded-lg border border-border bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold tracking-tight text-text">{title}</h3>
          {subtitle && <div className="mt-0.5 text-xs text-muted">{subtitle}</div>}
        </div>
        {actions}
      </div>
      <div className="flex-1">{children}</div>
    </div>
  )
}

function State({ loading, error, onRetry, children }: { loading: boolean; error: boolean; onRetry: () => void; children: ReactNode }) {
  if (loading) return <div className="py-10 text-center text-xs text-muted">Computing…</div>
  if (error) {
    return (
      <div className="py-8 text-center text-xs text-danger">
        Could not compute SLA for this window.
        <button type="button" onClick={onRetry} className="ml-1.5 font-medium underline">Retry</button>
      </div>
    )
  }
  return <>{children}</>
}

type RowSpec = { label: string; value: string; tone?: string; hint?: string }

function Rows({ rows }: { rows: RowSpec[] }) {
  return (
    <dl className="divide-y divide-border/60">
      {rows.map((r) => (
        <div key={r.label} className="flex items-baseline justify-between gap-3 px-4 py-2">
          <dt className="text-xs text-muted">
            {r.label}
            {r.hint && <span className="ml-1 text-2xs text-muted/80">({r.hint})</span>}
          </dt>
          <dd className={cn('text-right font-mono text-xs font-medium tabular-nums', r.tone)}>{r.value}</dd>
        </div>
      ))}
    </dl>
  )
}

export function SlaSummaryCard({ sla, rangeLabel, loading, error, onRetry, nowMs }: {
  sla?: SlaStats
  rangeLabel: string
  loading: boolean
  error: boolean
  onRetry: () => void
  nowMs: number
}) {
  // Older appliances report max 0 ms with no samples; treat an all-empty set as no data (B18).
  const noLatency = sla?.latency_sample_count === 0 ||
    (sla?.latency_sample_count == null && sla?.avg_response_ms == null && sla?.p95_response_ms == null)
  const lat = (v: number | null | undefined) => (noLatency ? NO_DATA : formatMs(v, NO_DATA))
  const hasPopulation = sla?.mttr_population != null
  const downtime = sla?.total_downtime_sec
  const rows: RowSpec[] = [
    {
      label: 'Confirmed availability',
      value: formatAvailability(sla?.uptime_pct, downtime, 3),
      tone: BAND_TEXT[availabilityBand(sla?.uptime_pct, downtime)],
    },
    { label: 'Raw probe success', value: pct(sla?.probe_uptime_pct, 3, NO_DATA), hint: 'includes unconfirmed failures' },
    { label: 'Confirmed downtime', value: sla?.uptime_pct == null ? NO_DATA : formatDurMeasured(downtime), tone: (downtime || 0) > 0 ? 'text-danger' : undefined },
    { label: 'Incidents', value: sla ? String(sla.incident_count ?? 0) : NO_DATA },
    { label: 'Longest incident', value: sla?.uptime_pct == null ? NO_DATA : formatDurMeasured(sla?.longest_incident_sec) },
    {
      label: 'MTTR',
      value: sla?.mttr_sec != null ? formatDur(sla.mttr_sec) : NO_DATA,
      hint: hasPopulation
        ? `completed incidents only${sla?.completed_incident_count != null ? ` · ${sla.completed_incident_count}` : ''}`
        : 'downtime ÷ incidents; may include clipped intervals',
    },
    {
      label: 'MTBF',
      value: sla?.mtbf_sec != null ? formatDur(sla.mtbf_sec) : NO_DATA,
      hint: hasPopulation ? 'mean up time between consecutive incidents; needs 2+' : 'covered up time ÷ incidents',
    },
  ]
  if (sla?.ongoing_incident) {
    rows.splice(5, 0, {
      label: 'Ongoing incident',
      value: `since ${exactTime(sla.ongoing_incident.start, false)} · ${formatDur(Math.max(sla.ongoing_incident.duration_sec, (nowMs - Date.parse(sla.ongoing_incident.start)) / 1000))}`,
      tone: 'text-danger',
      hint: 'excluded from MTTR',
    })
  }
  rows.push(
    { label: 'Avg response', value: lat(sla?.avg_response_ms), hint: 'successful probes' },
    { label: 'P95 response', value: lat(sla?.p95_response_ms), hint: 'all successful probes in window' },
    { label: 'Max response', value: lat(sla?.max_response_ms) },
    { label: 'Probe samples', value: sla ? (sla.sample_count ?? 0).toLocaleString() : NO_DATA },
    { label: 'Current up streak', value: sla?.uptime_streak_sec != null ? formatDur(sla.uptime_streak_sec) : NO_DATA, tone: sla?.uptime_streak_sec != null ? 'text-success' : undefined },
  )
  return (
    <Shell title="SLA summary" subtitle={`${rangeLabel} · confirmed Down incidents only`}>
      <State loading={loading} error={error} onRetry={onRetry}>
        <Rows rows={rows} />
        <CoverageFooter sla={sla} />
      </State>
    </Shell>
  )
}

function CoverageFooter({ sla }: { sla?: SlaStats }) {
  if (!sla) return null
  const parts: string[] = []
  if (sla.requested_sec != null) parts.push(`Requested ${formatDur(sla.requested_sec)}`)
  if (sla.effective_from && sla.requested_from && Date.parse(sla.effective_from) > Date.parse(sla.requested_from) + 1000) {
    parts.push(`monitoring evidence starts ${exactTime(sla.effective_from, false)}`)
  }
  if (sla.coverage_pct != null) parts.push(`${sla.coverage_pct.toFixed(1)}% observed`)
  if ((sla.unknown_sec ?? 0) > 0) parts.push(`${formatDur(sla.unknown_sec)} without evidence`)
  if ((sla.maintenance_sec ?? 0) > 0) parts.push(`${formatDur(sla.maintenance_sec)} maintenance excluded`)
  if (parts.length === 0) return null
  return <p className="border-t border-border/60 px-4 py-2 text-xs text-muted">{parts.join(' · ')}</p>
}

/** Target, achieved, budget and burn for the selected window (SLO feature). */
export function SloCard({ sla, rangeLabel, loading, error, onRetry, fallbackTarget }: {
  sla?: SlaStats
  rangeLabel: string
  loading: boolean
  error: boolean
  onRetry: () => void
  /** Used only when the appliance does not compute the budget itself. */
  fallbackTarget: { pct: number; source: string }
}) {
  const server = sla?.target_pct != null
  const target = server ? (sla!.target_pct as number) : fallbackTarget.pct
  const covered = sla?.covered_sec ?? null
  const used = server ? sla?.budget_used_sec ?? null : sla?.total_downtime_sec ?? null
  const budget = server ? sla?.error_budget_sec ?? null : covered != null ? ((100 - target) / 100) * covered : null
  const usedPct = server ? sla?.error_budget_used_pct ?? null : budget && used != null ? (used / budget) * 100 : budget === 0 && (used ?? 0) > 0 ? Infinity : null
  const remaining = server ? sla?.error_budget_remaining_sec ?? null : budget != null && used != null ? budget - used : null
  const burn = server ? sla?.burn_rate ?? null : covered && covered > 0 && target < 100 && used != null ? (used / covered) / ((100 - target) / 100) : null
  const achieved = sla?.uptime_pct ?? null
  const meets = server ? sla?.meets_target ?? null : achieved != null ? achieved >= target : null
  const sourceLabel = server
    ? sla?.target_source === 'service' ? 'service target' : sla?.target_source === 'policy' ? 'appliance default policy' : 'built-in default'
    : fallbackTarget.source
  const barPct = usedPct == null ? 0 : Math.max(0, Math.min(100, usedPct))
  const barTone = usedPct == null ? 'bg-surface3' : usedPct >= 100 ? 'bg-danger' : usedPct >= 75 ? 'bg-warning' : 'bg-success'

  return (
    <Shell
      title="SLO & error budget"
      subtitle={`${rangeLabel} · target ${target}% (${sourceLabel})`}
      actions={<Target className="h-4 w-4 text-muted" aria-hidden />}
    >
      <State loading={loading} error={error} onRetry={onRetry}>
        {achieved == null ? (
          <p className="px-4 py-6 text-center text-xs text-muted">No monitoring evidence in this window, so the objective cannot be assessed.</p>
        ) : (
          <div className="space-y-3 p-4">
            <div className="flex flex-wrap items-end justify-between gap-2">
              <div>
                <div className="text-2xs font-semibold uppercase tracking-wider text-muted">Achieved</div>
                <div className={cn('text-2xl font-bold tabular-nums', BAND_TEXT[availabilityBand(achieved, sla?.total_downtime_sec)])}>
                  {formatAvailability(achieved, sla?.total_downtime_sec, 3)}
                </div>
              </div>
              <span className={cn(
                'rounded-full px-2 py-0.5 text-xs font-semibold',
                meets == null ? 'bg-surface2 text-muted' : meets ? 'bg-success/10 text-success' : 'bg-danger/10 text-danger',
              )}>
                {meets == null ? 'Not assessed' : meets ? 'Meets target' : 'Below target'}
              </span>
            </div>
            <div>
              <div className="mb-1 flex justify-between text-xs text-muted">
                <span>Error budget used</span>
                <span className="font-mono tabular-nums">
                  {usedPct == null ? NO_DATA : !Number.isFinite(usedPct) || usedPct > 999 ? '>999% (exceeded)' : `${usedPct.toFixed(1)}%`}
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-surface2" role="meter" aria-valuemin={0} aria-valuemax={100}
                aria-valuenow={Math.round(barPct)} aria-label="Error budget used">
                <div className={cn('h-full rounded-full', barTone)} style={{ width: `${barPct}%` }} />
              </div>
            </div>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
              <dt className="text-muted">Budget</dt>
              <dd className="text-right font-mono tabular-nums">{budget == null ? NO_DATA : formatDurMeasured(budget)}</dd>
              <dt className="text-muted">Confirmed downtime</dt>
              <dd className="text-right font-mono tabular-nums">{used == null ? NO_DATA : formatDurMeasured(used)}</dd>
              <dt className="text-muted">Remaining</dt>
              <dd className={cn('text-right font-mono tabular-nums', remaining != null && remaining < 0 && 'text-danger')}>
                {remaining == null ? NO_DATA : remaining < 0 ? `−${formatDur(-remaining)} over` : formatDurMeasured(remaining)}
              </dd>
              <dt className="text-muted">Burn rate</dt>
              <dd className={cn('text-right font-mono tabular-nums', burn != null && burn > 1 && 'text-danger')} title="Downtime share ÷ allowed share; 1× uses the budget exactly over this window">
                {burn == null ? NO_DATA : burn > 999 ? '>999×' : `${burn.toFixed(2)}×`}
              </dd>
              <dt className="text-muted">Coverage</dt>
              <dd className="text-right font-mono tabular-nums">{sla?.coverage_pct != null ? `${sla.coverage_pct.toFixed(1)}%` : NO_DATA}</dd>
              <dt className="text-muted">Maintenance excluded</dt>
              <dd className="text-right font-mono tabular-nums">
                {sla?.maintenance_sec == null ? 'Not reported' : formatDurMeasured(sla.maintenance_sec)}
              </dd>
            </dl>
            <p className="text-xs leading-relaxed text-muted">
              The budget is the allowed confirmed downtime over the observed time in this window.
              {!server && ' Estimated in the browser: this appliance does not report the budget, and maintenance is not excluded.'}
            </p>
          </div>
        )}
      </State>
    </Shell>
  )
}
