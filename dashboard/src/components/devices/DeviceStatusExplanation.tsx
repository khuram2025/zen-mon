import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, Info } from 'lucide-react'
import { api } from '@/lib/api'
import { relativeTime } from '@/lib/utils'

type Breach = { metric: string; value: number; threshold: number; exceeded_by: number; excess_unit: string }
type Sample = { timestamp: string; rtt_ms: number; packet_loss_pct: number; is_up: boolean; stale?: boolean; settings_pending?: boolean; breaches: Breach[] }
type Explanation = {
  status: string; mode: string; source: string
  thresholds: { degraded_rtt_ms: number; degraded_loss_pct: number }
  latest?: Sample | null
  recent_degraded?: { timestamp: string; reason: string; sample?: Sample | null; thresholds_unchanged: boolean; recovered_at?: string | null } | null
}
const number = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 3 })

function Breaches({ sample }: { sample: Sample }) {
  return <div className="space-y-1">{sample.breaches.map(b => <p key={b.metric} className="text-sm">
    <b>{b.metric}: {number(b.value)}{b.metric === 'Packet loss' ? '%' : ' ms'}</b>
    {' exceeds '}{number(b.threshold)}{b.metric === 'Packet loss' ? '%' : ' ms'}
    {' by '}<b>{number(b.exceeded_by)} {b.excess_unit}</b>.
  </p>)}</div>
}

export function DeviceStatusExplanation({ deviceId }: { deviceId: string }) {
  const { data, isError } = useQuery<Explanation>({
    queryKey: ['device-status-explanation', deviceId],
    queryFn: async () => (await api.get(`/devices/${deviceId}/status-explanation`)).data,
    refetchInterval: 15_000,
  })
  if (isError) return <div className="rounded-lg border border-border p-3 text-sm text-muted">Status details are temporarily unavailable. The device status above remains the last reported status.</div>
  if (!data) return null
  if (data.mode !== 'ping') return <div className="flex gap-2 rounded-lg border border-border p-3 text-sm text-muted"><Info className="h-4 w-4 shrink-0" />{data.mode === 'managed' ? 'Status is reported by the managing controller; the global ping degradation thresholds do not determine this device’s status.' : 'Ping monitoring is disabled or has no assigned owner. Global ping degradation thresholds are not evaluated here.'}</div>
  const latest = data.latest
  const active = data.status === 'degraded'
  const recent = data.recent_degraded
  const currentEvidence = latest && !latest.stale && !latest.settings_pending
  return <section aria-label="Device status explanation" className={`rounded-xl border p-4 ${active ? 'border-warning/40 bg-warning/5' : 'border-border bg-surface'}`}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="flex items-center gap-2 text-sm font-semibold">{active ? <AlertTriangle className="h-4 w-4 text-warning" /> : <Info className="h-4 w-4 text-info" />} {active ? 'Why this device is degraded' : 'Reachability status details'}</h3>
      <Link className="text-xs text-primary hover:underline" to="/settings/general?tab=monitoring">Monitoring thresholds</Link>
    </div>
    <p className="mt-2 text-xs text-muted">Degraded when a responding ping check has latency above <b>{number(data.thresholds.degraded_rtt_ms)} ms</b> OR packet loss above <b>{number(data.thresholds.degraded_loss_pct)}%</b>. Equality does not trigger degradation.</p>
    {latest ? <div className="mt-3 rounded-lg border border-border bg-surface2/30 p-3">
      <div className="mb-2 text-xs text-muted">Latest check · {data.source} · {relativeTime(latest.timestamp)} · {new Date(latest.timestamp).toLocaleString()}</div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm"><span>Latency <b>{latest.is_up ? `${number(latest.rtt_ms)} ms` : 'No response'}</b> / {number(data.thresholds.degraded_rtt_ms)} ms limit</span><span>Packet loss <b>{number(latest.packet_loss_pct)}%</b> / {number(data.thresholds.degraded_loss_pct)}% limit</span></div>
      <div className="mt-2">{latest.stale ? <p className="text-xs text-warning">This sample is overdue and cannot explain the current network condition.</p> : latest.settings_pending ? <p className="text-xs text-muted">Threshold settings recently changed. Wait for the poller configuration refresh and next check.</p> : !latest.is_up ? <p className="text-xs text-warning">The latest check received no response; the Down confirmation policy applies.</p> : latest.breaches.length ? <Breaches sample={latest} /> : <p className="flex items-center gap-1 text-xs text-success"><CheckCircle2 className="h-3.5 w-3.5" />Both measurements are within the configured limits.</p>}</div>
      {active && currentEvidence && latest.is_up && !latest.breaches.length && <p className="mt-1 text-xs text-muted">The latest measurements no longer exceed the limits. The reported status may be awaiting refresh.</p>}
    </div> : <p className="mt-2 text-xs text-muted">No recent ping sample is available from the owning monitor.</p>}
    {recent && <details className="mt-3" open={active || undefined}>
      <summary className="cursor-pointer text-xs font-semibold">Most recent degraded event · {new Date(recent.timestamp).toLocaleString()}{recent.recovered_at ? ' · Recovered' : ''}</summary>
      <div className="mt-2 space-y-2 border-l-2 border-warning/40 pl-3 text-xs">
        {recent.sample && recent.thresholds_unchanged && recent.sample.breaches.length ? <><Breaches sample={recent.sample} /><p className="text-muted">Recorded near the transition: latency {number(recent.sample.rtt_ms)} ms; packet loss {number(recent.sample.packet_loss_pct)}%.</p></> : <p>{recent.reason}</p>}
        {!recent.thresholds_unchanged && <p className="text-muted">Threshold settings have changed since this event. Current limits are not used to explain the historical trigger.</p>}
        {recent.recovered_at && <p className="text-success">Recovered {new Date(recent.recovered_at).toLocaleString()} · lasted {Math.round((Date.parse(recent.recovered_at)-Date.parse(recent.timestamp))/1000)} seconds.</p>}
      </div>
    </details>}
    <p className="mt-3 text-[11px] text-muted">Status uses individual checks, not the selected chart’s average. Settings refresh on the controller’s configuration cycle, then apply to subsequent checks. These rules govern ping reachability; CPU, memory and hardware alerts have their own conditions.</p>
  </section>
}
