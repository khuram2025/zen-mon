import { Link } from 'react-router-dom'
import { Boxes } from 'lucide-react'
import { PanelMessage, QueryPanel } from '@/components/netflow/QueryPanel'
import { formatBytes, formatPercent } from '@/lib/netflow/format'
import { rankRows, splitPct } from './model'
import { CardFootnote, CardShell, Figure, RankedBars, SplitLegend, useBreakdown } from './parts'
import type { IpGroupsPage } from './shapes'
import type { OverviewCardProps } from './types'

/**
 * Traffic by managed IP group / site: bytes sent from and received into the group, with the
 * traffic that stayed inside it. Groups overlap and a flow between two groups counts for both, so
 * these are ranked bars with their own shares; traffic touching no group is listed separately.
 */
export function IpGroupsCard({ ctx, compact }: OverviewCardProps) {
  const { query, coverage } = useBreakdown<IpGroupsPage>('/netflow/ip-groups', ctx.filter, ctx.window)
  const meta = query.data?.meta ?? null
  const d = query.data?.data
  const max = compact ? 5 : 8
  return (
    <CardShell
      title="IP groups & sites"
      icon={Boxes}
      subtitle="Traffic involving each group (groups can overlap)"
      viewAll={ctx.sectionLink('ip-groups')}
      footer={
        <CardFootnote
          meta={meta}
          filter={ctx.filter}
          basis={d?.total ? `Shares of ${formatBytes(d.total.bytes)}; a flow between two groups counts for both` : undefined}
          intentional={{ group: 'every group stays visible; the selected one is marked' }}
        />
      }
    >
      {d && !d.configured ? (
        <PanelMessage
          className="py-6"
          icon={Boxes}
          title="No IP groups are configured"
          detail={
            <>
              Define sites and address groups in <Link to="/netflow/settings" className="font-medium text-primary hover:underline">NetFlow settings</Link> to see their traffic here.
            </>
          }
        />
      ) : (
        <QueryPanel query={query} label="IP group traffic" skeleton="rows" compact coverage={coverage} isEmpty={(r) => r.data.items.length === 0}>
          {(r) => {
            const rows = rankRows(r.data.items.slice(0, max), { key: (g) => g.name, label: (g) => g.name, value: (g) => g.bytes, total: r.data.total?.bytes ?? null })
            return (
              <div className="space-y-2">
                <SplitLegend a="Sent from group" b="Received into group" />
                <RankedBars
                  rows={rows}
                  selected={(g) => g.selected}
                  split={(g) => {
                    const s = splitPct(g.src_bytes, g.dst_bytes)
                    return s ? { a: s.a, b: s.b, aLabel: `Sent ${formatBytes(g.src_bytes)}`, bLabel: `Received ${formatBytes(g.dst_bytes)}` } : null
                  }}
                  label={(row) => (
                    <button
                      type="button"
                      onClick={() => ctx.setFilter({ group: row.item.selected ? undefined : row.item.name })}
                      aria-pressed={row.item.selected}
                      aria-label={`Filter the page to IP group ${row.item.name}`}
                      className="max-w-full break-words text-left font-medium text-text hover:text-primary hover:underline"
                    >
                      {row.item.name}
                    </button>
                  )}
                  sub={(g) => (g.internal_bytes > 0 ? `${formatBytes(g.internal_bytes)} stayed inside the group` : g.description)}
                />
                {r.data.ungrouped && (
                  <div className="flex items-center justify-between border-t border-border/60 pt-1.5 text-xs">
                    <span className="italic text-text2">Traffic touching no group</span>
                    <span>
                      <Figure value={r.data.ungrouped.bytes} className="font-semibold text-text" />
                      <span className="ml-1.5 text-[11px] text-text2">{formatPercent(r.data.ungrouped.share_pct)}</span>
                    </span>
                  </div>
                )}
              </div>
            )
          }}
        </QueryPanel>
      )}
    </CardShell>
  )
}
