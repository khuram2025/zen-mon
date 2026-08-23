import {
  Activity,
  AlertOctagon,
  Clock,
  Server,
  Target,
  Bell,
  TrendingUp,
  MapPin,
  AlertTriangle,
} from 'lucide-react'
import {
  AreaChart,
  Area,
  CartesianGrid,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts'
import { useTimeRange } from '@/components/TimeRangePicker'
import { useExecutiveReport } from '@/hooks/useReports'
import { KpiTile } from '@/components/reports/KpiTile'
import { ReportSection, EmptyReportState } from '@/components/reports/ReportSection'
import { Top10Table } from '@/components/reports/Top10Table'
import { Skeleton } from '@/components/ui/Skeleton'
import { cn } from '@/lib/utils'

function fmtPct(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  return `${v.toFixed(2)}%`
}

function fmtMin(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  if (v < 1) return `${(v * 60).toFixed(0)}s`
  if (v < 60) return `${v.toFixed(1)}m`
  return `${(v / 60).toFixed(1)}h`
}

const SEV_COLORS: Record<string, string> = {
  critical: 'text-rose-400 bg-rose-500/10 border-rose-500/30',
  warning: 'text-amber-400 bg-amber-500/10 border-amber-500/30',
  info: 'text-sky-400 bg-sky-500/10 border-sky-500/30',
}

export default function ExecutiveReport() {
  const { range } = useTimeRange()
  const { data, isLoading, error } = useExecutiveReport({ fromISO: range.fromISO, toISO: range.toISO })

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-[100px] rounded-lg" />
          ))}
        </div>
        <Skeleton className="h-[280px] rounded-lg" />
      </div>
    )
  }

  if (error || !data) {
    return (
      <ReportSection title="Executive Summary" icon={<Activity className="h-4 w-4" />}>
        <EmptyReportState message="Failed to load executive report. Try a different time range." />
      </ReportSection>
    )
  }

  const k = data.kpis
  const slaMet = k.sla_attained_pct >= k.sla_target_pct

  return (
    <div className="space-y-5">
      {/* KPI Strip */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        <KpiTile
          label="Availability"
          value={fmtPct(k.availability_pct)}
          delta={k.availability_delta_pct}
          accent={k.availability_pct && k.availability_pct >= 99 ? 'success' : 'warning'}
          icon={<Activity className="h-4 w-4" />}
        />
        <KpiTile
          label="Active Critical"
          value={k.active_critical_count}
          accent={k.active_critical_count > 0 ? 'danger' : 'success'}
          icon={<AlertOctagon className="h-4 w-4" />}
          subtitle={k.active_critical_count > 0 ? 'Investigate now' : 'All clear'}
        />
        <KpiTile
          label="MTTR"
          value={fmtMin(k.mttr_minutes)}
          delta={k.mttr_delta_minutes}
          invertDelta
          accent="info"
          icon={<Clock className="h-4 w-4" />}
        />
        <KpiTile
          label="Devices Monitored"
          value={k.devices_monitored}
          accent="primary"
          icon={<Server className="h-4 w-4" />}
        />
        <KpiTile
          label={`SLA  (target ${k.sla_target_pct}%)`}
          value={fmtPct(k.sla_attained_pct)}
          accent={slaMet ? 'success' : 'danger'}
          icon={<Target className="h-4 w-4" />}
          subtitle={slaMet ? 'Target met' : `${(k.sla_target_pct - k.sla_attained_pct).toFixed(2)}% short`}
        />
        <KpiTile
          label="Incidents"
          value={k.incidents_count}
          delta={k.incidents_delta}
          invertDelta
          accent={k.incidents_count > 10 ? 'warning' : 'info'}
          icon={<Bell className="h-4 w-4" />}
        />
      </div>

      {/* Availability Trend */}
      <ReportSection
        title="Availability Trend"
        icon={<TrendingUp className="h-4 w-4" />}
        description="Network-wide ping availability over the selected window"
      >
        {data.availability_trend.length === 0 ? (
          <EmptyReportState />
        ) : (
          <div className="h-[280px]">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={data.availability_trend} margin={{ top: 10, right: 16, left: -10, bottom: 0 }}>
                <defs>
                  <linearGradient id="availFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#6366F1" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="#6366F1" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(148, 163, 184, 0.15)" vertical={false} />
                <XAxis
                  dataKey="ts"
                  tickFormatter={(v) => {
                    const d = new Date(v)
                    return range.hours <= 48
                      ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                      : d.toLocaleDateString([], { month: 'short', day: 'numeric' })
                  }}
                  tick={{ fontSize: 11, fill: '#9BA1B0' }}
                  axisLine={false}
                  tickLine={false}
                  minTickGap={24}
                />
                <YAxis
                  domain={[
                    (dataMin: number) => Math.max(0, Math.floor(dataMin - 5)),
                    100,
                  ]}
                  tickFormatter={(v) => `${v}%`}
                  tick={{ fontSize: 11, fill: '#9BA1B0' }}
                  axisLine={false}
                  tickLine={false}
                  width={45}
                />
                <Tooltip
                  contentStyle={{ background: '#1A1D27', border: '1px solid #2D3140', borderRadius: 6, fontSize: 12 }}
                  labelFormatter={(v) => new Date(v as string).toLocaleString()}
                  formatter={(v: number) => [`${v?.toFixed(2)}%`, 'Availability']}
                />
                <ReferenceLine y={k.sla_target_pct} stroke="#22C55E" strokeDasharray="4 4" label={{ value: `SLA ${k.sla_target_pct}%`, position: 'right', fill: '#22C55E', fontSize: 10 }} />
                <Area type="monotone" dataKey="availability_pct" stroke="#6366F1" strokeWidth={2} fill="url(#availFill)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </ReportSection>

      <div className="grid gap-5 lg:grid-cols-2">
        {/* Top issues */}
        <ReportSection
          title="Top Issues"
          icon={<AlertTriangle className="h-4 w-4" />}
          description="Devices contributing most to downtime or alerts"
          padded={false}
        >
          {data.top_issues.length === 0 ? (
            <div className="px-5 pb-5"><EmptyReportState message="No incidents in this window." /></div>
          ) : (
            <ul className="divide-y divide-border">
              {data.top_issues.map((issue) => (
                <li key={issue.device_id} className="flex items-center gap-3 px-5 py-3">
                  <span className={cn('flex h-8 w-8 shrink-0 items-center justify-center rounded-md border', SEV_COLORS[issue.severity] || SEV_COLORS.info)}>
                    <AlertOctagon className="h-4 w-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{issue.hostname}</p>
                    <p className="text-xs text-muted">
                      {issue.alert_count} alert{issue.alert_count === 1 ? '' : 's'} · {fmtMin(issue.duration_minutes)} downtime
                    </p>
                  </div>
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">{issue.severity}</span>
                </li>
              ))}
            </ul>
          )}
        </ReportSection>

        {/* Location summary */}
        <ReportSection
          title="Health by Location"
          icon={<MapPin className="h-4 w-4" />}
          description="Per-site device count and availability over the report window"
          padded={false}
        >
          {data.location_summary.length === 0 ? (
            <div className="px-5 pb-5"><EmptyReportState /></div>
          ) : (
            <Top10Table
              rows={data.location_summary}
              columns={[
                { key: 'location', header: 'Location' },
                { key: 'devices', header: 'Devices', align: 'right' },
                {
                  key: 'down',
                  header: 'Down',
                  align: 'right',
                  render: (r) => (
                    <span className={r.down > 0 ? 'font-semibold text-rose-400' : 'text-muted'}>{r.down}</span>
                  ),
                },
                {
                  key: 'availability_pct',
                  header: 'Availability',
                  align: 'right',
                  render: (r) => {
                    // Null when the site reported no samples in the window.
                    if (r.availability_pct == null) return <span className="text-muted">—</span>
                    const ok = r.availability_pct >= 99
                    return <span className={ok ? 'text-emerald-400' : 'text-amber-400'}>{r.availability_pct.toFixed(1)}%</span>
                  },
                },
              ]}
              defaultSortKey="devices"
              defaultSortDir="desc"
            />
          )}
        </ReportSection>
      </div>

      {/* Outage timeline */}
      <ReportSection
        title="Recent Outages"
        icon={<Clock className="h-4 w-4" />}
        description="Latest device-down events in this window"
        padded={false}
      >
        {data.outage_timeline.length === 0 ? (
          <div className="px-5 pb-5"><EmptyReportState message="No device outages recorded." /></div>
        ) : (
          <Top10Table
            rows={data.outage_timeline.slice(0, 15)}
            columns={[
              {
                key: 'started_at',
                header: 'Started',
                render: (r) => r.started_at ? new Date(r.started_at).toLocaleString() : '—',
                sortValue: (r) => r.started_at ? Date.parse(r.started_at) : 0,
              },
              { key: 'hostname', header: 'Device' },
              { key: 'kind', header: 'Type' },
              {
                key: 'duration_minutes',
                header: 'Duration',
                align: 'right',
                render: (r) => fmtMin(r.duration_minutes),
                sortValue: (r) => r.duration_minutes,
              },
            ]}
            defaultSortKey="started_at"
            defaultSortDir="desc"
            rowKey={(_, i) => `${i}`}
          />
        )}
      </ReportSection>
    </div>
  )
}
