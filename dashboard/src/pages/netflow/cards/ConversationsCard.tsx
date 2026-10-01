import { useId, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDownUp, ArrowLeftRight, ArrowRight, Search } from 'lucide-react'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { formatBytes, formatPercent, ipDisplay } from '@/lib/netflow/format'
import { parseIpOrCidr } from '@/lib/netflow/ip'
import { cn } from '@/lib/utils'
import { forensicsLink, hostLink, orientationLabels, serviceText, splitPct } from './model'
import { CardFootnote, CardShell, ConfidenceChip, Figure, HostLink, SplitLegend, useBreakdown } from './parts'
import { BAR_CLASS, BAR2_CLASS } from './model'
import type { Conversation, Paged } from './shapes'
import type { OverviewCardProps } from './types'

/** Case-insensitive match of a local search against what a conversation row shows. */
export function conversationMatches(c: Conversation, q: string): boolean {
  const needle = q.trim().toLowerCase()
  if (!needle) return true
  return [c.client, c.server, ipDisplay(c.client), ipDisplay(c.server), c.service ?? '', c.application, c.protocol_name,
    c.service_port != null ? String(c.service_port) : '', c.client_hostname ?? '', c.server_hostname ?? '']
    .some((v) => v.toLowerCase().includes(needle))
}

/**
 * Top conversations, one row per exchange: client → server on a service with both directions'
 * bytes (NF-20, X-30). The search box only narrows the rows on screen and says so (NF-26); a
 * valid address or CIDR can be searched across all raw records instead.
 */
export function ConversationsCard({ ctx, compact }: OverviewCardProps) {
  const limit = compact ? 6 : 10
  const { query, coverage } = useBreakdown<Paged<Conversation>>('/netflow/top-conversations', ctx.filter, ctx.window, { limit })
  const [q, setQ] = useState('')
  const searchId = useId()
  const meta = query.data?.meta ?? null
  const mw = meta?.window ?? null
  const parsed = parseIpOrCidr(q.trim())
  const rawSearch = parsed ? forensicsLink(ctx.filter, { talker: q.trim() }, ctx.window, mw) : null
  const total = query.data?.data.total?.bytes ?? null

  return (
    <CardShell
      title="Top conversations"
      icon={ArrowDownUp}
      subtitle="Client → server per service, both directions in one row"
      viewAll={ctx.sectionLink('conversations')}
      footer={
        <CardFootnote
          meta={meta}
          filter={ctx.filter}
          basis={total != null ? `Share of all ${formatBytes(total)} in scope` : undefined}
        />
      }
    >
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <label htmlFor={searchId} className="sr-only">Search displayed top conversations</label>
        <div className="relative min-w-[12rem] flex-1">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-text2" aria-hidden />
          <input
            id={searchId}
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search displayed top conversations"
            className="w-full rounded-md border border-border bg-surface py-1.5 pl-7 pr-2 text-xs text-text placeholder:text-text2 focus:outline-none focus:ring-2 focus:ring-primary/40"
          />
        </div>
        {rawSearch ? (
          <Link to={rawSearch} className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1.5 text-xs font-medium text-primary hover:bg-surface2">
            Search all raw flows for {q.trim()} <ArrowRight className="h-3 w-3" aria-hidden />
          </Link>
        ) : q.trim() ? (
          <span className="text-[11px] text-text2">Only the {limit} rows below are searched; enter an IP or CIDR to search all raw flows.</span>
        ) : null}
      </div>
      <QueryPanel query={query} label="top conversations" skeleton="rows" compact coverage={coverage} isEmpty={(r) => r.data.items.length === 0}>
        {(r) => <ConversationList rows={r.data.items.filter((c) => conversationMatches(c, q))} ctx={ctx} mw={mw} total={r.data.total?.bytes ?? null} searched={!!q.trim()} />}
      </QueryPanel>
    </CardShell>
  )
}

function ConversationList({ rows, ctx, mw, total, searched }: { rows: Conversation[]; ctx: OverviewCardProps['ctx']; mw: { from: string; to: string } | null; total: number | null; searched: boolean }) {
  const shown = useMemo(() => rows, [rows])
  if (shown.length === 0) {
    return <p className="py-4 text-center text-xs text-text2">{searched ? 'No displayed conversation matches this search.' : 'No conversations.'}</p>
  }
  return (
    <div className="space-y-2">
      <SplitLegend a="Client → server" b="Server → client" />
      <ul role="list" className="divide-y divide-border/60">
        {shown.map((c) => {
          const labels = orientationLabels(c.orientation)
          const split = splitPct(c.bytes_c2s, c.bytes_s2c)
          const key = `${c.client}|${c.server}|${c.protocol}|${c.service_port ?? c.icmp_type_code ?? 0}`
          return (
            <li key={key} className="py-2">
              <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
                <div className="min-w-0 flex-1 text-xs">
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="sr-only">{labels.a}</span>
                    <HostLink ip={c.client} href={hostLink(c.client, ctx.filter, ctx.window, mw)} />
                    {c.orientation === 'client_server'
                      ? <ArrowRight className="h-3 w-3 shrink-0 text-text2" aria-label="to server" />
                      : <ArrowLeftRight className="h-3 w-3 shrink-0 text-text2" aria-label="and" />}
                    <span className="sr-only">{labels.b}</span>
                    <HostLink ip={c.server} href={hostLink(c.server, ctx.filter, ctx.window, mw)} />
                  </div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-text2">
                    <button
                      type="button"
                      className="font-medium text-text hover:text-primary hover:underline"
                      onClick={() => ctx.setFilter({ ...c.drill })}
                      aria-label={`Filter the page to service ${serviceText(c)}`}
                      title="Filter the page to this service"
                    >
                      {serviceText(c)}
                    </button>
                    <span aria-hidden>·</span>
                    <span>{c.application}</span>
                    <ConfidenceChip confidence={c.confidence} title={c.basis_text} />
                  </div>
                </div>
                <div className="shrink-0 text-right text-xs">
                  <Figure value={c.bytes} className="font-semibold text-text" />
                  <div className="text-[11px] tabular-nums text-text2">{total ? formatPercent((100 * c.bytes) / total) : '—'}</div>
                </div>
              </div>
              <div className="mt-1 flex h-1.5 w-full gap-[2px] overflow-hidden rounded-full bg-surface2" aria-hidden>
                {split && split.a > 0 && <div className={cn('h-full', BAR_CLASS)} style={{ flexGrow: split.a, flexBasis: 0 }} />}
                {split && split.b > 0 && <div className={cn('h-full', BAR2_CLASS)} style={{ flexGrow: split.b, flexBasis: 0 }} />}
              </div>
              <div className="mt-0.5 flex flex-wrap justify-between gap-x-2 text-[11px] tabular-nums text-text2">
                <span>{labels.forward} {formatBytes(c.bytes_c2s)}</span>
                <span>{labels.reverse} {formatBytes(c.bytes_s2c)}</span>
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
