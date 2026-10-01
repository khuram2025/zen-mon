import { Link } from 'react-router-dom'
import { Cable } from 'lucide-react'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { formatBps, ipDisplay } from '@/lib/netflow/format'
import { capacityLink } from '@/lib/netflow/interfaceLinks'
import { rankRows, splitPct } from './model'
import { CardFootnote, CardShell, RankedBars, SplitLegend, useBreakdown } from './parts'
import type { InterfaceRow, InterfacesPage } from './shapes'
import type { OverviewCardProps } from './types'

/** "Gi0/1 on edge-rtr-1" — the interface is identified by its exporter too (X-19). */
export function interfaceTitle(i: Pick<InterfaceRow, 'display_name' | 'device_hostname' | 'exporter_ip' | 'ifindex'>): string {
  const where = i.device_hostname || ipDisplay(i.exporter_ip)
  return `${i.display_name || `ifIndex ${i.ifindex}`} on ${where}`
}

/**
 * The interface's utilization lives on the Capacity page (one definition: per-direction samples,
 * exporter gaps left out, duplicate ingress/egress export counted once), never recomputed from
 * this ranking's volumes, which count records the page's way.
 */
export const UTILIZATION_LINK_TITLE = 'Utilization is measured on the Capacity page: per direction over complete 5-minute samples, with exporter gaps left out and duplicate ingress and egress export counted once'

/**
 * Interfaces by the traffic that crossed them, within every page filter except the interface
 * selection itself (NF-27): the selected interface is highlighted and the others stay listed for
 * comparison, with that scope stated. Interface identity is exporter + ifIndex.
 */
export function InterfacesCard({ ctx, compact }: OverviewCardProps) {
  const limit = compact ? 5 : 8
  const { query, coverage } = useBreakdown<InterfacesPage>('/netflow/interfaces', ctx.filter, ctx.window, { limit })
  const meta = query.data?.meta ?? null
  const scope = query.data?.data.scope
  return (
    <CardShell
      title="Top interfaces"
      icon={Cable}
      subtitle={scope?.interface_excluded ? 'Selected interface highlighted; other interfaces shown for comparison' : 'Within the current filters, per exporter interface'}
      viewAll={ctx.sectionLink('interfaces')}
      footer={
        <CardFootnote
          meta={meta}
          filter={ctx.filter}
          basis="Volumes overlap across interfaces (a flow crosses an ingress and an egress interface)"
          intentional={{ iface: 'other interfaces kept for comparison', iface_dir: 'other interfaces kept for comparison' }}
        />
      }
    >
      <QueryPanel query={query} label="interfaces" skeleton="rows" compact coverage={coverage} isEmpty={(r) => r.data.items.length === 0}>
        {(r) => {
          const rows = rankRows(r.data.items, { key: (i) => i.iface, label: interfaceTitle, value: (i) => i.bytes })
          return (
            <div className="space-y-2">
              <SplitLegend a="In (ingress)" b="Out (egress)" />
              <RankedBars
                rows={rows}
                selected={(i) => i.selected}
                split={(i) => {
                  const s = splitPct(i.in_bytes, i.out_bytes)
                  return s ? { a: s.a, b: s.b, aLabel: `In ${formatBps(i.in_bps_avg)} avg`, bLabel: `Out ${formatBps(i.out_bps_avg)} avg` } : null
                }}
                label={(row) => {
                  const i = row.item
                  return (
                    <button
                      type="button"
                      onClick={() => ctx.setFilter({ iface: i.selected ? undefined : i.iface })}
                      aria-pressed={i.selected}
                      aria-label={`${i.selected ? 'Clear' : 'Filter the page to'} interface ${interfaceTitle(i)}`}
                      className="max-w-full text-left hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
                    >
                      <span className="break-words font-medium text-text">{i.display_name}</span>
                      <span className="ml-1 break-all text-[11px] text-text2">
                        {i.device_hostname || ipDisplay(i.exporter_ip)} · #{i.ifindex}
                      </span>
                      {i.pinned && <span className="ml-1 text-[11px] font-medium text-primary">· selected, outside the top {limit}</span>}
                    </button>
                  )
                }}
                sub={(i) => (
                  <span className="tabular-nums">
                    In {formatBps(i.in_bps_avg)} · Out {formatBps(i.out_bps_avg)} avg ·{' '}
                    <Link to={capacityLink(i.iface, ctx.window)} className="text-primary hover:underline" title={UTILIZATION_LINK_TITLE}>
                      {i.speed_in_bps == null && i.speed_out_bps == null ? 'speed unknown' : 'utilization'}
                    </Link>
                  </span>
                )}
              />
            </div>
          )
        }}
      </QueryPanel>
    </CardShell>
  )
}
