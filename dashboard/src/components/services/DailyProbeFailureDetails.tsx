import { ServiceFailureReason } from './ServiceFailureReason'
import { buildProbeFailures, probeHistoryEmptyMessage } from './probeFailures'

/** Kept separate from loading so empty/error states and sample evidence can be regression tested. */
export function DailyProbeFailureDetails({ failures, loading, error, onRetry, downtimeSeconds, hasCoverage }: {
  failures?: ReturnType<typeof buildProbeFailures>
  loading: boolean
  error: boolean
  onRetry: () => void
  downtimeSeconds: number
  hasCoverage: boolean
}) {
  const count = failures?.reduce((sum, f) => sum + f.samples, 0) ?? 0
  const time = (t: number) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  const duration = (ms: number) => {
    const seconds = Math.round(ms / 1000)
    return seconds >= 3600 ? `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`
      : seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`
  }
  return (
    <div className="mt-3 border-t border-border/60 pt-2.5">
      <div className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-muted">
        Failures this day{!loading && !error && count > 0 && <span className="ml-1.5 font-mono text-danger">{count} failed {count === 1 ? 'check' : 'checks'}</span>}
      </div>
      <p className="mb-2 text-xs text-muted">Individual failures remain visible here. Only confirmed Down incidents reduce SLA; failures that recover before confirmation affect raw probe availability only.</p>
      {loading ? <p className="text-xs text-muted">Loading failed checks…</p>
        : error ? <div className="text-xs text-danger">Could not load failed checks. <button type="button" className="underline" onClick={onRetry}>Retry</button></div>
        : !failures?.length ? <p className="text-xs text-muted">{probeHistoryEmptyMessage(downtimeSeconds, hasCoverage)}</p>
        : <div className="space-y-2">
          {failures.map((f) => (
            <div key={f.start} className="rounded-md border border-danger/20 bg-danger/5 p-2 text-xs">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="font-semibold text-danger">{f.samples > 0 ? `${f.samples} failed ${f.samples === 1 ? 'check' : 'checks'}` : 'Failure carried into day'}</span>
                <span className="font-mono tabular-nums">{f.carriedIn ? '…' : ''}{time(f.start)} → {time(f.end)}</span>
                <span className="font-mono font-medium text-danger">{duration(f.end - f.start)} of failed-probe coverage</span>
              </div>
              <div className="mt-1 text-text2"><ServiceFailureReason reason={f.reason} failureStage={f.failureStage} /></div>
              {f.endKind !== 'recovered' && <p className="mt-1 text-2xs text-muted">{f.endKind === 'continued' ? 'Failed checks continue in the next period.' : f.endKind === 'gap'
                ? 'Coverage ended before a successful check was observed; the time shown is not a confirmed recovery.'
                : 'Coverage ends at the selected time boundary; recovery is not confirmed within this window.'}</p>}
            </div>
          ))}
        </div>}
    </div>
  )
}
