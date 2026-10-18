import { useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { HelpCircle } from 'lucide-react'
import { deviceAvailabilityBuckets, formatDeviceAvailability, type DeviceAvailabilityPoint } from '@/components/devices/availability'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Panel } from './charts'
import { formatAbsolute, formatSpan, MISSING, plural } from './format'
import { useDeviceAvailability, usePingMetrics } from './hooks'
import type { DeviceRange, DeviceRecord } from './types'

/* Availability & outages for the selected range. The headline is the shared
 * uptime-stats calculation for this device (two decimals, maintenance
 * excluded); failed checks are raw diagnostic evidence, not outages;
 * confirmed outages and downtime come from the recorded Down incidents
 * (GET /devices/{id}/availability-events); missing measurements are unknown. */

export type DeviceOutageEvidence = {
  outages: number
  downtime_seconds: number
  events_truncated: boolean
  events: { started_at: string | null; clipped_start: string; ended_at: string | null; ongoing: boolean; in_window_s: number }[]
}

/** Fetches the range's checks, the shared headline and the confirmed incidents, then draws the card. */
export function AvailabilityCard({ device, range }: { device: DeviceRecord; range: DeviceRange }) {
  const id = device.id
  const enabled = !!device.ping_enabled
  const fromTs = Date.parse(range.fromISO)
  const toTs = Date.parse(range.toISO)
  const ping = usePingMetrics(id, range, enabled)
  const availability = useDeviceAvailability(id, range, enabled)
  const evidence = useQuery<DeviceOutageEvidence>({
    queryKey: ['device', id, 'availability-events', range.fromISO, range.toISO],
    queryFn: async () => (await api.get(`/devices/${id}/availability-events?from=${encodeURIComponent(range.fromISO)}&to=${encodeURIComponent(range.toISO)}`)).data,
    enabled,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
    refetchInterval: range.isCustom ? false : 30_000,
  })
  const availabilityPct = availability.data?.devices?.[id]
  return (
    <AvailabilityTimelineCard
      points={ping.data?.points || []}
      pointsLoading={ping.isPending}
      pointsError={ping.isError && !ping.data}
      rangeLabel={range.label}
      fromTs={fromTs}
      toTs={toTs}
      availabilityPct={availabilityPct}
      availabilityLoading={availability.isPending}
      availabilityError={availability.isError && !availability.data}
      evidence={evidence.data}
      evidenceError={evidence.isError && !evidence.data}
    />
  )
}

function HowCounted() {
  return (
    <Popover>
      <PopoverTrigger className="inline-flex items-center gap-1 rounded text-xs text-muted hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
        <HelpCircle className="h-3.5 w-3.5" aria-hidden />How availability is counted
      </PopoverTrigger>
      <PopoverContent>
        <h4 className="mb-1 text-sm font-semibold">How availability is counted</h4>
        <ul className="list-disc space-y-1 pl-4 text-xs leading-relaxed text-muted">
          <li>Checks answered is the share of ping checks that got a reply in the selected range, to two decimals. Planned maintenance is excluded.</li>
          <li>A failed check lowers that figure; it does not by itself confirm an outage. It stays visible as diagnostic evidence.</li>
          <li>Down incidents follow the assigned monitor’s confirmation policy. Confirmed outages and downtime count only those incidents, excluding planned maintenance.</li>
          <li>Missing measurements are unknown, not healthy time. The strip includes raw checks taken during maintenance.</li>
        </ul>
      </PopoverContent>
    </Popover>
  )
}

