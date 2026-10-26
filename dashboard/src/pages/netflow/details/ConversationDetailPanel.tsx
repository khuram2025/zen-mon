import { ArrowLeftRight, ArrowRight, Layers, Network, Timer } from 'lucide-react'
import { buildNetflowLink } from '@/lib/netflow/links'
import { formatBytes, formatCountExact, formatDateTime, formatDuration, ipDisplay } from '@/lib/netflow/format'
import { conversationLinks, CONFIDENCE_LABELS, drillFilter, hostLink, isUnspecifiedIp, serviceText } from '../cards/model'
import { ConfidenceChip } from '../cards/parts'
import type { Conversation } from '../cards/shapes'
import { BytesMetric, DetailLink, DetailListPanel, DetailMetric, DirectionBar, InterfaceList, PillList } from './parts'
import type { DetailContext } from './IpTrafficDetailPanel'

const FLAG_NAMES: [number, string][] = [[1, 'FIN'], [2, 'SYN'], [4, 'RST'], [8, 'PSH'], [16, 'ACK'], [32, 'URG']]

export function describeTcpFlags(flags: number | null, known: boolean, protocol: number): { primary: string; detail: string } {
  if (protocol !== 6) return { primary: 'Not applicable', detail: 'TCP flags exist only on TCP' }
  if (!known || flags === null) return { primary: 'Unknown', detail: 'The exporters did not export TCP flags for these records' }
  if (flags === 0) return { primary: 'None set', detail: 'Flags were exported and all zero' }
  const found = FLAG_NAMES.filter(([bit]) => (flags & bit) !== 0).map(([, name]) => name)
  return { primary: found.join(' + '), detail: `Union of the control bits seen in this conversation (mask ${flags})` }
}

/**
 * One conversation: client → server on a service with both directions (NF-20). Service,
 * inferred application and IP protocol are separate facts; the classification states how it was
 * reached and with what confidence; interfaces are listed per exporter as observed (X-29). Raw
 * evidence links open each direction exactly, with the page filter and the frozen window.
 */
