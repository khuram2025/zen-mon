/**
 * Domain views for the Availability page.
 *
 * The executive dashboards live in dashboards.tsx. This module holds the
 * Operations views (groups, types, inventories, alert noise, pressure) and
 * the pieces both share (decision metrics, top risks, timeline items).
 */
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { Skeleton } from '@/components/ui/Skeleton'
import type {
  BusinessData, DeviceSegmentSummary, ExecutiveData, TechnicalData,
} from '@/hooks/useReports'
import type { ServerMonitoringOverview, TopPressureItem } from '@/types/servers'
import {
  fmtAvail, fmtDateTime, fmtDuration, fmtTarget, parseTs, plural,
} from './format'
import type {
  Assessment, DeviceLite, DeviceSummary, ServiceRow, ServiceSummary, Tone,
} from './model'
import { rankServices } from './model'
import { FlappingTag } from '@/components/devices/FlappingTag'
import { AvailCell, complianceMeta, type TimelineItem } from './charts'
import { AlertVolumeChart } from './viz'
import {
  CountDistribution, EvidenceEmpty, MetricCard, Panel, RiskList, SeverityPill, SimpleTable,
  StatusPill, TONE, ToneIcon, type CountSegment,
} from './ui'

export interface ViewCommon {
  assessment: Assessment
  /** Display label, e.g. "Last 7 days". */
  periodLabel: string
  /** Mid-sentence phrase, e.g. "the last 7 days". */
  period: string
  fromMs: number
  toMs: number
  /** Period-bound figures belong to a previous period (placeholder). */
  dimmed: boolean
  loading: boolean
  large: boolean
  /** Some sub-queries failed — empty states must not reassure. */
  partial: boolean
}

const NOT_ASSIGNED = new Set(['', 'unknown', 'unassigned', 'none', 'ungrouped', 'not assigned'])
export function isUnassigned(name: string | null | undefined) {
  return !name || NOT_ASSIGNED.has(name.trim().toLowerCase())
}

/* ── Shared executive frame ───────────────────────────────────────────── */

export function DecisionMetrics({ assessment, loading, dimmed, large }: Pick<ViewCommon, 'assessment' | 'loading' | 'dimmed' | 'large'>) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {(loading ? [undefined, undefined, undefined, undefined] : assessment.metrics).map((metric, i) => (
        <MetricCard key={metric?.key ?? i} metric={metric} loading={loading} dimmed={dimmed} large={large} />
      ))}
    </div>
  )
}

export function TopRisksPanel({ c, className, emptyText }: { c: ViewCommon; className?: string; emptyText: string }) {
  const { assessment, loading, partial } = c
  const noEvidence = assessment.state === 'not_measured'
  return (
    <Panel
      title="Top risks"
      subtitle="Confirmed exposure first; alert noise is listed under Operations"
      className={className}
    >
      <RiskList
        risks={assessment.risks}
        loading={loading}
        empty={partial
          ? <EvidenceEmpty kind="partial">No risks found in the data that loaded; some sources failed.</EvidenceEmpty>
          : noEvidence
            ? <EvidenceEmpty kind="no_evidence">No risks can be ranked without evidence for this period.</EvidenceEmpty>
            : <EvidenceEmpty kind="no_findings">{emptyText}</EvidenceEmpty>}
      />
    </Panel>
  )
}

/* ── Devices ──────────────────────────────────────────────────────────── */

