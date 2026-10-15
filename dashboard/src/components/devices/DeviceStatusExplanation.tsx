import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, Hourglass, Info, Waves } from 'lucide-react'
import { api } from '@/lib/api'
import { serverNow } from '@/lib/serverClock'
import { relativeTime } from '@/lib/utils'
import {
  explanationView, latestVerdict, limitText,
  type ExplanationSample, type ExplanationTone, type StatusExplanation,
} from './deviceStatus'

const number = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 3 })

/** The explanation of one device's status; shared by the explanation itself and the device header (one request). */
export function useDeviceStatusExplanation(deviceId: string | undefined, enabled = true) {
  return useQuery<StatusExplanation>({
    queryKey: ['device-status-explanation', deviceId],
    queryFn: async () => (await api.get(`/devices/${deviceId}/status-explanation`)).data,
    refetchInterval: 15_000,
    enabled: !!deviceId && enabled,
  })
}

const FRAME: Record<ExplanationTone, string> = {
  danger: 'border-danger/40 bg-danger/5',
  warning: 'border-warning/40 bg-warning/5',
  info: 'border-border bg-surface',
}
const ICON_TONE: Record<ExplanationTone, string> = { danger: 'text-danger', warning: 'text-warning', info: 'text-info' }
const VERDICT_TONE = { success: 'text-success', warning: 'text-warning', muted: 'text-muted', danger: 'text-danger' } as const

function Breaches({ sample }: { sample: ExplanationSample }) {
  return <div className="space-y-1">{sample.breaches.map(b => <p key={b.metric} className="text-sm">
    <b>{b.metric}: {number(b.value)}{b.metric === 'Packet loss' ? '%' : ' ms'}</b>
    {' exceeds the Degraded limit of '}{number(b.threshold)}{b.metric === 'Packet loss' ? '%' : ' ms'}
    {' by '}<b>{number(b.exceeded_by)} {b.excess_unit}</b>.
  </p>)}</div>
}

/**
 * Why a device has its status: the confirmation rules of the real policy (owner-specific Down count), progress
 * towards a pending change, the flapping episode and the latest check against both limits. Every sentence is built
 * from the API's `policy` and `state` (components/devices/deviceStatus.ts), never from constants.
 */