export function ConversationDetailPanel({ row, ctx }: { row: Conversation; ctx: DetailContext }) {
  const tz = ctx.meta?.tz ?? ctx.window.tz
  const links = conversationLinks(row, ctx.filter, ctx.window, ctx.meta)
  const flags = describeTcpFlags(row.tcp_flags, row.tcp_flags_known, row.protocol)
  const ifaceFilter = (i: { iface: string }) => buildNetflowLink('/netflow', { filter: drillFilter(ctx.filter, { iface: i.iface }), window: ctx.window })
  const orient = links.labels
  return (
    <div className="space-y-3 rounded-lg border border-border bg-surface p-3 sm:p-4">
      <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5 text-sm">
            <span className="text-xs text-text2">{orient.a}</span>
            <span className="break-all font-mono font-semibold text-text">{ipDisplay(row.client)}</span>
            {row.orientation === 'client_server' ? <ArrowRight className="h-4 w-4 text-text2" aria-hidden /> : <ArrowLeftRight className="h-4 w-4 text-text2" aria-hidden />}
            <span className="text-xs text-text2">{orient.b}</span>
            <span className="break-all font-mono font-semibold text-text">{ipDisplay(row.server)}</span>
          </div>
          <div className="mt-0.5 text-xs text-text2">
            {serviceText(row)} · first {formatDateTime(row.first_seen, tz, { zone: 'offset' })} · last {formatDateTime(row.last_seen, tz, { zone: 'none' })}
          </div>
          {row.orientation !== 'client_server' && (
            <div className="mt-0.5 text-xs text-text2">
              Direction unknown (no service port identifies a server): endpoints are ordered by address.
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-1.5">
          <DetailLink to={links.forward} primary>Raw flows {orient.forward}</DetailLink>
          <DetailLink to={links.reverse}>Raw flows {orient.reverse}</DetailLink>
          {!isUnspecifiedIp(row.client) && <DetailLink to={hostLink(row.client, ctx.filter, ctx.window, ctx.meta)}>{orient.a} profile</DetailLink>}
          {!isUnspecifiedIp(row.server) && <DetailLink to={hostLink(row.server, ctx.filter, ctx.window, ctx.meta)}>{orient.b} profile</DetailLink>}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2 xl:grid-cols-4">
        <BytesMetric label="Total" bytes={row.bytes} sub={`${formatCountExact(row.flows)} flows · ${formatCountExact(row.packets)} packets`} />
        <BytesMetric label={orient.forward} bytes={row.bytes_c2s} sub={`${formatCountExact(row.flows_c2s)} flows`} />
        <BytesMetric label={orient.reverse} bytes={row.bytes_s2c} sub={`${formatCountExact(row.flows_s2c)} flows`} />
        <DetailMetric
          label="Avg flow duration"
          value={formatDuration(row.avg_duration_ms)}
          sub={row.avg_duration_ms == null ? 'No valid start/end times exported' : `over ${formatCountExact(row.duration_valid_flows)} flows with valid times`}
        />
      </div>
      <DirectionBar a={row.bytes_c2s} b={row.bytes_s2c} aLabel={`${orient.forward} ${formatBytes(row.bytes_c2s)}`} bLabel={`${orient.reverse} ${formatBytes(row.bytes_s2c)}`} />

      <div className="grid gap-3 lg:grid-cols-3">
        <DetailListPanel title="Service">
          <div className="flex items-start gap-2">
            <Network className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
            <div className="min-w-0 text-xs">
              <div className="font-semibold text-text">{serviceText(row)}</div>
              <div className="text-xs text-text2">
                {row.port_applicable
                  ? row.service_port
                    ? `Service port ${row.service_port}, identified on either side of the flow (requests and responses).`
                    : 'Neither port matches a known service, so no service is claimed.'
                  : `${row.protocol_name} has no transport ports; a service does not apply.`}
              </div>
              {row.client_ports.length > 0 && (
                <div className="mt-1">
                  <PillList label="Client ports" values={row.client_ports.map(String)} empty="—" mono />
                </div>
              )}
            </div>
          </div>
          <div className="mt-2">
            <DetailLink to={buildNetflowLink('/netflow', { filter: drillFilter(ctx.filter, { ...row.drill }), window: ctx.window })}>Filter overview to this service</DetailLink>
          </div>
        </DetailListPanel>
        <DetailListPanel title="Application (inferred)">
          <div className="flex items-start gap-2">
            <Layers className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
            <div className="min-w-0 text-xs">
              <div className="flex flex-wrap items-center gap-1.5 font-semibold text-text">
                {row.application}
                <ConfidenceChip confidence={row.confidence} title={CONFIDENCE_LABELS[row.confidence]} />
              </div>
              <div className="text-xs text-text2">{row.basis_text} · category {row.category}</div>
              {row.visibility_note && <div className="mt-1 text-xs font-medium text-warning-fg">{row.visibility_note}</div>}
            </div>
          </div>
        </DetailListPanel>
        <DetailListPanel title="IP protocol and TCP flags">
          <div className="flex items-start gap-2">
            <Timer className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />
            <div className="min-w-0 text-xs">
              <div className="font-semibold text-text">{row.ip_protocol} ({row.protocol})</div>
              <div className="mt-1 text-text">Flags: {flags.primary}</div>
              <div className="text-xs text-text2">{flags.detail}</div>
            </div>
          </div>
        </DetailListPanel>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <DetailListPanel title="Where it was observed">
          <PillList label="Exporters" values={row.exporters.map((e) => (e.hostname ? `${e.hostname} (${ipDisplay(e.ip)})` : ipDisplay(e.ip)))} empty="Exporter not reported" />
          <InterfaceList title={`${orient.a}-side interfaces`} values={row.client_side_interfaces} empty="Not exported" filterTo={ifaceFilter} />
          <InterfaceList title={`${orient.b}-side interfaces`} values={row.server_side_interfaces} empty="Not exported" filterTo={ifaceFilter} />
        </DetailListPanel>
        <DetailListPanel title="Per flow">
          <div className="grid grid-cols-2 gap-2 text-xs">
            <div>
              <div className="text-2xs uppercase tracking-wider text-text2">Avg size</div>
              <div className="font-semibold tabular-nums text-text">{formatBytes(row.avg_bytes)}</div>
            </div>
            <div>
              <div className="text-2xs uppercase tracking-wider text-text2">Avg packets</div>
              <div className="font-semibold tabular-nums text-text">{row.avg_packets == null ? '—' : row.avg_packets.toLocaleString('en-US', { maximumFractionDigits: row.avg_packets < 10 ? 1 : 0 })}</div>
            </div>
          </div>
        </DetailListPanel>
      </div>
    </div>
  )
}