export function deviceTimelineItems(exec?: ExecutiveData, limit = 10): TimelineItem[] {
  return [...(exec?.outage_timeline || [])]
    .sort((a, b) => Number(!!b.ongoing) - Number(!!a.ongoing) || (parseTs(b.started_at) ?? 0) - (parseTs(a.started_at) ?? 0))
    .slice(0, limit)
    .map((o, i) => ({
      key: `${o.device_id}-${o.started_at}-${i}`,
      label: o.hostname,
      sub: o.kind && o.kind !== 'device' ? o.kind : null,
      to: `/devices/${o.device_id}`,
      startMs: parseTs(o.started_at),
      endMs: parseTs(o.ended_at ?? null),
      ongoing: !!o.ongoing,
      // Legacy reports send 0 for every Down row; without an end time or
      // ongoing flag that is not a measured duration.
      durationMin: o.ended_at === undefined && o.ongoing === undefined && !o.duration_minutes ? null : o.duration_minutes,
      // start_known=false: the log opened on a recovery; start is the clipped window start.
      startKnown: o.start_known !== false,
      inWindowMin: o.in_window_minutes ?? null,
    }))
}

function SegmentTable({ rows, target, empty }: { rows?: DeviceSegmentSummary[] | null; target: number | null; empty: string }) {
  if (rows == null) {
    return <EvidenceEmpty kind="no_evidence">{empty}</EvidenceEmpty>
  }
  const sorted = [...rows].sort((a, b) =>
    Number(isUnassigned(b.name)) - Number(isUnassigned(a.name)) || b.devices - a.devices)
  return (
    <SimpleTable
      columns={[{ label: 'Name' }, { label: 'Devices', numeric: true }, { label: 'Measured', numeric: true }, { label: 'Down now', numeric: true }, { label: 'Availability', numeric: true }]}
      empty={<EvidenceEmpty kind="no_evidence">No devices in this breakdown.</EvidenceEmpty>}
      rows={sorted.map((row) => ({
        key: row.name || 'unassigned',
        cells: [
          isUnassigned(row.name) ? <span className="text-muted">Not assigned</span> : <span className="font-medium">{row.name}</span>,
          row.devices,
          <span className={row.measured < row.devices ? 'text-warning' : 'text-text2'}>{row.measured}</span>,
          row.down_now > 0 ? <span className="font-semibold text-danger">{row.down_now}</span> : 0,
          <AvailCell value={row.availability_pct} target={target} />,
        ],
      }))}
    />
  )
}

