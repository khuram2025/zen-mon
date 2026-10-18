import { useMemo } from 'react'
import { ArrowRight, CircleSlash } from 'lucide-react'
import { DeviceWidgetGrid, type DeviceWidget } from '@/components/devices/DeviceWidgetGrid'
import { useDeviceStatusExplanation } from '@/components/devices/DeviceStatusExplanation'
import { averageDevicePacketLoss, deviceAvailabilityBuckets, formatDeviceAvailability, packetLossPercent } from '@/components/devices/availability'
import type { FlapEpisode } from '@/components/devices/deviceStatus'
import { serverNow } from '@/lib/serverClock'
import { cn } from '@/lib/utils'
import { buildActivityEvents, normalizeSeverity, type ActivityEvent } from './activity'
import { axisWidth, niceTicks, Panel, SERIES_COLORS, Sparkline, TimeSeriesPanel } from './charts'
import {
  formatAbsolute, formatBitRate, formatLinkSpeed, formatLossPct, formatMs, formatRelative, formatUsagePct, parseTime,
  latencyDomain, MISSING, PERCENT_DOMAIN, sig3, trendOf,
} from './format'
import { useDeviceAvailability, usePingMetrics, useSnmpMetrics } from './hooks'
import { KpiTile, UsageBar } from './KpiTile'
import { DeviceNetflowCard, useDeviceNetflow } from './NetflowCard'
import type { DeviceTabId } from './tabs'
import type { DeviceRange, DeviceRecord, DeviceSummary, HealthGroupDigest, Thresholds } from './types'

const STATUS_DOT: Record<string, string> = {
  crit: 'bg-danger', warn: 'bg-warning', ok: 'bg-success', info: 'bg-info', none: 'bg-muted/40', na: 'bg-transparent border border-border',
}

/** Is it healthy, and what is wrong? KPIs by device profile, latency and loss
 *  against the status limits, recent events, the busiest (WAN first)
 *  interfaces and template health groups with their values. */