function AvailabilityTimelineCard({
  points, pointsLoading, pointsError, rangeLabel, fromTs, toTs, availabilityPct, availabilityLoading, availabilityError, evidence, evidenceError,
}: {
  points: DeviceAvailabilityPoint[]
  pointsLoading?: boolean
  pointsError?: boolean
  availabilityPct?: number
  availabilityLoading?: boolean
  availabilityError?: boolean
  rangeLabel: string
  fromTs: number
  toTs: number
  evidence?: DeviceOutageEvidence
  evidenceError?: boolean
}) {
  const pct = availabilityPct ?? null
  const buckets = useMemo(() => deviceAvailabilityBuckets(points, fromTs, toTs), [points, fromTs, toTs])
  const [selected, setSelected] = useState<number | null>(null)
  const total = Math.round(buckets.reduce((sum, b) => sum + b.up + b.down, 0))
  const failed = Math.round(buckets.reduce((sum, b) => sum + b.down, 0))
  const firstFailure = buckets.findIndex(b => b.down > 0)
  const activeIndex = Math.max(0, Math.min(buckets.length - 1, selected ?? (firstFailure >= 0 ? firstFailure : buckets.length - 1)))
  const active = buckets[activeIndex]
  const withSeconds = buckets.length > 0 && (toTs - fromTs) / buckets.length < 60_000
  const intervalTime = (ts: number) => formatAbsolute(ts, { seconds: withSeconds })
  const label = (b: typeof active) => `${intervalTime(b.start)} – ${intervalTime(b.end)}: ${b.state === 'gap' ? 'No measurements' : `${formatDeviceAvailability(b.pct)} checks answered, ${Math.round(b.down)} failed of ${Math.round(b.up + b.down)} checks`}`
  const headline = availabilityError ? MISSING : availabilityLoading ? MISSING : formatDeviceAvailability(pct)
  const evidenceValue = (value: () => string) => evidenceError ? MISSING : evidence ? value() : MISSING
  return (
    <Panel
      title="Availability & outages"
      meta={`${rangeLabel} · ${total ? plural(total, 'recorded check') : 'no recorded checks'}`}
      actions={<HowCounted />}
    >
      <div className="space-y-4">
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div>
            <dt className="text-xs text-muted">Checks answered</dt>
            <dd className="mt-1 text-2xl font-semibold tabular-nums">{headline}</dd>
            <dd className="text-xs text-muted">{availabilityError ? 'Could not be loaded' : 'Maintenance excluded'}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted">Failed checks</dt>
            <dd className={cn('mt-1 text-2xl font-semibold tabular-nums', failed > 0 && 'text-warning')}>{total ? failed.toLocaleString() : MISSING}</dd>
            <dd className="text-xs text-muted">Raw diagnostic evidence</dd>
          </div>
          <div>
            <dt className="text-xs text-muted">Confirmed outages</dt>
            <dd className={cn('mt-1 text-2xl font-semibold tabular-nums', !evidenceError && (evidence?.outages ?? 0) > 0 && 'text-danger')}>{evidenceValue(() => evidence!.outages.toLocaleString())}</dd>
            <dd className="text-xs text-muted">Recorded Down incidents</dd>
          </div>
          <div>
            <dt className="text-xs text-muted">Confirmed downtime</dt>
            <dd className="mt-1 text-2xl font-semibold tabular-nums">{evidenceValue(() => evidence!.downtime_seconds > 0 ? formatSpan(evidence!.downtime_seconds) : '0s')}</dd>
            <dd className="text-xs text-muted">Within the selected period</dd>
          </div>
        </dl>

        <div>
          <div className="mb-2 text-xs font-medium">Ping checks · select an interval for details</div>
          {pointsError ? (
            <p className="rounded-lg border border-warning/40 bg-warning/5 p-2 text-xs text-warning">Ping checks could not be loaded. The strip below is unknown, not healthy.</p>
          ) : pointsLoading ? (
            <p className="text-xs text-muted">Loading ping checks…</p>
          ) : null}
          <div className="flex h-8 gap-px rounded-lg border border-border bg-surface2 p-0.5" role="group" aria-label="Ping check intervals">
            {buckets.map((b, i) => <button key={i} type="button" aria-label={label(b)} aria-pressed={i === activeIndex} tabIndex={i === activeIndex ? 0 : -1}
              onClick={() => setSelected(i)} onFocus={() => setSelected(i)}
              onKeyDown={event => {
                if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
                event.preventDefault()
                const next = event.key === 'ArrowRight' ? event.currentTarget.nextElementSibling : event.currentTarget.previousElementSibling
                if (next instanceof HTMLElement) next.focus()
              }}
              className={cn('min-w-0 flex-1 rounded-sm focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-2',
                b.state === 'up' ? 'bg-success' : b.state === 'gap' ? 'bg-transparent' : b.state === 'partial' ? 'bg-warning/70' : 'bg-warning',
                i === activeIndex && 'ring-2 ring-text/60')} />)}
          </div>
          <div className="mt-2 flex justify-between gap-2 text-xs text-muted"><span>{formatAbsolute(fromTs)}</span><span>{formatAbsolute(toTs)}</span></div>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
            <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-success" aria-hidden />All checks responded</span>
            <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-warning" aria-hidden />Failed checks</span>
            <span><span className="mr-1 inline-block h-2 w-2 rounded-sm border border-border bg-surface2" aria-hidden />No measurements (unknown)</span>
          </div>
          {active && (
            <div className="mt-3 flex items-center gap-2 rounded-lg border border-border bg-surface2/40 p-2 text-xs">
              <button type="button" aria-label="Previous interval" disabled={activeIndex === 0} onClick={() => setSelected(activeIndex - 1)} className="h-11 w-11 shrink-0 rounded-md border border-border text-lg disabled:opacity-30">‹</button>
              <span className="min-w-0 flex-1" aria-live="polite">{label(active)}</span>
              <button type="button" aria-label="Next interval" disabled={activeIndex === buckets.length - 1} onClick={() => setSelected(activeIndex + 1)} className="h-11 w-11 shrink-0 rounded-md border border-border text-lg disabled:opacity-30">›</button>
            </div>
          )}
        </div>

        <div className="border-t border-border pt-3">
          <h4 className="text-xs font-semibold">Confirmed outage history</h4>
          {evidenceError ? (
            <p className="mt-2 text-xs text-warning">Outage history could not be loaded. Outage count and downtime are unknown.</p>
          ) : !evidence ? (
            <p className="mt-2 text-xs text-muted">Loading confirmed history…</p>
          ) : evidence.outages === 0 ? (
            <p className="mt-2 text-xs text-muted">No confirmed outages were recorded in this period.{failed > 0 ? ' Failed checks remain visible above as diagnostic evidence.' : ''}</p>
          ) : (
            <details className="mt-2" open>
              <summary className="cursor-pointer text-xs text-muted">{plural(evidence.outages, 'confirmed outage')}{evidence.events_truncated ? ' · latest 100 shown' : ''}</summary>
              <ol className="mt-2 max-h-48 space-y-2 overflow-y-auto">
                {evidence.events.map((e, i) => (
                  <li key={i} className="border-l-2 border-danger/60 pl-3 text-xs">
                    <span className="font-medium">{formatAbsolute(e.started_at ?? e.clipped_start)}{!e.started_at ? ' (start unknown)' : ''}</span>
                    <span className="block text-muted">{e.ended_at ? `Recovered ${formatAbsolute(e.ended_at)}` : e.ongoing ? 'Ongoing · no recovery recorded' : 'No recovery recorded'} · {formatSpan(e.in_window_s)} in this period</span>
                  </li>
                ))}
              </ol>
            </details>
          )}
        </div>
      </div>
    </Panel>
  )
}