export function DevicesOperations({
  c, exec, technical, technicalLoading, live, attention, flapping = [], maintenance,
}: {
  c: ViewCommon
  exec?: ExecutiveData
  technical?: TechnicalData
  technicalLoading: boolean
  live?: DeviceSummary
  /** Down and degraded devices. */
  attention: DeviceLite[]
  /** Flapping devices (any status); those already listed as down or degraded get a tag instead of a second row. */
  flapping?: DeviceLite[]
  maintenance: DeviceLite[]
}) {
  const { assessment, dimmed, loading } = c
  const target = assessment.target
  const liveSegments: CountSegment[] = [
    { key: 'up', label: 'Up', value: live?.up ?? 0, tone: 'met' },
    { key: 'degraded', label: 'Degraded', value: live?.degraded ?? 0, tone: 'risk' },
    { key: 'down', label: 'Down', value: live?.down ?? 0, tone: 'breach' },
    { key: 'maintenance', label: 'Maintenance', value: live?.maintenance ?? 0, tone: 'maint' },
    { key: 'unknown', label: 'Unknown', value: live?.unknown ?? 0, tone: 'unknown' },
  ]
  const issues = exec?.top_issues || []
  const categoryOf = (i: (typeof issues)[number]) => i.category ?? (i.issue === 'noisy' ? 'alert_noise' : 'outage')
  const exposures = issues.filter((i) => categoryOf(i) !== 'alert_noise')
  const noise = issues.filter((i) => categoryOf(i) === 'alert_noise')
  const worst = (technical?.worst_devices || []).filter((d) => d.availability_pct < 100).slice(0, 15)
  const timeline = deviceTimelineItems(exec, 50)
  const techErrors = technical?.data_errors?.length ?? 0
  const listed = new Set([...attention, ...maintenance].map((d) => d.id))
  const flappingIds = new Set(flapping.map((d) => d.id))
  const flappingOnly = flapping.filter((d) => !listed.has(d.id))
  const liveFlapping = live?.flapping ?? 0

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Current device state" subtitle="Now — independent of the selected period">
          {live ? <CountDistribution segments={liveSegments} total={live.total} /> : <Skeleton className="h-16 w-full" />}
          {liveFlapping > 0 && (
            <p className="mt-2 text-sm text-muted">
              <Link to="/devices?flapping=1" className="font-medium text-warning hover:underline">{liveFlapping} flapping</Link>
              {' '}— also counted under their confirmed status above; their status notifications are held.
            </p>
          )}
        </Panel>
        <Panel title="Needs attention now" subtitle="Down, degraded or flapping devices, plus planned maintenance">
          {attention.length === 0 && flappingOnly.length === 0 && maintenance.length === 0 ? (
            live ? <EvidenceEmpty kind="no_findings">No devices are down, degraded or flapping now.</EvidenceEmpty> : <Skeleton className="h-16 w-full" />
          ) : (
            <ul className="max-h-72 space-y-1 overflow-y-auto">
              {[...attention, ...flappingOnly, ...maintenance].map((d) => {
                const flapOnly = !listed.has(d.id)
                const tone: Tone = d.status === 'down' ? 'breach' : d.status === 'maintenance' ? 'maint' : 'risk'
                return (
                  <li key={d.id}>
                    <Link to={`/devices/${d.id}`} className="flex min-h-[40px] items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-surface2/60">
                      <StatusPill tone={tone}>{flapOnly ? 'Flapping' : d.status === 'maintenance' ? 'Maintenance' : d.status === 'down' ? 'Down' : 'Degraded'}</StatusPill>
                      <span className="min-w-0 flex-1 truncate text-base text-text">{d.hostname}</span>
                      {!flapOnly && flappingIds.has(d.id) && <FlappingTag device={flapping.find((f) => f.id === d.id) ?? d} />}
                      <span className="hidden text-sm text-muted sm:inline">{d.location || 'No location'}</span>
                    </Link>
                  </li>
                )
              })}
            </ul>
          )}
        </Panel>
      </div>

      <Panel
        title="Alert volume by severity"
        subtitle={`Alerts raised per interval · ${c.periodLabel}`}
        className={cn(dimmed && 'opacity-60')}
      >
        {technicalLoading ? <Skeleton className="h-48 w-full" /> : <AlertVolumeChart points={technical?.alert_volume_by_severity || []} fromMs={c.fromMs} toMs={c.toMs} height={200} />}
      </Panel>

      <div className={cn('grid gap-4 lg:grid-cols-2', dimmed && 'opacity-60')}>
        <Panel title="By group" subtitle={`Same period and denominator as the headline · ${c.periodLabel}`}>
          {loading ? <Skeleton className="h-40 w-full" /> : <SegmentTable rows={exec?.group_summary} target={target} empty="Group breakdown is not reported by this appliance version." />}
        </Panel>
        <Panel title="By device type" subtitle={`Same period and denominator as the headline · ${c.periodLabel}`}>
          {loading ? <Skeleton className="h-40 w-full" /> : <SegmentTable rows={exec?.type_summary} target={target} empty="Device-type breakdown is not reported by this appliance version." />}
        </Panel>
      </div>

      <div className={cn('grid gap-4 lg:grid-cols-2', dimmed && 'opacity-60')}>
        <Panel title="Availability exposures" subtitle="Confirmed outages and degradation, ranked by downtime">
          <SimpleTable
            columns={[{ label: 'Device' }, { label: 'Issue' }, { label: 'Outages', numeric: true }, { label: 'Downtime', numeric: true }, { label: 'Severity', numeric: true }]}
            empty={c.partial
              ? <EvidenceEmpty kind="partial">No exposures in the data that loaded; some sources failed.</EvidenceEmpty>
              : assessment.state === 'not_measured'
                ? <EvidenceEmpty kind="no_evidence">No evidence for this period.</EvidenceEmpty>
                : <EvidenceEmpty kind="no_findings">No availability exposures in this period.</EvidenceEmpty>}
            rows={exposures.slice(0, 15).map((i, idx) => ({
              key: `${i.device_id}-${idx}`,
              cells: [
                <Link to={`/devices/${i.device_id}`} className="font-medium text-primary hover:underline">{i.hostname}</Link>,
                <span className="text-text2">{i.issue}</span>,
                i.outages ?? '—',
                fmtDuration(i.downtime_minutes ?? i.duration_minutes),
                <SeverityPill severity={i.severity} />,
              ],
            }))}
          />
        </Panel>
        <Panel title="Alert noise" subtitle="Operational alert volume — not availability loss">
          <SimpleTable
            columns={[{ label: 'Source' }, { label: 'Rule' }, { label: 'Alerts', numeric: true }, { label: 'Severity', numeric: true }]}
            empty={technicalLoading
              ? <Skeleton className="h-24 w-full" />
              : techErrors
                ? <EvidenceEmpty kind="partial">Alert noise could not be fully loaded.</EvidenceEmpty>
                : <EvidenceEmpty kind="no_findings">No noisy alert sources in this period.</EvidenceEmpty>}
            rows={(technical?.noisy_alerts?.length
              ? technical.noisy_alerts.slice(0, 12).map((n, idx) => ({
                  key: `${n.rule_key}-${n.device_id}-${idx}`,
                  cells: [
                    n.device_id ? <Link to={`/devices/${n.device_id}`} className="font-medium text-primary hover:underline">{n.hostname || n.device_id.slice(0, 8)}</Link> : <span className="text-muted">—</span>,
                    <span className="text-text2" title={n.sample_message || undefined}>{n.rule_key}</span>,
                    n.alert_count.toLocaleString(),
                    <SeverityPill severity={n.severity} />,
                  ],
                }))
              : noise.slice(0, 12).map((n, idx) => ({
                  key: `${n.device_id}-${idx}`,
                  cells: [
                    <Link to={`/devices/${n.device_id}`} className="font-medium text-primary hover:underline">{n.hostname}</Link>,
                    <span className="text-text2">{n.issue}</span>,
                    n.alert_count.toLocaleString(),
                    <SeverityPill severity={n.severity} />,
                  ],
                })))}
          />
        </Panel>
      </div>

      <div className={cn('grid gap-4 lg:grid-cols-2', dimmed && 'opacity-60')}>
        <Panel title="Lowest availability devices" subtitle={c.periodLabel}>
          <SimpleTable
            columns={[{ label: 'Device' }, { label: 'IP' }, { label: 'Outages', numeric: true }, { label: 'P95 RTT', numeric: true }, { label: 'Availability', numeric: true }]}
            empty={technicalLoading
              ? <Skeleton className="h-24 w-full" />
              : techErrors
                ? <EvidenceEmpty kind="partial">Device ranking could not be fully loaded.</EvidenceEmpty>
                : technical
                  ? <EvidenceEmpty kind="no_findings">Every measured device was fully available in this period.</EvidenceEmpty>
                  : <EvidenceEmpty kind="error">Device ranking is unavailable.</EvidenceEmpty>}
            rows={worst.map((d) => ({
              key: d.device_id,
              cells: [
                <Link to={`/devices/${d.device_id}`} className="font-medium text-primary hover:underline">{d.hostname}</Link>,
                <span className="font-mono text-sm text-muted">{d.ip}</span>,
                d.outage_count ?? '—',
                d.p95_rtt_ms != null ? `${d.p95_rtt_ms.toFixed(0)} ms` : '—',
                <AvailCell value={d.availability_pct} target={target} />,
              ],
            }))}
          />
        </Panel>
        <Panel title="Outage history" subtitle={`Start, recovery and duration · ${c.periodLabel}`}>
          <SimpleTable
            columns={[{ label: 'Device' }, { label: 'Started' }, { label: 'Recovered' }, { label: 'Duration', numeric: true }]}
            empty={assessment.state === 'not_measured'
              ? <EvidenceEmpty kind="no_evidence">No outage evidence for this period.</EvidenceEmpty>
              : <EvidenceEmpty kind="no_findings">No confirmed outages in this period.</EvidenceEmpty>}
            rows={timeline.map((t) => ({
              key: t.key,
              cells: [
                <Link to={t.to} className="font-medium text-primary hover:underline">{t.label}</Link>,
                <span className="text-sm tabular-nums text-text2">{t.startKnown === false ? 'Before period' : fmtDateTime(t.startMs)}</span>,
                t.ongoing
                  ? <StatusPill tone="breach">Ongoing</StatusPill>
                  : <span className="text-sm tabular-nums text-text2">{t.endMs ? fmtDateTime(t.endMs) : 'Unknown'}</span>,
                <span className={t.durationMin == null ? 'text-muted' : ''}>
                  {fmtDuration(t.durationMin)}
                  {t.inWindowMin != null && t.durationMin != null && Math.abs(t.inWindowMin - t.durationMin) >= 1 && (
                    <span className="block text-sm text-muted">{fmtDuration(t.inWindowMin)} in period</span>
                  )}
                </span>,
              ],
            }))}
          />
        </Panel>
      </div>
    </div>
  )
}