export function SummaryTab({ device, summary, range, onOpen }: {
  device: DeviceRecord
  summary?: DeviceSummary
  range: DeviceRange
  onOpen: (tab: DeviceTabId, group?: string) => void
}) {
  const id = device.id
  const snmp = !!device.snmp_enabled
  const fromTs = Date.parse(range.fromISO)
  const toTs = Date.parse(range.toISO)
  const ping = usePingMetrics(id, range, !!device.ping_enabled)
  const metrics = useSnmpMetrics(id, Math.max(1, Math.round(range.hours)), snmp)
  const availability = useDeviceAvailability(id, range, !!device.ping_enabled)
  const { data: explanation } = useDeviceStatusExplanation(id, !!device.ping_enabled)
  const netflow = useDeviceNetflow(id, device.ip_address, range)

  const limits = explanation?.thresholds
  const rttLimit = limits?.degraded_rtt_ms ?? null
  const lossLimit = limits?.degraded_loss_pct ?? null
  const points = ping.data?.points || []
  const rtt = points.map((p) => ({ ts: parseTime(p.timestamp) ?? NaN, value: p.rtt_ms ?? null }))
  const loss = points.map((p) => ({ ts: parseTime(p.timestamp) ?? NaN, value: packetLossPercent(p.packet_loss) }))
  // The latest check, not the latest reply: a device that stopped answering has no current latency.
  const lastCheck = rtt.length ? rtt[rtt.length - 1] : null
  const lastRtt = lastCheck?.value ?? null
  const unanswered = lastCheck != null && lastCheck.value == null
  const avgLoss = averageDevicePacketLoss(points)
  const availabilityPct = availability.data?.devices?.[id]
  const failedChecks = availability.data?.failed_checks?.[id]

  const isF5 = /f5/i.test(device.vendor || '') || /f5 big-ip/i.test(device.profile_name || '')
  // F5: overall RAM is system used / system total; TMM, non-TMM and swap stay separate tiles.
  const cpu = metrics.data?.cpu
  const mem = isF5 ? metrics.data?.f5_system_memory_pct : metrics.data?.memory
  const cpuValues = (cpu?.points || []).map((p) => p.value)
  const memValues = (mem?.points || []).map((p) => p.value)
  const cpuVal = cpuValues.length ? cpuValues[cpuValues.length - 1] : null
  const memVal = memValues.length ? memValues[memValues.length - 1] : null
  const cpuTh = summary?.kpis.cpu_thresholds
  const memTh = summary?.kpis.memory_thresholds
  const loadingFirst = (q: { isPending: boolean }) => q.isPending

  const toneFor = (v: number | null, th?: Thresholds | null): 'neutral' | 'warning' | 'critical' =>
    v == null || !th ? 'neutral' : th.crit != null && v >= th.crit ? 'critical' : th.warn != null && v >= th.warn ? 'warning' : 'neutral'

  const tiles: DeviceWidget[] = []
  const tile = (wid: string, title: string, content: React.ReactNode) => tiles.push({ id: wid, title, width: 2, height: 4, minWidth: 2, minHeight: 4, compact: true, content })
  if (device.ping_enabled) {
    tile('availability', 'Reachability', <KpiTile label="Checks answered" loading={loadingFirst(availability)}
      value={availability.isPending ? MISSING : formatDeviceAvailability(availabilityPct)}
      tone={availabilityPct != null && availabilityPct < 95 ? 'warning' : 'neutral'}
      sub={<>{avgLoss == null ? 'Packet loss —' : `${formatLossPct(avgLoss)} average packet loss`}{failedChecks ? ` · ${failedChecks.toLocaleString()} failed checks` : ''}</>}
      chart={<Sparkline label="Checks answered" domain={PERCENT_DOMAIN} stroke={SERIES_COLORS.success} format={(v) => `${v.toFixed(0)}%`}
        values={points.map((p) => p.uptime_pct ?? (p.is_up == null ? null : p.is_up ? 100 : 0))} />} />)
    tile('latency', 'Latency', <KpiTile label="Latency" loading={loadingFirst(ping)}
      value={formatMs(lastRtt)}
      tone={lastRtt != null && rttLimit != null && lastRtt >= rttLimit ? 'warning' : 'neutral'}
      sub={unanswered ? 'No reply to the latest check' : lastRtt == null && !ping.isPending ? 'No samples in this period' : rttLimit != null ? `Latest · limit ${sig3(rttLimit)} ms` : 'Latest sample'}
      trend={unanswered ? null : trendOf(rtt.map((p) => p.value), 'latency', range.label, rttLimit)}
      chart={<Sparkline label="Latency" domain={latencyDomain(rtt.map((p) => p.value), rttLimit)} threshold={rttLimit} format={formatMs} values={rtt.map((p) => p.value).slice(-60)} />} />)
  }
  if (cpuVal != null || (snmp && metrics.isPending)) {
    tile('cpu', 'CPU', <KpiTile label="CPU" loading={metrics.isPending} value={formatUsagePct(cpuVal)} tone={toneFor(cpuVal, cpuTh)}
      sub={cpuValues.length ? `Max ${formatUsagePct(Math.max(...cpuValues))} in ${range.label.replace(/^Last /, 'the last ')}` : undefined}
      trend={trendOf(cpuValues, 'percent', range.label, cpuTh?.warn)}
      chart={<Sparkline label="CPU" domain={PERCENT_DOMAIN} threshold={cpuTh?.crit ?? cpuTh?.warn} format={(v) => `${v.toFixed(0)}%`} values={cpuValues.slice(-60)} />} />)
  }
  if (memVal != null) {
    tile('memory', isF5 ? 'System RAM' : 'Memory', <KpiTile label={isF5 ? 'System RAM' : 'Memory'} value={formatUsagePct(memVal)} tone={toneFor(memVal, memTh)}
      sub={isF5 ? 'System used / system total' : memValues.length ? `Max ${formatUsagePct(Math.max(...memValues))}` : undefined}
      trend={trendOf(memValues, 'percent', range.label, memTh?.warn)}
      chart={<Sparkline label="Memory" domain={PERCENT_DOMAIN} threshold={memTh?.crit ?? memTh?.warn} stroke={SERIES_COLORS.accent} format={(v) => `${v.toFixed(0)}%`} values={memValues.slice(-60)} />} />)
  }
  if (isF5) {
    for (const item of [
      { id: 'f5-tmm-memory', title: 'TMM memory', metric: 'f5_tmm_memory_pct', sub: 'Traffic processing RAM' },
      { id: 'f5-host-memory', title: 'Non-TMM memory', metric: 'f5_host_memory_pct', sub: 'Host processes · separate from TMM' },
      { id: 'f5-swap-memory', title: 'Swap usage', metric: 'f5_swap_memory_pct', sub: 'Occupancy · not paging activity' },
    ]) {
      const values = (metrics.data?.[item.metric]?.points || []).map((p) => p.value)
      if (!values.length) continue
      tile(item.id, item.title, <KpiTile label={item.title} value={`${values[values.length - 1].toFixed(1)}%`} sub={item.sub}
        chart={<Sparkline label={item.title} domain={PERCENT_DOMAIN} stroke={SERIES_COLORS.accent} values={values.slice(-60)} />} />)
    }
  }
  for (const h of summary?.kpis.highlights || []) {
    tile(`highlight-${h.kind}`, h.label, <KpiTile label={h.label} value={h.kind === 'poe' ? `${sig3(h.value)} W` : Math.round(h.value).toLocaleString()} sub={h.sub || undefined} />)
  }
  if (snmp && summary?.interfaces) {
    const i = summary.interfaces
    const quiet = i.admin_down + i.unused + i.not_present
    tile('interfaces-up', 'Interfaces', <KpiTile label="Interfaces" value={`${i.up} up`}
      onClick={() => onOpen('interfaces')}
      sub={<>{i.unexpected_down ? <b className="font-medium text-warning">{i.unexpected_down} unexpected down</b> : '0 unexpected down'}{quiet ? ` · ${quiet} admin down or unused` : ''}</>} />)
  }

  const panels: DeviceWidget[] = []
  if (device.ping_enabled) {
    panels.push({ id: 'latency-loss', title: 'Latency and loss', width: 8, height: 10, minWidth: 4, minHeight: 6,
      content: <LatencyLossPanel rtt={rtt} loss={loss} points={points} fromTs={fromTs} toTs={toTs} range={range} rttLimit={rttLimit} lossLimit={lossLimit} onOpenEvents={() => onOpen('events')} loading={ping.isPending} /> })
  }
  panels.push({ id: 'recent-events', title: 'Recent events', width: 4, height: 10, minWidth: 3, minHeight: 5,
    content: <RecentEventsPanel summary={summary} onOpen={() => onOpen('events')} /> })
  if (snmp) {
    panels.push({ id: 'busiest-interfaces', title: 'Busiest interfaces', width: 6, height: 8, minWidth: 3, minHeight: 5,
      content: <BusiestInterfacesPanel summary={summary} onOpen={() => onOpen('interfaces')} /> })
  }
  if (summary?.health.groups.length) {
    panels.push({ id: 'health-groups', title: 'Health groups', width: 6, height: 8, minWidth: 3, minHeight: 5,
      content: <HealthGroupsPanel groups={summary.health.groups} notApplicable={summary.not_applicable} onOpen={onOpen} /> })
  }
  if (netflow.hasNetflow && netflow.overview) {
    panels.push({ id: 'netflow', title: 'NetFlow', width: 12, height: 11, minWidth: 4, minHeight: 7,
      content: <DeviceNetflowCard deviceId={id} exporter={netflow.exporter} qs={netflow.qs} overview={netflow.overview} range={range} /> })
  }

  return (
    <section aria-label="Device summary" className="space-y-3">
      <DeviceWidgetGrid deviceId={id} vendor={device.vendor || undefined} deviceType={device.device_type} widgets={[...tiles, ...panels]} />
    </section>
  )
}

