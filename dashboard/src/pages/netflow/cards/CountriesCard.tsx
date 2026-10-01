import { useState } from 'react'
import { Globe2 } from 'lucide-react'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { formatBytes } from '@/lib/netflow/format'
import { shareRows } from './model'
import { CardFootnote, CardShell, Chip, Segmented, ShareLegend, ShareStrip, useBreakdown } from './parts'
import type { CountriesPage, CountryRow } from './shapes'
import type { OverviewCardProps } from './types'

export type Side = 'src' | 'dst'
export const SIDE_OPTIONS: { value: Side; label: string; hint: string }[] = [
  { value: 'src', label: 'Source', hint: 'Where traffic came from' },
  { value: 'dst', label: 'Destination', hint: 'Where traffic went' },
]

/** Regional-indicator flag for an ISO country code; none for private/reserved rows. */
export function flagEmoji(cc: string | null | undefined): string {
  if (!cc || !/^[A-Za-z]{2}$/.test(cc)) return ''
  const base = 0x1f1e6
  const up = cc.toUpperCase()
  return String.fromCodePoint(base + up.charCodeAt(0) - 65, base + up.charCodeAt(1) - 65)
}

/**
 * Traffic by country for one side at a time (IPv4 and IPv6). Each side partitions all traffic;
 * private and documentation space are their own rows, and addresses beyond the top networks
 * looked up are the stated remainder, not dropped.
 */
export function CountriesCard({ ctx, compact }: OverviewCardProps) {
  const [side, setSide] = useState<Side>('dst')
  const { query, coverage } = useBreakdown<CountriesPage>('/netflow/countries', ctx.filter, ctx.window)
  const meta = query.data?.meta ?? null
  const d = query.data?.data
  const unsupported = d && !d.available ? d.reason ?? 'No GeoIP country database is installed' : null
  const max = compact ? 5 : 7
  return (
    <CardShell
      title="Countries"
      icon={Globe2}
      subtitle={`By ${side === 'src' ? 'source' : 'destination'} address, IPv4 and IPv6`}
      controls={<Segmented label="Country side" value={side} options={SIDE_OPTIONS} onChange={setSide} />}
      viewAll={ctx.sectionLink('countries', { side })}
      footer={
        <CardFootnote
          meta={meta}
          filter={ctx.filter}
          basis={
            d?.total
              ? `Share of ${formatBytes(d.total.bytes)} · GeoIP ${d.geoip.country_db_version ?? 'version unknown'}`
              : undefined
          }
        />
      }
    >
      <QueryPanel
        query={query}
        label="countries"
        skeleton="rows"
        compact
        coverage={coverage}
        unsupported={unsupported}
        isEmpty={(r) => r.data.items.length === 0}
      >
        {(r) => {
          const field = side === 'src' ? 'src_bytes' : 'dst_bytes'
          const rows = [...r.data.items].sort((a, b) => b[field] - a[field]).filter((c) => c[field] > 0)
          const result = shareRows<CountryRow>(rows, {
            key: (c) => c.country ?? c.kind,
            label: (c) => c.country_name,
            value: (c) => c[field],
            total: r.data.total?.bytes ?? null,
            max,
            remainderLabel: 'Other countries and unresolved addresses',
            colors: true,
          })
          return (
            <div className="space-y-2">
              <ShareStrip result={result} />
              <ShareLegend
                result={result}
                render={(row) => (
                  <span className="inline-flex flex-wrap items-center gap-1">
                    {row.item?.country && <span aria-hidden>{flagEmoji(row.item.country)}</span>}
                    <span>{row.label}</span>
                    {row.item?.kind && row.item.kind !== 'country' && <Chip title={row.item.hint}>no country</Chip>}
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