/* ── Services ─────────────────────────────────────────────────────────── */

export function serviceTimelineItems(business?: BusinessData, limit = 10, fromMs = 0): TimelineItem[] {
  return [...(business?.service_outages || [])]
    .sort((a, b) => Number(!!b.ongoing) - Number(!!a.ongoing) || (parseTs(b.started_at) ?? 0) - (parseTs(a.started_at) ?? 0))
    .slice(0, limit)
    .map((o, i) => ({
      key: `${o.service_check_id}-${o.started_at}-${i}`,
      label: o.name,
      sub: o.group_name && !isUnassigned(o.group_name) ? o.group_name : null,
      to: `/services/${o.service_check_id}`,
      startMs: parseTs(o.started_at),
      endMs: parseTs(o.ended_at ?? null),
      ongoing: !!o.ongoing,
      durationMin: o.ended_at === undefined && o.ongoing === undefined && !o.duration_minutes ? null : o.duration_minutes,
      // Newer backends clip an episode already running at the window start
      // to the start itself; that start is not the real one.
      startKnown: !(o.ongoing !== undefined && (parseTs(o.started_at) ?? Infinity) <= fromMs + 1_000),
    }))
}

function MatrixCount({ n, tone }: { n: number; tone: Tone }) {
  if (!n) return <span className="text-muted">0</span>
  return (
    <span className={cn('inline-flex items-center justify-end gap-1 font-semibold', TONE[tone].text)}>
      <ToneIcon tone={tone} className="h-3.5 w-3.5" />{n}
    </span>
  )
}

