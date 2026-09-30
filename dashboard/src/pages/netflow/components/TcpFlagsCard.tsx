import { ShieldAlert } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import type { TcpFlagSummary } from '../types'
import { EmptyState } from './EmptyState'

function TcpFlagsCard({ data, activeFlag, onSelect }: { data?: TcpFlagSummary; activeFlag?: string | null; onSelect?: (flag: string) => void }) {
  if (!data) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">TCP Flags</CardTitle>
          <p className="text-[11px] text-muted">Loading…</p>
        </CardHeader>
        <CardContent><EmptyState compact /></CardContent>
      </Card>
    )
  }
  const total = Math.max(1, data.total_tcp)
  const synRatio = data.syn_only / total
  const ackRatio = data.ack_only / total
  // Heuristic: high SYN-only ratio suggests scan or SYN flood; high RST suggests probes.
  const scanWarn = synRatio > 0.4 && data.syn_only > 1000
  const tiles: { key: string; label: string; value: number; tone: string }[] = [
    { key: 'syn_only', label: 'SYN-only (scan/SYN-flood)', value: data.syn_only, tone: scanWarn ? 'border-rose-500/50 bg-rose-500/10 text-rose-300' : 'border-cyan-400/30 bg-cyan-500/10 text-cyan-300' },
    { key: 'ack_only', label: 'ACK-only (mid-session)', value: data.ack_only, tone: 'border-violet-400/30 bg-violet-500/10 text-violet-300' },
    { key: 'rst', label: 'RST', value: data.rst, tone: 'border-amber-400/30 bg-amber-500/10 text-amber-300' },
    { key: 'fin', label: 'FIN', value: data.fin, tone: 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300' },
    { key: 'psh', label: 'PSH', value: data.psh, tone: 'border-indigo-400/30 bg-indigo-500/10 text-indigo-300' },
    { key: 'no_flags', label: 'No flags (anomaly)', value: data.no_flags, tone: data.no_flags > total * 0.05 ? 'border-rose-500/50 bg-rose-500/10 text-rose-300' : 'border-border bg-surface2/40 text-muted' },
  ]
  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2 pb-2">
        <ShieldAlert className={`h-4 w-4 ${scanWarn ? 'text-rose-400' : 'text-amber-400'}`} />
        <div>
          <CardTitle className="text-sm">TCP Flags</CardTitle>
          <p className="text-[11px] text-muted">{data.total_tcp.toLocaleString()} TCP flows · {(synRatio * 100).toFixed(1)}% SYN-only</p>
        </div>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-2">
        {tiles.map((t) => {
          const active = activeFlag === t.key
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => onSelect?.(t.key)}
              className={`rounded-md border p-2 text-left transition-transform hover:scale-[1.02] ${t.tone} ${active ? 'ring-2 ring-primary/60' : ''}`}
            >
              <div className="text-[10px] uppercase tracking-wider opacity-70">{t.label}</div>
              <div className="mt-0.5 text-sm font-semibold">{t.value.toLocaleString()}</div>
              <div className="text-[9px] opacity-70">{((t.value / total) * 100).toFixed(2)}%</div>
            </button>
          )
        })}
      </CardContent>
    </Card>
  )
}

export { TcpFlagsCard }
