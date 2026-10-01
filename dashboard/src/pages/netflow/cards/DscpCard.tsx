import { Gauge } from 'lucide-react'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { dscpLabel, formatBytes } from '@/lib/netflow/format'
import { dscpShareItems, forensicsLink, shareRows } from './model'
import { CardFootnote, CardShell, ShareLegend, ShareStrip, useBreakdown } from './parts'
import type { DscpPage } from './shapes'
import type { OverviewCardProps } from './types'

/**
 * Traffic by DSCP class; every class present is in the denominator (NF-25). Traffic whose exporter
 * did not send the ToS field is its own "Not exported" row: unknown, never Best Effort.
 */
export function DscpCard({ ctx, compact }: OverviewCardProps) {
  const { query, coverage } = useBreakdown<DscpPage>('/netflow/dscp', ctx.filter, ctx.window)
  const meta = query.data?.meta ?? null
  const max = compact ? 4 : 6
  return (
    <CardShell
      title="QoS / DSCP"
      icon={Gauge}
      subtitle="Share of traffic by DSCP code point"
      viewAll={ctx.sectionLink('dscp')}
      footer={
        <CardFootnote
          meta={meta}
          filter={ctx.filter}
          basis={query.data?.data.total ? `Share of ${formatBytes(query.data.data.total.bytes)}` : undefined}
          intentional={{ dscp: 'every class stays visible; the selected one is marked' }}
        />
      }
    >
      <QueryPanel query={query} label="DSCP classes" skeleton="rows" compact coverage={coverage} isEmpty={(r) => r.data.items.length === 0 && !r.data.not_exported}>
        {(r) => {
          const result = shareRows(dscpShareItems(r.data, max), {
            key: (d) => (d.notExported ? 'dscp-not-exported' : `dscp${d.dscp}`),
            label: (d) => (d.notExported ? 'Not exported (DSCP unknown)' : dscpLabel(d.dscp)),
            value: (d) => d.bytes,
            total: r.data.total?.bytes ?? null,
            max,
            remainderLabel: 'Other classes',
            colors: true,
          })
          return (
            <div className="space-y-2">
              <ShareStrip result={result} />
              <ShareLegend
                result={result}
                selected={(d) => d.selected || ctx.filter.dscp === d.dscp}
                action={{
                  onSelect: (d) => ctx.setFilter({ dscp: ctx.filter.dscp === d.dscp ? undefined : d.dscp }),
                  selectLabel: (d) => `Filter the page to ${dscpLabel(d.dscp)}`,
                  // Records without ToS cannot be filtered by DSCP: no filter, no raw-flow link.
                  selectable: (d) => !d.notExported,
                  href: (d) => (d.notExported ? null : forensicsLink(ctx.filter, { dscp: d.dscp }, ctx.window, meta?.window)),
                  hrefLabel: (d) => `Raw flows marked ${dscpLabel(d.dscp)}`,
                }}
              />
            </div>
          )
        }}
      </QueryPanel>
    </CardShell>
  )
}