export function ServicesOperations({
  c, business, rows, live,
}: {
  c: ViewCommon
  business?: BusinessData
  rows: ServiceRow[]
  live?: ServiceSummary
}) {
  const { dimmed, loading, partial } = c
  const p95 = useMemo(
    () => new Map((business?.response_time_quantiles || []).map((r) => [r.service_check_id, r.p95_ms])),
    [business],
  )
  const ranked = useMemo(() => rankServices(rows), [rows])
  const liveSegments: CountSegment[] = [
    { key: 'up', label: 'Up', value: live?.up ?? 0, tone: 'met' },
    { key: 'warning', label: 'Warning', value: (live?.warning ?? 0) + (live?.degraded ?? 0), tone: 'risk' },
    { key: 'down', label: 'Down', value: live?.down ?? 0, tone: 'breach' },
    { key: 'unknown', label: 'Unknown', value: live?.unknown ?? 0, tone: 'unknown' },
  ]
  const byType = useMemo(() => {
    const map = new Map<string, { type: string; total: number; measured: number; breached: number; failed: number }>()
    for (const r of rows) {
      const cur = map.get(r.type) ?? { type: r.type, total: 0, measured: 0, breached: 0, failed: 0 }
      cur.total += 1
      if (r.availability != null) cur.measured += 1
      if (r.compliance === 'breached') cur.breached += 1
      cur.failed += r.failedChecks
      map.set(r.type, cur)
    }
    return [...map.values()].sort((a, b) => b.total - a.total)
  }, [rows])
  const tls = business?.tls_warnings || []
  const tlsEvidence = business?.tls_evidence
  const confirmed = rows.some((r) => r.probeAvailability != null)

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Current service state" subtitle="Now — independent of the selected period">
          {live ? <CountDistribution segments={liveSegments} total={live.total} empty="No service checks configured" /> : <Skeleton className="h-16 w-full" />}
        </Panel>
        <Panel title="By service type" subtitle={c.periodLabel} className={cn(dimmed && 'opacity-60')}>
          <SimpleTable
            columns={[{ label: 'Type' }, { label: 'Services', numeric: true }, { label: 'Measured', numeric: true }, { label: 'Breached', numeric: true }, { label: 'Failed probes', numeric: true }]}
            empty={<EvidenceEmpty kind="no_evidence">No service checks are configured.</EvidenceEmpty>}
            rows={byType.map((t) => ({
              key: t.type,
              cells: [<span className="font-medium uppercase">{t.type}</span>, t.total, t.measured, <MatrixCount n={t.breached} tone="breach" />, t.failed.toLocaleString()],
            }))}
          />
        </Panel>
      </div>

      <Panel
        title="Service portfolio"
        subtitle={`${confirmed ? 'Availability on the confirmed-incident basis; probe success shown separately' : 'Raw probe success (legacy appliance)'} · ${c.periodLabel}`}
        className={cn(dimmed && 'opacity-60')}
      >
        {loading ? <Skeleton className="h-60 w-full" /> : (
          <SimpleTable
            caption="All services with target, compliance and ownership"
            columns={[
              { label: 'Service' }, { label: 'Group' }, { label: 'Criticality' }, { label: 'Owner' },
              { label: 'Target', numeric: true }, { label: 'Availability', numeric: true }, { label: 'Probe success', numeric: true },
              { label: 'Confirmed down', numeric: true }, { label: 'Failed probes', numeric: true }, { label: 'P95', numeric: true },
              { label: 'Compliance', numeric: true }, { label: 'Now', numeric: true },
            ]}
            empty={partial
              ? <EvidenceEmpty kind="partial">Service data did not load completely.</EvidenceEmpty>
              : <EvidenceEmpty kind="no_evidence">No service checks are configured.</EvidenceEmpty>}
            rows={ranked.map((r) => {
              const meta = complianceMeta(r.compliance)
              const p = p95.get(r.id)
              return {
                key: r.id,
                cells: [
                  <Link to={`/services/${r.id}`} className="font-medium text-primary hover:underline">{r.name}</Link>,
                  r.group ? <span className="text-text2">{r.group}</span> : <span className="text-muted">Not assigned</span>,
                  r.criticality ? <span className="capitalize text-text2">{r.criticality}</span> : <span className="text-muted">Not set</span>,
                  r.owner ? <span className="text-text2">{r.owner}</span> : <span className="text-muted">None</span>,
                  <span title={`Source: ${r.targetSource}`}>{fmtTarget(r.target)}</span>,
                  <AvailCell value={r.availability} target={r.target} />,
                  <span className="text-text2">{fmtAvail(r.probeAvailability, '—')}</span>,
                  fmtDuration(r.downtimeMin, '—'),
                  r.failedChecks.toLocaleString(),
                  p != null ? `${p.toFixed(0)} ms` : '—',
                  <StatusPill tone={meta.tone}>{meta.label}</StatusPill>,
                  <StatusPill tone={r.status === 'up' ? 'met' : r.status === 'down' ? 'breach' : r.status === 'unknown' ? 'unknown' : 'risk'}>
                    <span className="capitalize">{r.status}</span>
                  </StatusPill>,
                ],
              }
            })}
          />
        )}
      </Panel>

      <Panel
        title="Certificate continuity"
        subtitle={tlsEvidence
          ? `${tlsEvidence.with_expiry_data} of ${plural(tlsEvidence.eligible, 'HTTPS/TLS check')} report certificate expiry. Expiry data does not prove active TLS bindings.`
          : 'Certificate inventory coverage is not reported by this appliance version.'}
      >
        <SimpleTable
          columns={[{ label: 'Service' }, { label: 'Expires' }, { label: 'Days left', numeric: true }, { label: 'Risk', numeric: true }]}
          empty={tlsEvidence
            ? tlsEvidence.eligible === 0
              ? <EvidenceEmpty kind="no_evidence">No HTTPS or TLS checks are configured, so certificate risk is not assessed.</EvidenceEmpty>
              : tlsEvidence.with_expiry_data < tlsEvidence.eligible
                ? <EvidenceEmpty kind="partial">No expiry risks among {tlsEvidence.with_expiry_data} certificates with data; {plural(tlsEvidence.eligible - tlsEvidence.with_expiry_data, 'check')} without expiry data.</EvidenceEmpty>
                : <EvidenceEmpty kind="no_findings">No certificate expiry risks among {plural(tlsEvidence.with_expiry_data, 'certificate')}.</EvidenceEmpty>
            : <EvidenceEmpty kind="no_evidence">No expiry warnings reported; certificate coverage is unknown.</EvidenceEmpty>}
          rows={tls.slice(0, 15).map((t) => ({
            key: t.service_check_id,
            cells: [
              <Link to={`/services/${t.service_check_id}`} className="font-medium text-primary hover:underline">{t.name}</Link>,
              <span className="text-text2">{t.tls_expiry_date ? new Date(t.tls_expiry_date).toLocaleDateString() : 'Unknown'}</span>,
              t.days_remaining,
              <SeverityPill severity={t.severity} />,
            ],
          }))}
        />
      </Panel>
    </div>
  )
}

