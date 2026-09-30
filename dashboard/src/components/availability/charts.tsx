/**
 * Shared chart pieces for the Availability page: compliance labels, the
 * incident timeline (explicit start, recovery or ongoing state) and the
 * target-aware availability cell. The dashboard visuals live in viz.tsx.
 */
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import type { ComplianceState } from '@/hooks/useReports'
import {
  availNumber, fmtDateTime, fmtDuration,
} from './format'
import type { Tone } from './model'
import { ToneIcon } from './ui'

/* ── Compliance labels ─────────────────────────────────────────────────── */

const COMPLIANCE_META: Record<ComplianceState, { tone: Tone; label: string }> = {
  met: { tone: 'met', label: 'Met' },
  at_risk: { tone: 'risk', label: 'At risk' },
  breached: { tone: 'breach', label: 'Breached' },
  not_assessed: { tone: 'unknown', label: 'Not assessed' },
}

export function complianceMeta(c: ComplianceState) {
  return COMPLIANCE_META[c]
}

/* ── Incident timeline ─────────────────────────────────────────────────── */

export interface TimelineItem {
  key: string
  label: string
  sub?: string | null
  to: string
  startMs: number | null
  endMs: number | null
  ongoing: boolean
  /** Full episode length; null when genuinely unknown. */
  durationMin: number | null
  /** false when the episode began before the period (start clipped/unknown). */
  startKnown?: boolean
  /** Minutes of the episode inside the selected period. */
  inWindowMin?: number | null
}

export function IncidentTimeline({
  items, fromMs, toMs, empty,
}: {
  items: TimelineItem[]
  fromMs: number
  toMs: number
  empty: ReactNode
}) {
  if (!items.length) return <>{empty}</>
  const span = Math.max(1, toMs - fromMs)
  const pct = (t: number) => ((Math.max(fromMs, Math.min(toMs, t)) - fromMs) / span) * 100
  const mid = fromMs + span / 2
  const axisFmt = (t: number) => span > 48 * 3_600_000
    ? new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' })
    : new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return (
    <div>
      <ul className="space-y-2">
        {items.map((item) => {
          const start = item.startMs
          const end = item.ongoing
            ? toMs
            : item.endMs ?? (start != null && item.durationMin != null ? start + item.durationMin * 60_000 : null)
          const left = start != null ? pct(start) : null
          const right = end != null ? pct(end) : null
          return (
            <li key={item.key} className="grid gap-x-3 gap-y-1 sm:grid-cols-[minmax(0,11rem)_minmax(0,1fr)_minmax(7rem,auto)] sm:items-center">
              <div className="min-w-0">
                <Link to={item.to} className="block truncate text-base font-medium text-text hover:text-primary" title={item.label}>{item.label}</Link>
                {item.sub && <div className="truncate text-sm text-muted">{item.sub}</div>}
              </div>
              <div className="relative h-5 rounded bg-surface2/70" aria-hidden>
                {left != null && right != null ? (
                  <div
                    className={cn('absolute inset-y-0.5 rounded-sm', item.ongoing
                      ? 'bg-[repeating-linear-gradient(135deg,rgb(var(--danger))_0_4px,rgb(var(--danger)/0.45)_4px_8px)]'
                      : 'bg-danger/80', item.startKnown === false && 'rounded-l-none border-l-2 border-dashed border-text2')}
                    style={{ left: `${left}%`, width: `max(3px, ${Math.max(0, right - left)}%)` }}
                  />
                ) : left != null ? (
                  <div className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rotate-45 border-2 border-danger bg-surface" style={{ left: `${left}%` }} />
                ) : null}
              </div>
              <div className="text-sm sm:text-right">
                <div className="tabular-nums text-text2">{item.startKnown === false ? 'Started before period' : fmtDateTime(start)}</div>
                <div className={cn('font-medium tabular-nums', item.ongoing ? 'text-danger' : end == null ? 'text-muted' : 'text-text')}>
                  {item.ongoing
                    ? `Ongoing · ${fmtDuration(item.durationMin, 'duration unknown')}`
                    : end == null
                      ? 'Recovery unknown'
                      : `Recovered · ${fmtDuration(item.durationMin ?? (start != null && item.startKnown !== false ? (end - start) / 60_000 : null))}`}
                </div>
                {item.inWindowMin != null && item.durationMin != null && Math.abs(item.inWindowMin - item.durationMin) >= 1 && (
                  <div className="tabular-nums text-muted">{fmtDuration(item.inWindowMin)} in period</div>
                )}
              </div>
            </li>
          )
        })}
      </ul>
      <div className="mt-2 hidden grid-cols-[minmax(0,11rem)_minmax(0,1fr)_minmax(7rem,auto)] gap-x-3 text-sm text-muted sm:grid">
        <span />
        <div className="flex justify-between tabular-nums"><span>{axisFmt(fromMs)}</span><span>{axisFmt(mid)}</span><span>{axisFmt(toMs)}</span></div>
        <span />
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted">
        <span className="inline-flex items-center gap-1.5"><span className="h-3 w-5 rounded-sm bg-danger/80" /> Outage (start to recovery)</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-3 w-5 rounded-sm bg-[repeating-linear-gradient(135deg,rgb(var(--danger))_0_4px,rgb(var(--danger)/0.45)_4px_8px)]" /> Ongoing</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rotate-45 border-2 border-danger" /> Recovery unknown</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-3 w-5 rounded-r-sm border-l-2 border-dashed border-text2 bg-danger/80" /> Started before period</span>
      </div>
    </div>
  )
}

/** Availability value cell with target-aware tone + text cue. */
export function AvailCell({ value, target }: { value: number | null; target: number | null }) {
  if (value == null) return <span className="text-sm text-muted">Not measured</span>
  const below = target != null && value < target
  return (
    <span className={cn('inline-flex items-center justify-end gap-1 font-medium tabular-nums', below ? 'text-danger' : 'text-text')}>
      {below && <ToneIcon tone="breach" className="h-3.5 w-3.5" />}
      {availNumber(value).toFixed(2)}%
    </span>
  )
}
