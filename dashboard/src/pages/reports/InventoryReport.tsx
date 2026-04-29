import { Boxes, Server, MapPin, Cable, Radio, PackagePlus } from 'lucide-react'
import { useInventoryReport } from '@/hooks/useReports'
import { KpiTile } from '@/components/reports/KpiTile'
import { ReportSection, EmptyReportState } from '@/components/reports/ReportSection'
import { Top10Table } from '@/components/reports/Top10Table'
import { StatusDonut, DonutLegend } from '@/components/reports/StatusDonut'
import { Skeleton } from '@/components/ui/Skeleton'
import { cn } from '@/lib/utils'

const TYPE_PALETTE = [
  '#6366F1', '#22C55E', '#F59E0B', '#EF4444', '#0EA5E9', '#A855F7', '#14B8A6', '#F97316',
]

export default function InventoryReport() {
  const { data, isLoading, error } = useInventoryReport()

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-[100px] rounded-lg" />
          ))}
        </div>
        <Skeleton className="h-[260px] rounded-lg" />
      </div>
    )
  }

  if (error || !data) {
    return (
      <ReportSection title="Inventory" icon={<Boxes className="h-4 w-4" />}>
        <EmptyReportState message="Failed to load inventory data." />
      </ReportSection>
    )
  }

  const typeSlices = data.devices_by_type.map((t, i) => ({
    label: t.type,
    value: t.count,
    color: TYPE_PALETTE[i % TYPE_PALETTE.length],
  }))

  const sensorOnline = data.sensors.filter((s) => s.status === 'active' || s.status === 'online').length
  const sensorPct = data.sensors.length ? (sensorOnline / data.sensors.length) * 100 : 0

  return (
    <div className="space-y-5">
      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <KpiTile
          label="Total Devices"
          value={data.totals.devices}
          accent="primary"
          icon={<Server className="h-4 w-4" />}
        />
        <KpiTile
          label="Interfaces (Monitored)"
          value={`${data.interface_totals.monitored} / ${data.interface_totals.total}`}
          accent="info"
          icon={<Cable className="h-4 w-4" />}
          subtitle={data.interface_totals.down ? `${data.interface_totals.down} down` : 'All up'}
        />
        <KpiTile
          label="Sensors Online"
          value={`${sensorOnline} / ${data.totals.sensors}`}
          accent={sensorPct >= 90 ? 'success' : 'warning'}
          icon={<Radio className="h-4 w-4" />}
          subtitle={data.totals.sensors === 0 ? 'No sensors enrolled' : `${sensorPct.toFixed(0)}% healthy`}
        />
        <KpiTile
          label="Recently Added"
          value={data.recently_added_devices.length}
          accent="info"
          icon={<PackagePlus className="h-4 w-4" />}
          subtitle="Last 30 days"
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-3">
        {/* Devices by type donut */}
        <ReportSection
          title="Devices by Type"
          icon={<Boxes className="h-4 w-4" />}
          description="Composition of your fleet"
        >
          {typeSlices.length === 0 ? (
            <EmptyReportState message="No devices yet." />
          ) : (
            <div className="space-y-3">
              <StatusDonut
                data={typeSlices}
                centerValue={String(data.totals.devices)}
                centerLabel="Total"
              />
              <DonutLegend data={typeSlices} />
            </div>
          )}
        </ReportSection>

        {/* Devices by vendor */}
        <ReportSection
          title="Devices by Vendor"
          icon={<Server className="h-4 w-4" />}
          description="Vendor distribution from discovery"
          padded={false}
        >
          {data.devices_by_vendor.length === 0 ? (
            <div className="px-5 pb-5"><EmptyReportState /></div>
          ) : (
            <ul className="divide-y divide-border">
              {data.devices_by_vendor.slice(0, 10).map((v) => {
                const max = data.devices_by_vendor[0]?.count || 1
                const pct = (v.count / max) * 100
                return (
                  <li key={v.vendor} className="px-5 py-2.5">
                    <div className="mb-1 flex items-center justify-between text-xs">
                      <span className="font-medium">{v.vendor}</span>
                      <span className="text-muted">{v.count}</span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-surface2">
                      <div className="h-full rounded-full bg-primary" style={{ width: `${pct}%` }} />
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </ReportSection>

        {/* Devices by location */}
        <ReportSection
          title="Devices by Location"
          icon={<MapPin className="h-4 w-4" />}
          description="Geographic / site distribution"
          padded={false}
        >
          {data.devices_by_location.length === 0 ? (
            <div className="px-5 pb-5"><EmptyReportState /></div>
          ) : (
            <ul className="divide-y divide-border">
              {data.devices_by_location.slice(0, 10).map((l) => (
                <li key={l.location} className="flex items-center justify-between px-5 py-2.5 text-sm">
                  <span className="truncate">{l.location}</span>
                  <span className="rounded bg-surface2 px-2 py-0.5 text-[11px] font-semibold">{l.count}</span>
                </li>
              ))}
            </ul>
          )}
        </ReportSection>
      </div>

      {/* Sensors fleet */}
      <ReportSection
        title="Sensor Fleet"
        icon={<Radio className="h-4 w-4" />}
        description="Remote monitoring sensors and their health"
        padded={false}
      >
        {data.sensors.length === 0 ? (
          <div className="px-5 pb-5">
            <EmptyReportState message="No sensors enrolled. Sensors are optional remote pollers." />
          </div>
        ) : (
          <Top10Table
            rows={data.sensors}
            columns={[
              { key: 'name', header: 'Sensor' },
              { key: 'site', header: 'Site', render: (s) => s.site || '—' },
              {
                key: 'status',
                header: 'Status',
                render: (s) => {
                  const ok = s.status === 'active' || s.status === 'online'
                  return (
                    <span className={cn(
                      'rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase',
                      ok ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400',
                    )}>
                      {s.status}
                    </span>
                  )
                },
              },
              {
                key: 'last_heartbeat',
                header: 'Last Heartbeat',
                render: (s) => s.last_heartbeat ? new Date(s.last_heartbeat).toLocaleString() : '—',
                sortValue: (s) => s.last_heartbeat ? Date.parse(s.last_heartbeat) : 0,
              },
              { key: 'queue_depth', header: 'Queue', align: 'right' },
              { key: 'version', header: 'Ver', render: (s) => s.version || '—' },
            ]}
            defaultSortKey="last_heartbeat"
            defaultSortDir="desc"
            rowKey={(s) => s.sensor_id}
          />
        )}
      </ReportSection>

      {/* Recently added */}
      <ReportSection
        title="Recently Added Devices"
        icon={<PackagePlus className="h-4 w-4" />}
        description="Onboarded in the last 30 days"
        padded={false}
      >
        {data.recently_added_devices.length === 0 ? (
          <div className="px-5 pb-5"><EmptyReportState message="No new devices in the last 30 days." /></div>
        ) : (
          <Top10Table
            rows={data.recently_added_devices}
            columns={[
              { key: 'hostname', header: 'Hostname' },
              { key: 'ip', header: 'IP' },
              { key: 'device_type', header: 'Type' },
              { key: 'vendor', header: 'Vendor', render: (d) => d.vendor || '—' },
              { key: 'location', header: 'Location', render: (d) => d.location || '—' },
              {
                key: 'added_at',
                header: 'Added',
                render: (d) => d.added_at ? new Date(d.added_at).toLocaleDateString() : '—',
                sortValue: (d) => d.added_at ? Date.parse(d.added_at) : 0,
              },
            ]}
            defaultSortKey="added_at"
            defaultSortDir="desc"
            rowKey={(d) => d.device_id}
          />
        )}
      </ReportSection>
    </div>
  )
}