/* ── Servers (current snapshot) ───────────────────────────────────────── */

export function serverSegments(overview?: ServerMonitoringOverview): CountSegment[] {
  const s = overview?.status_counts || {}
  // Neutral/amber only: a health snapshot is not an availability SLA.
  return [
    { key: 'healthy', label: 'Healthy', value: s.healthy ?? 0, tone: 'neutral' },
    { key: 'warning', label: 'Warning', value: s.warning ?? 0, tone: 'risk' },
    { key: 'critical', label: 'Critical', value: s.critical ?? 0, tone: 'risk' },
    { key: 'stale', label: 'Stale (no telemetry)', value: s.stale ?? 0, tone: 'unknown' },
    { key: 'unknown', label: 'Unknown', value: s.unknown ?? 0, tone: 'unknown' },
  ]
}

export function SitesPanel({ overview }: { overview?: ServerMonitoringOverview }) {
  const total = overview?.total ?? 0
  const sites = overview?.sites || []
  const assigned = sites.reduce((s, x) => s + x.server_count, 0)
  const unassigned = Math.max(0, total - assigned)
  const rows = [...sites.filter((s) => s.server_count > 0).sort((a, b) => b.server_count - a.server_count)]
  return (
    <Panel title="Workloads by site" subtitle="Current enrolment">
      <SimpleTable
        columns={[{ label: 'Site' }, { label: 'Servers', numeric: true }]}
        empty={<EvidenceEmpty kind="no_evidence">No servers are enrolled.</EvidenceEmpty>}
        rows={[
          ...(unassigned ? [{ key: '__none__', cells: [<span className="inline-flex items-center gap-1.5 text-muted"><ToneIcon tone="unknown" className="h-3.5 w-3.5" />Not assigned</span>, unassigned] }] : []),
          ...rows.map((s) => ({ key: s.id, cells: [<span className="font-medium">{s.name}</span>, s.server_count] })),
        ]}
      />
    </Panel>
  )
}

