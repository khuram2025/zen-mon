/**
 * Executive dashboards for the Availability page — the visual layer over the
 * shared assessment model (model.ts). Every figure here comes from the same
 * report payloads and assessment as the brief and the CSV export; this file
 * only decides how to draw them.
 *
 *   Devices  — availability gauge + error budget, KPI tiles with sparklines,
 *              availability & outage activity, fleet state donut, heatmap,
 *              site tiles, segment bullets, downtime share, leaderboard,
 *              time-to-recover distribution and the outage timeline.
 *   Services — compliance donut, availability vs target, error-budget burn,
 *              confirmed downtime over time, business groups, p95 response.
 *   Servers  — current health snapshot (no period availability exists).
 */
import { useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Skeleton } from '@/components/ui/Skeleton'
import type { BusinessData, ComplianceState, ExecutiveData } from '@/hooks/useReports'
import type { ServerMonitoringOverview } from '@/types/servers'
import {
  fmtAvail, fmtDuration, fmtPp, fmtShare, fmtTarget, parseTs, plural,
} from './format'
import type { ComplianceCounts, DeviceSummary, ServiceRow, ServiceSummary } from './model'
import { rankServices } from './model'
import { IncidentTimeline, complianceMeta } from './charts'
import { EvidenceEmpty, Panel, StatusPill } from './ui'
import {
  AvailabilityActivityChart, AvailabilityHeatmap, BandLegend, BudgetMeter, BulletList, CategoryBars, ColumnChart,
  Donut, DonutLegend, HealthTiles, KpiTile, RankBars, RingGauge, Sparkline, StackedBar, availDomain, budgetUsed, episodeActivity, useVizColors, type DonutSegment,
} from './viz'
import {
  PressureList, TopRisksPanel, deviceTimelineItems, isUnassigned, serverSegments, serviceTimelineItems,
  type ViewCommon,
} from './views'

/* ── Shared bits ───────────────────────────────────────────────────────── */

