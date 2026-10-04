import { buildNetflowLink } from '@/lib/netflow/links'
import { formatBytes, formatBytesExact, formatCountExact, formatPercent } from '@/lib/netflow/format'
import { drillFilter, forensicsLink, serviceText } from '../cards/model'
import { ConfidenceChip } from '../cards/parts'
import type { ApplicationRow } from '../cards/shapes'
import { BytesMetric, DetailLink, DetailListPanel, DirectionBar } from './parts'
import type { DetailContext } from './IpTrafficDetailPanel'

/**
 * One inferred application: the services that make it up (both directions of each), how its
 * bytes were classified and with what confidence, and what flow records cannot see (tunnels).
 */
export function ApplicationDetailPanel({ row, ctx }: { row: ApplicationRow; ctx: DetailContext }) {
  const cb = row.confidence_bytes
  const known = row.request_bytes + row.response_bytes
  return (
    <div className="space-y-3 rounded-lg border border-border bg-surface p-3 sm:p-4">
      <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5 text-sm font-semibold text-text">
            {row.name}
            <ConfidenceChip confidence={row.confidence} />
          </div>
          <div className="text-[11px] text-text2">Category {row.category}. Inferred from exporter App-ID, operator rules, the IP protocol and service ports — not deep packet inspection.</div>
          {row.visibility_note && <div className="mt-0.5 text-[11px] font-medium text-warning-fg">{row.visibility_note}</div>}
        </div>
        <div className="flex flex-wrap gap-1.5">
          <DetailLink to={forensicsLink(ctx.filter, { app: row.name }, ctx.window, ctx.meta)} primary>Raw flows</DetailLink>
          <DetailLink to={buildNetflowLink('/netflow', { filter: drillFilter(ctx.filter, { app: row.name }), window: ctx.window })}>Filter overview</DetailLink>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-3">
        <BytesMetric label="Traffic" bytes={row.bytes} sub={`${formatCountExact(row.flows)} flows`} />
        <BytesMetric label="Requests (to the service)" bytes={known > 0 ? row.request_bytes : null} sub={known > 0 ? undefined : 'Direction unknown (no service port)'} />
        <BytesMetric label="Responses (from the service)" bytes={known > 0 ? row.response_bytes : null} sub={known > 0 && row.bytes > known ? `${formatBytes(row.bytes - known)} direction unknown` : known > 0 ? undefined : 'Direction unknown (no service port)'} />
      </div>
      {known > 0 && <DirectionBar a={row.request_bytes} b={row.response_bytes} aLabel="Requests" bLabel="Responses" />}
      <DetailListPanel title="Classification confidence (bytes)">
        <ul role="list" className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
          {(['high', 'medium', 'low', 'n/a'] as const).map((c) => (
            <li key={c} className="flex flex-col">
              <ConfidenceChip confidence={c} />
              <span className="mt-0.5 font-semibold tabular-nums text-text" title={formatBytesExact(cb[c])}>{formatBytes(cb[c])}</span>
              <span className="text-[11px] text-text2">{row.bytes ? formatPercent((100 * cb[c]) / row.bytes) : '—'}</span>
            </li>
          ))}
        </ul>
      </DetailListPanel>
      <DetailListPanel title="Services in this application">
        <div className="relative overflow-x-auto">
          <table className="w-full min-w-[32rem] text-xs">
            <caption className="sr-only">Services classified as {row.name}</caption>
            <thead>
              <tr className="text-left text-[11px] text-text2">
                <th scope="col" className="py-1 font-medium">Service</th>
                <th scope="col" className="py-1 font-medium">How</th>
                <th scope="col" className="py-1 text-right font-medium">Requests</th>
                <th scope="col" className="py-1 text-right font-medium">Responses</th>
                <th scope="col" className="py-1 text-right font-medium">Total</th>
                <th scope="col" className="py-1"><span className="sr-only">Raw flows</span></th>
              </tr>
            </thead>
            <tbody>
              {row.services.map((s) => (
                <tr key={`${s.protocol}-${s.port}-${s.icmp_type_code ?? ''}`} className="border-t border-border/60">
                  <td className="py-1 font-medium text-text">{serviceText(s)}</td>
                  <td className="py-1 text-[11px] text-text2"><ConfidenceChip confidence={s.confidence} title={s.basis_text} /> {s.basis_text}</td>
                  <td className="py-1 text-right tabular-nums" title={s.request_bytes + s.response_bytes > 0 ? formatBytesExact(s.request_bytes) : 'Direction unknown (no service port)'}>{s.request_bytes + s.response_bytes > 0 ? formatBytes(s.request_bytes) : '—'}</td>
                  <td className="py-1 text-right tabular-nums" title={s.request_bytes + s.response_bytes > 0 ? formatBytesExact(s.response_bytes) : 'Direction unknown (no service port)'}>{s.request_bytes + s.response_bytes > 0 ? formatBytes(s.response_bytes) : '—'}</td>
                  <td className="py-1 text-right font-semibold tabular-nums" title={formatBytesExact(s.bytes)}>{formatBytes(s.bytes)}</td>
                  <td className="py-1 pl-2 text-right">
                    <DetailLink to={forensicsLink(ctx.filter, { app: row.name, ...s.drill }, ctx.window, ctx.meta)}>Flows</DetailLink>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DetailListPanel>
    </div>
  )
}
