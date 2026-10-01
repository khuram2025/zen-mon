import { buildNetflowLink } from '@/lib/netflow/links'
import { formatBytes, formatBytesExact, formatCountExact, formatDateTime, protocolLabel } from '@/lib/netflow/format'
import { drillFilter, forensicsLink, serviceText } from '../cards/model'
import type { ProtocolRow } from '../cards/shapes'
import { BytesMetric, DetailLink, DetailListPanel } from './parts'
import type { DetailContext } from './IpTrafficDetailPanel'

/** One IP protocol with its top services; portless protocols say that services do not apply. */
export function ProtocolDetailPanel({ row, ctx }: { row: ProtocolRow; ctx: DetailContext }) {
  const known = row.request_bytes + row.response_bytes
  const tz = ctx.meta?.tz ?? ctx.window.tz
  return (
    <div className="space-y-3 rounded-lg border border-border bg-surface p-3 sm:p-4">
      <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-text">{protocolLabel(row.protocol)}</div>
          <div className="text-[11px] text-text2">
            Seen {formatDateTime(row.first_seen, tz, { zone: 'offset' })} → {formatDateTime(row.last_seen, tz, { zone: 'none' })}
          </div>
          {!row.port_applicable && (
            <div className="mt-0.5 text-[11px] text-text2">{row.name} carries no transport ports: services are not applicable{row.protocol === 1 || row.protocol === 58 ? '; message types are listed instead' : ''}.</div>
          )}
        </div>
        <div className="flex flex-wrap gap-1.5">
          <DetailLink to={forensicsLink(ctx.filter, { protocol: row.protocol }, ctx.window, ctx.meta)} primary>Raw flows</DetailLink>
          <DetailLink to={buildNetflowLink('/netflow', { filter: drillFilter(ctx.filter, { protocol: row.protocol }), window: ctx.window })}>Filter overview</DetailLink>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-3">
        <BytesMetric label="Traffic" bytes={row.bytes} sub={`${formatCountExact(row.flows)} flows · ${formatCountExact(row.packets)} packets`} />
        <BytesMetric label="Requests" bytes={known > 0 ? row.request_bytes : null} sub={known > 0 ? undefined : 'Direction unknown (no service port)'} />
        <BytesMetric label="Responses" bytes={known > 0 ? row.response_bytes : null} sub={known > 0 && row.bytes > known ? `${formatBytes(row.bytes - known)} direction unknown` : known > 0 ? undefined : 'Direction unknown (no service port)'} />
      </div>
      <DetailListPanel title={row.port_applicable ? 'Top services' : row.protocol === 1 || row.protocol === 58 ? 'Message types' : 'Traffic'}>
        <ul role="list" className="space-y-1">
          {row.services.map((s) => (
            <li key={`${s.port}-${s.icmp_type_code ?? ''}`} className="flex flex-wrap items-center justify-between gap-2 text-xs">
              <span className="min-w-0 break-words">
                <span className="font-medium text-text">{serviceText(s)}</span>
                <span className="ml-1 text-[11px] text-text2">{s.application}</span>
              </span>
              <span className="flex items-center gap-2">
                <span className="tabular-nums text-text" title={formatBytesExact(s.bytes)}>{formatBytes(s.bytes)}</span>
                <DetailLink to={forensicsLink(ctx.filter, { ...s.drill }, ctx.window, ctx.meta)}>Flows</DetailLink>
              </span>
            </li>
          ))}
        </ul>
      </DetailListPanel>
    </div>
  )
}