function LatencyLossPanel({ rtt, loss, points, fromTs, toTs, range, rttLimit, lossLimit, onOpenEvents, loading }: {
  rtt: Array<{ ts: number; value: number | null }>
  loss: Array<{ ts: number; value: number | null }>
  points: Parameters<typeof deviceAvailabilityBuckets>[0]
  fromTs: number; toTs: number; range: DeviceRange
  rttLimit: number | null; lossLimit: number | null
  onOpenEvents: () => void
  loading: boolean
}) {
  const rttDomain = latencyDomain(rtt.map((p) => p.value), rttLimit)
  const leftPad = axisWidth(niceTicks(rttDomain[1]).map((v) => `${sig3(v)} ms`))
  const buckets = useMemo(() => deviceAvailabilityBuckets(points, fromTs, toTs), [points, fromTs, toTs])
  return (
    <Panel title="Latency and packet loss" meta={<>{range.label} · dashed lines are the Degraded limits from the status policy</>}>
      {loading ? <div className="flex h-[230px] items-center justify-center text-sm text-muted">Loading checks…</div> : <>
        <TimeSeriesPanel height={150} syncId="device-summary" fromTs={fromTs} toTs={toTs} rangeHours={range.hours} domain={rttDomain}
          format={formatMs} axisFormat={(v) => `${sig3(v)} ms`}
          thresholds={rttLimit != null ? [{ value: rttLimit, label: `limit ${sig3(rttLimit)} ms`, tone: 'warning' }] : []}
          series={[{ key: 'rtt', name: 'Latency', color: SERIES_COLORS.primary, points: rtt }]} />
        <TimeSeriesPanel height={80} syncId="device-summary" fromTs={fromTs} toTs={toTs} rangeHours={range.hours}
          domain={[0, Math.min(100, Math.max(10, (lossLimit ?? 0) * 1.5, ...loss.map((p) => p.value ?? 0)))]} format={formatLossPct} axisFormat={(v) => `${Math.round(v)}%`}
          thresholds={lossLimit != null ? [{ value: lossLimit, label: `limit ${sig3(lossLimit)}%`, tone: 'warning' }] : []}
          series={[{ key: 'loss', name: 'Packet loss', color: SERIES_COLORS.warning, points: loss }]} emptyText="No packet-loss samples" />
        <div className="mt-2" style={{ marginLeft: leftPad, marginRight: 16 }}>
          <button type="button" onClick={onOpenEvents} aria-label="Ping check results across the period; open the events timeline"
            className="flex h-3 w-full gap-px overflow-hidden rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
            {buckets.map((b, i) => (
              <span key={i} className={cn('h-full min-w-0 flex-1', b.state === 'up' ? 'bg-success' : b.state === 'gap' ? 'bg-surface2' : b.state === 'down' ? 'bg-danger' : 'bg-warning')}
                title={`${formatAbsolute(b.start)} – ${formatAbsolute(b.end)}: ${b.state === 'gap' ? 'no measurements' : `${formatDeviceAvailability(b.pct)} of checks answered`}`} />
            ))}
          </button>
          <div className="mt-1 flex flex-wrap gap-x-3 text-xs text-muted">
            <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-success" />all checks answered</span>
            <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-warning" />some failed</span>
            <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-danger" />all failed</span>
            <span><span className="mr-1 inline-block h-2 w-2 rounded-sm border border-border bg-surface2" />no measurements</span>
          </div>
        </div>
      </>}
    </Panel>
  )
}

