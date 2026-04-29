import {
  AlertTriangle,
  Bell,
  Network,
  Server,
  Cable,
  Clock,
  Wrench,
} from 'lucide-react'
import {
  AreaChart,
  Area,
  CartesianGrid,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import { useTimeRange } from '@/components/TimeRangePicker'
import { useTechnicalReport } from '@/hooks/useReports'
import { ReportSection, EmptyReportState } from '@/components/reports/ReportSection'
import { Top10Table } from '@/components/reports/Top10Table'
import { Skeleton } from '@/components/ui/Skeleton'
import { cn } from '@/lib/utils'

function fmtMs(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  return `${v.toFixed(1)} ms`
}

function fmtBps(bps: number) {
  if (!bps) return '0 bps'
  const k = 1000
  if (bps >= k * k * k) return `${(bps / (k * k * k)).toFixed(2)} Gbps`
  if (bps >= k * k) return `${(bps / (k * k)).toFixed(2)} Mbps`
  if (bps >= k) return `${(bps / k).toFixed(1)} Kbps`
  return `${bps.toFixed(0)} bps`
}

function fmtMin(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  if (v < 1) return `${(v * 60).toFixed(0)}s`
  if (v < 60) return `${v.toFixed(1)}m`
  return `${(v / 60).toFixed(1)}h`
}

export default function TechnicalReport() {
  const { range } = useTimeRange()
  const { data, isLoading, error } = useTechnicalReport({ fromISO: range.fromISO, toISO: range.toISO })

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-[260px] rounded-lg" />
        <Skeleton className="h-[280px] rounded-lg" />
      </div>
    )
  }

  if (error || !data) {
    return (
      <ReportSection title="Technical Report" icon={<Wrench className="h-4 w-4" />}>
        <EmptyReportState message="Failed to load technical report." />
      </ReportSection>
    )
  }

  return (
    <div className="space-y-5">
      <div className="grid gap-5 lg:grid-cols-2">
        {/* Worst availability devices */}
        <ReportSection
          title="Worst-Performing Devices"
          icon={<Server className="h-4 w-4" />}
          description="Top 10 devices ranked by availability"
          padded={false}
        >
          {data.worst_devices.length === 0 ? (
            <div className="px-5 pb-5"><EmptyReportState /></div>
          ) : (
            <Top10Table
              rows={data.worst_devices}
              columns={[
                { key: 'hostname', header: 'Hostname' },
                { key: 'ip', header: 'IP' },
                {
                  key: 'availability_pct',
                  header: 'Avail',
                  align: 'right',
                  render: (r) => {
                    const cls = r.availability_pct >= 99 ? 'text-emerald-400' : r.availability_pct >= 95 ? 'text-amber-400' : 'text-rose-400'
                    return <span className={cn('font-semibold', cls)}>{r.availability_pct.toFixed(1)}%</span>
                  },
                  sortValue: (r) => r.availability_pct,
                },
                { key: 'outage_count', header: 'Outages', align: 'right' },
                {
                  key: 'avg_rtt_ms',
                  header: 'Avg RTT',
                  align: 'right',
                  render: (r) => fmtMs(r.avg_rtt_ms),
                  sortValue: (r) => r.avg_rtt_ms ?? 0,
                },
              ]}
              defaultSortKey="availability_pct"
              defaultSortDir="asc"
              rowKey={(r) => r.device_id}
            />
          )}
        </ReportSection>

        {/* Noisy alerts */}
        <ReportSection
          title="Noisy Alert Sources"
          icon={<Bell className="h-4 w-4" />}
          description="Alert rules / messages firing most often"
          padded={false}
        >
          {data.noisy_alerts.length === 0 ? (
            <div className="px-5 pb-5"><EmptyReportState /></div>
          ) : (
            <Top10Table
              rows={data.noisy_alerts}
              columns={[
                { key: 'hostname', header: 'Device', render: (r) => r.hostname || '—' },
                { key: 'sample_message', header: 'Message', render: (r) => <span className="text-xs">{r.sample_message || '—'}</span> },
                {
                  key: 'severity',
                  header: 'Severity',
                  render: (r) => (
                    <span
                      className={cn(
                        'rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase',
                        r.severity === 'critical' ? 'bg-rose-500/15 text-rose-400'
                          : r.severity === 'warning' ? 'bg-amber-500/15 text-amber-400'
                          : 'bg-sky-500/15 text-sky-400',
                      )}
                    >
                      {r.severity || 'info'}
                    </span>
                  ),
                },
                { key: 'alert_count', header: 'Count', align: 'right' },
              ]}
              defaultSortKey="alert_count"
              defaultSortDir="desc"
              rowKey={(r) => r.rule_key}
            />
          )}
        </ReportSection>
      </div>

      {/* Alert volume by severity */}
      <ReportSection
        title="Alert Volume Over Time"
        icon={<AlertTriangle className="h-4 w-4" />}
        description="Alert counts bucketed by severity across this window"
      >
        {data.alert_volume_by_severity.length === 0 ? (
          <EmptyReportState message="No alerts triggered in this window." />
        ) : (
          <div className="h-[260px]">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={data.alert_volume_by_severity} margin={{ top: 10, right: 16, left: -10, bottom: 0 }}>
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
                <YAxis tick={{ fontSize: 11, fill: '#9BA1B0' }} axisLine={false} tickLine={false} width={32} allowDecimals={false} />
                <Tooltip contentStyle={{ background: '#1A1D27', border: '1px solid #2D3140', borderRadius: 6, fontSize: 12 }} />
                <Area type="monotone" stackId="1" dataKey="critical" stroke="#EF4444" fill="#EF4444" fillOpacity={0.45} />
                <Area type="monotone" stackId="1" dataKey="warning" stroke="#F59E0B" fill="#F59E0B" fillOpacity={0.45} />
                <Area type="monotone" stackId="1" dataKey="info" stroke="#0EA5E9" fill="#0EA5E9" fillOpacity={0.45} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </ReportSection>

      {/* Top bandwidth */}
      <ReportSection
        title="Top Bandwidth Interfaces"
        icon={<Network className="h-4 w-4" />}
        description="Highest average traffic in / out (requires interface metrics)"
        padded={false}
      >
        {data.top_bandwidth_interfaces.length === 0 ? (
          <div className="px-5 pb-5">
            <EmptyReportState message="No interface metrics available. Enable SNMP polling on devices to populate this view." />
          </div>
        ) : (
          <Top10Table
            rows={data.top_bandwidth_interfaces}
            columns={[
              { key: 'hostname', header: 'Device' },
              { key: 'if_name', header: 'Interface' },
              {
                key: 'in_bps_avg',
                header: 'In avg',
                align: 'right',
                render: (r) => fmtBps(r.in_bps_avg),
                sortValue: (r) => r.in_bps_avg,
              },
              {
                key: 'out_bps_avg',
                header: 'Out avg',
                align: 'right',
                render: (r) => fmtBps(r.out_bps_avg),
                sortValue: (r) => r.out_bps_avg,
              },
              {
                key: 'utilization_pct',
                header: 'Util',
                align: 'right',
                render: (r) => r.utilization_pct === null ? '—' : (
                  <span className={r.utilization_pct >= 80 ? 'text-rose-400 font-semibold' : r.utilization_pct >= 60 ? 'text-amber-400' : ''}>
                    {r.utilization_pct.toFixed(0)}%
                  </span>
                ),
                sortValue: (r) => r.utilization_pct ?? 0,
              },
            ]}
            defaultSortKey="in_bps_avg"
            defaultSortDir="desc"
            rowKey={(r) => `${r.device_id}-${r.if_index}`}
          />
        )}
      </ReportSection>

      <div className="grid gap-5 lg:grid-cols-2">
        {/* Interface status snapshot */}
        <ReportSection
          title="Monitored Interfaces"
          icon={<Cable className="h-4 w-4" />}
          description="Currently monitored interfaces and their operational state"
          padded={false}
        >
          {data.interface_errors.length === 0 ? (
            <div className="px-5 pb-5"><EmptyReportState /></div>
          ) : (
            <Top10Table
              rows={data.interface_errors.slice(0, 15)}
              columns={[
                { key: 'hostname', header: 'Device' },
                { key: 'if_name', header: 'Interface' },
                {
                  key: 'oper_status',
                  header: 'Status',
                  render: (r) => {
                    const isUp = (r.oper_status || '').toLowerCase() === 'up'
                    return (
                      <span className={cn(
                        'rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase',
                        isUp ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400',
                      )}>
                        {r.oper_status || 'unknown'}
                      </span>
                    )
                  },
                },
              ]}
              rowKey={(r) => `${r.device_id}-${r.if_index}`}
            />
          )}
        </ReportSection>

        {/* Outage history */}
        <ReportSection
          title="Outage History"
          icon={<Clock className="h-4 w-4" />}
          description="Chronological list of device-down events"
          padded={false}
        >
          {data.outage_history.length === 0 ? (
            <div className="px-5 pb-5"><EmptyReportState message="No outages in this window." /></div>
          ) : (
            <Top10Table
              rows={data.outage_history.slice(0, 15)}
              columns={[
                {
                  key: 'started_at',
                  header: 'Started',
                  render: (r) => r.started_at ? new Date(r.started_at).toLocaleString() : '—',
                  sortValue: (r) => r.started_at ? Date.parse(r.started_at) : 0,
                },
                { key: 'hostname', header: 'Device' },
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
    </div>
  )
}
