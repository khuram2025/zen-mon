/**
 * Charts for the Availability page, following the management review's graph
 * guidance: one dominant series, a dashed configured-target line, missing
 * evidence drawn as hatched "unknown" (never smoothed through), incident
 * markers, ranked target plots instead of near-identical progress bars, and
 * incident bars with explicit start, recovery or ongoing state.
 */
import { useMemo, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  CartesianGrid, ComposedChart, Line, ReferenceArea, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { cn } from '@/lib/utils'
import type { ComplianceState } from '@/hooks/useReports'
import {
  availNumber, fmtAvail, fmtDateTime, fmtDuration, fmtShare, fmtTarget, timezoneLabel,
} from './format'
import type { ComplianceCounts, ServiceRow, Tone } from './model'
import { EvidenceEmpty, TONE, ToneIcon } from './ui'

/* ── Availability trend ────────────────────────────────────────────────── */

export interface TrendPoint { ts: string; availability_pct: number | null }

interface TrendDatum { t: number; v: number | null }

function niceTicks(fromMs: number, toMs: number): { ticks: number[]; fmt: (t: number) => string } {
  const span = toMs - fromMs
  const hour = 3_600_000
  if (span <= 48 * hour) {
    const stepH = span <= 2 * hour ? 0.25 : span <= 6 * hour ? 1 : span <= 12 * hour ? 2 : span <= 24 * hour ? 3 : 6
    const step = stepH * hour
    const start = new Date(fromMs)
    start.setMinutes(stepH < 1 ? Math.ceil(start.getMinutes() / 15) * 15 : 0, 0, 0)
    if (stepH >= 1) start.setHours(Math.ceil(start.getHours() / stepH) * stepH)
    const ticks: number[] = []
    for (let t = start.getTime(); t <= toMs; t += step) if (t >= fromMs) ticks.push(t)
    const multiDay = new Date(fromMs).toDateString() !== new Date(toMs).toDateString()
    return {
      ticks,
      fmt: (t) => {
        const d = new Date(t)
        const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        return multiDay && d.getHours() === 0 && d.getMinutes() === 0
          ? d.toLocaleDateString([], { month: 'short', day: 'numeric' })
          : time
      },
    }
  }
  const days = span / (24 * hour)
  const stepDays = Math.max(1, Math.ceil(days / 8))
  const start = new Date(fromMs)
  start.setHours(0, 0, 0, 0)
  if (start.getTime() < fromMs) start.setDate(start.getDate() + 1)
  const ticks: number[] = []
  for (const d = new Date(start); d.getTime() <= toMs; d.setDate(d.getDate() + stepDays)) ticks.push(d.getTime())
  return { ticks, fmt: (t) => new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' }) }
}

export function AvailabilityTrendChart({
  points, fromMs, toMs, target, incidents, height = 280, basis,
}: {
  points: TrendPoint[]
  fromMs: number
  toMs: number
  target: number | null
  incidents: Array<{ startMs: number; label: string }>
  height?: number
  basis: string
}) {
  const model = useMemo(() => {
    const data: TrendDatum[] = points
      .map((p) => ({ t: Date.parse(p.ts), v: p.availability_pct }))
      .filter((d) => Number.isFinite(d.t))
      .sort((a, b) => a.t - b.t)
    const diffs = data.slice(1).map((d, i) => d.t - data[i].t).sort((a, b) => a - b)
    const width = diffs.length ? diffs[Math.floor(diffs.length / 2)] : Math.max(60_000, (toMs - fromMs) / 24)
    const gaps: Array<[number, number]> = []
    const push = (a: number, b: number) => {
      if (b <= a) return
      const last = gaps[gaps.length - 1]
      if (last && a <= last[1] + 1) last[1] = Math.max(last[1], b)
      else gaps.push([a, b])
    }
    if (!data.length) push(fromMs, toMs)
    else {
      if (data[0].t - fromMs > width * 1.5) push(fromMs, data[0].t)
      data.forEach((d, i) => {
        if (d.v == null) push(d.t, Math.min(toMs, data[i + 1]?.t ?? d.t + width))
        else if (data[i + 1] && data[i + 1].t - d.t > width * 1.5) push(d.t + width, data[i + 1].t)
      })
      const lastT = data[data.length - 1].t
      if (toMs - lastT > width * 2) push(lastT + width, toMs)
    }
    const vals = data.map((d) => d.v).filter((v): v is number => v != null)
    const floor = Math.min(...vals, target ?? 100)
    const domain: [number, number] = floor >= 99 ? [99, 100] : floor >= 95 ? [95, 100] : floor >= 90 ? [90, 100] : floor >= 70 ? [70, 100] : [0, 100]
    const yTicks = domain[0] === 99 ? [99, 99.25, 99.5, 99.75, 100]
      : domain[0] === 95 ? [95, 96, 97, 98, 99, 100]
        : domain[0] === 90 ? [90, 92, 94, 96, 98, 100]
          : domain[0] === 70 ? [70, 80, 90, 100] : [0, 25, 50, 75, 100]
    const unknownMs = gaps.reduce((s, [a, b]) => s + (b - a), 0)
    const minGap = (toMs - fromMs) / 120
    const markers: typeof incidents = []
    for (const inc of [...incidents].sort((a, b) => a.startMs - b.startMs)) {
      if (inc.startMs < fromMs || inc.startMs > toMs) continue
      if (markers.length && inc.startMs - markers[markers.length - 1].startMs < minGap) continue
      markers.push(inc)
      if (markers.length >= 40) break
    }
    return { data, gaps, vals, domain, yTicks, unknownMs, markers, sparse: data.length < 50 }
  }, [points, fromMs, toMs, target, incidents])

  const { ticks, fmt } = useMemo(() => niceTicks(fromMs, toMs), [fromMs, toMs])

  if (!model.vals.length) {
    return (
      <EvidenceEmpty kind="no_evidence">
        No availability evidence was recorded in this period. It is shown as unknown, not as 0% or as healthy.
      </EvidenceEmpty>
    )
  }

  const unknownShare = ((model.unknownMs / (toMs - fromMs)) * 100)
  return (
    <div>
      <div style={{ height }} role="img" aria-label={`Availability trend, ${model.vals.length} measured intervals, ${fmtShare(unknownShare)} of the period without evidence`}>
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={model.data} margin={{ top: 12, right: 12, left: 0, bottom: 0 }}>
            <defs>
              <pattern id="avail-unknown-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                <rect width="6" height="6" fill="rgb(var(--muted))" fillOpacity={0.06} />
                <line x1="0" y1="0" x2="0" y2="6" stroke="rgb(var(--muted))" strokeOpacity={0.45} strokeWidth={1.5} />
              </pattern>
            </defs>
            <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.6} vertical={false} />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={[fromMs, toMs]}
              ticks={ticks}
              tickFormatter={fmt}
              tick={{ fontSize: 12, fill: 'rgb(var(--muted))' }}
              axisLine={false}
              tickLine={false}
              allowDataOverflow
            />
            <YAxis
              domain={model.domain}
              ticks={model.yTicks}
              tickFormatter={(v) => `${v}%`}
              tick={{ fontSize: 12, fill: 'rgb(var(--muted))' }}
              axisLine={false}
              tickLine={false}
              width={56}
              allowDataOverflow
            />
            {model.gaps.map(([a, b]) => (
              <ReferenceArea key={`gap-${a}`} x1={a} x2={b} y1={model.domain[0]} y2={model.domain[1]} fill="url(#avail-unknown-hatch)" stroke="none" ifOverflow="hidden" />
            ))}
            {model.markers.map((m) => (
              <ReferenceLine key={`inc-${m.startMs}`} x={m.startMs} stroke="rgb(var(--danger))" strokeOpacity={0.55} strokeWidth={1} />
            ))}
            {target != null && (
              <ReferenceLine
                y={target}
                stroke="rgb(var(--text2))"
                strokeDasharray="6 4"
                strokeWidth={1.25}
                ifOverflow="extendDomain"
                label={{ value: `Configured target ${fmtTarget(target)}`, position: 'insideBottomRight', fill: 'rgb(var(--text2))', fontSize: 12, dy: -4 }}
              />
            )}
            <Tooltip content={<TrendTooltip target={target} basis={basis} />} />
            <Line
              type="linear"
              dataKey="v"
              stroke="rgb(var(--primary))"
              strokeWidth={2}
              connectNulls={false}
              isAnimationActive={false}
              dot={model.sparse ? { r: 2.5, fill: 'rgb(var(--primary))', strokeWidth: 0 } : false}
              activeDot={{ r: 4, fill: 'rgb(var(--primary))', stroke: 'rgb(var(--surface))', strokeWidth: 2 }}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted">
        <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-5 bg-primary" /> Availability</span>
        {target != null && <span className="inline-flex items-center gap-1.5"><span className="w-5 border-t-2 border-dashed border-text2" /> Configured target</span>}
        <span className="inline-flex items-center gap-1.5"><span className="h-3 w-4 rounded-sm border border-border bg-[repeating-linear-gradient(135deg,rgb(var(--muted)/0.5)_0_2px,transparent_2px_5px)]" /> No evidence ({fmtShare(unknownShare)})</span>
        {model.markers.length > 0 && <span className="inline-flex items-center gap-1.5"><span className="h-3 w-0.5 bg-danger/70" /> Outage start</span>}
        {model.domain[0] > 0 && <span>Axis zoomed to {model.domain[0]}–100%</span>}
      </div>
      <details className="mt-2 text-sm">
        <summary className="cursor-pointer text-muted hover:text-text">Show data table</summary>
        <div className="mt-2 max-h-56 overflow-y-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <caption className="sr-only">Availability by interval</caption>
            <thead><tr className="border-b border-border text-left text-muted"><th className="px-3 py-1.5 font-medium">Interval start ({timezoneLabel()})</th><th className="px-3 py-1.5 text-right font-medium">Availability</th></tr></thead>
            <tbody>
              {model.data.map((d) => (
                <tr key={d.t} className="border-b border-border/50 last:border-0">
                  <td className="px-3 py-1">{fmtDateTime(d.t)}</td>
                  <td className="px-3 py-1 text-right tabular-nums">{fmtAvail(d.v, 'No evidence')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  )
}

function TrendTooltip({ active, payload, label, target, basis }: any) {
  if (!active || label == null) return null
  const v = payload?.[0]?.value as number | null | undefined
  return (
    <div className="max-w-[260px] rounded-lg border border-border bg-surface px-3 py-2 text-sm shadow-elevated">
      <div className="text-muted">{new Date(label).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}</div>
      <div className="text-xs text-muted">{timezoneLabel()}</div>
      <div className={cn('mt-1 text-lg font-semibold tabular-nums', v == null ? 'text-muted' : 'text-text')}>
        {v == null ? 'No evidence' : fmtAvail(v)}
      </div>
      {target != null && v != null && (
        <div className={cn('text-sm', v >= target ? 'text-success' : 'text-danger')}>
          {v >= target ? 'At or above' : 'Below'} target {fmtTarget(target)}
        </div>
      )}
      <div className="mt-1 text-xs text-muted">{basis}</div>
    </div>
  )
}

/* ── Services vs target (ranked dot plot) ──────────────────────────────── */

const COMPLIANCE_META: Record<ComplianceState, { tone: Tone; label: string }> = {
  met: { tone: 'met', label: 'Met' },
  at_risk: { tone: 'risk', label: 'At risk' },
  breached: { tone: 'breach', label: 'Breached' },
  not_assessed: { tone: 'unknown', label: 'Not assessed' },
}

export function complianceMeta(c: ComplianceState) {
  return COMPLIANCE_META[c]
}

function Marker({ compliance }: { compliance: ComplianceState }) {
  if (compliance === 'breached') {
    return <span className="block h-3 w-3 rotate-45 rounded-[2px] bg-danger ring-2 ring-surface" />
  }
  if (compliance === 'at_risk') {
    return <span className="block h-0 w-0 border-x-[7px] border-b-[12px] border-x-transparent border-b-warning" />
  }
  return <span className="block h-3 w-3 rounded-full bg-success ring-2 ring-surface" />
}

export function ServiceTargetPlot({ rows, emptyText }: { rows: ServiceRow[]; emptyText: ReactNode }) {
  const measured = rows.filter((r) => r.availability != null)
  // Zoom to the band around the targets so 99.00% and 99.99% stay distinct;
  // values far below it are pinned to the left edge and labelled off scale.
  const axisMin = useMemo(() => {
    const minTarget = Math.min(...rows.map((r) => r.target), 100)
    const nearby = measured.map((r) => r.availability as number).filter((v) => v >= minTarget - 5)
    const floor = Math.min(...nearby, minTarget)
    return [99.5, 99, 98, 95, 90, 80, 50, 0].find((c) => c <= floor - 0.02) ?? 0
  }, [measured, rows])
  if (!rows.length) return <>{emptyText}</>
  const pos = (v: number) => `${((Math.max(axisMin, Math.min(100, v)) - axisMin) / (100 - axisMin)) * 100}%`
  const ticks = [axisMin, axisMin + (100 - axisMin) / 2, 100]

  return (
    <div>
      <div className="hidden grid-cols-[minmax(0,1.2fr)_minmax(0,2fr)_minmax(7.5rem,auto)] gap-3 pb-1 text-sm text-muted sm:grid">
        <span>Service</span>
        <div className="relative h-4">
          {ticks.map((t, i) => (
            <span key={t} className={cn('absolute tabular-nums', i === 0 ? 'left-0' : i === 2 ? 'right-0' : '-translate-x-1/2')} style={i === 1 ? { left: '50%' } : undefined}>
              {Number(t.toFixed(2))}%
            </span>
          ))}
        </div>
        <span className="text-right">Availability</span>
      </div>
      <ul className="divide-y divide-border/60">
        {rows.map((row) => {
          const meta = COMPLIANCE_META[row.compliance]
          const clipped = row.availability != null && row.availability < axisMin
          return (
            <li key={row.id} className="grid gap-x-3 gap-y-1 py-2 sm:grid-cols-[minmax(0,1.2fr)_minmax(0,2fr)_minmax(7.5rem,auto)] sm:items-center">
              <div className="min-w-0">
                <Link to={`/services/${row.id}`} className="block truncate text-base font-medium text-text hover:text-primary" title={row.name}>{row.name}</Link>
                <div className="truncate text-sm text-muted">
                  {[row.criticality ? `${row.criticality[0].toUpperCase()}${row.criticality.slice(1)}` : null, row.owner, row.group ?? 'Not assigned'].filter(Boolean).join(' · ')}
                </div>
              </div>
              <div className="relative h-7" aria-hidden>
                <div className="absolute inset-x-0 top-1/2 h-px bg-border" />
                <div className="absolute inset-y-0 border-l-2 border-dashed border-text2/70" style={{ left: pos(row.target) }} title={`Configured target ${fmtTarget(row.target)}`} />
                {row.availability != null ? (
                  <div className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2" style={{ left: pos(row.availability) }}>
                    <Marker compliance={row.compliance} />
                  </div>
                ) : (
                  <div className="absolute left-0 top-1/2 h-3 w-3 -translate-y-1/2 rounded-full border-2 border-dashed border-muted" />
                )}
                {clipped && <span className="absolute -top-1 left-4 text-xs text-danger">◀ below axis</span>}
              </div>
              <div className="flex items-baseline justify-between gap-2 sm:block sm:text-right">
                <div className={cn('text-base font-semibold tabular-nums', row.availability == null ? 'text-muted' : 'text-text')}>
                  {fmtAvail(row.availability)}
                </div>
                <div className={cn('inline-flex items-center gap-1 text-sm', TONE[meta.tone].text)}>
                  <ToneIcon tone={meta.tone} className="h-3.5 w-3.5" />
                  {meta.label} · target {fmtTarget(row.target)}
                </div>
                {row.downtimeMin != null && row.downtimeMin > 0 && (
                  <div className="text-sm text-muted">{fmtDuration(row.downtimeMin)} confirmed down</div>
                )}
              </div>
            </li>
          )
        })}
      </ul>
      {axisMin > 0 && <p className="mt-2 text-sm text-muted">Axis zoomed to {axisMin}–100%. Dashed line: each service's configured target.</p>}
    </div>
  )
}

/* ── Target compliance (stacked counts) ────────────────────────────────── */

export function ComplianceStack({
  counts, active, onSelect,
}: {
  counts: ComplianceCounts
  active: ComplianceState | null
  onSelect: (c: ComplianceState | null) => void
}) {
  const order: ComplianceState[] = ['met', 'at_risk', 'breached', 'not_assessed']
  const total = counts.eligible || order.reduce((s, k) => s + counts[k], 0)
  if (!total) return <EvidenceEmpty kind="no_evidence">No services are eligible for target assessment.</EvidenceEmpty>
  return (
    <div className="space-y-3">
      <div className="flex h-4 overflow-hidden rounded-full bg-surface2" role="img" aria-label={order.map((k) => `${COMPLIANCE_META[k].label} ${counts[k]}`).join(', ')}>
        {order.map((k) => counts[k] > 0 && (
          <div
            key={k}
            className={cn('h-full border-r border-surface last:border-r-0', k === 'not_assessed'
              ? 'bg-[repeating-linear-gradient(135deg,rgb(var(--muted)/0.55)_0_3px,rgb(var(--muted)/0.2)_3px_6px)]'
              : TONE[COMPLIANCE_META[k].tone].dot)}
            style={{ width: `${(counts[k] / total) * 100}%` }}
          />
        ))}
      </div>
      <div className="grid grid-cols-2 gap-2">
        {order.map((k) => {
          const meta = COMPLIANCE_META[k]
          const selected = active === k
          return (
            <button
              key={k}
              type="button"
              aria-pressed={selected}
              onClick={() => onSelect(selected ? null : k)}
              className={cn(
                'flex min-h-[44px] items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left transition',
                selected ? cn(TONE[meta.tone].border, TONE[meta.tone].bg) : 'border-border hover:bg-surface2/60',
              )}
            >
              <span className={cn('inline-flex items-center gap-1.5 text-base', TONE[meta.tone].text)}>
                <ToneIcon tone={meta.tone} />
                {meta.label}
              </span>
              <span className="text-lg font-semibold tabular-nums text-text">{counts[k]}</span>
            </button>
          )
        })}
      </div>
      <p className="text-sm text-muted">
        Of {total} eligible services · {total - counts.not_assessed} measured. Select a category to filter the list.
      </p>
    </div>
  )
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