const EVENT_TONE: Record<ActivityEvent['severity'], string> = {
  critical: 'text-danger', warning: 'text-warning', success: 'text-success', info: 'text-info',
}

export function summaryEvents(summary: DeviceSummary): ActivityEvent[] {
  const now = serverNow()
  const from = new Date(now - 30 * 86400_000).toISOString()
  const to = new Date(now).toISOString()
  const recent = summary.recent
  const events = buildActivityEvents(recent.traps || [], recent.status_changes || [], {}, from, to, (recent.flap_episodes || []) as unknown as FlapEpisode[])
  const alerts: ActivityEvent[] = (summary.alerts?.items || []).filter((a) => a.triggered_at).map((a) => ({
    id: `alert-${a.id}`, timestamp: a.triggered_at!, severity: normalizeSeverity(a.severity), icon: ArrowRight,
    kind: 'alert', title: a.message, subtitle: `Alert · ${a.status}`,
  }))
  return [...alerts, ...events].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))
}

function RecentEventsPanel({ summary, onOpen }: { summary?: DeviceSummary; onOpen: () => void }) {
  const events = summary ? summaryEvents(summary).slice(0, 6) : []
  return (
    <Panel title="Recent events" meta="Latest 30 days" actions={<button type="button" onClick={onOpen} className="inline-flex items-center gap-1 font-medium text-primary hover:underline">View timeline<ArrowRight className="h-3 w-3" /></button>}>
      {!summary ? <p className="text-sm text-muted">Loading…</p> : events.length === 0 ? (
        <p className="text-sm text-muted">No status changes, traps or alerts in the last 30 days.</p>
      ) : (
        <ol className="space-y-2.5">
          {events.map((e) => {
            const Icon = e.icon
            return (
              <li key={e.id} className="grid grid-cols-[16px_minmax(0,1fr)_auto] items-start gap-2 text-sm">
                <Icon className={cn('mt-0.5 h-4 w-4', EVENT_TONE[e.severity])} aria-label={e.severity} />
                <div className="min-w-0">
                  <div className="break-words font-medium">{e.title}</div>
                  {e.subtitle && <div className="break-words text-xs text-muted">{e.subtitle}</div>}
                </div>
                <time className="shrink-0 text-xs text-muted" dateTime={e.timestamp} title={formatAbsolute(e.timestamp, { seconds: true })}>{formatRelative(e.timestamp)}</time>
              </li>
            )
          })}
        </ol>
      )}
    </Panel>
  )
}