export function PressureList({ title, items, unit = 'pct' }: { title: string; items: TopPressureItem[]; unit?: 'pct' | 'bps' }) {
  const fmtBps = (value: number) => {
    const units = ['B/s', 'KB/s', 'MB/s', 'GB/s', 'TB/s']
    let v = value
    let i = 0
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1 }
    return `${v.toFixed(1)} ${units[i]}`
  }
  const max = Math.max(1, ...items.map((i) => i.value))
  return (
    <Panel title={title} subtitle="Now">
      {items.length === 0 ? <EvidenceEmpty kind="no_evidence">No utilisation data reported.</EvidenceEmpty> : (
        <ul className="space-y-2">
          {items.slice(0, 6).map((item) => {
            const tone: Tone = unit === 'pct' ? (item.value >= 90 ? 'breach' : item.value >= 75 ? 'risk' : 'neutral') : 'neutral'
            const width = unit === 'pct' ? Math.min(100, item.value) : (item.value / max) * 100
            return (
              <li key={item.server_id}>
                <Link to={`/servers/${item.server_id}`} className="block rounded-lg px-1 py-1 hover:bg-surface2/50">
                  <div className="flex items-center justify-between gap-2 text-base">
                    <span className="truncate text-text">{item.display_name || item.hostname || item.server_id.slice(0, 8)}</span>
                    <span className={cn('inline-flex items-center gap-1 font-semibold tabular-nums', tone === 'neutral' ? 'text-text' : TONE[tone].text)}>
                      {tone !== 'neutral' && <ToneIcon tone={tone} className="h-3.5 w-3.5" />}
                      {unit === 'pct' ? `${item.value.toFixed(1)}%` : fmtBps(item.value)}
                    </span>
                  </div>
                  <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface2">
                    <div className={cn('h-full rounded-full', tone === 'neutral' ? 'bg-primary/70' : TONE[tone].dot)} style={{ width: `${Math.max(2, width)}%` }} />
                  </div>
                </Link>
              </li>
            )
          })}
        </ul>
      )}
    </Panel>
  )
}

