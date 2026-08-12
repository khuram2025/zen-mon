/**
 * Application team dashboard.
 *
 * The app owners' one screen: golden signals across every instrumented
 * service, SLO error budgets, synthetic journeys, the error inbox and the
 * uptime checks that front the apps.
 *
 * Sources: /apm/services (+ per-service /red), /apm/slos (+ /budget),
 * /apm/synthetics, /apm/errors, /apm/data-quality, /service-checks/summary.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  Boxes,
  Bug,
  Gauge as GaugeIcon,
  HeartPulse,
  Radio,
  Shield,
  Target,
  Workflow,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api } from '@/lib/api'
import { Card } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { relativeTime } from '@/lib/utils'
import { HealthBadge, fmtMs, fmtPct, fmtRps } from '@/components/apm/shared'
import type {
  DataQuality, ErrorListResponse, REDPoint, ServiceListResponse, ServiceRED, Slo, SloBudget,
  SyntheticMonitor,
} from '@/types/apm'
import {
  Empty,
  KpiCard,
  LiveClock,
  PctBar,
  RangeKey,
  RangePills,
  SectionHeader,
  SERIES,
  TeamHeader,
  chartTooltipStyle,
  fmtCount,
} from './shared'

type ServiceCheckSummary = { total: number; up: number; down: number; warning: number; degraded: number; unknown: number }

const HEALTH_RANK: Record<string, number> = { critical: 0, degraded: 1, no_data: 2, healthy: 3 }

export function ApplicationTeamDashboard() {
  const [rangeKey, setRangeKey] = useState<RangeKey>('1h')
  const apmRange = rangeKey // APM endpoints accept 1h/6h/24h/7d range keys

  const services = useQuery<ServiceListResponse>({
    queryKey: ['appteam', 'services', apmRange],
    queryFn: async () => (await api.get(`/apm/services?range=${apmRange}`)).data,
    refetchInterval: 15_000,
    retry: 1,
  })
  const list = services.data?.services ?? []

  const errors = useQuery<ErrorListResponse>({
    queryKey: ['appteam', 'errors'],
    queryFn: async () => (await api.get('/apm/errors?range_=24h&status=unresolved')).data,
    refetchInterval: 30_000,
    retry: 1,
  }).data

  const slos = useQuery<{ items: Slo[] }>({
    queryKey: ['appteam', 'slos'],
    queryFn: async () => (await api.get('/apm/slos')).data,
    refetchInterval: 60_000,
    retry: 1,
  }).data

  const sloIds = useMemo(() => (slos?.items ?? []).slice(0, 6).map((s) => s.id), [slos])
  const budgets = useQuery<SloBudget[]>({
    queryKey: ['appteam', 'slo-budgets', sloIds],
    enabled: sloIds.length > 0,
    refetchInterval: 60_000,
    retry: 1,
    queryFn: async () => {
      const out = await Promise.all(
        sloIds.map(async (id) => {
          try { return (await api.get<SloBudget>(`/apm/slos/${id}/budget`)).data } catch { return null }
        }),
      )
      return out.filter((b): b is SloBudget => b != null)
    },
  }).data

  const synthetics = useQuery<{ monitors: SyntheticMonitor[]; summary: Record<string, number> }>({
    queryKey: ['appteam', 'synthetics'],
    queryFn: async () => (await api.get('/apm/synthetics')).data,
    refetchInterval: 60_000,
    retry: 1,
  }).data

  const quality = useQuery<DataQuality>({
    queryKey: ['appteam', 'quality'],
    queryFn: async () => (await api.get('/apm/data-quality?hours=24')).data,
    refetchInterval: 60_000,
    retry: 1,
  }).data

  const checks = useQuery<ServiceCheckSummary>({
    queryKey: ['appteam', 'checks'],
    queryFn: async () => (await api.get('/service-checks/summary')).data,
    refetchInterval: 30_000,
    retry: 1,
  }).data

  const activeAlerts = useQuery<{ data: Array<{
    id: string; server_id: string | null; device_id: string | null; service_check_id: string | null
    service_check_name: string | null; severity: string; message: string; triggered_at: string
    metadata?: { metric?: string }
  }> }>({
    queryKey: ['appteam', 'alerts'],
    queryFn: async () => (await api.get('/alerts?status=active&limit=100')).data,
    refetchInterval: 15_000,
  }).data?.data

  /* — derived — */

  const healthyCount = list.filter((s) => s.health === 'healthy').length
  const criticalCount = list.filter((s) => s.health === 'critical').length
  const degradedCount = list.filter((s) => s.health === 'degraded').length

  const totalRps = list.reduce((a, s) => a + s.rps, 0)
  const totalReqs = list.reduce((a, s) => a + s.request_count, 0)
  const fleetErrorRate = totalReqs > 0
    ? list.reduce((a, s) => a + s.request_count * s.error_rate, 0) / totalReqs
    : 0
  // No exact fleet p95 exists across digests; the worst service is the honest headline.
  const worstP95 = list.reduce((a, s) => Math.max(a, s.p95_ms), 0)

  const attention = useMemo(
    () => [...list]
      .filter((s) => s.health !== 'healthy')
      .sort((a, b) =>
        (HEALTH_RANK[a.health] ?? 9) - (HEALTH_RANK[b.health] ?? 9)
        || b.error_rate - a.error_rate || b.p95_ms - a.p95_ms)
      .slice(0, 7),
    [list],
  )

  const busiest = useMemo(
    () => [...list].sort((a, b) => b.request_count - a.request_count).slice(0, 5),
    [list],
  )

  // Fleet trend from the busiest services' RED series (one request per service,
  // merged client-side — same approach as the APM overview).
  const topNames = useMemo(() => busiest.map((s) => s.name), [busiest])
  const trends = useQuery<REDPoint[]>({
    queryKey: ['appteam', 'fleet-red', apmRange, topNames],
    enabled: topNames.length > 0,
    refetchInterval: 30_000,
    retry: 1,
    queryFn: async () => {
      const series = await Promise.all(topNames.map(async (name) =>
        (await api.get<REDPoint[]>(`/apm/services/${encodeURIComponent(name)}/red?range=${apmRange}`)).data))
      const merged = new Map<string, { rps: number; reqs: number; errs: number; p95: number }>()
      series.forEach((points, i) => {
        const svc = list.find((s) => s.name === topNames[i])
        const bucketReqs = svc && points.length ? svc.request_count / points.length : 0
        points.forEach((p) => {
          const slot = merged.get(p.timestamp) ?? { rps: 0, reqs: 0, errs: 0, p95: 0 }
          slot.rps += p.rps
          slot.reqs += bucketReqs
          slot.errs += bucketReqs * p.error_rate
          slot.p95 = Math.max(slot.p95, p.p95_ms)
          merged.set(p.timestamp, slot)
        })
      })
      return [...merged.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([timestamp, v]) => ({
          timestamp,
          rps: Number(v.rps.toFixed(3)),
          error_rate: v.reqs > 0 ? v.errs / v.reqs : 0,
          p50_ms: 0,
          p95_ms: Number(v.p95.toFixed(2)),
        }))
    },
  }).data

  const slosAtRisk = (budgets ?? []).filter((b) => (b.budget_remaining ?? 1) < 0.25).length
  const monitors = synthetics?.monitors ?? []
  const monitorsDown = monitors.filter((m) => m.status === 'down').length
  const openIssues = errors?.issues ?? []

  const appAlerts = useMemo(
    () => (activeAlerts || []).filter((a) =>
      a.service_check_id != null || (a.metadata?.metric || '').startsWith('apm_')),
    [activeAlerts],
  )

  const ingestHealthy = quality ? quality.health === 'healthy' && quality.issues.length === 0 : null

  /* — render — */

  return (
    <div className="space-y-4 animate-fade-in">
      <TeamHeader
        title="Application Operations"
        subtitle={list.length
          ? <>{list.length} instrumented services · {fmtRps(totalRps)} fleet throughput · refreshes every 15s</>
          : 'Golden signals, error budgets and user journeys · refreshes every 15s'}
        right={<><LiveClock /><RangePills value={rangeKey} onChange={setRangeKey} keys={['1h', '6h', '24h']} /></>}
      />

      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <KpiCard to="/apm/services" label="Services" icon={<Boxes className="h-4 w-4" />}
          accent={criticalCount > 0 ? 'danger' : degradedCount > 0 ? 'warning' : 'success'}
          value={list.length
            ? <>{healthyCount}<span className="text-base font-medium text-muted">/{list.length}</span></>
            : '—'}
          sub={criticalCount > 0
            ? <span className="text-[10.5px] font-semibold text-danger">{criticalCount} critical</span>
            : degradedCount > 0
              ? <span className="text-[10.5px] text-warning">{degradedCount} degraded</span>
              : list.length ? <span className="text-[10.5px] text-success">all healthy</span>
                : <span className="text-[10.5px] text-muted">no services reporting</span>}
          foot={<ApmHealthBar healthy={healthyCount} degraded={degradedCount} critical={criticalCount} noData={list.length - healthyCount - degradedCount - criticalCount} />}
        />
        <KpiCard to="/apm" label="Throughput" icon={<Activity className="h-4 w-4" />} accent="info"
          value={fmtRps(totalRps)}
          sub={<span className="text-[10.5px] text-muted">{fmtCount(totalReqs)} requests · {rangeKey}</span>}
          foot={<div className="text-[10px] text-muted">sum across services</div>}
        />
        <KpiCard to="/apm/services" label="Error Rate" icon={<Bug className="h-4 w-4" />}
          accent={fleetErrorRate >= 0.05 ? 'danger' : fleetErrorRate >= 0.01 ? 'warning' : 'success'}
          value={fmtPct(fleetErrorRate)}
          sub={<span className="text-[10.5px] text-muted">request-weighted fleet rate</span>}
          foot={<PctBar value={fleetErrorRate * 100} warnAt={1} dangerAt={5} />}
        />
        <KpiCard to="/apm/services" label="Worst p95" icon={<GaugeIcon className="h-4 w-4" />}
          accent={worstP95 >= 2000 ? 'danger' : worstP95 >= 800 ? 'warning' : 'success'}
          value={worstP95 ? fmtMs(worstP95) : '—'}
          sub={<span className="text-[10.5px] text-muted">slowest service latency</span>}
          foot={<div className="text-[10px] text-muted">what a user actually waits on</div>}
        />
        <KpiCard to="/apm/slos" label="SLO Budgets" icon={<Target className="h-4 w-4" />}
          accent={slosAtRisk > 0 ? 'danger' : (slos?.items?.length ?? 0) > 0 ? 'success' : 'primary'}
          value={slos?.items?.length ? <>{slosAtRisk}<span className="text-base font-medium text-muted"> at risk</span></> : '—'}
          sub={<span className="text-[10.5px] text-muted">{slos?.items?.length ?? 0} objectives tracked</span>}
          foot={<div className="text-[10px] text-muted">&lt;25% error budget left = at risk</div>}
        />
        <KpiCard to="/apm/synthetics" label="Synthetics" icon={<Workflow className="h-4 w-4" />}
          accent={monitorsDown > 0 ? 'danger' : monitors.length ? 'success' : 'primary'}
          value={monitors.length
            ? <>{monitors.length - monitorsDown}<span className="text-base font-medium text-muted">/{monitors.length}</span></>
            : '—'}
          sub={monitorsDown > 0
            ? <span className="text-[10.5px] font-semibold text-danger">{monitorsDown} journeys failing</span>
            : <span className="text-[10.5px] text-success">journeys passing</span>}
          foot={<div className="text-[10px] text-muted">scripted user flows</div>}
        />
        <KpiCard to="/apm/errors" label="Open Errors" icon={<AlertTriangle className="h-4 w-4" />}
          accent={openIssues.length > 0 ? 'warning' : 'success'}
          value={fmtCount(errors?.counts?.unresolved ?? openIssues.length)}
          sub={<span className="text-[10.5px] text-muted">unresolved groups · 24h</span>}
          foot={<div className="text-[10px] text-muted">{fmtCount(openIssues.reduce((s, i) => s + i.occurrences, 0))} occurrences</div>}
        />
        <KpiCard to="/services" label="Uptime Checks" icon={<Shield className="h-4 w-4" />}
          accent={(checks?.down ?? 0) > 0 ? 'danger' : 'success'}
          value={checks ? <>{checks.up}<span className="text-base font-medium text-muted">/{checks.total}</span></> : '—'}
          sub={checks && checks.down > 0
            ? <span className="text-[10.5px] font-semibold text-danger">{checks.down} down</span>
            : <span className="text-[10.5px] text-success">all passing</span>}
          foot={<div className="text-[10px] text-muted">HTTP · TCP · DNS probes</div>}
        />
      </div>

      {/* Fleet golden signals — one measure per chart, shared time axis */}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <FleetChart data={trends ?? []} dataKey="rps" color={SERIES.requests} label={`Requests / s · top ${topNames.length} services`} fmt={(v) => fmtRps(v)} />
        <FleetChart data={trends ?? []} dataKey="error_rate" color={SERIES.errors} label="Error rate" fmt={(v) => fmtPct(v)} />
        <FleetChart data={trends ?? []} dataKey="p95_ms" color={SERIES.latency} label="p95 latency" fmt={(v) => fmtMs(v)} />
      </div>

      {/* Services + SLOs */}
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-12">
        <Card className="xl:col-span-7">
          <SectionHeader icon={<Boxes className="h-3.5 w-3.5" />} title="Services Needing Attention"
            hint={`${attention.length} of ${list.length}`}
            right={<Link to="/apm/services" className="text-xs text-primary hover:underline">All services →</Link>} />
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-left text-[10px] uppercase tracking-wider text-muted">
                  <th className="px-4 py-2 font-semibold">Service</th>
                  <th className="px-2 py-2 font-semibold">Health</th>
                  <th className="px-2 py-2 text-right font-semibold">Req/s</th>
                  <th className="px-2 py-2 text-right font-semibold">Errors</th>
                  <th className="px-2 py-2 text-right font-semibold">p95</th>
                  <th className="px-2 py-2 text-right font-semibold">Apdex</th>
                </tr>
              </thead>
              <tbody>
                {!attention.length && (
                  <tr><td colSpan={6}><Empty text={list.length ? 'Every service is healthy 🎉' : 'No services reporting — instrument an app via APM Settings'} /></td></tr>
                )}
                {attention.map((s) => <ServiceRow key={s.name} s={s} />)}
                {attention.length > 0 && attention.length < 4 && busiest
                  .filter((b) => !attention.some((a) => a.name === b.name))
                  .slice(0, 4 - attention.length)
                  .map((s) => <ServiceRow key={s.name} s={s} dim />)}
              </tbody>
            </table>
          </div>
        </Card>

        <Card className="xl:col-span-5">
          <SectionHeader icon={<Target className="h-3.5 w-3.5" />} title="SLO Error Budgets" hint="rolling windows"
            right={<Link to="/apm/slos" className="text-xs text-primary hover:underline">SLOs →</Link>} />
          <div className="space-y-2.5 px-4 pb-4 pt-3">
            {!sloIds.length && <Empty text="No SLOs defined — create objectives in APM → SLOs" />}
            {(budgets ?? []).map((b) => {
              const remaining = b.budget_remaining
              const pct = remaining != null ? Math.max(0, Math.min(100, remaining * 100)) : null
              const tone = pct == null ? 'bg-muted' : pct < 10 ? 'bg-danger' : pct < 25 ? 'bg-warning' : 'bg-success'
              return (
                <div key={b.slo.id}>
                  <div className="flex items-baseline justify-between gap-2 text-xs">
                    <span className="min-w-0 truncate">
                      <span className="font-medium text-text">{b.slo.name}</span>
                      <span className="ml-1.5 text-[10px] text-muted">{b.slo.service_name} · {b.slo.sli_type} · {b.slo.target}%</span>
                    </span>
                    <span className={`shrink-0 font-mono text-[11px] font-semibold tabular-nums ${pct != null && pct < 25 ? 'text-danger' : 'text-text2'}`}>
                      {pct != null ? `${pct.toFixed(0)}% left` : 'no traffic'}
                    </span>
                  </div>
                  <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-surface2">
                    <div className={`h-full rounded-full ${tone}`} style={{ width: `${Math.max(2, pct ?? 0)}%` }} />
                  </div>
                  <div className="mt-0.5 text-[9.5px] text-muted">
                    {fmtCount(b.window_requests)} requests · {b.window_days}d window
                  </div>
                </div>
              )
            })}
          </div>
        </Card>
      </div>

      {/* Synthetics · errors · app alerts */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2 xl:grid-cols-12">
        <Card className="xl:col-span-4">
          <SectionHeader icon={<Workflow className="h-3.5 w-3.5" />} title="Synthetic Journeys"
            right={<Link to="/apm/synthetics" className="text-xs text-primary hover:underline">Synthetics →</Link>} />
          <div className="space-y-1.5 px-3 pb-3 pt-2">
            {!monitors.length && <Empty text="No synthetic monitors configured" />}
            {monitors.slice(0, 7).map((m) => (
              <div key={m.id} className="flex items-center gap-2.5 rounded-md px-2 py-1.5 transition hover:bg-surface2/50">
                <span className={`h-2 w-2 shrink-0 rounded-full ${m.status === 'down' ? 'bg-danger' : m.status === 'up' ? 'bg-success' : 'bg-muted/60'}`} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium text-text">{m.name}</div>
                  <div className="text-[10px] text-muted">
                    {m.uptime_pct != null ? `${m.uptime_pct.toFixed(2)}% uptime` : 'no runs yet'}
                    {m.avg_ms != null ? ` · avg ${fmtMs(m.avg_ms)}` : ''}
                  </div>
                </div>
                <span className="shrink-0 text-[10px] tabular-nums text-muted">{m.last_run_at ? relativeTime(m.last_run_at) : '—'}</span>
              </div>
            ))}
          </div>
        </Card>

        <Card className="xl:col-span-5">
          <SectionHeader icon={<Bug className="h-3.5 w-3.5" />} title="Error Inbox" hint="unresolved · 24h"
            right={<Link to="/apm/errors" className="text-xs text-primary hover:underline">All errors →</Link>} />
          <div className="px-3 pb-3 pt-2">
            {!openIssues.length && <Empty text="Error inbox is clear 🎉" />}
            <div className="space-y-0.5">
              {openIssues.slice(0, 7).map((e) => (
                <Link key={e.group_id} to={`/apm/errors/${e.group_id}`} className="flex gap-2.5 rounded-md px-2 py-1.5 transition hover:bg-surface2/50">
                  <div className="mt-0.5 w-1 shrink-0 self-stretch rounded-full bg-danger" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="truncate font-mono text-xs font-semibold text-text">{e.exception_type}</span>
                      <span className="shrink-0 text-[10px] tabular-nums text-muted">{relativeTime(e.last_seen)}</span>
                    </div>
                    <div className="truncate text-[11px] text-text2" title={e.message}>{e.message}</div>
                    <div className="text-[9.5px] text-muted">{e.service}{e.http_route ? ` · ${e.http_route}` : ''}</div>
                  </div>
                  <span className="self-center rounded bg-danger/15 px-1.5 font-mono text-[10px] font-bold text-danger">
                    ×{fmtCount(e.occurrences)}
                  </span>
                </Link>
              ))}
            </div>
          </div>
        </Card>

        <Card className="xl:col-span-3">
          <SectionHeader icon={<HeartPulse className="h-3.5 w-3.5" />} title="App Alerts & Ingest" />
          <div className="space-y-3 px-4 pb-4 pt-3">
            <div>
              <div className="pb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">Active app alerts</div>
              {!appAlerts.length && <div className="text-[10.5px] text-muted">Nothing firing for checks or APM rules</div>}
              <div className="space-y-1">
                {appAlerts.slice(0, 4).map((a) => (
                  <Link to={`/alerts/${a.id}`} key={a.id} className="flex items-center gap-2 rounded-md px-1.5 py-1 text-xs transition hover:bg-surface2/50">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${a.severity === 'critical' ? 'bg-danger' : a.severity === 'warning' ? 'bg-warning' : 'bg-info'}`} />
                    <span className="min-w-0 flex-1 truncate text-text2">{a.service_check_name || a.message.replace(/^\[[^\]]+\]\s*/, '')}</span>
                    <span className="shrink-0 text-[10px] tabular-nums text-muted">{relativeTime(a.triggered_at)}</span>
                  </Link>
                ))}
              </div>
            </div>
            <div className="border-t border-border pt-3">
              <div className="pb-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">Telemetry ingest · 24h</div>
              <div className="flex items-center gap-2 text-xs">
                <Radio className={`h-3.5 w-3.5 ${ingestHealthy === false ? 'text-warning' : 'text-success'}`} />
                <span className="text-text2">
                  {quality
                    ? ingestHealthy
                      ? 'Ingest pipeline healthy'
                      : (quality.issues[0] || 'Ingest issues detected')
                    : 'awaiting ingest stats'}
                </span>
              </div>
              {quality && (
                <div className="mt-1.5 space-y-0.5 text-[10px] text-muted">
                  <div>{fmtCount(quality.ingest.accepted)} spans accepted</div>
                  <div>{fmtCount(quality.ingest.rejected)} rejected · {fmtCount(quality.ingest.dropped)} dropped</div>
                  <div>{quality.services.filter((s) => !s.reporting).length} services gone silent</div>
                </div>
              )}
              <div className="mt-1.5 text-[10px]">
                <Link to="/apm/usage" className="text-primary hover:underline">Ingest usage →</Link>
              </div>
            </div>
          </div>
        </Card>
      </div>
    </div>
  )
}

/* ── Local building blocks ──────────────────────────────────────────────── */

function ServiceRow({ s, dim }: { s: ServiceRED; dim?: boolean }) {
  return (
    <tr className={`border-b border-border/50 transition hover:bg-surface2/40 ${dim ? 'opacity-70' : ''}`}>
      <td className="max-w-[220px] px-4 py-2">
        <Link to={`/apm/services/${encodeURIComponent(s.name)}`} className="block min-w-0">
          <span className="block truncate font-medium text-text">{s.name}</span>
          <span className="block truncate text-[10px] text-muted">{s.envs?.join(', ') || '—'}</span>
        </Link>
      </td>
      <td className="px-2 py-2"><HealthBadge health={s.health} /></td>
      <td className="px-2 py-2 text-right font-mono text-[11px] tabular-nums text-text2">{fmtRps(s.rps)}</td>
      <td className={`px-2 py-2 text-right font-mono text-[11px] tabular-nums ${s.error_rate >= 0.05 ? 'text-danger' : s.error_rate >= 0.01 ? 'text-warning' : 'text-text2'}`}>
        {fmtPct(s.error_rate)}
      </td>
      <td className="px-2 py-2 text-right font-mono text-[11px] tabular-nums text-text2">{fmtMs(s.p95_ms)}</td>
      <td className="px-2 py-2 text-right font-mono text-[11px] tabular-nums text-text2">{s.apdex != null ? s.apdex.toFixed(2) : '—'}</td>
    </tr>
  )
}

function ApmHealthBar({ healthy, degraded, critical, noData }: { healthy: number; degraded: number; critical: number; noData: number }) {
  const total = healthy + degraded + critical + noData
  if (!total) return <div className="h-1.5 rounded-full bg-surface2" />
  const seg = (n: number) => `${(n / total) * 100}%`
  return (
    <div className="flex h-1.5 w-full gap-px overflow-hidden rounded-full bg-surface2">
      <div style={{ width: seg(healthy) }} className="bg-success" />
      <div style={{ width: seg(degraded) }} className="bg-warning" />
      <div style={{ width: seg(critical) }} className="bg-danger" />
      <div style={{ width: seg(noData) }} className="bg-muted/60" />
    </div>
  )
}

function FleetChart({ data, dataKey, color, label, fmt }: {
  data: REDPoint[]
  dataKey: 'rps' | 'error_rate' | 'p95_ms'
  color: string
  label: string
  fmt: (v: number) => string
}) {
  const gid = `appteam-${dataKey}`
  return (
    <Card>
      <div className="px-4 pt-3 text-[10.5px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="h-[132px] px-2 pb-2 pt-1">
        {!data.length ? (
          <div className="flex h-full items-center justify-center text-xs text-muted">awaiting trace data</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 10, right: 10, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={color} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={color} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="rgb(var(--border))" strokeOpacity={0.5} strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="timestamp" stroke="rgb(var(--muted))" fontSize={10} tickLine={false} axisLine={false} minTickGap={44}
                tickFormatter={(t: string) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
              />
              <YAxis stroke="rgb(var(--muted))" fontSize={10} tickLine={false} axisLine={false} width={46} tickFormatter={(v: number) => fmt(v)} />
              <Tooltip
                contentStyle={chartTooltipStyle}
                labelFormatter={(t) => new Date(String(t)).toLocaleString()}
                formatter={(v: number | string) => [fmt(Number(v)), label]}
              />
              <Area type="monotone" dataKey={dataKey} stroke={color} strokeWidth={2} fill={`url(#${gid})`} isAnimationActive={false} dot={false} activeDot={{ r: 3, fill: color }} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </Card>
  )
}
