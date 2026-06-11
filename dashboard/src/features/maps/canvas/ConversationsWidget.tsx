import { useQuery } from '@tanstack/react-query'
import { ArrowRight } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import type { MapShape } from '../core'

/* Live "Top Conversations" panel placed on the map as an annotation. Polls
 * real NetFlow data (optionally scoped to one exporter/device) and renders
 * ranked conversation bars that re-sort and re-scale as traffic shifts. */

type Conversation = {
  src: string
  dst: string
  protocol_name: string
  service: string
  application: string
  bytes: number
  flows: number
}

const RANK_COLORS = ['#22d3ee', '#34d399', '#a78bfa', '#fbbf24', '#f472b6', '#60a5fa', '#f87171', '#4ade80', '#c084fc', '#fb923c']

export function ConversationsWidget({ shape }: { shape: MapShape }) {
  const m = shape.metadata || {}
  const limit = m.limit || 5
  const hours = m.hours || 1
  const exporter = m.exporter || null

  const q = useQuery<Conversation[]>({
    queryKey: ['map-conversations', exporter, limit, hours],
    queryFn: async () => {
      const scope = exporter ? `&exporter=${encodeURIComponent(exporter)}` : ''
      return (await api.get(`/netflow/top-conversations?hours=${hours}&limit=${limit}${scope}`)).data
    },
    refetchInterval: 30_000,
    retry: 1,
  })

  const rows = q.data || []
  const max = rows[0]?.bytes || 1

  return (
    <div className="flex h-full w-full flex-col overflow-hidden rounded-lg border border-border bg-surface/95 shadow-xl backdrop-blur">
      <div className="flex items-center justify-between gap-2 border-b border-border bg-surface2/50 px-2.5 py-1.5">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="relative flex h-1.5 w-1.5 shrink-0">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-60" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-success" />
          </span>
          <span className="truncate text-[10px] font-bold uppercase tracking-wider text-text">
            Top {limit} Conversations
          </span>
        </div>
        <span className="shrink-0 font-mono text-[8.5px] text-muted">
          {exporter ? exporter : 'all exporters'} · {hours}h
        </span>
      </div>

      <div className="min-h-0 flex-1 space-y-[5px] overflow-hidden px-2.5 py-2">
        {q.isLoading && <div className="py-3 text-center text-[10px] text-muted">Loading flows…</div>}
        {!q.isLoading && !rows.length && <div className="py-3 text-center text-[10px] text-muted">No flow records{exporter ? ' for this device' : ''}</div>}
        {rows.map((c, i) => (
          <div key={`${c.src}-${c.dst}-${c.service}`} className="group">
            <div className="flex items-baseline justify-between gap-1.5">
              <span className="flex min-w-0 items-center gap-1 font-mono text-[9.5px] font-medium leading-tight text-text">
                <span className="w-2.5 shrink-0 text-[8px] text-muted">{i + 1}</span>
                <span className="truncate">{c.src}</span>
                <ArrowRight className="h-2.5 w-2.5 shrink-0 text-muted" />
                <span className="truncate">{c.dst}</span>
              </span>
              <span className="shrink-0 font-mono text-[9px] font-semibold tabular-nums" style={{ color: RANK_COLORS[i % RANK_COLORS.length] }}>
                {fmtBytes(c.bytes)}
              </span>
            </div>
            <div className="mt-[2px] flex items-center gap-1.5">
              <div className="h-[5px] flex-1 overflow-hidden rounded-full bg-surface2/90">
                <div
                  className="h-full rounded-full transition-all duration-1000 ease-out"
                  style={{
                    width: `${Math.max(2, (c.bytes / max) * 100)}%`,
                    background: `linear-gradient(90deg, ${RANK_COLORS[i % RANK_COLORS.length]}99, ${RANK_COLORS[i % RANK_COLORS.length]})`,
                  }}
                />
              </div>
              <span className={cn('shrink-0 rounded bg-surface2 px-1 py-px text-[7.5px] font-semibold leading-none text-muted')}>
                {c.service || c.protocol_name}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function fmtBytes(b: number): string {
  if (!isFinite(b) || b <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0, v = b
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`
}