export function DeviceStatusExplanation({ deviceId, compact = false }: { deviceId: string; compact?: boolean }) {
  const { data, isError } = useDeviceStatusExplanation(deviceId)
  if (isError) return <div className="rounded-lg border border-border p-3 text-sm text-muted">Status details are temporarily unavailable. The device status above remains the last confirmed status.</div>
  if (!data) return null
  if (data.mode !== 'ping') return <div className="flex gap-2 rounded-lg border border-border p-3 text-sm text-muted"><Info className="h-4 w-4 shrink-0" />{data.mode === 'managed' ? 'Status is reported by the managing controller; the global ping confirmation rules do not determine this device’s status.' : 'Ping monitoring is disabled or has no assigned owner. The global ping confirmation rules are not evaluated here.'}</div>
  const view = explanationView(data, serverNow())
  const latest = data.latest
  const recent = data.recent_degraded
  const HeadIcon = view.tone === 'info' ? Info : data.flapping?.active && data.status !== 'down' && data.status !== 'degraded' ? Waves : AlertTriangle
  if (compact) return <details className={`rounded-xl border bg-surface p-4 ${view.active ? (view.tone === 'danger' ? 'border-danger/40' : 'border-warning/40') : 'border-border'}`} open={view.active || undefined}>
    <summary className="cursor-pointer text-sm font-semibold">{view.active ? view.heading : 'Reachability details'}<span className="mt-1 block text-xs font-normal text-muted">{view.summary}</span></summary>
    <div className="mt-3"><DeviceStatusExplanation deviceId={deviceId} /></div>
  </details>
  const verdict = latest ? latestVerdict(latest, data) : null
  return <section aria-label="Device status explanation" className={`rounded-xl border p-4 ${FRAME[view.tone]}`}>
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="flex items-center gap-2 text-sm font-semibold"><HeadIcon className={`h-4 w-4 ${ICON_TONE[view.tone]}`} /> {view.heading}</h3>
      <span className="text-xs text-muted">{view.scopeNote} · <Link className="text-primary hover:underline" to="/settings/general?tab=monitoring">Monitoring settings</Link></span>
    </div>
    {view.statusLine && <p className="mt-1 text-xs text-muted">Confirmed status: <b className="text-text">{view.statusLine}</b></p>}
    {view.maintenance && <p className="mt-2 text-xs text-muted">{view.maintenance}</p>}
    {view.pending && <p className="mt-2 flex items-center gap-1.5 text-sm"><Hourglass className="h-3.5 w-3.5 shrink-0 text-warning" /><span><b>Pending:</b> {view.pending}.</span></p>}
    {view.stateNote && <p className="mt-2 text-xs text-muted">{view.stateNote}</p>}
    {view.flapping && <div className="mt-2 flex gap-2 rounded-lg border border-warning/30 bg-warning/5 p-2.5 text-xs">
      <Waves className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" /><p>{view.flapping}</p>
    </div>}
    <ul className="mt-3 list-disc space-y-1 pl-4 text-xs text-muted">
      {view.rules.map((rule) => <li key={rule}>{rule}</li>)}
    </ul>
    {view.flapLimit && <p className="mt-2 text-xs text-warning">{view.flapLimit}</p>}
    {latest ? <div className="mt-3 rounded-lg border border-border bg-surface2/30 p-3">
      <div className="mb-2 text-xs text-muted">Latest check · {data.source} · {relativeTime(latest.timestamp)} · {new Date(latest.timestamp).toLocaleString()}</div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
        <span>Latency <b>{latest.is_up ? `${number(latest.rtt_ms)} ms` : 'No response'}</b> <span className="text-xs text-muted">({limitText('latency', data)})</span></span>
        <span>Packet loss <b>{number(latest.packet_loss_pct)}%</b> <span className="text-xs text-muted">({limitText('loss', data)})</span></span>
      </div>
      <div className="mt-2">
        {verdict
          ? <p className={`flex items-center gap-1 text-xs ${VERDICT_TONE[verdict.tone]}`}>{verdict.tone === 'success' && <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />}{verdict.text}</p>
          : latest.breaches.length ? <Breaches sample={latest} /> : null}
      </div>
    </div> : <p className="mt-3 text-xs text-muted">No recent ping sample is available from the owning monitor.</p>}
    {recent && <details className="mt-3" open={data.status === 'degraded' || undefined}>
      <summary className="cursor-pointer text-xs font-semibold">Most recent degraded event · {new Date(recent.timestamp).toLocaleString()}{recent.recovered_at ? ' · Recovered' : ''}</summary>
      <div className="mt-2 space-y-2 border-l-2 border-warning/40 pl-3 text-xs">
        {recent.sample && recent.thresholds_unchanged && recent.sample.breaches.length ? <><Breaches sample={recent.sample} /><p className="text-muted">Check recorded near the transition: latency {number(recent.sample.rtt_ms)} ms; packet loss {number(recent.sample.packet_loss_pct)}%.</p></> : null}
        <p className={recent.sample && recent.thresholds_unchanged && recent.sample.breaches.length ? 'text-muted' : undefined}>{recent.reason}</p>
        {!recent.thresholds_unchanged && <p className="text-muted">Threshold settings have changed since this event. Current limits are not used to explain the historical change.</p>}
        {recent.recovered_at && <p className="text-success">Recovered {new Date(recent.recovered_at).toLocaleString()} · lasted {Math.round((Date.parse(recent.recovered_at)-Date.parse(recent.timestamp))/1000)} seconds.</p>}
      </div>
    </details>}
    <p className="mt-3 text-[11px] text-muted">The status follows these confirmation rules, not the selected chart’s average. Settings refresh on the controller’s configuration cycle (about a minute) and apply to later checks. These rules govern ping reachability; CPU, memory and hardware alerts have their own conditions.</p>
  </section>
}
