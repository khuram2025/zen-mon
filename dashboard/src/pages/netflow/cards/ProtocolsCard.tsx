import { Database } from 'lucide-react'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { formatBytes, protocolLabel } from '@/lib/netflow/format'
import { forensicsLink, serviceText, shareRows } from './model'
import { CardFootnote, CardShell, ShareLegend, ShareStrip, useBreakdown } from './parts'
import type { Paged, ProtocolRow } from './shapes'
import type { OverviewCardProps } from './types'

/** IP protocols as shares of all traffic, each with its leading service. */
export function ProtocolsCard({ ctx, compact }: OverviewCardProps) {
  const { query, coverage } = useBreakdown<Paged<ProtocolRow>>('/netflow/protocols', ctx.filter, ctx.window)
  const meta = query.data?.meta ?? null
  const max = compact ? 4 : 6
  return (
    <CardShell
      title="IP protocols"
      icon={Database}
      subtitle="Share of all traffic by IP protocol"
      viewAll={ctx.sectionLink('protocols')}
      footer={
        <CardFootnote
          meta={meta}
          filter={ctx.filter}
          basis={query.data?.data.total ? `Share of ${formatBytes(query.data.data.total.bytes)}` : undefined}
        />
      }
    >
      <QueryPanel query={query} label="protocols" skeleton="rows" compact coverage={coverage} isEmpty={(r) => r.data.items.length === 0}>
        {(r) => {
          const result = shareRows(r.data.items, {
            key: (p) => `p${p.protocol}`,
            label: (p) => protocolLabel(p.protocol),
            value: (p) => p.bytes,
            total: r.data.total?.bytes ?? null,
            max,
            remainderLabel: 'Other protocols',
            colors: true,
          })
          return (
            <div className="space-y-2">
              <ShareStrip result={result} />
              <ShareLegend
                result={result}
                selected={(p) => ctx.filter.protocol === p.protocol}
                sub={(p) => {
                  const top = p.services[0]
                  if (!p.port_applicable) return 'No ports: services not applicable'
                  return top ? `Top service: ${serviceText({ ...top, protocol_name: p.name })}` : null
                }}
                action={{
                  onSelect: (p) => ctx.setFilter({ protocol: ctx.filter.protocol === p.protocol ? undefined : p.protocol }),
                  selectLabel: (p) => `Filter the page to ${protocolLabel(p.protocol)}`,
                  href: (p) => forensicsLink(ctx.filter, { protocol: p.protocol }, ctx.window, meta?.window),
                  hrefLabel: (p) => `Raw flows for ${protocolLabel(p.protocol)}`,
                }}
              />
            </div>
          )
        }}
      </QueryPanel>
    </CardShell>
  )
}
