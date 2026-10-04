import { Link } from 'react-router-dom'
import { ShieldAlert } from 'lucide-react'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { StatusTile } from '@/components/netflow/StatusTile'
import { useNfCoverage, useNfQuery, nfParams } from '@/lib/netflow/api'
import { TCP_FLAG_LABELS, type TcpFlagFilter } from '@/lib/netflow/filters'
import { formatCount, formatCountExact, formatPercent } from '@/lib/netflow/format'
import { coverageState } from '@/lib/netflow/status'
import { RST_STATUS_TEXT } from '../overviewModel'
import { forensicsLink } from './model'
import { CardFootnote, CardShell } from './parts'
import type { TcpFlags } from './shapes'
import type { OverviewCardProps } from './types'

const PRIMARY: { key: TcpFlagFilter; label: string; caption: string }[] = [
  { key: 'syn_only', label: 'SYN only', caption: 'Connection attempts that never completed a handshake in this record' },
  { key: 'rst', label: 'RST', caption: 'Resets: refused or aborted connections' },
  { key: 'fin', label: 'FIN', caption: 'Orderly closes' },
  { key: 'no_flags', label: 'No flags', caption: 'Flags exported and all zero' },
]
const SECONDARY: TcpFlagFilter[] = ['ack_only', 'psh', 'urg']

/**
 * TCP control flags, counted only over flows whose flags were exported: a template without the
 * TCP-flags field stores zeros that are not observations, so the card says "unknown" instead of
 * "no SYN/RST" (NF-07), and states which share of TCP flows the counts cover.
 */
export function TcpFlagsCard({ ctx, compact }: OverviewCardProps) {
  const params = nfParams(ctx.window, ctx.filter)
  const query = useNfQuery<TcpFlags>('/netflow/tcp-flags', params, { window: ctx.window })
  const coverage = useNfCoverage(ctx.window, ctx.filter)
  const meta = query.data?.meta ?? null
  const d = query.data?.data
  const unsupported = d && (d.support === 'unsupported' || d.support === 'not_applicable') ? d.reason ?? true : null
  const pctOf = (n: number, base: number) => (base > 0 ? formatPercent((100 * n) / base) : '—')
  // RST uses the one server-side ratio (/device-status, KPI): not stated below its minimum sample.
  const caption = (f: TcpFlags, key: TcpFlagFilter) => {
    if (key === 'rst' && f.rst_ratio) {
      return f.rst_ratio.pct === null ? RST_STATUS_TEXT[f.rst_ratio.status] : `${formatPercent(f.rst_ratio.pct)} of flagged TCP`
    }
    return `${pctOf(f[key], f.flagged_tcp)} of flagged TCP`
  }
  return (
    <CardShell
      title="TCP flags"
      icon={ShieldAlert}
      subtitle="Share of TCP flows whose flags were exported"
      viewAll={ctx.sectionLink('tcp-flags')}
      footer={
        <CardFootnote
          meta={meta}
          filter={ctx.filter}
          basis={d && d.total_tcp > 0 ? `Flags known for ${formatCountExact(d.flagged_tcp)} of ${formatCountExact(d.total_tcp)} TCP flows (${formatPercent(d.flag_coverage_pct)})` : undefined}
          intentional={{ tcpflag: 'every flag stays visible; the selected one is marked' }}
        />
      }
    >
      <QueryPanel
        query={query}
        label="TCP flag counts"
        skeleton="tiles"
        compact
        coverage={coverageState(coverage.data?.data)}
        unsupported={unsupported}
        isEmpty={(r) => r.data.total_tcp === 0}
        empty="No TCP flows in this window and filter"
      >
        {(r) => {
          const f = r.data
          const base = f.flagged_tcp
          return (
            <div className="space-y-2">
              {f.support === 'partial' && (
                <p className="rounded border border-warning/35 bg-warning/10 px-2 py-1 text-[11px] text-text">
                  <span className="font-semibold text-warning-fg">Partial:</span> {f.reason}
                </p>
              )}
              <div className={compact ? 'grid grid-cols-2 gap-2' : 'grid grid-cols-2 gap-2 sm:grid-cols-4'}>
                {PRIMARY.map((p) => (
                  <StatusTile
                    key={p.key}
                    tone="neutral"
                    label={p.label}
                    value={formatCount(f[p.key])}
                    title={`${formatCountExact(f[p.key])} flows · ${p.caption}`}
                    caption={caption(f, p.key)}
                    active={ctx.filter.tcpflag === p.key}
                    onClick={() => ctx.setFilter({ tcpflag: ctx.filter.tcpflag === p.key ? undefined : p.key })}
                  />
                ))}
              </div>
              <ul role="list" className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-text2">
                {SECONDARY.map((k) => (
                  <li key={k}>
                    <Link
                      to={forensicsLink(ctx.filter, { tcpflag: k, protocol: 6 }, ctx.window, meta?.window)}
                      className="hover:text-primary hover:underline"
                      title={`Raw flows: ${TCP_FLAG_LABELS[k]}`}
                    >
                      {TCP_FLAG_LABELS[k]}: <span className="font-semibold tabular-nums text-text">{formatCountExact(f[k])}</span> ({pctOf(f[k], base)})
                    </Link>
                  </li>
                ))}
                {f.unknown_tcp > 0 && (
                  <li>
                    Flags unknown: <span className="font-semibold tabular-nums text-text">{formatCountExact(f.unknown_tcp)}</span> TCP flows
                  </li>
                )}
              </ul>
            </div>
          )
        }}
      </QueryPanel>
    </CardShell>
  )
}