function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T
  options: Array<{ key: T; label: string }>
  onChange: (v: T) => void
  label: string
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-lg border border-border bg-surface2/60 p-0.5">
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          aria-pressed={value === o.key}
          onClick={() => onChange(o.key)}
          className={cn(
            'min-h-[32px] rounded-md px-2.5 text-sm font-medium transition-colors',
            value === o.key ? 'bg-surface text-text shadow-sm ring-1 ring-border' : 'text-muted hover:text-text',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

function Hero({ children }: { children: ReactNode }) {
  return <div className="grid gap-4 xl:grid-cols-12">{children}</div>
}

function HeroCard({ title, subtitle, children, className }: { title: string; subtitle?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cn(
      'relative flex flex-col overflow-hidden rounded-xl border border-border bg-surface p-4 shadow-card dark:shadow-card-dark xl:col-span-4',
      'bg-[radial-gradient(120%_80%_at_50%_0%,rgb(var(--primary)/0.08),transparent_60%)]',
      className,
    )}>
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold text-text">{title}</h2>
          {subtitle && <p className="text-sm text-muted">{subtitle}</p>}
        </div>
      </div>
      {children}
    </section>
  )
}

function Row({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('grid gap-4 xl:grid-cols-12', className)}>{children}</div>
}

function Loading({ h = 'h-60' }: { h?: string }) {
  return <Skeleton className={cn('w-full', h)} />
}

/** Compact duration for tile headlines ("143d 8h" → value + unit). */
function durationParts(min: number | null | undefined): { value: string; unit?: string } {
  if (min == null) return { value: '—' }
  if (min <= 0) return { value: '0', unit: 'min' }
  if (min < 60) return { value: min < 10 ? min.toFixed(1).replace(/\.0$/, '') : String(Math.round(min)), unit: 'min' }
  if (min < 48 * 60) return { value: (min / 60).toFixed(min < 600 ? 1 : 0).replace(/\.0$/, ''), unit: 'hours' }
  return { value: (min / 1440).toFixed(min < 14400 ? 1 : 0).replace(/\.0$/, ''), unit: 'days' }
}

function toneOfVerdict(v: boolean | null | undefined): 'met' | 'breach' | 'unknown' {
  return v == null ? 'unknown' : v ? 'met' : 'breach'
}

function deltaText(pp: number | null | undefined): { text: string; cls: string } | null {
  const t = fmtPp(pp)
  if (!t || pp == null) return null
  return { text: `${t} vs previous period`, cls: Math.abs(pp) < 0.005 ? 'text-muted' : pp > 0 ? 'text-success' : 'text-danger' }
}

/* ── Devices ───────────────────────────────────────────────────────────── */

type SegmentKey = 'site' | 'group' | 'type'

export function DevicesDashboard({
  c, exec, live, brief,
}: {
  c: ViewCommon
  exec?: ExecutiveData
  live?: DeviceSummary
  brief: ReactNode
}) {
  const colors = useVizColors()
  const { assessment, fromMs, toMs, loading, dimmed, large, partial, periodLabel } = c
  const target = assessment.target
  const k = exec?.kpis
  const avail = k?.availability_pct ?? null
  const measured = avail != null && (k?.devices_measured ?? 1) > 0
  const verdict = measured ? (k?.sla_met ?? (avail as number) >= (target ?? 99.9)) : null
  const legacy = !!k && k.outage_episodes === undefined
  const activity = exec?.outage_activity ?? null
  const trend = exec?.availability_trend ?? []
  const status = exec?.status_now ?? (live ? { ...live } : null)
  const [segment, setSegment] = useState<SegmentKey>('group')
  const [shareBy, setShareBy] = useState<'group' | 'type' | 'site'>('group')
  const dim = dimmed ? 'opacity-60' : ''

  const statusSegments: DonutSegment[] = status ? [
    { key: 'up', label: 'Up', value: status.up, color: colors.c('success'), to: '/devices?status=up' },
    { key: 'degraded', label: 'Degraded', value: status.degraded, color: colors.c('warning'), to: '/devices?status=degraded' },
    { key: 'down', label: 'Down', value: status.down, color: colors.c('danger'), to: '/devices?status=down' },
    { key: 'maintenance', label: 'Maintenance', value: status.maintenance, color: colors.c('accent'), to: '/devices?status=maintenance' },
    { key: 'unknown', label: 'Unknown', value: status.unknown, color: colors.c('muted'), hatch: true, to: '/devices?status=unknown' },
  ] : []

  const segRows = useMemo(() => {
    const src = segment === 'site'
      ? (exec?.location_summary || []).map((r) => ({ name: r.location, devices: r.devices, down: r.down, availability: r.availability_pct, measured: r.measured ?? null, downtime: r.downtime_minutes ?? null }))
      : ((segment === 'group' ? exec?.group_summary : exec?.type_summary) || []).map((r) => ({ name: r.name, devices: r.devices, down: r.down_now, availability: r.availability_pct, measured: r.measured, downtime: r.downtime_minutes ?? null }))
    return src.sort((a, b) =>
      (a.availability ?? 101) - (b.availability ?? 101) || b.devices - a.devices)
  }, [exec, segment])

  const tiles = useMemo(() => (exec?.location_summary || [])
    .map((r) => ({
      key: r.location || '__none__',
      name: isUnassigned(r.location) ? 'Not assigned' : r.location,
      unassigned: isUnassigned(r.location),
      availability: r.availability_pct,
      devices: r.devices,
      measured: r.measured ?? null,
      downNow: r.down,
      downtimeMin: r.downtime_minutes ?? null,
    }))
    .sort((a, b) => Number(a.unassigned) - Number(b.unassigned) || b.downNow - a.downNow || (a.availability ?? 101) - (b.availability ?? 101)),
  [exec])

  const shareSegments: DonutSegment[] = useMemo(() => {
    const src = shareBy === 'site'
      ? (exec?.location_summary || []).map((r) => ({ name: r.location, v: r.downtime_minutes ?? 0 }))
      : ((shareBy === 'group' ? exec?.group_summary : exec?.type_summary) || []).map((r) => ({ name: r.name, v: r.downtime_minutes ?? 0 }))
    const palette = ['danger', 'warning', 'amber', 'accent', 'primary', 'info'] as const
    const sorted = src.filter((r) => r.v > 0).sort((a, b) => b.v - a.v)
    const head = sorted.slice(0, 5)
    const rest = sorted.slice(5).reduce((s, r) => s + r.v, 0)
    const segs: DonutSegment[] = head.map((r, i) => ({ key: r.name, label: isUnassigned(r.name) ? 'Not assigned' : r.name, value: Math.round(r.v), color: colors.c(palette[i]) }))
    if (rest > 0) segs.push({ key: '__other__', label: `${sorted.length - 5} more`, value: Math.round(rest), color: colors.c('muted') })
    return segs
  }, [exec, shareBy, colors])
  const shareReported = (exec?.group_summary || []).some((r) => r.downtime_minutes !== undefined)

  const leaderboard = useMemo(() => {
    const rows = exec?.downtime_by_device ?? (exec?.top_issues || [])
      .filter((i) => (i.category ?? 'outage') !== 'alert_noise')
      .map((i) => ({ device_id: i.device_id, hostname: i.hostname, downtime_minutes: i.downtime_minutes ?? i.duration_minutes, outages: i.outages ?? 0, ongoing: i.severity === 'critical', availability_pct: i.availability_pct ?? null, location: null as string | null | undefined }))
    return rows.slice(0, 8).map((r) => ({
      key: r.device_id,
      label: r.hostname,
      sub: [r.location && !isUnassigned(r.location) ? r.location : null, `${plural(r.outages, 'outage')}`, r.availability_pct != null ? `${fmtAvail(r.availability_pct)} available` : null].filter(Boolean).join(' · '),
      value: r.downtime_minutes ?? 0,
      display: fmtDuration(r.downtime_minutes),
      color: r.ongoing ? colors.c('danger') : colors.c('warning'),
      to: `/devices/${r.device_id}`,
      badge: r.ongoing ? <span className="rounded-full bg-danger/15 px-1.5 text-xs font-semibold text-danger">Ongoing</span> : null,
    }))
  }, [exec, colors])

  const timeline = useMemo(() => deviceTimelineItems(exec, 12), [exec])
  const outageSpark = activity?.map((a) => a.outages_started) ?? []
  const downtimeSpark = activity?.map((a) => a.downtime_minutes) ?? []
  const hist = exec?.recovery_histogram ?? null
  const cov = exec?.coverage
  const measuredN = cov?.measured_entities ?? k?.devices_measured ?? null
  const expectedN = cov?.expected_entities ?? k?.devices_expected ?? null
  const delta = deltaText(k?.availability_delta_pp ?? k?.availability_delta_pct)
  const ongoing = k?.ongoing_outages ?? 0
  const downtime = durationParts(k?.outage_downtime_minutes)
  const mttr = durationParts(k?.mttr_minutes)
  const used = budgetUsed(avail, target)

  return (
    <div className="flex flex-col gap-4">
      <Hero>
        <HeroCard title="Device availability" subtitle={`${periodLabel} · ping reachability, maintenance excluded`} className={dim}>
          {loading ? <Loading h="h-64" /> : (
            <>
              <div className="mt-2"><RingGauge value={avail} target={target} caption="of ping samples succeeded" size={large ? 250 : 216} /></div>
              <div className="-mt-1 flex flex-wrap items-center justify-center gap-2">
                <StatusPill tone={toneOfVerdict(verdict)}>
                  {verdict == null ? 'Not measured' : verdict ? 'Target met' : 'Below target'}
                </StatusPill>
                {delta && <span className={cn('text-sm font-medium tabular-nums', delta.cls)}>{delta.text}</span>}
              </div>
              <div className="mt-4"><BudgetMeter avail={avail} target={target} /></div>
              <p className="mt-2 text-xs text-muted">
                {used != null && used > 1
                  ? `Unavailability is ${used >= 10 ? Math.round(used).toLocaleString() : used.toFixed(1)}× what the ${fmtTarget(target)} target allows.`
                  : used != null
                    ? `${((1 - used) * 100).toFixed(0)}% of the downtime allowed by the ${fmtTarget(target)} target remains.`
                    : 'No availability evidence in this period.'}
              </p>
            </>
          )}
        </HeroCard>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:col-span-8">
          {loading ? Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-[168px] w-full rounded-xl" />) : (
            <>
              <KpiTile
                label={legacy ? 'Alerts raised' : 'Confirmed outages'}
                value={(k?.outage_episodes ?? k?.incidents_count ?? 0).toLocaleString()}
                tone={ongoing > 0 ? 'breach' : (k?.outage_episodes ?? 0) > 0 ? 'risk' : 'met'}
                chip={legacy ? { text: 'Legacy count', tone: 'unknown' } : ongoing > 0 ? { text: `${ongoing} ongoing`, tone: 'breach' } : { text: (k?.outage_episodes ?? 0) ? 'All recovered' : 'None', tone: (k?.outage_episodes ?? 0) ? 'risk' : 'met' }}
                sub={k?.incidents_delta != null ? `${k.incidents_delta > 0 ? '+' : ''}${k.incidents_delta} vs previous period` : 'Down-to-recovery episodes'}
                visual={outageSpark.length > 1 ? <Sparkline values={outageSpark} color={colors.c('warning')} kind="bars" height={40} /> : undefined}
                to="/devices?status=down"
                title="Distinct down-to-recovery episodes intersecting the period; repeated alerts for one outage count once."
                dimmed={dimmed}
              />
              <KpiTile
                label="Device downtime"
                value={downtime.value}
                unit={downtime.unit}
                tone={(k?.outage_downtime_minutes ?? 0) > 0 ? 'breach' : 'met'}
                chip={k?.maintenance_minutes ? { text: `${fmtDuration(k.maintenance_minutes)} maint. excluded`, tone: 'neutral' } : null}
                sub="Summed device-time down in the period"
                visual={downtimeSpark.length > 1 ? <Sparkline values={downtimeSpark} color={colors.c('danger')} height={40} min={0} /> : undefined}
                title="Sum over devices of confirmed outage time inside the period; planned maintenance subtracted."
                dimmed={dimmed}
              />
              <KpiTile
                label="Mean time to recover"
                value={k?.mttr_minutes == null ? '—' : mttr.value}
                unit={k?.mttr_minutes == null ? undefined : mttr.unit}
                tone={k?.mttr_minutes == null ? 'unknown' : 'neutral'}
                chip={k?.mttr_delta_minutes != null
                  ? { text: `${k.mttr_delta_minutes > 0 ? '+' : '−'}${fmtDuration(Math.abs(k.mttr_delta_minutes))}`, tone: k.mttr_delta_minutes > 0 ? 'breach' : 'met' }
                  : null}
                sub={k?.mttr_minutes == null ? 'No outage recovered in the period' : `Over ${plural(hist?.reduce((s, b) => s + b.count, 0) ?? 0, 'recovered outage')}`}
                visual={hist?.some((b) => b.count) ? <Sparkline values={hist.map((b) => b.count)} color={colors.c('primary')} kind="bars" height={40} /> : undefined}
                title="Mean duration of outages that recovered inside the period. Chip: change vs the previous period."
                dimmed={dimmed}
              />
              <KpiTile
                label="Devices down now"
                value={status ? status.down.toLocaleString() : '—'}
                unit={status ? `of ${status.total}` : undefined}
                tone={!status ? 'unknown' : status.down > 0 ? 'breach' : 'met'}
                chip={status?.degraded ? { text: `${status.degraded} degraded`, tone: 'risk' } : null}
                sub="Current state, independent of the period"
                visual={status ? <StackedBar parts={statusSegments.map((s) => ({ key: s.key, value: s.value, color: s.color, label: s.label, hatch: s.hatch }))} /> : undefined}
                to="/devices?status=down"
              />
              <KpiTile
                label="Active critical alerts"
                value={(k?.active_critical_now ?? k?.active_critical_count ?? 0).toLocaleString()}
                tone={(k?.active_critical_now ?? k?.active_critical_count ?? 0) > 0 ? 'breach' : 'met'}
                chip={(k?.active_critical_now ?? 0) > 0 ? { text: 'Needs action', tone: 'breach' } : { text: 'None active', tone: 'met' }}
                sub={k?.critical_raised_in_period != null ? `${plural(k.critical_raised_in_period, 'critical alert')} raised in ${c.period}` : 'Active now'}
                visual={k?.alerts_count != null ? (
                  <div className="text-xs text-muted">
                    <StackedBar
                      parts={[
                        { key: 'crit', value: k.critical_raised_in_period ?? 0, color: colors.c('danger'), label: 'Critical raised' },
                        { key: 'other', value: Math.max(0, k.alerts_count - (k.critical_raised_in_period ?? 0)), color: colors.c('muted', 0.5), label: 'Other alerts' },
                      ]}
                      height={8}
                    />
                    <div className="mt-1">{plural(k.alerts_count, 'alert')} in period · alerts are not outages</div>
                  </div>
                ) : undefined}
                to="/alerts?severity=critical"
              />
              <KpiTile
                label="Evidence coverage"
                value={measuredN != null && expectedN != null ? `${measuredN}/${expectedN}` : '—'}
                unit="devices"
                tone={measuredN == null ? 'unknown' : measuredN < (expectedN ?? 0) || (cov?.covered_pct ?? 100) < 90 ? 'risk' : 'met'}
                chip={cov?.covered_pct != null ? { text: `${fmtShare(cov.covered_pct)} of period`, tone: cov.covered_pct < 90 ? 'risk' : 'neutral' } : null}
                sub="Devices with at least one sample"
                visual={(
                  <div className="space-y-1.5">
                    <MiniMeter label="Devices measured" value={measuredN != null && expectedN ? (measuredN / expectedN) * 100 : null} />
                    <MiniMeter label="Period span covered" value={cov?.covered_pct ?? null} />
                  </div>
                )}
              />
            </>
          )}
        </div>
      </Hero>

      {brief}

      <Row>
        <Panel
          className={cn('xl:col-span-8', dim)}
          title="Availability & outage activity"
          subtitle={`Fleet availability per interval with devices down beneath · ${periodLabel}`}
        >
          {loading ? <Loading h="h-[340px]" /> : trend.some((p) => p.availability_pct != null) || activity?.some((a) => a.entities_down) ? (
            <>
              <AvailabilityActivityChart trend={trend} activity={activity} target={target} fromMs={fromMs} toMs={toMs} height={large ? 420 : 340} />
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted">
                <BandLegend target={target} className="gap-x-3" />
                {activity && (
                  <>
                    <span className="inline-flex items-center gap-1.5"><span className="h-3 w-2 rounded-sm bg-danger/80" />Devices down</span>
                    <span className="inline-flex items-center gap-1.5"><span className="text-[10px] leading-none" style={{ color: colors.c('amber') }}>▲</span>Outages started</span>
                  </>
                )}
              </div>
            </>
          ) : (
            <EvidenceEmpty kind="no_evidence">No availability evidence was recorded in this period. It is shown as unknown, not as 0% or as healthy.</EvidenceEmpty>
          )}
        </Panel>
        <Panel className="xl:col-span-4" title="Fleet status now" subtitle="Current state — independent of the period">
          {!status ? <Loading h="h-56" /> : status.total === 0 ? <EvidenceEmpty kind="no_evidence">No devices in inventory.</EvidenceEmpty> : (
            <div className="flex flex-col items-center gap-4 sm:flex-row xl:flex-col 2xl:flex-row">
              <Donut segments={statusSegments} centerValue={`${Math.round((status.up / status.total) * 100)}%`} centerLabel={`${status.up} of ${status.total} up`} size={large ? 200 : 176} />
              <DonutLegend segments={statusSegments} />
            </div>
          )}
        </Panel>
      </Row>

      <Row>
        <Panel
          className={cn('xl:col-span-8', dim)}
          title="Availability heatmap"
          subtitle={`When availability was lost · ${trend.length && Date.parse(trend[trend.length - 1].ts) - Date.parse(trend[0].ts) > 20 * 3_600_000 ? 'rows are days, columns are time of day (browser time)' : 'one cell per interval'}`}
        >
          {loading ? <Loading h="h-52" /> : trend.length ? (
            <>
              <AvailabilityHeatmap points={trend} target={target} />
              <BandLegend target={target} className="mt-2" />
            </>
          ) : <EvidenceEmpty kind="no_evidence">No availability intervals reported for this period.</EvidenceEmpty>}
        </Panel>
        <TopRisksPanel c={c} className="xl:col-span-4" emptyText="No devices are down and no confirmed outages were recorded in the period." />
      </Row>

      <Row className={dim}>
        <Panel
          className="xl:col-span-6"
          title="Availability by segment"
          subtitle={`Same period and denominator as the headline · worst first`}
          actions={<Segmented label="Segment" value={segment} onChange={setSegment} options={[{ key: 'group', label: 'Group' }, { key: 'type', label: 'Type' }, { key: 'site', label: 'Site' }]} />}
        >
          {loading ? <Loading /> : (
            <div className="max-h-[420px] overflow-y-auto pr-1">
              <BulletList
                domain={availDomain(segRows.map((r) => r.availability), target)}
                empty={<EvidenceEmpty kind={partial ? 'partial' : 'no_evidence'}>{segment === 'site' ? 'No site assignments are reported.' : 'This breakdown is not reported by this appliance version.'}</EvidenceEmpty>}
                rows={segRows.map((r) => ({
                  key: r.name || '__none__',
                  label: isUnassigned(r.name) ? 'Not assigned' : r.name,
                  muted: isUnassigned(r.name),
                  sub: `${r.devices} device${r.devices === 1 ? '' : 's'}${r.measured != null && r.measured < r.devices ? ` · ${r.measured} measured` : ''}`,
                  value: r.availability,
                  target,
                  right: r.down > 0
                    ? <span className="rounded-full bg-danger/12 px-1.5 text-xs font-semibold text-danger">{r.down} down</span>
                    : r.downtime ? <span className="text-xs text-muted">{fmtDuration(r.downtime)} down</span> : null,
                }))}
              />
            </div>
          )}
        </Panel>
        <Panel className="xl:col-span-6" title="Site health map" subtitle={`Availability for ${c.period} · badge = devices down now`}>
          {loading ? <Loading /> : (
            <div className="max-h-[440px] overflow-y-auto pr-1">
              <HealthTiles
                tiles={tiles}
                target={target}
                empty={<EvidenceEmpty kind={partial ? 'partial' : 'no_evidence'}>No site assignments are reported.</EvidenceEmpty>}
              />
            </div>
          )}
        </Panel>
      </Row>

      <Row className={dim}>
        <Panel className="xl:col-span-4" title="Downtime leaderboard" subtitle={`Devices with the most confirmed downtime · ${periodLabel}`}>
          {loading ? <Loading /> : (
            <RankBars
              rows={leaderboard}
              empty={partial
                ? <EvidenceEmpty kind="partial">No downtime in the data that loaded; some sources failed.</EvidenceEmpty>
                : measured ? <EvidenceEmpty kind="no_findings">No confirmed device downtime in this period.</EvidenceEmpty>
                  : <EvidenceEmpty kind="no_evidence">No evidence for this period.</EvidenceEmpty>}
            />
          )}
        </Panel>
        <Panel
          className="xl:col-span-4"
          title="Where downtime occurred"
          subtitle="Share of confirmed device downtime"
          actions={<Segmented label="Downtime share by" value={shareBy} onChange={setShareBy} options={[{ key: 'group', label: 'Group' }, { key: 'type', label: 'Type' }, { key: 'site', label: 'Site' }]} />}
        >
          {loading ? <Loading /> : !shareReported ? (
            <EvidenceEmpty kind="no_evidence">Downtime by segment is not reported by this appliance version.</EvidenceEmpty>
          ) : shareSegments.length ? (
            <div className="flex flex-col items-center gap-4">
              <Donut
                segments={shareSegments}
                centerValue={durationParts(k?.outage_downtime_minutes).value}
                centerLabel={`${durationParts(k?.outage_downtime_minutes).unit ?? ''} device downtime`}
                size={168}
              />
              <DonutLegend segments={shareSegments} format={(v) => fmtDuration(v)} />
            </div>
          ) : <EvidenceEmpty kind="no_findings">No confirmed device downtime in this period.</EvidenceEmpty>}
        </Panel>
        <Panel className="xl:col-span-4" title="Time to recover" subtitle="Outages that recovered in the period, by duration">
          {loading ? <Loading /> : !hist ? (
            <EvidenceEmpty kind="no_evidence">Recovery distribution is not reported by this appliance version.</EvidenceEmpty>
          ) : hist.every((b) => !b.count) ? (
            <EvidenceEmpty kind={ongoing ? 'partial' : 'no_findings'}>{ongoing ? `No outage recovered in the period; ${plural(ongoing, 'outage')} still ongoing.` : 'No outages recovered in this period.'}</EvidenceEmpty>
          ) : (
            <>
              <ColumnChart bins={hist} color={colors.c('primary')} caption="Time to recover" />
              <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
                <div className="rounded-lg bg-surface2/70 px-3 py-2">
                  <div className="text-xs text-muted">Mean time to recover</div>
                  <div className="font-semibold tabular-nums text-text">{fmtDuration(k?.mttr_minutes, '—')}</div>
                </div>
                <div className="rounded-lg bg-surface2/70 px-3 py-2">
                  <div className="text-xs text-muted">Still ongoing</div>
                  <div className={cn('font-semibold tabular-nums', ongoing ? 'text-danger' : 'text-text')}>{plural(ongoing, 'outage')}</div>
                </div>
              </div>
            </>
          )}
        </Panel>
      </Row>

      <Panel
        className={dim}
        title="Outage timeline"
        subtitle={`Confirmed device outages, start to recovery · most recent ${Math.min(12, exec?.outage_timeline?.length ?? 0)} of ${(k?.outage_episodes ?? exec?.outage_timeline?.length ?? 0).toLocaleString()}`}
        actions={<Link to="/availability?mode=operations" className="inline-flex min-h-[36px] items-center gap-1 rounded-lg border border-border px-2.5 text-sm text-primary hover:bg-primary/5">Full history <ArrowRight className="h-3.5 w-3.5" /></Link>}
      >
        {loading ? <Loading h="h-40" /> : (
          <IncidentTimeline
            items={timeline}
            fromMs={fromMs}
            toMs={toMs}
            empty={partial
              ? <EvidenceEmpty kind="partial">No outages in the data that loaded; some sources failed.</EvidenceEmpty>
              : assessment.state === 'not_measured'
                ? <EvidenceEmpty kind="no_evidence">No outage evidence: nothing was measured in this period.</EvidenceEmpty>
                : <EvidenceEmpty kind="no_findings">No confirmed outages were recorded in this period.</EvidenceEmpty>}
          />
        )}
      </Panel>
    </div>
  )
}

