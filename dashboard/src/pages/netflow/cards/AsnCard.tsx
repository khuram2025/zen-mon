import { useState } from 'react'
import { Landmark } from 'lucide-react'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { formatBytes } from '@/lib/netflow/format'
import { shareRows } from './model'
import { CardFootnote, CardShell, Chip, Segmented, ShareLegend, ShareStrip, useBreakdown } from './parts'
import { SIDE_OPTIONS, type Side } from './CountriesCard'
import type { AsnRow, AsnsPage } from './shapes'
import type { OverviewCardProps } from './types'

export const AS_SOURCE_TEXT: Record<string, string> = {
  flow: 'AS number exported by the router (BGP)',
  geoip: 'AS number from the GeoIP ASN database',
  'flow+geoip': 'AS number from the router and the GeoIP ASN database',
}

/**
 * Top autonomous systems for one side (CR-20). The router's own AS numbers are used when it
 * exports them; otherwise the GeoIP ASN database places the address. Only router-exported AS
 * numbers can be filtered on (the `asn` filter matches the flow fields), so GeoIP-only rows are
 * informational.
 */
export function AsnCard({ ctx, compact }: OverviewCardProps) {
  const [side, setSide] = useState<Side>('dst')
  const { query, coverage } = useBreakdown<AsnsPage>('/netflow/asns', ctx.filter, ctx.window, { limit: 200 })
  const meta = query.data?.meta ?? null
  const d = query.data?.data
  const max = compact ? 5 : 7
  const unsupported = d && !d.geoip.asn_db && d.basis_bytes.flow === 0 ? 'Neither the exporters nor a GeoIP ASN database provide AS numbers' : null
  return (
    <CardShell
      title="Autonomous systems"
      icon={Landmark}
      subtitle={`By ${side === 'src' ? 'source' : 'destination'} AS`}
      controls={<Segmented label="AS side" value={side} options={SIDE_OPTIONS} onChange={setSide} />}
      viewAll={ctx.sectionLink('asns', { side })}
      footer={
        <CardFootnote
          meta={meta}
          filter={ctx.filter}
          basis={d?.total ? `Share of ${formatBytes(d.total.bytes)}` : undefined}
          intentional={{ asn: 'every AS stays visible; the selected one is marked' }}
        />
      }
    >
      <QueryPanel query={query} label="autonomous systems" skeleton="rows" compact coverage={coverage} unsupported={unsupported} isEmpty={(r) => r.data.items.length === 0}>
        {(r) => {
          const field = side === 'src' ? 'src_bytes' : 'dst_bytes'
          const rows = [...r.data.items].sort((a, b) => b[field] - a[field]).filter((a) => a[field] > 0)
          const result = shareRows<AsnRow>(rows, {
            key: (a) => (a.asn != null ? `AS${a.asn}` : a.kind),
            label: (a) => (a.asn != null ? `AS${a.asn}${a.as_name ? ` ${a.as_name}` : ''}` : a.as_name ?? a.kind),
            value: (a) => a[field],
            total: r.data.total?.bytes ?? null,
            max,
            remainderLabel: 'Other autonomous systems',
            colors: true,
          })
          return (
            <div className="space-y-2">
              <ShareStrip result={result} />
              <ShareLegend
                result={result}
                selected={(a) => a.selected}
                sub={(a) => (a.source ? <Chip title={AS_SOURCE_TEXT[a.source]}>{a.source === 'geoip' ? 'GeoIP' : 'Router'}</Chip> : a.hint ?? null)}
                action={{
                  onSelect: (a) => {
                    if (a.asn == null || a.source === 'geoip') return
                    ctx.setFilter({ asn: ctx.filter.asn === a.asn ? undefined : a.asn })
                  },
                  selectLabel: (a) =>
                    a.asn != null && a.source !== 'geoip'
                      ? `Filter the page to AS${a.asn}`
                      : `AS${a.asn ?? ''} comes from GeoIP only; the AS filter matches router-exported AS numbers`,
                }}
              />
            </div>
          )
        }}
      </QueryPanel>
    </CardShell>
  )
}