function BusiestInterfacesPanel({ summary, onOpen }: { summary?: DeviceSummary; onOpen: () => void }) {
  const rows = summary?.busiest_interfaces
  const unavailable = summary?.unavailable.includes('interface_rates')
  return (
    <Panel title="WAN and busiest interfaces" meta="Latest sample · WAN links first"
      actions={<button type="button" onClick={onOpen} className="inline-flex items-center gap-1 font-medium text-primary hover:underline">All interfaces<ArrowRight className="h-3 w-3" /></button>}>
      {!summary ? <p className="text-sm text-muted">Loading…</p> : unavailable ? <p className="text-sm text-warning">Interface rates are temporarily unavailable.</p> : !rows?.length ? (
        <p className="text-sm text-muted">No interface traffic in the last 15 minutes.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs text-muted">
              <tr className="border-b border-border"><th className="py-1.5 pr-2 text-left font-medium">Interface</th><th className="px-2 text-right font-medium">Speed</th><th className="px-2 text-right font-medium">In</th><th className="px-2 text-right font-medium">Out</th><th className="pl-2 text-left font-medium">Utilisation</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.if_index} className="border-b border-border/50 last:border-0">
                  <td className="py-1.5 pr-2"><span className="font-medium">{r.name}</span>{r.alias && <span className="text-muted"> · {r.alias}</span>}{r.wan && <span className="ml-1.5 rounded bg-surface2 px-1 text-xs text-muted">WAN</span>}</td>
                  <td className="whitespace-nowrap px-2 text-right tabular-nums text-muted">{formatLinkSpeed(r.speed_bps)}</td>
                  <td className="whitespace-nowrap px-2 text-right tabular-nums">{formatBitRate(r.in_bps)}</td>
                  <td className="whitespace-nowrap px-2 text-right tabular-nums">{formatBitRate(r.out_bps)}</td>
                  <td className="w-32 pl-2"><div className="flex items-center gap-2"><UsageBar pct={r.util_pct} warn={70} crit={90} /><span className="w-10 shrink-0 text-right text-xs tabular-nums">{r.util_pct == null ? MISSING : formatUsagePct(r.util_pct)}</span></div></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function HealthGroupsPanel({ groups, notApplicable, onOpen }: {
  groups: HealthGroupDigest[]
  notApplicable: DeviceSummary['not_applicable']
  onOpen: (tab: DeviceTabId, group?: string) => void
}) {
  const rank: Record<string, number> = { crit: 0, warn: 1, info: 3, ok: 2, none: 4, na: 5 }
  const sorted = [...groups].sort((a, b) => (rank[a.status] ?? 4) - (rank[b.status] ?? 4))
  return (
    <Panel title="Health groups" meta="Template readings · attention first">
      <div className="device-health-summary">
        {sorted.map((g) => (
          <button key={g.key} type="button" onClick={() => onOpen('metrics', g.key)}
            className={cn('min-w-0 rounded-lg border border-border p-2.5 text-left transition-colors hover:border-primary/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary', g.status === 'na' && 'opacity-70')}>
            <div className="flex items-center gap-1.5 text-sm font-medium">
              {g.status === 'na' ? <CircleSlash className="h-3 w-3 shrink-0 text-muted" aria-hidden /> : <span className={cn('h-2 w-2 shrink-0 rounded-full', STATUS_DOT[g.status] || STATUS_DOT.none)} aria-hidden />}
              <span className="min-w-0 break-words">{g.name}</span>
              <span className="sr-only">{g.status === 'crit' ? 'critical' : g.status === 'warn' ? 'warning' : g.status === 'na' ? 'not applicable' : g.status === 'ok' ? 'healthy' : ''}</span>
            </div>
            <div className="mt-0.5 break-words text-xs text-muted">{g.status === 'na' ? `Not applicable · ${g.summary}` : g.summary || 'No readings'}</div>
          </button>
        ))}
      </div>
      {notApplicable.length > 0 && <p className="mt-2 text-xs text-muted">Not applicable groups are left out of health.</p>}
    </Panel>
  )
}