function MiniMeter({ label, value }: { label: string; value: number | null }) {
  const colors = useVizColors()
  const color = value == null ? colors.c('muted') : value >= 90 ? colors.c('success') : colors.c('warning')
  return (
    <div>
      <div className="flex justify-between text-xs text-muted"><span>{label}</span><span className="tabular-nums">{fmtShare(value, 'n/a')}</span></div>
      <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-surface3">
        <div className="h-full rounded-full" style={{ width: `${Math.max(0, Math.min(100, value ?? 0))}%`, background: color }} />
      </div>
    </div>
  )
}

/* ── Services ──────────────────────────────────────────────────────────── */

export function ServicesDashboard({
  c, business, rows, counts, live, brief,
}: {
  c: ViewCommon
  business?: BusinessData
  rows: ServiceRow[]
  counts: ComplianceCounts
  live?: ServiceSummary
  brief: ReactNode
}) {
  const colors = useVizColors()
  const { fromMs, toMs, loading, dimmed, partial, assessment, periodLabel, large } = c
  const dim = dimmed ? 'opacity-60' : ''
  const [filter, setFilter] = useState<ComplianceState | null>(null)
  const ranked = useMemo(() => rankServices(rows), [rows])
  const measuredRows = rows.filter((r) => r.availability != null)
  const measuredEligible = counts.met + counts.at_risk + counts.breached

  const compSegments: DonutSegment[] = [
    { key: 'met', label: 'Met', value: counts.met, color: colors.c('success') },
    { key: 'at_risk', label: 'At risk', value: counts.at_risk, color: colors.c('amber') },
    { key: 'breached', label: 'Breached', value: counts.breached, color: colors.c('danger') },
    { key: 'not_assessed', label: 'Not assessed', value: counts.not_assessed, color: colors.c('muted'), hatch: true },
  ]
  const liveSegments: DonutSegment[] = live ? [
    { key: 'up', label: 'Up', value: live.up, color: colors.c('success'), to: '/services' },
    { key: 'warning', label: 'Warning / degraded', value: (live.warning ?? 0) + (live.degraded ?? 0), color: colors.c('warning'), to: '/services' },
    { key: 'down', label: 'Down', value: live.down, color: colors.c('danger'), to: '/services' },
    { key: 'unknown', label: 'Unknown', value: live.unknown, color: colors.c('muted'), hatch: true, to: '/services' },
  ] : []

  const episodes = useMemo(() => (business?.service_outages || []).map((o) => ({
    id: o.service_check_id,
    startMs: parseTs(o.started_at),
    endMs: parseTs(o.ended_at ?? null),
    ongoing: !!o.ongoing,
  })), [business])
  const activity = useMemo(() => episodeActivity(episodes, fromMs, toMs), [episodes, fromMs, toMs])
  const ongoing = episodes.filter((e) => e.ongoing).length

  const plotted = filter ? ranked.filter((r) => r.compliance === filter) : ranked
  const burn = ranked
    .filter((r) => r.errorBudgetUsed != null)
    .sort((a, b) => (b.errorBudgetUsed ?? 0) - (a.errorBudgetUsed ?? 0))
    .slice(0, 8)

  const groups = useMemo(() => {
    const map = new Map<string, { name: string; unassigned: boolean; total: number; met: number; at_risk: number; breached: number; not_assessed: number; down: number; downtime: number }>()
    for (const r of rows) {
      const key = r.group ?? '__none__'
      const cur = map.get(key) ?? { name: r.group ?? 'Not assigned', unassigned: !r.group, total: 0, met: 0, at_risk: 0, breached: 0, not_assessed: 0, down: 0, downtime: 0 }
      cur.total += 1
      cur[r.compliance] += 1
      if (r.status === 'down') cur.down += 1
      cur.downtime += r.downtimeMin ?? 0
      map.set(key, cur)
    }
    return [...map.values()].sort((a, b) => Number(a.unassigned) - Number(b.unassigned) || b.breached - a.breached || b.total - a.total)
  }, [rows])

  const p95 = (business?.response_time_quantiles || [])
    .filter((r) => r.p95_ms != null)
    .sort((a, b) => (b.p95_ms ?? 0) - (a.p95_ms ?? 0))
    .slice(0, 8)
  const timeline = useMemo(() => serviceTimelineItems(business, 12, fromMs), [business, fromMs])
  const downtime = durationParts(business?.confirmed_downtime_minutes)

  return (
    <div className="flex flex-col gap-4">
      <Hero>
        <HeroCard title="Target compliance" subtitle={`${periodLabel} · ${assessment.targetLabel}`} className={dim}>
          {loading ? <Loading h="h-64" /> : (
            <div className="mt-3 flex flex-col items-center gap-3">
              <Donut
                segments={compSegments}
                centerValue={measuredEligible ? `${counts.met}/${measuredEligible}` : '—'}
                centerLabel={measuredEligible ? 'measured services met target' : 'Not assessed'}
                active={filter}
                onSelect={(k) => setFilter((f) => (f === k ? null : k as ComplianceState))}
                size={large ? 220 : 196}
                thickness={24}
              />
              <DonutLegend segments={compSegments} active={filter} onSelect={(k) => setFilter((f) => (f === k ? null : k as ComplianceState))} />
              <p className="text-xs text-muted">Select a state to filter the services chart.</p>
            </div>
          )}
        </HeroCard>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:col-span-8">
          {loading ? Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-[168px] w-full rounded-xl" />) : (
            <>
              <KpiTile
                label="Confirmed downtime"
                value={business?.confirmed_downtime_minutes === undefined ? '—' : downtime.value}
                unit={downtime.unit}
                tone={(business?.confirmed_downtime_minutes ?? 0) > 0 ? 'breach' : measuredRows.length ? 'met' : 'unknown'}
                chip={ongoing ? { text: `${ongoing} ongoing`, tone: 'breach' } : null}
                sub="Time with ≥1 service down, overlaps merged"
                visual={<Sparkline values={activity.map((a) => a.downtime_minutes)} color={colors.c('danger')} height={40} min={0} />}
              />
              <KpiTile
                label="Outage episodes"
                value={episodes.length.toLocaleString()}
                tone={ongoing ? 'breach' : episodes.length ? 'risk' : 'met'}
                chip={episodes.length ? { text: ongoing ? 'Active' : 'All recovered', tone: ongoing ? 'breach' : 'risk' } : { text: 'None', tone: 'met' }}
                sub="Confirmed down-to-recovery episodes"
                visual={<Sparkline values={activity.map((a) => a.outages_started)} color={colors.c('warning')} kind="bars" height={40} />}
              />
              <KpiTile
                label="Services down now"
                value={live ? live.down.toLocaleString() : '—'}
                unit={live ? `of ${live.total}` : undefined}
                tone={!live ? 'unknown' : live.down ? 'breach' : 'met'}
                chip={live?.unknown ? { text: `${live.unknown} unknown`, tone: 'unknown' } : null}
                sub="Current state, independent of the period"
                visual={live ? <StackedBar parts={liveSegments.map((s) => ({ key: s.key, value: s.value, color: s.color, label: s.label, hatch: s.hatch }))} /> : undefined}
                to="/services"
              />
              <KpiTile
                label="Customer impact"
                value={business?.customer_impact_minutes == null ? 'Not assessed' : durationParts(business.customer_impact_minutes).value}
                unit={business?.customer_impact_minutes == null ? undefined : durationParts(business.customer_impact_minutes).unit}
                tone={business?.customer_impact_minutes == null ? 'unknown' : business.customer_impact_minutes > 0 ? 'breach' : 'met'}
                sub={`${business?.impact_assessed_services ?? rows.filter((r) => r.mapped).length} of ${rows.length} services mapped to a group and criticality`}
                visual={<MiniMeter label="Business mapping" value={rows.length ? ((business?.impact_assessed_services ?? rows.filter((r) => r.mapped).length) / rows.length) * 100 : null} />}
                title="Confirmed downtime of mapped services, overlap-merged per business group. Not assessed until services carry a group and criticality."
              />
              <KpiTile
                label="Worst error budget"
                value={burn[0]?.errorBudgetUsed == null ? '—' : burn[0].errorBudgetUsed > 1000 ? `${Math.round(burn[0].errorBudgetUsed / 100)}×` : `${Math.round(burn[0].errorBudgetUsed)}%`}
                tone={burn[0]?.errorBudgetUsed == null ? 'unknown' : burn[0].errorBudgetUsed > 100 ? 'breach' : burn[0].errorBudgetUsed > 75 ? 'risk' : 'met'}
                sub={burn[0] ? burn[0].name : 'No measured budgets'}
                visual={burn.length ? <Sparkline values={burn.map((b) => Math.min(200, b.errorBudgetUsed ?? 0))} color={colors.c('warning')} kind="bars" height={40} min={0} /> : undefined}
                to={burn[0] ? `/services/${burn[0].id}` : undefined}
                title="Share of the downtime allowed by the service's target that was used in the period."
              />
              <KpiTile
                label="Evidence coverage"
                value={`${measuredRows.length}/${rows.length}`}
                unit="services"
                tone={!rows.length ? 'unknown' : measuredRows.length < rows.length ? 'risk' : 'met'}
                sub="Services with confirmed-basis evidence"
                visual={<MiniMeter label="Services measured" value={rows.length ? (measuredRows.length / rows.length) * 100 : null} />}
              />
            </>
          )}
        </div>
      </Hero>

      {brief}

      <Row className={dim}>
        <Panel
          className="xl:col-span-8"
          title={filter ? `Services: ${complianceMeta(filter).label.toLowerCase()}` : 'Availability against target'}
          subtitle={`${assessment.basis} ${periodLabel}. Worst first.`}
          actions={filter ? <button type="button" onClick={() => setFilter(null)} className="min-h-[34px] rounded-lg border border-border px-2.5 text-sm text-text2 hover:text-text">Clear filter</button> : undefined}
        >
          {loading ? <Loading /> : (
            <div className="max-h-[440px] overflow-y-auto pr-1">
              <BulletList
                domain={availDomain(plotted.map((r) => r.availability), Math.min(...plotted.map((r) => r.target), 100))}
                empty={partial
                  ? <EvidenceEmpty kind="partial">No services to show in the data that loaded; some sources failed.</EvidenceEmpty>
                  : !rows.length ? <EvidenceEmpty kind="no_evidence">No service checks are configured.</EvidenceEmpty>
                    : <EvidenceEmpty kind={filter === 'not_assessed' ? 'no_evidence' : 'no_findings'}>No services in this category.</EvidenceEmpty>}
                rows={plotted.map((r) => {
                  const meta = complianceMeta(r.compliance)
                  return {
                    key: r.id,
                    label: r.name,
                    sub: [r.group, r.criticality].filter(Boolean).join(' · ') || r.type.toUpperCase(),
                    value: r.availability,
                    target: r.target,
                    to: `/services/${r.id}`,
                    right: <StatusPill tone={meta.tone} className="hidden py-0 text-xs sm:inline-flex">{meta.label}</StatusPill>,
                  }
                })}
              />
            </div>
          )}
        </Panel>
        <Panel className="xl:col-span-4" title="Error budget burn" subtitle="Downtime used ÷ downtime the target allows">
          {loading ? <Loading /> : (
            <RankBars
              max={200}
              rows={burn.map((r) => {
                const u = r.errorBudgetUsed ?? 0
                return {
                  key: r.id,
                  label: r.name,
                  sub: `Target ${fmtTarget(r.target)} · ${fmtAvail(r.availability)} available`,
                  value: Math.min(200, u),
                  display: u > 1000 ? `${Math.round(u / 100)}× budget` : `${Math.round(u)}%`,
                  color: u > 100 ? colors.c('danger') : u > 75 ? colors.c('amber') : colors.c('success'),
                  to: `/services/${r.id}`,
                }
              })}
              empty={<EvidenceEmpty kind={measuredRows.length ? 'no_findings' : 'no_evidence'}>{measuredRows.length ? 'No error budget reported for measured services.' : 'No measured services in this period.'}</EvidenceEmpty>}
            />
          )}
          {burn.length > 0 && <p className="mt-3 text-xs text-muted">Bars cap at 200%; 100% means the whole allowance for the period is used.</p>}
        </Panel>
      </Row>

      <Row className={dim}>
        <Panel className="xl:col-span-8" title="Confirmed downtime over time" subtitle={`Services confirmed down per interval · ${periodLabel}`}>
          {loading ? <Loading h="h-[260px]" /> : episodes.length ? (
            <>
              <AvailabilityActivityChart trend={null} activity={activity} target={null} fromMs={fromMs} toMs={toMs} height={large ? 320 : 260} entity="services" />
              <div className="mt-2 flex flex-wrap gap-x-4 text-sm text-muted">
                <span className="inline-flex items-center gap-1.5"><span className="h-3 w-2 rounded-sm bg-danger/80" />Services down</span>
                <span className="inline-flex items-center gap-1.5"><span className="text-[10px]" style={{ color: colors.c('amber') }}>▲</span>Outages started</span>
                {business?.confirmed_downtime_minutes != null && <span>Total {fmtDuration(business.confirmed_downtime_minutes)} with at least one service down</span>}
              </div>
            </>
          ) : (
            <EvidenceEmpty kind={measuredRows.length ? 'no_findings' : 'no_evidence'}>{measuredRows.length ? 'No confirmed service outages in this period.' : 'No service evidence for this period.'}</EvidenceEmpty>
          )}
        </Panel>
        <Panel className="xl:col-span-4" title="Service state now" subtitle="Current state — independent of the period">
          {!live ? <Loading h="h-56" /> : live.total === 0 ? <EvidenceEmpty kind="no_evidence">No service checks configured.</EvidenceEmpty> : (
            <div className="flex flex-col items-center gap-4 sm:flex-row xl:flex-col 2xl:flex-row">
              <Donut segments={liveSegments} centerValue={`${live.up}/${live.total}`} centerLabel="services up" />
              <DonutLegend segments={liveSegments} />
            </div>
          )}
        </Panel>
      </Row>

      <Row className={dim}>
        <Panel className="xl:col-span-6" title="Business groups" subtitle="Target compliance by group · down now is current">
          {loading ? <Loading /> : !groups.length ? <EvidenceEmpty kind="no_evidence">No services are configured.</EvidenceEmpty> : (
            <ul className="space-y-3">
              {groups.map((g) => (
                <li key={g.name} className="rounded-lg border border-border px-3 py-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className={cn('font-medium', g.unassigned ? 'italic text-muted' : 'text-text')}>{g.name}</span>
                    <span className="flex items-center gap-2 text-sm text-muted">
                      {plural(g.total, 'service')}
                      {g.down > 0 && <span className="rounded-full bg-danger/12 px-1.5 text-xs font-semibold text-danger">{g.down} down</span>}
                    </span>
                  </div>
                  <div className="mt-2">
                    <StackedBar
                      height={9}
                      parts={[
                        { key: 'met', value: g.met, color: colors.c('success'), label: 'Met' },
                        { key: 'at_risk', value: g.at_risk, color: colors.c('amber'), label: 'At risk' },
                        { key: 'breached', value: g.breached, color: colors.c('danger'), label: 'Breached' },
                        { key: 'not_assessed', value: g.not_assessed, color: colors.c('muted', 0.7), label: 'Not assessed', hatch: true },
                      ]}
                    />
                  </div>
                  <div className="mt-1.5 flex flex-wrap gap-x-3 text-xs text-muted">
                    <span className="text-success">{g.met} met</span>
                    {g.at_risk > 0 && <span style={{ color: colors.c('amber') }}>{g.at_risk} at risk</span>}
                    {g.breached > 0 && <span className="text-danger">{g.breached} breached</span>}
                    {g.not_assessed > 0 && <span>{g.not_assessed} not assessed</span>}
                    {g.downtime > 0 && <span>· {fmtDuration(g.downtime)} confirmed down</span>}
                  </div>
                </li>
              ))}
            </ul>
          )}
          {!loading && rows.some((r) => !r.group) && (
            <p className="mt-3 text-sm text-muted">Services without a business group cannot be assessed for customer impact.</p>
          )}
        </Panel>
        <Panel className="xl:col-span-6" title="Slowest responses" subtitle={`p95 response time · ${periodLabel}`}>
          {loading ? <Loading /> : p95.length ? (
            <CategoryBars
              items={p95.map((r) => ({ key: r.service_check_id, label: r.name, value: Math.round(r.p95_ms ?? 0), color: (r.p95_ms ?? 0) >= 2000 ? colors.c('danger') : (r.p95_ms ?? 0) >= 800 ? colors.c('warning') : colors.c('primary') }))}
              height={Math.max(160, p95.length * 34 + 30)}
              valueFormat={(v) => (v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`)}
            />
          ) : <EvidenceEmpty kind="no_evidence">No response times recorded in this period.</EvidenceEmpty>}
        </Panel>
      </Row>

      <Panel className={dim} title="Service incident timeline" subtitle={`Confirmed outages, start to recovery · ${periodLabel}`}>
        {loading ? <Loading h="h-40" /> : (
          <IncidentTimeline
            items={timeline}
            fromMs={fromMs}
            toMs={toMs}
            empty={partial
              ? <EvidenceEmpty kind="partial">No outages in the data that loaded; some sources failed.</EvidenceEmpty>
              : assessment.state === 'not_measured'
                ? <EvidenceEmpty kind="no_evidence">No outage evidence: nothing was measured in this period.</EvidenceEmpty>
                : <EvidenceEmpty kind="no_findings">No confirmed service outages in this period.</EvidenceEmpty>}
          />
        )}
      </Panel>
    </div>
  )
}

/* ── Servers (current snapshot) ────────────────────────────────────────── */

export function ServersDashboard({ c, overview, brief }: { c: ViewCommon; overview?: ServerMonitoringOverview; brief: ReactNode }) {
  const colors = useVizColors()
  const total = overview?.total ?? 0
  const sc = overview?.status_counts || {}
  const toneColor = { neutral: colors.c('success'), risk: colors.c('warning'), unknown: colors.c('muted') } as Record<string, string>
  const health: DonutSegment[] = serverSegments(overview).map((s) => ({
    key: s.key,
    label: s.label,
    value: s.value,
    color: s.key === 'critical' ? colors.c('danger') : toneColor[s.tone] ?? colors.c('muted'),
    hatch: s.tone === 'unknown',
    to: '/servers',
  }))
  const palette = ['primary', 'accent', 'info', 'amber', 'warning'] as const
  const os: DonutSegment[] = Object.entries(overview?.os_counts || {}).filter(([, v]) => (v ?? 0) > 0).map(([k, v], i) => ({
    key: k, label: k[0].toUpperCase() + k.slice(1), value: v ?? 0, color: colors.c(palette[i % palette.length]),
  }))
  const agents: DonutSegment[] = Object.entries(overview?.agent_counts || {}).map(([k, v]) => ({
    key: k,
    label: k[0].toUpperCase() + k.slice(1),
    value: v ?? 0,
    color: k === 'online' ? colors.c('success') : k === 'offline' || k === 'error' ? colors.c('warning') : colors.c('muted'),
    hatch: !(k === 'online' || k === 'offline' || k === 'error'),
  }))
  const online = overview?.agent_counts?.online ?? 0
  const attention = (sc.critical ?? 0) + (sc.warning ?? 0) + (sc.stale ?? 0) + (sc.unknown ?? 0)
  const sites = (overview?.sites || []).filter((s) => s.server_count > 0).sort((a, b) => b.server_count - a.server_count)
  const assigned = sites.reduce((s, x) => s + x.server_count, 0)
  const cpu = overview?.top_cpu?.[0]
  const mem = overview?.top_memory?.[0]
  const disk = overview?.top_disk?.[0]
  const pressureTone = (v?: number) => (v == null ? 'unknown' : v >= 90 ? 'breach' : v >= 75 ? 'risk' : 'neutral') as 'unknown' | 'breach' | 'risk' | 'neutral'

  return (
    <div className="flex flex-col gap-4">
      <Hero>
        <HeroCard title="Server health now" subtitle="Current snapshot — the period does not apply">
          {!overview ? <Loading h="h-64" /> : total === 0 ? <EvidenceEmpty kind="no_evidence">No servers are enrolled.</EvidenceEmpty> : (
            <div className="mt-3 flex flex-col items-center gap-3">
              <Donut segments={health} centerValue={`${sc.healthy ?? 0}/${total}`} centerLabel="healthy now" size={c.large ? 220 : 196} thickness={24} />
              <DonutLegend segments={health} />
            </div>
          )}
        </HeroCard>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:col-span-8">
          <KpiTile label="Needing attention" value={overview ? String(attention) : '—'} tone={attention ? 'risk' : 'met'} sub={`${sc.critical ?? 0} critical · ${sc.warning ?? 0} warning · ${sc.stale ?? 0} stale`} visual={<StackedBar parts={health.map((h) => ({ key: h.key, value: h.value, color: h.color, label: h.label, hatch: h.hatch }))} />} to="/servers" />
          <KpiTile label="Agents reporting" value={overview ? `${online}/${total}` : '—'} tone={total && online < total ? 'risk' : 'met'} sub="Telemetry coverage" visual={<MiniMeter label="Reporting" value={total ? (online / total) * 100 : null} />} to="/server-agents" />
          <KpiTile label="Sites" value={String(sites.length)} tone="neutral" sub={`${assigned} of ${total} servers assigned`} visual={<MiniMeter label="Assigned to a site" value={total ? (assigned / total) * 100 : null} />} />
          <KpiTile label="Peak CPU · 10 min" value={cpu ? cpu.value.toFixed(1) : '—'} unit={cpu ? '%' : undefined} tone={pressureTone(cpu?.value)} sub={cpu ? cpu.display_name || cpu.hostname || '' : 'Not reported'} visual={cpu ? <MiniMeter label="Utilisation" value={cpu.value} /> : undefined} to={cpu ? `/servers/${cpu.server_id}` : undefined} />
          <KpiTile label="Peak memory" value={mem ? mem.value.toFixed(1) : '—'} unit={mem ? '%' : undefined} tone={pressureTone(mem?.value)} sub={mem ? mem.display_name || mem.hostname || '' : 'Not reported'} visual={mem ? <MiniMeter label="Utilisation" value={mem.value} /> : undefined} to={mem ? `/servers/${mem.server_id}` : undefined} />
          <KpiTile label="Peak disk" value={disk ? disk.value.toFixed(1) : '—'} unit={disk ? '%' : undefined} tone={pressureTone(disk?.value)} sub={disk ? disk.display_name || disk.hostname || '' : 'Not reported'} visual={disk ? <MiniMeter label="Utilisation" value={disk.value} /> : undefined} to={disk ? `/servers/${disk.server_id}` : undefined} />
        </div>
      </Hero>

      {brief}

      <Row>
        <Panel className="xl:col-span-4" title="Operating systems" subtitle="Estate mix">
          {os.length ? (
            <div className="flex flex-col items-center gap-3">
              <Donut segments={os} centerValue={String(total)} centerLabel="servers" size={160} />
              <DonutLegend segments={os} />
            </div>
          ) : <EvidenceEmpty kind="no_evidence">No servers enrolled.</EvidenceEmpty>}
        </Panel>
        <Panel className="xl:col-span-4" title="Telemetry agents" subtitle="Now">
          {agents.some((a) => a.value) ? (
            <div className="flex flex-col items-center gap-3">
              <Donut segments={agents} centerValue={`${online}`} centerLabel="reporting" size={160} />
              <DonutLegend segments={agents} />
            </div>
          ) : <EvidenceEmpty kind="no_evidence">No agents enrolled.</EvidenceEmpty>}
        </Panel>
        <Panel className="xl:col-span-4" title="Workloads by site" subtitle="Current enrolment">
          <RankBars
            rows={[
              ...(total - assigned > 0 ? [{ key: '__none__', label: <span className="italic text-muted">Not assigned</span>, value: total - assigned, display: String(total - assigned), color: colors.c('muted') }] : []),
              ...sites.map((s) => ({ key: s.id, label: s.name, value: s.server_count, display: String(s.server_count), color: colors.c('primary') })),
            ]}
            empty={<EvidenceEmpty kind="no_evidence">No servers are enrolled.</EvidenceEmpty>}
          />
        </Panel>
      </Row>

      <div className="grid gap-4 lg:grid-cols-3">
        <PressureList title="CPU utilisation" items={overview?.top_cpu || []} />
        <PressureList title="Memory utilisation" items={overview?.top_memory || []} />
        <PressureList title="Disk utilisation" items={overview?.top_disk || []} />
      </div>
      <p className="text-sm text-muted">
        Servers use a current health snapshot; no historical server availability is measured. Utilisation is operational context, not business impact.
      </p>
    </div>
  )
}
