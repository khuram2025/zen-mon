import { Layers } from 'lucide-react'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { formatBytes } from '@/lib/netflow/format'
import { forensicsLink, shareRows } from './model'
import { CardFootnote, CardShell, ConfidenceChip, ShareLegend, ShareStrip, useBreakdown } from './parts'
import type { ApplicationRow, Paged } from './shapes'
import type { OverviewCardProps } from './types'

/**
 * Traffic by inferred application as a share of all traffic (every application is in the
 * denominator; the ones not listed fold into the remainder). Each row states how confidently
 * its bytes were classified, and tunnels say that their contents are not visible (NF-20).
 */
export function ApplicationsCard({ ctx, compact }: OverviewCardProps) {
  const { query, coverage } = useBreakdown<Paged<ApplicationRow>>('/netflow/applications', ctx.filter, ctx.window)
  const meta = query.data?.meta ?? null
  const max = compact ? 5 : 6
  return (
    <CardShell
      title="Applications"
      icon={Layers}
      subtitle="Inferred from exporter App-ID, rules, IP protocol and service ports"
      viewAll={ctx.sectionLink('applications')}
      footer={
        <CardFootnote
          meta={meta}
          filter={ctx.filter}
          basis={query.data?.data.total ? `Share of ${formatBytes(query.data.data.total.bytes)}` : undefined}
          intentional={{ app: 'every application stays visible; the selected one is marked' }}
        />
      }
    >
      <QueryPanel query={query} label="applications" skeleton="rows" compact coverage={coverage} isEmpty={(r) => r.data.items.length === 0}>
        {(r) => {
          const result = shareRows(r.data.items, {
            key: (a) => a.name,
            label: (a) => a.name,
            value: (a) => a.bytes,
            total: r.data.total?.bytes ?? null,
            max,
            remainderLabel: `Other applications (${Math.max(0, r.data.items.length - max)})`,
            colors: true,
          })
          return (
            <div className="space-y-2">
              <ShareStrip result={result} />
              <ShareLegend
                result={result}
                selected={(a) => a.selected || ctx.filter.app === a.name}
                sub={(a) => (
                  <span className="inline-flex flex-wrap items-center gap-1">
                    <span>{a.category}</span>
                    <ConfidenceChip confidence={a.confidence} title="How the bytes of this application were classified" />
                    {a.visibility_note && <span className="text-text2">{a.visibility_note}</span>}
                  </span>
                )}
                action={{
                  onSelect: (a) => ctx.setFilter({ app: ctx.filter.app === a.name ? undefined : a.name }),
                  selectLabel: (a) => `Filter the page to application ${a.name}`,
                  href: (a) => forensicsLink(ctx.filter, { app: a.name }, ctx.window, meta?.window),
                  hrefLabel: (a) => `Raw flows for ${a.name}`,
                }}
              />
            </div>
          )
        }}
      </QueryPanel>
    </CardShell>
  )
}
