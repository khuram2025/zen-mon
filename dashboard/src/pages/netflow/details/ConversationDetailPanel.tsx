import { Link } from 'react-router-dom'
import { ArrowDownUp, ChevronRight, Cable, Layers, Network, Router } from 'lucide-react'
import { formatBytes } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import type { Conversation } from '../types'
import { normalizeInterfaceRefs, describeTcpFlags, inferApplication, describePortClass } from '../model'
import { DetailMetric, PortBadge, TrafficShapeBar, InterfaceChipGroup } from './parts'

function ConversationDetailPanel({ row, forensicsTo }: { row: Conversation; forensicsTo: string }) {
  const exporters = row.exporters || []
  const firstSeen = row.first_seen ? new Date(row.first_seen).toLocaleString() : '—'
  const lastSeen = row.last_seen ? new Date(row.last_seen).toLocaleString() : '—'
  const avgBytes = row.avg_bytes || (row.flows ? row.bytes / row.flows : 0)
  const avgPackets = row.avg_packets || (row.flows ? row.packets / row.flows : 0)
  const tcpFlags = describeTcpFlags(row.tcp_flags || 0)
  const application = row.application || inferApplication(row.dst_port)
  const portClass = row.port_class || describePortClass(row.dst_port)
  const sourcePorts = (row.src_ports || []).slice(0, 10)
  const inputIfs = normalizeInterfaceRefs(row.input_interfaces, row.input_snmp).slice(0, 8)
  const outputIfs = normalizeInterfaceRefs(row.output_interfaces, row.output_snmp).slice(0, 8)
  const durationMs = Math.max(0, row.avg_duration_ms || 0)

  return (
    <div className="rounded-lg border border-border bg-surface p-4 shadow-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm font-semibold text-text">{row.src}</span>
            <ArrowDownUp className="h-4 w-4 text-primary" />
            <span className="font-mono text-sm font-semibold text-text">{row.dst}</span>
            <Badge variant="outline">{row.protocol_name}</Badge>
            <Badge variant="outline">{row.service}</Badge>
          </div>
          <div className="mt-1 text-[11px] text-muted">
            Port {row.dst_port} · first seen {firstSeen} · last seen {lastSeen}
          </div>
        </div>
        <Link
          to={forensicsTo}
          className="inline-flex shrink-0 items-center justify-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/15"
        >
          Open raw flows
          <ChevronRight className="h-3 w-3" />
        </Link>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <DetailMetric label="Total Traffic" value={formatBytes(row.bytes)} sub={`${row.flows.toLocaleString()} flows`} />
        <DetailMetric label="Packets" value={row.packets.toLocaleString()} sub={`${avgPackets.toFixed(1)} avg / flow`} />
        <DetailMetric label="Avg Flow Size" value={formatBytes(avgBytes)} sub={`${durationMs.toFixed(durationMs >= 100 ? 0 : 1)} ms avg duration`} />
        <DetailMetric label="TCP Flags" value={tcpFlags.primary} sub={tcpFlags.detail} />
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-3">
        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
            <Layers className="h-3.5 w-3.5 text-primary" />
            Application
          </div>
          <div className="text-base font-semibold text-text">{application}</div>
          <div className="mt-1 text-[11px] text-muted">
            {application === 'Other'
              ? 'No known application bucket matched this destination port.'
              : 'Inferred from the destination service port.'}
          </div>
        </div>

        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
            <Cable className="h-3.5 w-3.5 text-primary" />
            Ports Used
          </div>
          <div className="grid grid-cols-2 gap-2">
            <PortBadge label="Source" value={sourcePorts.length ? sourcePorts.join(', ') : 'Not reported'} />
            <PortBadge label="Destination" value={`${row.dst_port}`} />
          </div>
          <div className="mt-2 text-[11px] text-muted">{portClass}</div>
        </div>

        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
            <Network className="h-3.5 w-3.5 text-primary" />
            Protocol / Service
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Badge variant="outline">{row.protocol_name}</Badge>
            <Badge variant="outline">{row.service}</Badge>
            <Badge variant="outline">IP proto {row.protocol}</Badge>
          </div>
          <div className="mt-2 text-[11px] text-muted">
            {row.protocol_name === 'TCP' || row.protocol_name === 'UDP'
              ? `${row.protocol_name} conversation to destination port ${row.dst_port}.`
              : `${row.protocol_name} traffic does not normally expose TCP/UDP application ports.`}
          </div>
        </div>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(280px,0.8fr)]">
        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 flex items-center justify-between text-[11px]">
            <span className="font-semibold uppercase tracking-wider text-muted">Traffic Shape</span>
            <span className="text-muted">{formatBytes(row.bytes)} total</span>
          </div>
          <div className="grid gap-2 sm:grid-cols-3">
            <TrafficShapeBar label="Bytes / flow" value={avgBytes} max={Math.max(avgBytes, row.bytes / Math.max(1, row.flows))} display={formatBytes(avgBytes)} color="bg-cyan-400" />
            <TrafficShapeBar label="Packets / flow" value={avgPackets} max={Math.max(1, avgPackets)} display={avgPackets.toFixed(1)} color="bg-violet-400" />
            <TrafficShapeBar label="Duration" value={durationMs} max={Math.max(1, durationMs)} display={`${durationMs.toFixed(durationMs >= 100 ? 0 : 1)} ms`} color="bg-emerald-400" />
          </div>
        </div>

        <div className="rounded-md border border-border/70 bg-surface2/30 p-3">
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">Exporting Device</div>
          {exporters.length === 0 ? (
            <div className="text-xs text-muted">Exporter not reported for this group.</div>
          ) : (
            <div className="space-y-1.5">
              {exporters.map((exporter) => (
                <Link
                  key={exporter.ip}
                  to={`/netflow/devices/${encodeURIComponent(exporter.ip)}`}
                  className="flex items-center gap-2 rounded border border-border/70 bg-surface px-2 py-1.5 hover:border-primary/50 hover:bg-primary/5"
                >
                  <Router className="h-3.5 w-3.5 text-primary" />
                  <span className="min-w-0 flex-1 truncate text-xs font-medium">{exporter.hostname || exporter.ip}</span>
                  <span className="font-mono text-[10px] text-muted">{exporter.ip}</span>
                </Link>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <InterfaceChipGroup title="Input Interfaces" values={inputIfs} empty="No input interface reported" />
        <InterfaceChipGroup title="Output Interfaces" values={outputIfs} empty="No output interface reported" />
      </div>
    </div>
  )
}

export { ConversationDetailPanel }
