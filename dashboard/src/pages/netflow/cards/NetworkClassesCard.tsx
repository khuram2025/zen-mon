import { Network } from 'lucide-react'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { formatBytes, formatPercent } from '@/lib/netflow/format'
import { cn } from '@/lib/utils'
import { assignColorSlots, seriesClass, shareRows } from './model'
import { CardFootnote, CardShell, Figure, ShareStrip, useBreakdown } from './parts'
import type { NetClassesPage } from './shapes'
import type { OverviewCardProps } from './types'

/**
 * Address classes on each side of the traffic. Each side partitions all traffic on its own (a
 * flow has one source and one destination), so there is one 100% strip per side, and the colours
 * follow the class, not its rank.
 */
export function NetworkClassesCard({ ctx }: OverviewCardProps) {
  const { query, coverage } = useBreakdown<NetClassesPage>('/netflow/network-classes', ctx.filter, ctx.window)
  const meta = query.data?.meta ?? null
  return (
    <CardShell
      title="Network classes"
      icon={Network}
      subtitle="Private, public, CGNAT, multicast and IPv6 classes, per side"
      viewAll={ctx.sectionLink('network-classes')}
      footer={
        <CardFootnote
          meta={meta}
          filter={ctx.filter}
          basis={query.data?.data.total ? `Each side is a share of ${formatBytes(query.data.data.total.bytes)}` : undefined}
          intentional={{ netclass: 'every class stays visible; the selected one is marked' }}
        />
      }
    >
      <QueryPanel query={query} label="network classes" skeleton="rows" compact coverage={coverage} isEmpty={(r) => r.data.items.length === 0}>
        {(r) => {
          const items = r.data.items
          const total = r.data.total?.bytes ?? null
          const slots = assignColorSlots(items.map((c) => c.name))
          const side = (field: 'src_bytes' | 'dst_bytes') => {
            const res = shareRows(items, { key: (c) => c.name, label: (c) => c.name, value: (c) => c[field], total })
            res.rows.forEach((row) => (row.slot = slots.get(row.key)))
            return res
          }
          return (
            <div className="space-y-2">
              {(['src_bytes', 'dst_bytes'] as const).map((f) => (
                <div key={f} className="flex items-center gap-2">
                  <span className="w-20 shrink-0 text-xs text-text2">{f === 'src_bytes' ? 'As source' : 'As destination'}</span>
                  <ShareStrip result={side(f)} className="flex-1" />
                </div>
              ))}
              <table className="w-full text-xs">
                <caption className="sr-only">Traffic by network class and side</caption>
                <thead>
                  <tr className="text-xs text-text2">
                    <th scope="col" className="py-1 text-left font-medium">Class</th>
                    <th scope="col" className="py-1 text-right font-medium">As source</th>
                    <th scope="col" className="py-1 text-right font-medium">As destination</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((c) => {
                    const selected = c.selected || ctx.filter.netclass === c.name
                    return (
                      <tr key={c.name} className={cn('border-t border-border/60', selected && 'bg-primary/5')}>
                        <th scope="row" className="py-1 text-left font-medium">
                          <button
                            type="button"
                            onClick={() => ctx.setFilter({ netclass: ctx.filter.netclass === c.name ? undefined : c.name })}
                            aria-pressed={selected}
                            aria-label={`Filter the page to network class ${c.name} (either side)`}
                            className="inline-flex items-center gap-1.5 text-left text-text hover:text-primary hover:underline"
                          >
                            <span className={cn('h-2.5 w-2.5 shrink-0 rounded-sm', seriesClass(slots.get(c.name)))} aria-hidden />
                            {c.name}
                          </button>
                        </th>
                        <td className="py-1 text-right">
                          <Figure value={c.src_bytes} className="text-text" />
                          <div className="text-xs text-text2">{formatPercent(c.src_share_pct)}</div>
                        </td>
                        <td className="py-1 text-right">
                          <Figure value={c.dst_bytes} className="text-text" />
                          <div className="text-xs text-text2">{formatPercent(c.dst_share_pct)}</div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )
        }}
      </QueryPanel>
    </CardShell>
  )
}
