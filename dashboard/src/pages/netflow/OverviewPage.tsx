import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  ArrowDownUp,
  ArrowRightLeft,
  Gauge,
  HeartPulse,
  Layers,
  Network,
  RadioTower,
  Router,
  Search,
  ShieldAlert,
  Sparkles,
  Waypoints,
  Wifi,
} from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { TimeRangePicker } from '@/components/TimeRangePicker'
import { ClockSkewBanner } from '@/components/services/list/ClockSkewBanner'
import { useServerNowTick } from '@/components/services/detail/clock'
import { NetflowPageHeader } from '@/components/netflow/NetflowPageHeader'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { FilterChips } from '@/components/netflow/FilterChips'
import { DataQualityBar } from '@/components/netflow/DataQualityBar'
import { nfKeys, nfParams, useNfCoverage, useNfQuery } from '@/lib/netflow/api'
import {
  FW_EVENT_FLOWS,
  mergeFlowFilter,
  removeFlowFilterKeys,
  writeFlowFilterKeepingInvalid,
  type FlowFilter,
  type FlowFilterKey,
} from '@/lib/netflow/filters'
import { useFlowFilter } from '@/lib/netflow/useFlowFilter'
import { buildNetflowLink, exporterPath } from '@/lib/netflow/links'
import { apiErrorRef, coverageState } from '@/lib/netflow/status'
import { formatBps, formatBpsExact, formatBytes, formatBytesExact, formatCount, formatCountExact, ipDisplay } from '@/lib/netflow/format'
import type { NfExporterStatus, NfMeta } from '@/lib/netflow/types'
import { displayTimeZone, freezeWindow, useNfWindow, windowLabel, type NfWindow } from '@/lib/netflow/window'
import { cn } from '@/lib/utils'
import {
  ApplicationsCard,
  AsnCard,
  ConversationsCard,
  CountriesCard,
  DscpCard,
  InterfacesCard,
  IpGroupsCard,
  NetworkClassesCard,
  ProtocolsCard,
  TcpFlagsCard,
  TopEndpointsCard,
  TopTalkersCard,
  type OverviewContext,
} from './cards'
import {
  buildTelemetryInsights,
  deltaReasonText,
  exporterHealthRows,
  exporterListUnknown,
  formatDelta,
  parseHostFilter,
  rateLabel,
  rateUnknownText,
  rezoneHourDow,
  windowHasHourDow,
  type ComparePayload,
  type DeviceStatusPayload,
  type ExporterTraffic,
  type FindingsSummary,
  type HeatmapPayload,
  type OverviewTotals,
  type SeriesPoint,
} from './overviewModel'
import { KpiCard } from './components/KpiCard'
import { ThroughputChart } from './components/ThroughputChart'
import { ThroughputDisplay } from './components/ThroughputDisplay'
import { TrafficHeatmap, heatmapPeriod } from './components/TrafficHeatmap'
import { TelemetryInsights } from './components/TelemetryInsights'
import { ExporterHealthCard } from './components/ExporterHealthCard'
import { ExporterNotFoundState, ScopeEmptyState, isNotFoundError, isScopeEmpty, useNetflowScope } from '@/components/netflow/NetflowAccess'
import { ExplainDrawer } from './ExplainDrawer'
import { SaveCurrentViewButton } from './SaveCurrentViewButton'

export function NetflowDevicePage() {
  const { ip = '' } = useParams<{ ip: string }>()
  return <NetflowPage exporter={ip} />
}

const HEATMAP_HOURS = 168
// A 7-day heatmap hardly changes in minutes; it is refreshed every 15 minutes for relative windows.
const HEATMAP_POLL_MS = 900_000
type Baseline = 'previous_equal' | 'same_time_last_week'

/** A relative 7-day window in the page's zone, for the heatmap under short page windows. */
function heatmapWindow(w: NfWindow): NfWindow {
  const spanH = (Date.parse(w.to) - Date.parse(w.from)) / 3_600_000
  if (spanH >= HEATMAP_HOURS - 0.5 || w.mode === 'fixed') return w
  return { ...w, key: '7d', mode: 'relative', hours: HEATMAP_HOURS, label: 'Last 7d' }
}

