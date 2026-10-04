import { buildNetflowLink } from '@/lib/netflow/links'
import { formatCountExact, formatDateTime, formatPercent, ipDisplay, protocolName } from '@/lib/netflow/format'
import type { FlowFilter } from '@/lib/netflow/filters'
import type { NfMetaWindow, NfWindow } from '@/lib/netflow/window'
import { drillFilter, forensicsLink, hostLink, isUnspecifiedIp, rankPhrase, serviceText, UNRECORDED_ADDRESS_HINT, UNRECORDED_ADDRESS_LABEL } from '../cards/model'
import type { RankBy, Talker } from '../cards/shapes'
import { BytesMetric, DetailLink, DetailListPanel, DirectionBar, PillList } from './parts'

export interface DetailContext {
  filter: FlowFilter
  window: NfWindow
  /** Bounds of the data on screen (freezes evidence links). */
  meta?: Pick<NfMetaWindow, 'from' | 'to' | 'tz'> | null
}

/**
 * One host of a talker/endpoint ranking: what it sent and received (the ranked metric first),
 * the services it took part in, where it was observed, and links that keep the page filter and
 * the exact window: raw flows either side (talker), only as source, only as destination.
 */
export function IpTrafficDetailPanel({ row, by, ctx }: { row: Talker; by: RankBy; ctx: DetailContext }) {
  const tz = ctx.meta?.tz ?? ctx.window.tz
  const ip = row.ip
  const shown = ipDisplay(ip)
  const ranked = by === 'src' ? row.src_bytes : by === 'dst' ? row.dst_bytes : row.bytes
  return (
    <div className="space-y-3 rounded-lg border border-border bg-surface p-3 sm:p-4">
      <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="break-all font-mono text-sm font-semibold text-text">{shown}</div>
          {isUnspecifiedIp(ip) && <div className="text-[11px] italic text-text2" title={UNRECORDED_ADDRESS_HINT}>{UNRECORDED_ADDRESS_LABEL}: {UNRECORDED_ADDRESS_HINT}</div>}
          {row.hostname && <div className="break-all text-[11px] text-text2">{row.hostname}</div>}
          <div className="mt-0.5 text-[11px] text-text2">
            {rankPhrase(by)} · seen {formatDateTime(row.first_seen, tz, { zone: 'offset' })} → {formatDateTime(row.last_seen, tz, { zone: 'none' })}
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <DetailLink to={forensicsLink(ctx.filter, { talker: ip }, ctx.window, ctx.meta)} primary>Raw flows (either side)</DetailLink>
          <DetailLink to={forensicsLink(ctx.filter, { src: ip }, ctx.window, ctx.meta)}>Sent</DetailLink>
          <DetailLink to={forensicsLink(ctx.filter, { dst: ip }, ctx.window, ctx.meta)}>Received</DetailLink>
          {!isUnspecifiedIp(ip) && <DetailLink to={hostLink(ip, ctx.filter, ctx.window, ctx.meta)}>Host profile</DetailLink>}
          <DetailLink to={buildNetflowLink('/netflow', { filter: drillFilter(ctx.filter, { talker: ip }), window: ctx.window })}>Filter overview</DetailLink>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2 xl:grid-cols-4">
        <BytesMetric label={by === 'src' ? 'Sent (ranked)' : 'Sent'} bytes={row.src_bytes} sub={`${formatCountExact(row.src_flows)} flows · ${formatCountExact(row.src_packets)} packets`} />
        <BytesMetric label={by === 'dst' ? 'Received (ranked)' : 'Received'} bytes={row.dst_bytes} sub={`${formatCountExact(row.dst_flows)} flows · ${formatCountExact(row.dst_packets)} packets`} />
        <BytesMetric label={by === 'total' ? 'Total (ranked)' : 'Total'} bytes={row.bytes} sub={`${formatCountExact(row.flows)} flows`} />
        <BytesMetric label="Ranked value" bytes={ranked} sub={row.share_pct != null ? `${formatPercent(row.share_pct)} of traffic in scope` : undefined} />
      </div>
      <DirectionBar a={row.src_bytes} b={row.dst_bytes} aLabel="Sent" bLabel="Received" />

      <div className="grid gap-3 lg:grid-cols-2">
        <DetailListPanel title="Services this host took part in">
          {row.services.length ? (
            <ul role="list" className="space-y-1">
              {row.services.map((s) => (
                <li key={`${s.protocol}-${s.service_port ?? s.icmp_type_code ?? 'x'}`} className="flex flex-wrap items-center justify-between gap-x-2 text-xs">
                  <span className="min-w-0 break-words text-text">
                    {serviceText({ ...s, protocol_name: protocolName(s.protocol) ?? undefined })}
                    <span className="ml-1 text-[11px] text-text2">{s.application}</span>
                  </span>
                  <DetailLink to={forensicsLink(ctx.filter, { talker: ip, ...s.drill }, ctx.window, ctx.meta)}>
                    <span className="tabular-nums">{formatCountExact(s.flows)} flows</span>
                  </DetailLink>
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-xs text-text2">No service detail.</div>
          )}
        </DetailListPanel>
        <DetailListPanel title="Observed by">
          <PillList label="Exporters" values={row.exporters.map((e) => ipDisplay(e.ip))} empty="Exporter not reported" mono />
          <PillList label="IP protocols" values={row.protocols.map((p) => `${p.name} (${p.protocol})`)} empty="—" />
          <PillList
            label="Context"
            values={[row.group ? `Group: ${row.group}` : '', row.country_name ? `Country: ${row.country_name}` : '', row.asn ? `AS${row.asn}${row.as_name ? ` ${row.as_name}` : ''}` : '']}
            empty="No group, GeoIP or AS context"
          />
        </DetailListPanel>
      </div>
    </div>
  )
}
