import { Link } from 'react-router-dom'
import { Layers } from 'lucide-react'
import { formatBytes } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { DetailMetric, PortBreakdown } from './parts'

function ApplicationDetailPanel({ row, filterTo }: { row: any; filterTo: string }) {
  const avgBytes = row.flows ? Number(row.bytes || 0) / Number(row.flows) : 0
  return (
    <div className="rounded-lg border border-border bg-surface p-4 shadow-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Layers className="h-4 w-4 text-primary" />
            <span className="text-sm font-semibold text-text">{row.name}</span>
            <Badge variant="outline">Application bucket</Badge>
          </div>
          <div className="mt-1 text-[11px] text-muted">Application is inferred from destination service ports.</div>
        </div>
        <Link to={filterTo} className="inline-flex shrink-0 items-center justify-center rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/15">
          Filter dashboard
        </Link>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <DetailMetric label="Traffic" value={formatBytes(row.bytes || 0)} sub={`${Number(row.flows || 0).toLocaleString()} flows`} />
        <DetailMetric label="Packets" value={Number(row.packets || 0).toLocaleString()} sub={`${avgBytes ? formatBytes(avgBytes) : '0 B'} avg / flow`} />
        <DetailMetric label="Services" value={String((row.ports || []).length)} sub="top destination ports" />
      </div>
      <div className="mt-4">
        <PortBreakdown ports={row.ports || []} totalBytes={Number(row.bytes || 0)} />
      </div>
    </div>
  )
}

export { ApplicationDetailPanel }
