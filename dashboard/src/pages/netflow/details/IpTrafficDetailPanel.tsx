import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { formatBytes } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { DetailMetric, DetailListPanel, PillList, TrafficShapeBar } from './parts'

function IpTrafficDetailPanel({ row, mode, filterTo, forensicsTo }: { row: any; mode: 'talker' | 'endpoint'; filterTo: string; forensicsTo: string }) {
  const srcBytes = Number(row.src_bytes || 0)
  const dstBytes = Number(row.dst_bytes || 0)
  const totalBytes = Math.max(1, Number(row.bytes || 0))
  const avgBytes = row.flows ? Number(row.bytes || 0) / Number(row.flows) : 0
  const firstSeen = row.first_seen ? new Date(row.first_seen).toLocaleString() : '—'
  const lastSeen = row.last_seen ? new Date(row.last_seen).toLocaleString() : '—'

  return (
    <div className="rounded-lg border border-border bg-surface p-4 shadow-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-semibold text-text">{row.ip}</span>
            <Badge variant="outline">{mode === 'endpoint' ? 'Endpoint' : 'Talker'}</Badge>
          </div>
          <div className="mt-1 text-[11px] text-muted">first seen {firstSeen} · last seen {lastSeen}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link to={filterTo} className="inline-flex items-center justify-center rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted hover:text-text">
            Filter dashboard
          </Link>
          <Link to={forensicsTo} className="inline-flex items-center justify-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/15">
            Open raw flows
            <ChevronRight className="h-3 w-3" />
          </Link>
        </div>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <DetailMetric label="Total Traffic" value={formatBytes(row.bytes || 0)} sub={`${Number(row.flows || 0).toLocaleString()} flows`} />
        <DetailMetric label="Packets" value={Number(row.packets || 0).toLocaleString()} sub={`${avgBytes ? formatBytes(avgBytes) : '0 B'} avg / flow`} />
        <DetailMetric label="As Source" value={formatBytes(srcBytes)} sub={`${Number(row.src_flows || 0).toLocaleString()} source flows`} />
        <DetailMetric label="As Destination" value={formatBytes(dstBytes)} sub={`${Number(row.dst_flows || 0).toLocaleString()} destination flows`} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(260px,0.8fr)]">
        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 flex items-center justify-between text-[11px]">
            <span className="font-semibold uppercase tracking-wider text-muted">Direction Split</span>
            <span className="text-muted">{formatBytes(totalBytes)} total</span>
          </div>
          <TrafficShapeBar label="Source bytes" value={srcBytes} max={totalBytes} display={`${((srcBytes / totalBytes) * 100).toFixed(0)}%`} color="bg-cyan-400" />
          <div className="mt-2">
            <TrafficShapeBar label="Destination bytes" value={dstBytes} max={totalBytes} display={`${((dstBytes / totalBytes) * 100).toFixed(0)}%`} color="bg-violet-400" />
          </div>
        </div>
        <DetailListPanel title="Observed Context">
          <PillList label="Protocols" values={(row.protocols || []).map((p: any) => p.name || `Protocol ${p.protocol}`)} empty="No protocol detail" />
          <PillList label="Ports" values={(row.ports || []).map((p: any) => p.service || String(p.port))} empty="No destination ports" />
          <PillList label="Exporters" values={(row.exporters || []).map((e: any) => e.hostname || e.ip)} empty="No exporter detail" mono />
        </DetailListPanel>
      </div>
    </div>
  )
}

export { IpTrafficDetailPanel }
