import { useMemo, useState } from 'react'
import { Server, MapPin } from 'lucide-react'
import type { HopNode, PathGraphData } from './types'
import { ANON_HEX, INTERNAL_HEX, lossHex, nodeLabel, flag, fmtMs, fmtPct } from './helpers'

const COL_W = 176
const NODE_R = 21
const ROW_H = 84
const PAD_T = 72
const PAD_L = 104

interface Pos { x: number; y: number; node: HopNode | null; ttl: number; anon: boolean }

// per-link latency colour (high-delay links turn red, ThousandEyes-style)
function linkHex(delta: number): string {
  if (delta >= 60) return '#ef4444'
  if (delta >= 20) return '#f59e0b'
  return '#64748b'
}

export function PathGraph({ data, onSelect, selectedIp }: {
  data: PathGraphData
  onSelect: (n: HopNode | null) => void
  selectedIp: string | null
}) {
  const [hover, setHover] = useState<Pos | null>(null)
  const warn = data.probe.loss_warn_pct
  const crit = data.probe.loss_crit_pct

  const layout = useMemo(() => {
    const hops = [...data.hops].sort((a, b) => a.ttl - b.ttl)
    const maxTtl = hops.length ? Math.max(...hops.map((h) => h.ttl)) : 1
    const cols = maxTtl + 1 // +1 for source column
    const maxNodes = Math.max(1, ...hops.map((h) => Math.max(1, h.nodes.length)))
    const height = PAD_T + maxNodes * ROW_H
    const width = PAD_L + cols * COL_W + 40
    const midY = height / 2

    const pos = new Map<string, Pos>()
    const source: Pos = { x: PAD_L, y: midY, node: null, ttl: 0, anon: false }
    for (const h of hops) {
      const x = PAD_L + h.ttl * COL_W
      const nodes = h.nodes.length ? h.nodes : [null]
      const n = nodes.length
      const totalH = (n - 1) * ROW_H
      nodes.forEach((node, i) => {
        const y = midY - totalH / 2 + i * ROW_H
        const key = node ? node.ip : `anon-${h.ttl}`
        pos.set(key, { x, y, node, ttl: h.ttl, anon: !node })
      })
    }
    return { hops, maxTtl, width, height, source, pos }
  }, [data])

  // edges (source -> ttl1, then API edges)
  const edges = useMemo(() => {
    const out: { x1: number; y1: number; x2: number; y2: number; w: number; color: string; gap: boolean; key: string }[] = []
    const maxFlows = Math.max(1, ...data.edges.map((e) => e.flows), data.probe.flows)
    const width = (f: number) => 1.4 + (Math.log2(f + 1) / Math.log2(maxFlows + 1)) * 5
    // source -> first hop
    const firstHop = layout.hops.find((h) => h.ttl === layout.hops[0]?.ttl)
    if (firstHop) {
      for (const node of (firstHop.nodes.length ? firstHop.nodes : [null])) {
        const key = node ? node.ip : `anon-${firstHop.ttl}`
        const p = layout.pos.get(key)
        if (!p) continue
        out.push({ x1: layout.source.x + NODE_R, y1: layout.source.y, x2: p.x - NODE_R, y2: p.y,
          w: width(node?.flow_count || data.probe.flows), color: '#64748b', gap: false, key: `src-${key}` })
      }
    }
    for (const e of data.edges) {
      const a = layout.pos.get(e.from_ip)
      const b = layout.pos.get(e.to_ip)
      if (!a || !b) continue
      const delta = (b.node?.rtt_avg || 0) - (a.node?.rtt_avg || 0)
      out.push({ x1: a.x + NODE_R, y1: a.y, x2: b.x - NODE_R, y2: b.y, w: width(e.flows),
        color: linkHex(delta), gap: e.gap, key: `${e.from_ip}-${e.to_ip}` })
    }
    return out
  }, [data, layout])

  // AS cloud bounding boxes
  const clouds = useMemo(() => {
    const groups = new Map<string, { label: string; xs: number[]; ys: number[]; internal: boolean }>()
    for (const [, p] of layout.pos) {
      if (!p.node) continue
      const key = p.node.asn ? `as${p.node.asn}` : (p.node.is_internal ? 'internal' : null)
      if (!key) continue
      const label = p.node.asn
        ? `${p.node.as_name || 'AS' + p.node.asn} · AS${p.node.asn}`
        : 'Your network'
      const g = groups.get(key) || { label, xs: [], ys: [], internal: !p.node.asn && p.node.is_internal }
      g.xs.push(p.x); g.ys.push(p.y)
      groups.set(key, g)
    }
    return [...groups.values()].map((g) => {
      const minX = Math.min(...g.xs) - NODE_R - 16
      const maxX = Math.max(...g.xs) + NODE_R + 16
      const minY = Math.min(...g.ys) - NODE_R - 30
      const maxY = Math.max(...g.ys) + NODE_R + 30
      return { ...g, x: minX, y: minY, w: maxX - minX, h: maxY - minY }
    })
  }, [layout])

  const { width, height, source } = layout

  return (
    <div className="relative w-full overflow-x-auto rounded-lg border border-border bg-surface2/30">
      <div className="relative" style={{ width, height, minWidth: '100%' }}>
        <svg width={width} height={height} className="block">
          {/* AS clouds */}
          {clouds.map((c, i) => (
            <g key={i}>
              <rect x={c.x} y={c.y} width={c.w} height={c.h} rx={16}
                fill={c.internal ? 'rgba(59,130,246,0.07)' : 'rgba(100,116,139,0.08)'}
                stroke={c.internal ? 'rgba(59,130,246,0.35)' : 'rgba(100,116,139,0.3)'}
                strokeDasharray="5 4" strokeWidth={1.5} />
              <text x={c.x + 12} y={c.y + 18} fontSize={11} fontWeight={600}
                style={{ fill: c.internal ? INTERNAL_HEX : 'rgb(var(--muted))' }}>{c.label}</text>
            </g>
          ))}

          {/* edges */}
          {edges.map((e) => {
            const dx = Math.max(24, (e.x2 - e.x1) / 2)
            return (
              <path key={e.key} d={`M${e.x1},${e.y1} C${e.x1 + dx},${e.y1} ${e.x2 - dx},${e.y2} ${e.x2},${e.y2}`}
                fill="none" stroke={e.color} strokeWidth={e.w} strokeOpacity={0.7}
                strokeDasharray={e.gap ? '6 5' : undefined} strokeLinecap="round" />
            )
          })}

          {/* source node */}
          <g>
            <circle cx={source.x} cy={source.y} r={NODE_R} fill="rgb(var(--surface))" stroke={INTERNAL_HEX} strokeWidth={2.5} />
            <text x={source.x} y={source.y + 5} textAnchor="middle" fontSize={16}>📡</text>
            <text x={source.x} y={source.y + NODE_R + 16} textAnchor="middle" fontSize={11} fontWeight={600}
              style={{ fill: 'rgb(var(--text))' }}>Appliance</text>
            <text x={source.x} y={source.y - NODE_R - 8} textAnchor="middle" fontSize={10}
              style={{ fill: 'rgb(var(--muted))' }}>source</text>
          </g>

          {/* hop nodes */}
          {[...layout.pos.values()].map((p) => {
            if (p.anon) {
              return (
                <g key={`anon-${p.ttl}`} style={{ cursor: 'default' }}>
                  <circle cx={p.x} cy={p.y} r={NODE_R - 3} fill="none" stroke={ANON_HEX}
                    strokeWidth={1.5} strokeDasharray="3 3" />
                  <text x={p.x} y={p.y + 5} textAnchor="middle" fontSize={16} style={{ fill: ANON_HEX }}>∗</text>
                  <text x={p.x} y={p.y + NODE_R + 14} textAnchor="middle" fontSize={10} style={{ fill: 'rgb(var(--muted))' }}>
                    hop {p.ttl} · no reply
                  </text>
                </g>
              )
            }
            const n = p.node!
            const fill = lossHex(n.loss_pct, warn, crit)
            const hasLoss = (n.loss_pct || 0) > 0
            const isSel = selectedIp === n.ip
            const r = n.is_dest ? NODE_R + 3 : NODE_R
            return (
              <g key={n.ip} style={{ cursor: 'pointer' }}
                onClick={() => onSelect(n)}
                onMouseEnter={() => setHover(p)} onMouseLeave={() => setHover(null)}>
                {/* selection halo */}
                {isSel && <circle cx={p.x} cy={p.y} r={r + 6} fill="none" stroke="rgb(var(--primary))" strokeWidth={2} />}
                {/* destination target ring */}
                {n.is_dest && <circle cx={p.x} cy={p.y} r={r + 4} fill="none" stroke={fill} strokeWidth={1.5} strokeOpacity={0.5} />}
                {/* loss ring */}
                {hasLoss && <circle cx={p.x} cy={p.y} r={r + 2.5} fill="none" stroke="#ef4444" strokeWidth={2.5} />}
                <circle cx={p.x} cy={p.y} r={r} fill={fill}
                  stroke={n.device_id ? INTERNAL_HEX : 'rgba(0,0,0,0.15)'} strokeWidth={n.device_id ? 3 : 1} />
                {/* device / internal glyph */}
                {n.device_id
                  ? <g transform={`translate(${p.x - 7},${p.y - 7})`}><Server size={14} color="#fff" /></g>
                  : n.is_dest
                    ? <g transform={`translate(${p.x - 7},${p.y - 7})`}><MapPin size={14} color="#fff" /></g>
                    : <text x={p.x} y={p.y + 4} textAnchor="middle" fontSize={11} fontWeight={700} fill="#fff">{p.ttl}</text>}
                {/* labels */}
                <text x={p.x} y={p.y + r + 15} textAnchor="middle" fontSize={11} fontWeight={600} style={{ fill: 'rgb(var(--text))' }}>
                  {nodeLabel(n.ip, n.hostname)}
                </text>
                <text x={p.x} y={p.y + r + 29} textAnchor="middle" fontSize={10} style={{ fill: 'rgb(var(--muted))' }}>
                  {fmtMs(n.rtt_avg)}{hasLoss ? ` · ${fmtPct(n.loss_pct)} loss` : ''}
                </text>
              </g>
            )
          })}
        </svg>

        {/* hover tooltip */}
        {hover?.node && (
          <div className="pointer-events-none absolute z-10 rounded-md border border-border bg-surface px-3 py-2 text-xs shadow-lg"
            style={{ left: Math.min(hover.x + 12, width - 220), top: Math.max(8, hover.y - 70), width: 210 }}>
            <div className="font-semibold text-text">{hover.node.hostname || hover.node.ip}</div>
            {hover.node.hostname && <div className="text-muted">{hover.node.ip}</div>}
            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-muted">
              <span>RTT {fmtMs(hover.node.rtt_avg)}</span>
              <span>loss {fmtPct(hover.node.loss_pct)}</span>
              {hover.node.asn && <span>AS{hover.node.asn}</span>}
              {hover.node.country && <span>{flag(hover.node.country)} {hover.node.country}</span>}
            </div>
            {hover.node.as_name && <div className="mt-0.5 truncate text-muted">{hover.node.as_name}</div>}
            {hover.node.device_name && <div className="mt-0.5 font-medium text-primary">▣ {hover.node.device_name}</div>}
          </div>
        )}
      </div>
    </div>
  )
}
