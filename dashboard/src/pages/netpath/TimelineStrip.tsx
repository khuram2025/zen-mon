import { useEffect, useRef, useState } from 'react'
import type { SnapshotSummary } from './types'
import { STATUS_HEX, fmtClock, fmtMs } from './helpers'

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [w, setW] = useState(800)
  useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver((e) => setW(e[0].contentRect.width))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  return [ref, w] as const
}

const H = 96
const PAD = 8
const TOP = 16

// The 30-day time-travel timeline (SolarWinds' most-loved widget), rebuilt at
// per-run fidelity: status bars, a latency sparkline, and route-change /
// outage markers. Click any point to render the path exactly as it was then.
export function TimelineStrip({ snapshots, selectedId, onSelect }: {
  snapshots: SnapshotSummary[]
  selectedId: number | null
  onSelect: (s: SnapshotSummary) => void
}) {
  const [ref, W] = useWidth<HTMLDivElement>()
  if (!snapshots.length) {
    return <div className="flex h-24 items-center justify-center text-sm text-muted">No history in this window yet.</div>
  }
  const n = snapshots.length
  const innerW = Math.max(1, W - PAD * 2)
  const xFor = (i: number) => (n === 1 ? PAD + innerW / 2 : PAD + (i / (n - 1)) * innerW)
  const rtts = snapshots.map((s) => (s.reached ? s.rtt_ms : null))
  const maxRtt = Math.max(10, ...rtts.filter((v): v is number => v != null))
  const yFor = (v: number) => TOP + (1 - Math.min(v, maxRtt) / maxRtt) * (H - TOP - 24)

  // sparkline segments (break on unreachable)
  const segs: string[] = []
  let cur: string[] = []
  snapshots.forEach((s, i) => {
    if (s.reached && s.rtt_ms != null) cur.push(`${xFor(i)},${yFor(s.rtt_ms)}`)
    else { if (cur.length) segs.push(cur.join(' ')); cur = [] }
  })
  if (cur.length) segs.push(cur.join(' '))

  const selIdx = snapshots.findIndex((s) => s.id === selectedId)

  return (
    <div ref={ref} className="w-full">
      <svg width={W} height={H} className="block">
        {/* status bars */}
        {snapshots.map((s, i) => {
          const x = xFor(i)
          const bw = Math.max(1.5, innerW / n - 1)
          const col = STATUS_HEX[s.status] || STATUS_HEX.pending
          return (
            <rect key={s.id} x={x - bw / 2} y={H - 18} width={bw} height={12} rx={1}
              fill={col} fillOpacity={s.id === selectedId ? 1 : 0.55} />
          )
        })}
        {/* sparkline */}
        {segs.map((pts, i) => (
          <polyline key={i} points={pts} fill="none" stroke="rgb(var(--primary))" strokeWidth={1.8} strokeOpacity={0.9} />
        ))}
        {/* event markers */}
        {snapshots.map((s, i) => {
          if (!s.path_changed && s.reached) return null
          const x = xFor(i)
          const col = s.path_changed ? '#a855f7' : '#ef4444'
          return <path key={`m${s.id}`} d={`M${x},2 L${x - 4},9 L${x + 4},9 Z`} fill={col} />
        })}
        {/* selection cursor */}
        {selIdx >= 0 && (
          <line x1={xFor(selIdx)} y1={2} x2={xFor(selIdx)} y2={H - 6} stroke="rgb(var(--primary))" strokeWidth={1.5} strokeDasharray="3 2" />
        )}
        {/* click targets */}
        {snapshots.map((s, i) => {
          const x = xFor(i)
          const bw = innerW / n
          return <rect key={`h${s.id}`} x={x - bw / 2} y={0} width={Math.max(bw, 4)} height={H}
            fill="transparent" style={{ cursor: 'pointer' }} onClick={() => onSelect(s)}>
            <title>{`${fmtClock(s.run_at)} · ${s.status} · ${fmtMs(s.rtt_ms)}${s.path_changed ? ' · route changed' : ''}`}</title>
          </rect>
        })}
      </svg>
      <div className="flex items-center justify-between px-1 text-[11px] text-muted">
        <span>{fmtClock(snapshots[0].run_at)}</span>
        <span className="flex items-center gap-3">
          <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: '#a855f7' }} /> route change</span>
          <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: '#ef4444' }} /> unreachable</span>
        </span>
        <span>{fmtClock(snapshots[snapshots.length - 1].run_at)}</span>
      </div>
    </div>
  )
}
