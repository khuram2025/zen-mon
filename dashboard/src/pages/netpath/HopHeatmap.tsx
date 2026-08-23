import type { HopLadderRow } from './types'
import { fmtClock, fmtMs, fmtPct } from './helpers'

// Green→amber→red heat scale for latency, relative to the worst hop in view.
function heat(rtt: number | null, max: number): string {
  if (rtt == null) return 'transparent'
  const t = Math.min(1, rtt / Math.max(1, max))
  // 130 (green) -> 40 (amber) -> 0 (red)
  const hue = 130 - t * 130
  return `hsl(${hue} 70% 45%)`
}

// Per-hop latency heatmap (hop rows × time columns) — the NOC "where does it
// hurt, and when" view. A red underline marks intervals with packet loss.
export function HopHeatmap({ times, ladder }: { times: string[]; ladder: HopLadderRow[] }) {
  if (!ladder.length) {
    return <div className="flex h-40 items-center justify-center text-sm text-muted">No per-hop history yet.</div>
  }
  const maxRtt = Math.max(10, ...ladder.flatMap((r) => r.series.map((c) => c?.rtt ?? 0)))
  const colW = Math.max(4, Math.min(16, Math.floor(900 / Math.max(1, times.length))))

  return (
    <div className="overflow-x-auto">
      <div className="inline-block min-w-full">
        {ladder.map((row) => (
          <div key={row.ttl} className="flex items-center gap-2 border-b border-border/40 py-1">
            <div className="flex w-52 shrink-0 items-center gap-2">
              <span className="inline-flex h-5 w-6 items-center justify-center rounded bg-surface2 text-[11px] font-semibold text-muted">
                {row.ttl}
              </span>
              <span className="truncate text-xs text-text" title={row.ip || ''}>
                {row.is_dest ? '🎯 ' : ''}{row.ip || '—'}
              </span>
            </div>
            <div className="flex items-end gap-[1px]">
              {row.series.map((cell, i) => (
                <div key={i}
                  className="h-6 rounded-[1px]"
                  style={{
                    width: colW,
                    background: cell?.anon ? 'repeating-linear-gradient(45deg,#94a3b833,#94a3b833 2px,transparent 2px,transparent 4px)' : heat(cell?.rtt ?? null, maxRtt),
                    borderBottom: cell && (cell.loss ?? 0) > 0 ? '2px solid #ef4444' : '2px solid transparent',
                  }}
                  title={cell ? `${fmtClock(times[i])}\n${cell.ip || row.ip || ''}\nRTT ${fmtMs(cell.rtt)} · loss ${fmtPct(cell.loss)}` : `${fmtClock(times[i])}\nno data`}
                />
              ))}
            </div>
          </div>
        ))}
        <div className="mt-2 flex items-center gap-3 pl-2 text-[11px] text-muted">
          <span>Latency</span>
          <span className="inline-block h-3 w-24 rounded" style={{ background: 'linear-gradient(90deg,hsl(130 70% 45%),hsl(65 70% 45%),hsl(0 70% 45%))' }} />
          <span>low → high</span>
          <span className="ml-3 border-b-2 border-danger">▁</span><span>loss</span>
          <span className="ml-3">▨ no reply</span>
        </div>
      </div>
    </div>
  )
}
