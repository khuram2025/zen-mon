import { useState } from 'react'
import { RadioTower } from 'lucide-react'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { formatBytes, ipDisplay } from '@/lib/netflow/format'
import { hostLink, rankField, rankPhrase, rankRows, RANK_BY_OPTIONS, shareRows, splitPct } from './model'
import { CardFootnote, CardShell, HostLabel, RankedBars, Segmented, ShareLegend, ShareStrip, SplitLegend, useBreakdown } from './parts'
import type { RankBy, Talker, TalkersPage } from './shapes'
import type { OverviewCardProps } from './types'

/**
 * Hosts ranked by what they sent, received, or took part in (NF-24). Sent and received rankings
 * partition the scope, so they are shown as shares of all traffic with the remainder; involvement
 * double counts (every flow has two ends), so it is shown as ranked bars whose shares overlap.
 */
export function TopTalkersCard({ ctx, compact }: OverviewCardProps) {
  const [by, setBy] = useState<RankBy>('src')
  return <TalkersCardBody ctx={ctx} compact={compact} by={by} onBy={setBy} section="top-talkers" title="Top talkers" />
}

export function TalkersCardBody({
  ctx,
  compact,
  by,
  onBy,
  section,
  title,
}: OverviewCardProps & { by: RankBy; onBy?: (b: RankBy) => void; section: string; title: string }) {
  const limit = compact ? 5 : 7
  const { query, coverage } = useBreakdown<TalkersPage>(section === 'top-endpoints' ? '/netflow/top-endpoints' : '/netflow/top-talkers', ctx.filter, ctx.window, { by, limit })
  const meta = query.data?.meta ?? null
  const mw = meta?.window ?? null
  const toggle = (t: Talker) => ctx.setFilter({ talker: ctx.filter.talker === t.ip ? undefined : t.ip })
  const additive = by !== 'total'
  return (
    <CardShell
      title={title}
      icon={RadioTower}
      subtitle={additive ? `${rankPhrase(by)}, as a share of all traffic in scope` : 'All traffic each host took part in — hosts overlap, so shares do not add up'}
      controls={onBy ? <Segmented label={`${title} ranking`} value={by} options={RANK_BY_OPTIONS} onChange={onBy} /> : undefined}
      viewAll={ctx.sectionLink(section, { by })}
      footer={
        <CardFootnote
          meta={meta}
          filter={ctx.filter}
          basis={query.data?.data.total ? `Scope: ${formatBytes(query.data.data.total.bytes)} in ${query.data.data.distinct?.toLocaleString('en-US') ?? '—'} hosts` : undefined}
        />
      }
    >
      <QueryPanel query={query} label={title.toLowerCase()} skeleton="rows" compact coverage={coverage} isEmpty={(r) => r.data.items.length === 0}>
        {(r) => {
          const page = r.data
          if (additive) {
            const result = shareRows(page.items, {
              key: (t) => t.ip,
              label: (t) => ipDisplay(t.ip),
              value: (t) => Number((t as unknown as Record<string, number>)[rankField(by, 'bytes')] ?? 0),
              total: page.total?.bytes ?? null,
              remainderLabel: `All other hosts (${Math.max(0, (page.distinct ?? page.items.length) - page.items.length).toLocaleString('en-US')})`,
              colors: true,
            })
            return (
              <div className="space-y-2">
                <ShareStrip result={result} />
                <ShareLegend
                  result={result}
                  selected={(t) => ctx.filter.talker === t.ip}
                  render={(row) => (
                    <span className="break-all font-mono">{row.label}</span>
                  )}
                  sub={(t) => (t.hostname ? t.hostname : null)}
                  action={{
                    onSelect: toggle,
                    selectLabel: (t) => `Filter the page to host ${ipDisplay(t.ip)} (source or destination)`,
                    href: (t) => hostLink(t.ip, ctx.filter, ctx.window, mw),
                    hrefLabel: (t) => `Host profile for ${ipDisplay(t.ip)}`,
                  }}
                />
              </div>
            )
          }
          const rows = rankRows(page.items, { key: (t) => t.ip, label: (t) => ipDisplay(t.ip), value: (t) => t.bytes, total: page.total?.bytes ?? null })
          return (
            <div className="space-y-2">
              <SplitLegend a="Sent" b="Received" />
              <RankedBars
                rows={rows}
                selected={(t) => ctx.filter.talker === t.ip}
                split={(t) => {
                  const s = splitPct(t.src_bytes, t.dst_bytes)
                  return s ? { a: s.a, b: s.b, aLabel: `Sent ${formatBytes(t.src_bytes)}`, bLabel: `Received ${formatBytes(t.dst_bytes)}` } : null
                }}
                label={(row) => (
                  <HostLabel
                    ip={row.item.ip}
                    hostname={row.item.hostname}
                    profileHref={hostLink(row.item.ip, ctx.filter, ctx.window, mw)}
                    onFilter={() => toggle(row.item)}
                  />
                )}
              />
            </div>
          )
        }}
      </QueryPanel>
    </CardShell>
  )
}