export function ServersOperations({ overview }: { overview?: ServerMonitoringOverview }) {
  const total = overview?.total ?? 0
  const os: CountSegment[] = Object.entries(overview?.os_counts || {}).map(([k, v]) => ({ key: k, label: k[0].toUpperCase() + k.slice(1), value: v ?? 0, tone: 'neutral' as Tone }))
  const agents: CountSegment[] = Object.entries(overview?.agent_counts || {}).map(([k, v]) => ({
    key: k,
    label: k[0].toUpperCase() + k.slice(1),
    value: v ?? 0,
    tone: (k === 'online' ? 'neutral' : k === 'offline' || k === 'error' ? 'risk' : 'unknown') as Tone,
  }))
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 lg:grid-cols-3">
        <Panel title="Server health" subtitle="Now">
          {overview ? <CountDistribution segments={serverSegments(overview)} total={total} empty="No servers enrolled" /> : <Skeleton className="h-16 w-full" />}
        </Panel>
        <Panel title="Operating systems" subtitle="Estate mix">
          <CountDistribution segments={os} total={total} empty="No servers enrolled" />
        </Panel>
        <Panel title="Telemetry coverage" subtitle="Agents">
          <CountDistribution segments={agents} total={Math.max(total, agents.reduce((s, a) => s + a.value, 0))} empty="No agents enrolled" />
        </Panel>
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <PressureList title="CPU utilisation" items={overview?.top_cpu || []} />
        <PressureList title="Memory utilisation" items={overview?.top_memory || []} />
        <PressureList title="Disk utilisation" items={overview?.top_disk || []} />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <PressureList title="Highest network throughput" items={overview?.top_network || []} unit="bps" />
        <SitesPanel overview={overview} />
      </div>
      <p className="text-sm text-muted">
        Resource utilisation is operational context, not business impact. Servers use a current health snapshot; no historical availability is measured for them.
      </p>
    </div>
  )
}