export function NetflowPage({ exporter }: { exporter?: string } = {}) {
  const tr = useNfWindow()
  const w = tr.window
  const ff = useFlowFilter()
  const [sp, setSp] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const now = useServerNowTick(30_000)
  const deviceView = !!exporter
  const urlFilter = ff.filter
  // The deep-dive's exporter comes from the path; every query and link carries it.
  const filter: FlowFilter = useMemo(() => (exporter ? { ...urlFilter, exporter } : urlFilter), [urlFilter, exporter])
  const windowEndsNow = w.mode === 'relative' || Date.parse(w.to) >= now - 5 * 60_000
  const compare = sp.get('compare') === '1'
  const spanMs = Date.parse(w.to) - Date.parse(w.from)
  // "Same time last week" only exists for windows of at most 7 days (the API answers 400 beyond).
  const baseline: Baseline = sp.get('baseline') === 'same_time_last_week' && spanMs <= 7 * 86_400_000 ? 'same_time_last_week' : 'previous_equal'
  const [explainOpen, setExplainOpen] = useState(false)

  const setFilter = (patch: Partial<FlowFilter>) => {
    if (deviceView && 'exporter' in patch && patch.exporter !== exporter) {
      // Leaving the deep-dive: another exporter, or all of them.
      const rest = mergeFlowFilter(urlFilter, { ...patch, exporter: undefined })
      const target = patch.exporter ? exporterPath(String(patch.exporter)) : '/netflow'
      navigate(buildNetflowLink(target, { filter: rest, window: w }))
      return
    }
    ff.setFilter(patch as Partial<Record<FlowFilterKey, unknown>>)
  }
  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(sp)
    if (value === null) next.delete(key)
    else next.set(key, value)
    setSp(next, { replace: true })
  }

  // ── Links that keep the investigation context (filter + window) ──
  const linkFilter = filter
  const sectionLink = (section: string, extra?: Record<string, string>) => buildNetflowLink(`/netflow/${section}`, { filter: linkFilter, window: w, extra })
  const pageLink = (path: string, extra?: Record<string, string>) => buildNetflowLink(path, { filter: linkFilter, window: w, extra })
  const exporterLink = (ip: string) => buildNetflowLink(exporterPath(ip), { filter: removeFlowFilterKeys(filter, ['exporter']), window: w })
  // Explained volume counts flow records only: the drill-down excludes NSEL create/deny events too.
  const rawFlowsLink = (patch: Partial<FlowFilter>, meta: NfMeta | null) => {
    const f = mergeFlowFilter(filter, patch as Partial<Record<FlowFilterKey, unknown>>)
    return buildNetflowLink('/netflow/forensics', { filter: f.fw_event === undefined ? { ...f, fw_event: FW_EVENT_FLOWS } : f, window: freezeWindow(w, meta?.window), extra: { run: 1 } })
  }
  const links = {
    anomalies: pageLink('/netflow/anomalies'),
    health: '/netflow/health',
    alerts: '/alerts',
    exporter: exporterLink,
  }

  const ctx: OverviewContext = useMemo(
    () => ({ filter, window: w, exporter, setFilter: (p) => setFilter(p), sectionLink }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [filter, w, exporter, sp],
  )

  // ── Queries (all enveloped, filter + window, appliance clock) ──
  const baseParams = useMemo(() => nfParams(w, filter), [w, filter])
  const overview = useNfQuery<OverviewTotals>('/netflow/overview', baseParams, { window: w })
  const seriesParams = useMemo(() => nfParams(w, filter, compare ? { baseline } : { fill: 1 }), [w, filter, compare, baseline])
  const series = useNfQuery<SeriesPoint[]>('/netflow/timeseries', seriesParams, { window: w, enabled: !compare })
  const comparison = useNfQuery<ComparePayload>('/netflow/timeseries/compare', seriesParams, { window: w, enabled: compare })
  const device = useNfQuery<DeviceStatusPayload>('/netflow/device-status', baseParams, {
    window: w,
    refetchInterval: w.mode === 'relative' ? 60_000 : false,
  })
  const hmWindow = useMemo(() => heatmapWindow(w), [w])
  const hmBasis = sp.get('hm_basis') === 'sum' ? 'sum' : 'avg'
  const hmParams = useMemo(
    () => nfParams(hmWindow, removeFlowFilterKeys(filter, ['hour', 'dow']), { basis: hmBasis }),
    [hmWindow, filter, hmBasis],
  )
  const heatmap = useNfQuery<HeatmapPayload>('/netflow/heatmap', hmParams, {
    window: hmWindow,
    refetchInterval: hmWindow.mode === 'relative' ? HEATMAP_POLL_MS : false,
  })
  const exporters = useNfQuery<ExporterTraffic[]>('/netflow/exporters', baseParams, { window: w })
  // Receipt state for a window ending now; for a past window only the names and device mapping
  // are used (today's state never describes the past), so it is not polled there.
  const status = useNfQuery<NfExporterStatus>('/netflow/exporters/status', null, {
    queryKey: nfKeys.exporterStatus(),
    refetchInterval: windowEndsNow ? 30_000 : false,
    keepPrevious: false,
  })
  const findingsParams = useMemo(() => nfParams(w, filter.exporter ? { exporter: filter.exporter } : null, { status: 'open', limit: 0 }), [w, filter.exporter])
  const findings = useNfQuery<FindingsSummary>('/netflow/findings', findingsParams, { window: w, refetchInterval: 120_000 })
  const coverage = useNfCoverage(w, filter)
  const scope = useNetflowScope()

  const cov = coverage.data?.data ?? null
  const covState = coverageState(cov)
  const statusAvailable: boolean | null = !windowEndsNow ? null : status.data ? true : status.isError ? false : null
  const healthRows = useMemo(
    () =>
      exporterHealthRows({
        windowEndsNow,
        status: status.data ? { available: true, rows: status.data.data.exporters ?? [] } : status.isError ? { available: false, rows: [] } : null,
        traffic: exporters.data?.data ?? [],
        coverage: cov,
        nowMs: now,
        exporter,
      }),
    [windowEndsNow, status.data, status.isError, exporters.data, cov, now, exporter],
  )
  // NF-03: a query that failed with nothing to show is unknown, never an empty list or a zero.
  const failedNoData = (q: { isError: boolean; data: unknown }) => q.isError && q.data === undefined
  const statusFailed = failedNoData(status)
  const trafficFailed = failedNoData(exporters)
  const coverageFailed = failedNoData(coverage)
  const rowsUnknown = exporterListUnknown({ windowEndsNow, rows: healthRows.length, statusFailed, trafficFailed, coverageFailed })
  const rowsError = rowsUnknown
    ? {
        error: (windowEndsNow ? status.error ?? exporters.error : exporters.error ?? coverage.error) ?? null,
        onRetry: () => Promise.all([status.refetch(), exporters.refetch(), coverage.refetch()]),
        retrying: status.isFetching || exporters.isFetching || coverage.isFetching,
      }
    : null
  const insightInputs = [status, exporters, device, findings, coverage]
  const failedInputs = insightInputs.filter(failedNoData)
  const insights = useMemo(
    () =>
      buildTelemetryInsights({
        windowEndsNow,
        health: healthRows,
        statusAvailable,
        device: device.data?.data ?? null,
        findings: findings.data ? { available: true, data: findings.data.data } : findings.isError ? { available: false, data: null } : null,
        coverage: cov,
        totals: overview.data?.data ?? null,
        links,
        failed: {
          exporterState: windowEndsNow && statusFailed && trafficFailed,
          signals: failedNoData(device),
          coverage: coverageFailed,
        },
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [windowEndsNow, healthRows, statusAvailable, device.data, device.isError, findings.data, findings.isError, cov, overview.data, sp, statusFailed, trafficFailed, coverageFailed],
  )

  const totals = overview.data?.data
  const rate = rateLabel(windowEndsNow, totals?.rate_window_s)
  const live = healthRows.filter((r) => r.mode !== 'historical')
  const receiving = live.filter((r) => r.state === 'receiving').length
  const cmp = comparison.data?.data
  const priorLabel = baseline === 'same_time_last_week' ? 'Same time last week' : 'Previous period'
  const rateUnknown = rateUnknownText(totals?.rate_status, windowEndsNow, totals?.rate_window_s)
  const hmFilterTz = filter.tz ?? w.tz
  // The hour/day filter selects local hours in its own zone: warn only when the window holds none
  // of them (not merely because it is shorter than a week), and name the window in the page's zone.
  const whenOutOfRange = useMemo(
    () => (filter.hour != null || filter.dow != null) && !windowHasHourDow(Date.parse(w.from), Date.parse(w.to), hmFilterTz, filter.hour, filter.dow),
    [filter.hour, filter.dow, w.from, w.to, hmFilterTz],
  )

  // The heatmap's Local/UTC switch changes the page zone, which is also the zone an applied hour/day
  // filter is evaluated in: convert the selection so it keeps selecting the same records, and refuse
  // a switch the selection cannot follow exactly (a half-hour offset, or a day-only filter).
  const zoneChoice = (v: 'utc' | 'local') => (v === 'utc' ? 'UTC' : displayTimeZone())
  const rezoned = (v: 'utc' | 'local') =>
    filter.hour == null && filter.dow == null ? {} : rezoneHourDow({ hour: filter.hour, dow: filter.dow }, hmFilterTz, zoneChoice(v), Math.min(Date.parse(w.to), now))
  const setDisplayZone = (v: 'utc' | 'local') => {
    const sel = rezoned(v)
    if (sel === null) return
    const next = new URLSearchParams(sp)
    if (v === 'utc') next.set('tz', 'UTC')
    else next.delete('tz')
    if (filter.hour != null || filter.dow != null) {
      if (sel.hour != null) next.set('hour', String(sel.hour))
      else next.delete('hour')
      if (sel.dow != null) next.set('dow', String(sel.dow))
      else next.delete('dow')
    }
    setSp(next, { replace: true })
  }

  // Heatmap cell → hour/day filter in the heatmap's zone (one URL write; widens a short window).
  const selectCell = (dow: number, hour: number) => {
    const same = filter.hour === hour && filter.dow === dow
    const patch = same ? { hour: undefined, dow: undefined } : { hour, dow, tz: heatmap.data?.data.tz ?? hmFilterTz }
    const next = writeFlowFilterKeepingInvalid(sp, mergeFlowFilter(urlFilter, patch), ff.invalid)
    if (!same && w.mode === 'relative' && (w.hours ?? 0) < HEATMAP_HOURS) {
      next.set('range', '7d')
      next.delete('from')
      next.delete('to')
    }
    setSp(next, { replace: true })
  }

  const title = deviceView ? (
    <>
      Exporter <span className="break-all font-mono text-primary">{ipDisplay(exporter)}</span>
    </>
  ) : (
    'NetFlow overview'
  )

  // One state instead of a page of failed panels (NF-04): an exporter outside the caller's scope
  // (or unknown) answers 404 everywhere; a scope without flow exporters is an access limit, not a
  // collection gap.
  if (deviceView && exporter && isNotFoundError(overview.error)) return <ExporterNotFoundState ip={ipDisplay(exporter)} />
  if (!deviceView && isScopeEmpty(scope.data)) return <ScopeEmptyState />

  return (
    <div className="space-y-4">
      <NetflowPageHeader
        title={title}
        icon={deviceView ? Router : Network}
        crumb={deviceView ? `Exporter ${ipDisplay(exporter)}` : 'Overview'}
        description={
          deviceView
            ? 'Every panel and link on this page is limited to this exporter.'
            : 'Traffic, exporters and data quality for the selected window and filters.'
        }
        actions={
          <>
            <HostFilterBox value={urlFilter.talker ?? ''} onApply={(v) => setFilter({ talker: v ?? undefined })} />
            <TimeRangePicker
              rangeIdx={tr.rangeIdx}
              isCustom={tr.isCustom}
              customFrom={tr.range.fromISO}
              customTo={tr.range.toISO}
              onPreset={tr.setPreset}
              onCustom={tr.setCustom}
              calendarPresets
              activeKey={tr.range.key}
              onCalendar={tr.setCalendar}
            />
            <button
              type="button"
              onClick={() => setExplainOpen(true)}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-xs font-medium text-text hover:bg-surface2"
            >
              <Sparkles className="h-3.5 w-3.5 text-primary" aria-hidden />
              What changed?
            </button>
            <SaveCurrentViewButton kind="overview" filter={filter} window={w} metaWindow={overview.data?.meta?.window ?? null} />
          </>
        }
      />

      <ClockSkewBanner />

      <FilterChips
        filter={urlFilter}
        onRemove={(keys) => ff.removeKeys(keys)}
        onClearAll={ff.clearAll}
        invalid={ff.invalid}
        errors={ff.errors}
        tz={w.tz}
        copyLink={`${location.pathname}${location.search}`}
      />

      {whenOutOfRange && (
        <div role="status" className="flex flex-wrap items-center gap-2 rounded-md border border-warning/35 bg-warning/10 px-3 py-2 text-xs text-text">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning-fg" aria-hidden />
          <span>
            The hour/day filter selects local hours ({hmFilterTz}) that the {windowLabel(w)} window does not contain, so panels are empty.
          </span>
          <button type="button" onClick={() => tr.setPreset(2)} className="rounded border border-border bg-surface px-2 py-0.5 font-medium hover:bg-surface2">
            Use last 7 days
          </button>
          <button type="button" onClick={() => ff.removeKeys(['hour', 'dow'])} className="rounded border border-border bg-surface px-2 py-0.5 font-medium hover:bg-surface2">
            Clear hour/day
          </button>
        </div>
      )}

      <DataQualityBar meta={overview.data?.meta} window={w} filter={filter} />

      {/* KPIs (NF-30): one column on phones, never truncated */}
      <QueryPanel
        query={overview}
        label="the traffic totals"
        skeleton="tiles"
        isEmpty={() => false}
        coverage={covState}
        noCoverage="The exporters in scope delivered no flow records in this window, so traffic is unknown here, not zero."
      >
        {(r) => (
          <div className="grid grid-cols-1 gap-3 min-[360px]:grid-cols-2 md:grid-cols-3 xl:grid-cols-6">
            <KpiCard
              icon={ArrowDownUp}
              label="Traffic volume"
              value={formatBytes(r.data.bytes)}
              title={formatBytesExact(r.data.bytes)}
              badge={compare && cmp ? <DeltaChip pct={cmp.totals.delta_pct} reason={cmp.totals.delta_reason} label={priorLabel} /> : undefined}
              sub={`${formatCountExact(r.data.packets)} packets`}
            />
            <KpiCard
              icon={Activity}
              label={rate.label}
              value={r.data.current_bps == null ? 'Unknown' : formatBps(r.data.current_bps)}
              title={r.data.current_bps == null ? `Unknown: ${rateUnknownText(r.data.rate_status, windowEndsNow, r.data.rate_window_s) ?? 'no rate for these minutes'}` : `${formatBpsExact(r.data.current_bps)} — ${rate.hint}`}
              sub={
                r.data.current_bps == null
                  ? (rateUnknownText(r.data.rate_status, windowEndsNow, r.data.rate_window_s) ?? 'Unknown, not zero')
                  : windowEndsNow ? 'Newest minutes can still be arriving' : 'At the end of the selected window'
              }
            />
            <KpiCard
              icon={Wifi}
              label="Flow records"
              value={formatCount(r.data.flows)}
              title={`${formatCountExact(r.data.flows)} flow records`}
              sub={r.data.top_protocol ? `Largest protocol: ${r.data.top_protocol.name}` : 'No traffic matched'}
            />
            <KpiCard
              icon={ArrowRightLeft}
              label="Source hosts"
              value={formatCount(r.data.src_hosts)}
              title={`${formatCountExact(r.data.src_hosts)} distinct source addresses`}
              sub="Distinct source addresses"
            />
            <KpiCard
              icon={Waypoints}
              label="Destination hosts"
              value={formatCount(r.data.dst_hosts)}
              title={`${formatCountExact(r.data.dst_hosts)} distinct destination addresses`}
              sub={r.data.hosts != null ? `${formatCountExact(r.data.hosts)} distinct addresses overall` : 'Distinct destination addresses'}
            />
            <KpiCard
              icon={RadioTower}
              label={windowEndsNow ? 'Exporters receiving' : 'Exporters with traffic'}
              value={windowEndsNow && live.length ? `${receiving} of ${live.length}` : formatCountExact(r.data.exporters)}
              title={windowEndsNow ? 'Exporters whose flows arrived within the expected interval (appliance clock)' : 'Exporters that delivered matching records in this window'}
              sub={
                windowEndsNow
                  ? statusAvailable === false
                    ? 'Estimated from the last receipt time'
                    : `${formatCountExact(r.data.exporters)} with matching traffic`
                  : 'Live state is not shown for a past window'
              }
            />
          </div>
        )}
      </QueryPanel>

      {/* Throughput + exporter health */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,2fr)_minmax(300px,1fr)]">
        <Card className="min-w-0">
          <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-2 pb-2">
            <div className="min-w-0">
              <CardTitle className="text-sm">Traffic throughput</CardTitle>
              <p className="text-xs text-text2">
                {windowLabel(w)}
                {compare && ` · vs ${priorLabel.toLowerCase()}, same filters`}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <label className="inline-flex cursor-pointer items-center gap-1.5 text-text2">
                <input
                  type="checkbox"
                  className="h-3.5 w-3.5 accent-[rgb(var(--primary))]"
                  checked={compare}
                  onChange={(e) => setParam('compare', e.target.checked ? '1' : null)}
                />
                Compare
              </label>
              {compare && (
                <select
                  aria-label="Comparison period"
                  value={baseline}
                  onChange={(e) => setParam('baseline', e.target.value === 'previous_equal' ? null : e.target.value)}
                  className="rounded-md border border-border bg-surface px-1.5 py-1 text-xs text-text"
                >
                  <option value="previous_equal">Previous period</option>
                  <option value="same_time_last_week" disabled={spanMs > 7 * 86_400_000}>
                    Same time last week
                  </option>
                </select>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            <ThroughputDisplay bps={totals?.current_bps} label={rate.label} hint={rate.hint} unknownReason={totals && totals.current_bps == null ? rateUnknown : null} />
            {compare ? (
              <QueryPanel
                query={comparison}
                label="the throughput comparison"
                skeleton="chart"
                coverage={covState}
                isEmpty={(r) => !r.data.points.some((p) => p.state === 'data' || p.state === 'idle')}
              >
                {(r) => (
                  <div className="space-y-2">
                    <CompareSummary data={r.data} label={priorLabel} tz={w.tz} />
                    <ThroughputChart
                      points={r.data.points}
                      prior={r.data.prior_points}
                      bucketS={r.data.bucket_s}
                      fromMs={Date.parse(r.data.window.from)}
                      toMs={Date.parse(r.data.window.to)}
                      tz={w.tz}
                      priorLabel={priorLabel}
                    />
                  </div>
                )}
              </QueryPanel>
            ) : (
              <QueryPanel
                query={series}
                label="the throughput chart"
                skeleton="chart"
                coverage={covState}
                isEmpty={(r) => !r.data.some((p) => p.state === 'data' || p.state === 'idle')}
              >
                {(r) => (
                  <ThroughputChart
                    points={r.data}
                    bucketS={r.meta?.window?.granularity_s ?? 60}
                    fromMs={Date.parse(r.meta?.window?.from ?? w.from)}
                    toMs={Date.parse(r.meta?.window?.to ?? w.to)}
                    tz={w.tz}
                  />
                )}
              </QueryPanel>
            )}
          </CardContent>
        </Card>
        <ExporterHealthCard
          rows={healthRows}
          rowsError={rowsError}
          rowsLoading={exporters.isLoading || (windowEndsNow && status.isLoading)}
          device={device}
          windowEndsNow={windowEndsNow}
          exporterLink={exporterLink}
          healthLink={links.health}
          deviceView={deviceView}
        />
      </div>

      {/* Heatmap + insights */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card className="min-w-0">
          <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-2 pb-2">
            <div className="min-w-0">
              <CardTitle className="text-sm">Traffic by weekday and hour</CardTitle>
              <p className="text-xs text-text2">
                {heatmap.data ? heatmapPeriod(heatmap.data.data) : hmWindow.label}
                {hmWindow !== w && ' · last 7 days (the page window is shorter)'}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Segmented
                label="Time zone"
                value={sp.get('tz') === 'UTC' ? 'utc' : 'local'}
                options={[
                  { value: 'local', text: 'Local', disabledReason: rezoned('local') === null ? `The hour/day filter (${hmFilterTz}) has no exact equivalent in ${zoneChoice('local')}; clear it to switch` : undefined },
                  { value: 'utc', text: 'UTC', disabledReason: rezoned('utc') === null ? `The hour/day filter (${hmFilterTz}) has no exact equivalent in UTC; clear it to switch` : undefined },
                ]}
                onChange={(v) => setDisplayZone(v === 'utc' ? 'utc' : 'local')}
              />
              <Segmented
                label="Cell value"
                value={hmBasis}
                options={[
                  { value: 'avg', text: 'Average hour' },
                  { value: 'sum', text: 'Total' },
                ]}
                onChange={(v) => setParam('hm_basis', v === 'avg' ? null : v)}
              />
            </div>
          </CardHeader>
          <CardContent>
            <QueryPanel
              query={heatmap}
              label="the traffic heatmap"
              skeleton="block"
              isEmpty={(r) => !r.data.cells.some((c) => c.state === 'data' || c.state === 'partial' || c.state === 'zero')}
              empty="No records in this period"
            >
              {(r) => <TrafficHeatmap data={r.data} activeHour={filter.hour ?? null} activeDow={filter.dow ?? null} onSelect={selectCell} />}
            </QueryPanel>
          </CardContent>
        </Card>
        <TelemetryInsights
          insights={insights}
          links={links}
          loading={overview.isLoading || device.isLoading || findings.isLoading || (windowEndsNow && status.isLoading)}
          onRetry={failedInputs.length ? () => Promise.all(failedInputs.map((q) => q.refetch())) : undefined}
          retrying={failedInputs.some((q) => q.isFetching)}
          errorRef={failedInputs.map((q) => apiErrorRef(q.error)).find(Boolean) ?? null}
        />
      </div>

      {/* Breakdowns (self-fetching cards, contract §14) */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 2xl:grid-cols-4">
        <TopTalkersCard ctx={ctx} compact />
        <TopEndpointsCard ctx={ctx} compact />
        <ApplicationsCard ctx={ctx} compact />
        <ProtocolsCard ctx={ctx} compact />
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <ConversationsCard ctx={ctx} />
        <InterfacesCard ctx={ctx} compact />
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        <DscpCard ctx={ctx} compact />
        <TcpFlagsCard ctx={ctx} compact />
        <NetworkClassesCard ctx={ctx} compact />
        <CountriesCard ctx={ctx} compact />
        <AsnCard ctx={ctx} compact />
        <IpGroupsCard ctx={ctx} compact />
      </div>

      <nav aria-label="NetFlow tools" className="flex flex-wrap items-center gap-2 text-xs">
        <QuickLink to={buildNetflowLink('/netflow/forensics', { filter, window: w, extra: { run: 1 } })} icon={Search} text="Raw flows (Forensics)" />
        <QuickLink to={links.anomalies} icon={ShieldAlert} text="Anomalies" />
        <QuickLink to={pageLink('/netflow/capacity')} icon={Gauge} text="Capacity" />
        <QuickLink to="/netflow/saved-views" icon={Layers} text="Saved views" />
        <QuickLink to={links.health} icon={HeartPulse} text="Collector health" />
      </nav>

      <ExplainDrawer
        open={explainOpen}
        onOpenChange={setExplainOpen}
        window={w}
        filter={filter}
        onApplyFilter={(patch) => setFilter(patch)}
        rawFlowsLink={rawFlowsLink}
      />
    </div>
  )
}

/* ─── Pieces ───────────────────────────────────────────────────────── */

/**
 * The header's host box (NF-26): an applied filter on source OR destination for every panel, not a
 * search over ten loaded rows. Validates locally; Enter or the button applies; empty clears.
 */
function HostFilterBox({ value, onApply }: { value: string; onApply: (v: string | null) => void }) {
  const [draft, setDraft] = useState(value)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    setDraft(value)
    setError(null)
  }, [value])
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const parsed = parseHostFilter(draft)
    if (!parsed.ok) {
      setError(parsed.error)
      return
    }
    setError(null)
    onApply(parsed.value)
  }
  return (
    <form onSubmit={submit} role="search" aria-label="Host filter" className="flex min-w-0 flex-col">
      <div className="flex items-center gap-1">
        <label htmlFor="nf-host-filter" className="sr-only">
          Filter every panel by host (source or destination IP or CIDR)
        </label>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-text2" aria-hidden />
          <input
            id="nf-host-filter"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Host IP or CIDR"
            aria-invalid={!!error}
            aria-describedby={error ? 'nf-host-filter-err' : undefined}
            className={cn(
              'h-8 w-44 rounded-md border bg-surface pl-7 pr-2 text-xs text-text placeholder:text-text2 focus:outline-none focus:ring-2 focus:ring-primary/40 sm:w-52',
              error ? 'border-danger' : 'border-border',
            )}
          />
        </div>
        <button type="submit" className="h-8 rounded-md border border-border bg-surface px-2.5 text-xs font-medium text-text hover:bg-surface2">
          Apply
        </button>
      </div>
      {error && (
        <span id="nf-host-filter-err" role="alert" className="mt-1 max-w-[16rem] text-xs text-danger-fg">
          {error}
        </span>
      )}
    </form>
  )
}

function DeltaChip({ pct, reason, label }: { pct: number | null; reason: ComparePayload['totals']['delta_reason']; label: string }) {
  if (pct == null) {
    return (
      <span className="rounded border border-border bg-surface2 px-1.5 py-0.5 text-xs font-medium text-text2" title={`No change stated: ${deltaReasonText(reason)}`}>
        vs {label.toLowerCase()}: n/a
      </span>
    )
  }
  return (
    <span className="rounded border border-border bg-surface2 px-1.5 py-0.5 text-xs font-semibold tabular-nums text-text" title={`Change against the ${label.toLowerCase()}, same filters`}>
      {formatDelta(pct)}
    </span>
  )
}

function CompareSummary({ data, label, tz }: { data: ComparePayload; label: string; tz: string }) {
  const t = data.totals
  const prior = windowLabel({ from: data.prior_window.from, to: data.prior_window.to, mode: 'fixed', tz })
  return (
    <div className="rounded-md border border-border bg-surface2/40 px-2.5 py-1.5 text-xs text-text2">
      <span className="font-medium text-text">{label}:</span> {prior} ·{' '}
      <b className="font-semibold text-text">{formatBytes(t.prior_bytes)}</b> then, <b className="font-semibold text-text">{formatBytes(t.bytes)}</b> now
      {t.delta_pct != null ? (
        <>
          {' '}
          (<b className="font-semibold text-text">{formatDelta(t.delta_pct)}</b>)
        </>
      ) : (
        <> · no change stated: {deltaReasonText(t.delta_reason)}</>
      )}
      {data.prior_coverage.coverage_pct != null && data.prior_coverage.coverage_pct < 100 && (
        <> · records in {Math.round(data.prior_coverage.coverage_pct)}% of its buckets</>
      )}
      {data.notes.map((n) => (
        <span key={n}> · {n}</span>
      ))}
    </div>
  )
}

function Segmented({ label, value, options, onChange }: { label: string; value: string; options: { value: string; text: string; disabledReason?: string }[]; onChange: (v: string) => void }) {
  return (
    <div role="group" aria-label={label} className="inline-flex overflow-hidden rounded-md border border-border">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          disabled={!!o.disabledReason && value !== o.value}
          title={o.disabledReason}
          onClick={() => onChange(o.value)}
          className={cn('px-2 py-1 font-medium disabled:cursor-not-allowed disabled:opacity-50', value === o.value ? 'bg-primary/10 text-text' : 'bg-surface text-text2 hover:text-text')}
        >
          {o.text}
        </button>
      ))}
    </div>
  )
}

function QuickLink({ to, icon: Icon, text }: { to: string; icon: typeof Search; text: string }) {
  return (
    <Link to={to} className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-text2 hover:border-primary/40 hover:bg-primary/5 hover:text-text">
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {text}
    </Link>
  )
}
