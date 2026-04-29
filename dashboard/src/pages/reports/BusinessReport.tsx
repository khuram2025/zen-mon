import { Briefcase, ShieldAlert, Gauge, Users, Clock } from 'lucide-react'
import { useTimeRange } from '@/components/TimeRangePicker'
import { useBusinessReport } from '@/hooks/useReports'
import { KpiTile } from '@/components/reports/KpiTile'
import { ReportSection, EmptyReportState } from '@/components/reports/ReportSection'
import { Top10Table } from '@/components/reports/Top10Table'
import { Skeleton } from '@/components/ui/Skeleton'
import { cn } from '@/lib/utils'

function fmtPct(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  return `${v.toFixed(2)}%`
}

function fmtMs(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  return `${v.toFixed(0)} ms`
}

function fmtMin(v: number | null | undefined) {
  if (v === null || v === undefined) return '—'
  if (v < 1) return `${(v * 60).toFixed(0)}s`
  if (v < 60) return `${v.toFixed(1)}m`
  return `${(v / 60).toFixed(1)}h`
}

export default function BusinessReport() {
  const { range } = useTimeRange()
  const { data, isLoading, error } = useBusinessReport({ fromISO: range.fromISO, toISO: range.toISO })

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-[100px] rounded-lg" />
          ))}
        </div>
        <Skeleton className="h-[280px] rounded-lg" />
      </div>
    )
  }

  if (error || !data) {
    return (
      <ReportSection title="Business Report" icon={<Briefcase className="h-4 w-4" />}>
        <EmptyReportState message="Failed to load business report." />
      </ReportSection>
    )
  }

  // KPI roll-ups
  const totalServices = data.service_availability.length
  const passing = data.service_availability.filter((s) => (s.availability_pct ?? 0) >= 99).length
  const overallAvail = totalServices
    ? data.service_availability
        .filter((s) => s.availability_pct !== null)
        .reduce((sum, s) => sum + (s.availability_pct ?? 0), 0) / Math.max(1, totalServices)
    : null
  const tlsCritical = data.tls_warnings.filter((w) => w.severity === 'critical').length

  return (
    <div className="space-y-5">
      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <KpiTile
          label="Service Availability"
          value={fmtPct(overallAvail)}
          accent={overallAvail && overallAvail >= 99 ? 'success' : 'warning'}
          icon={<Gauge className="h-4 w-4" />}
          subtitle={`${passing}/${totalServices} services ≥ 99%`}
        />
        <KpiTile
          label="Customer-Impact"
          value={fmtMin(data.customer_impact_minutes)}
          accent={data.customer_impact_minutes > 30 ? 'danger' : 'success'}
          icon={<Users className="h-4 w-4" />}
          subtitle="Downtime minutes (grouped services)"
        />
        <KpiTile
          label="TLS Warnings"
          value={data.tls_warnings.length}
          accent={tlsCritical > 0 ? 'danger' : data.tls_warnings.length ? 'warning' : 'success'}
          icon={<ShieldAlert className="h-4 w-4" />}
          subtitle={tlsCritical > 0 ? `${tlsCritical} critical` : 'No urgent expiries'}
        />
        <KpiTile
          label="Service Outages"
          value={data.service_outages.length}
          accent={data.service_outages.length > 0 ? 'warning' : 'success'}
          icon={<Clock className="h-4 w-4" />}
        />
      </div>

      {/* Service availability table */}
      <ReportSection
        title="Service Availability"
        icon={<Gauge className="h-4 w-4" />}
        description="Per-service uptime, grouped by service group"
        padded={false}
      >
        {data.service_availability.length === 0 ? (
          <div className="px-5 pb-5"><EmptyReportState message="No service checks configured yet." /></div>
        ) : (
          <Top10Table
            rows={data.service_availability}
            columns={[
              { key: 'name', header: 'Service' },
              {
                key: 'type',
                header: 'Type',
                render: (r) => (
                  <span className="rounded bg-surface2 px-1.5 py-0.5 text-[10px] font-semibold uppercase">{r.type}</span>
                ),
              },
              { key: 'group_name', header: 'Group' },
              {
                key: 'availability_pct',
                header: 'Availability',
                align: 'right',
                render: (r) => {
                  if (r.availability_pct === null) return <span className="text-muted">No data</span>
                  const cls = r.availability_pct >= 99 ? 'text-emerald-400' : r.availability_pct >= 95 ? 'text-amber-400' : 'text-rose-400'
                  return <span className={cn('font-semibold', cls)}>{r.availability_pct.toFixed(2)}%</span>
                },
                sortValue: (r) => r.availability_pct ?? -1,
              },
              { key: 'checks_total', header: 'Checks', align: 'right' },
              {
                key: 'checks_failed',
                header: 'Failed',
                align: 'right',
                render: (r) => (
                  <span className={r.checks_failed > 0 ? 'text-rose-400 font-semibold' : 'text-muted'}>{r.checks_failed}</span>
                ),
              },
            ]}
            defaultSortKey="availability_pct"
            defaultSortDir="asc"
            rowKey={(r) => r.service_check_id}
          />
        )}
      </ReportSection>

      <div className="grid gap-5 lg:grid-cols-2">
        {/* Response quantiles */}
        <ReportSection
          title="Response Time Percentiles"
          icon={<Gauge className="h-4 w-4" />}
          description="p50 / p95 / p99 latency per service"
          padded={false}
        >
          {data.response_time_quantiles.length === 0 ? (
            <div className="px-5 pb-5"><EmptyReportState /></div>
          ) : (
            <Top10Table
              rows={data.response_time_quantiles}
              columns={[
                { key: 'name', header: 'Service' },
                { key: 'p50_ms', header: 'p50', align: 'right', render: (r) => fmtMs(r.p50_ms), sortValue: (r) => r.p50_ms ?? 0 },
                { key: 'p95_ms', header: 'p95', align: 'right', render: (r) => fmtMs(r.p95_ms), sortValue: (r) => r.p95_ms ?? 0 },
                {
                  key: 'p99_ms',
                  header: 'p99',
                  align: 'right',
                  render: (r) => {
                    if (r.p99_ms === null) return '—'
                    const cls = r.p99_ms > 1000 ? 'text-rose-400' : r.p99_ms > 500 ? 'text-amber-400' : ''
                    return <span className={cn('font-semibold', cls)}>{fmtMs(r.p99_ms)}</span>
                  },
                  sortValue: (r) => r.p99_ms ?? 0,
                },
              ]}
              defaultSortKey="p95_ms"
              defaultSortDir="desc"
              rowKey={(r) => r.service_check_id}
            />
          )}
        </ReportSection>

        {/* TLS warnings */}
        <ReportSection
          title="TLS Certificate Warnings"
          icon={<ShieldAlert className="h-4 w-4" />}
          description="Certificates approaching or past expiry"
          padded={false}
        >
          {data.tls_warnings.length === 0 ? (
            <div className="px-5 pb-5">
              <EmptyReportState message="No TLS certificates expire soon. ✓" />
            </div>
          ) : (
            <Top10Table
              rows={data.tls_warnings}
              columns={[
                { key: 'name', header: 'Service' },
                {
                  key: 'tls_expiry_date',
                  header: 'Expires',
                  render: (r) => r.tls_expiry_date ? new Date(r.tls_expiry_date).toLocaleDateString() : '—',
                  sortValue: (r) => r.tls_expiry_date ? Date.parse(r.tls_expiry_date) : 0,
                },
                {
                  key: 'days_remaining',
                  header: 'Days left',
                  align: 'right',
                  render: (r) => {
                    const cls = r.days_remaining < 7 ? 'text-rose-400 font-semibold' : r.days_remaining < 30 ? 'text-amber-400' : ''
                    return <span className={cls}>{r.days_remaining}</span>
                  },
                  sortValue: (r) => r.days_remaining,
                },
                {
                  key: 'severity',
                  header: 'Severity',
                  render: (r) => (
                    <span className={cn(
                      'rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase',
                      r.severity === 'critical' ? 'bg-rose-500/15 text-rose-400' : 'bg-amber-500/15 text-amber-400',
                    )}>
                      {r.severity}
                    </span>
                  ),
                },
              ]}
              defaultSortKey="days_remaining"
              defaultSortDir="asc"
              rowKey={(r) => r.service_check_id}
            />
          )}
        </ReportSection>
      </div>

      {/* Service outages list */}
      <ReportSection
        title="Service Outage History"
        icon={<Clock className="h-4 w-4" />}
        description="Service downtime events"
        padded={false}
      >
        {data.service_outages.length === 0 ? (
          <div className="px-5 pb-5"><EmptyReportState message="No service outages in this window. ✓" /></div>
        ) : (
          <Top10Table
            rows={data.service_outages.slice(0, 20)}
            columns={[
              {
                key: 'started_at',
                header: 'Started',
                render: (r) => r.started_at ? new Date(r.started_at).toLocaleString() : '—',
                sortValue: (r) => r.started_at ? Date.parse(r.started_at) : 0,
              },
              { key: 'name', header: 'Service' },
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
