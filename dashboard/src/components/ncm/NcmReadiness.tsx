import { useQuery } from '@tanstack/react-query'
import { ArrowRight, Clock, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Ago, SectionError, ToneIcon, useClockOffset } from './ui'
import { absoluteUtc, type Tone } from './model'

const CHECK_TONE: Record<string, Tone> = { pass: 'success', blocked: 'danger', pending: 'warning', info: 'neutral' }
const CHECK_TEXT: Record<string, string> = { pass: 'Ready', blocked: 'Action needed', pending: 'Not verified yet', info: 'For information' }
const ACTION_TEXT: Record<string, string> = { profile: 'Choose a profile', settings: 'Open settings', scope: 'Review scope' }

export type ReadinessAction = 'profile' | 'settings' | 'scope'

/**
 * Onboarding checklist. Required prerequisites are enforced by the server when a
 * backup is queued; the same result is shown here with a direct next step.
 */
export function NcmReadiness({ deviceId, onAction, compact = false }: { deviceId: string; onAction?: (a: ReadinessAction) => void; compact?: boolean }) {
  const { data, isPending, isError, refetch, isFetching } = useQuery<any>({
    queryKey: ['ncm', 'readiness', deviceId],
    queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-readiness`)).data,
    refetchInterval: 30000,
  })
  if (isError && !data) return <SectionError what="Backup readiness" onRetry={() => refetch()} />
  if (isPending) return <p className="text-sm text-muted">Checking saved prerequisites…</p>
  const required = data.checks.filter((c: any) => c.required)
  const verification = data.checks.filter((c: any) => !c.required)
  const next = data.checks.find((c: any) => c.required && c.status === 'blocked')
  const Row = ({ c }: { c: any }) => (
    <li className="flex min-w-0 gap-2 rounded-md border border-border p-2.5">
      <ToneIcon tone={CHECK_TONE[c.status] || 'warning'} className="mt-0.5 h-4 w-4" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center justify-between gap-x-2 text-sm font-medium">
          <span>{c.label}</span>
          <span className={cn('text-xs', c.status === 'pass' ? 'text-success' : c.status === 'blocked' ? 'text-danger' : c.status === 'info' ? 'text-muted' : 'text-warning')}>{CHECK_TEXT[c.status] || c.status}</span>
        </div>
        <p className="mt-0.5 text-xs text-muted">{c.detail}</p>
        {c.action && onAction && c.status !== 'pass' && (
          <Button size="sm" variant="link" className="h-auto px-0 py-0.5 text-xs" onClick={() => onAction(c.action)}>
            {ACTION_TEXT[c.action] || 'Fix'} <ArrowRight className="h-3 w-3" />
          </Button>
        )}
      </div>
    </li>
  )
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className={cn('text-sm font-medium', data.can_capture ? 'text-success' : 'text-danger')}>
          {data.can_capture
            ? 'Prerequisites are in place: a backup can be queued. The first completed capture verifies authentication and privileges.'
            : `Backups are blocked until ${data.blockers.length} prerequisite${data.blockers.length === 1 ? ' is' : 's are'} resolved${next ? ` — next: ${next.label}` : ''}.`}
        </p>
        <Button size="sm" variant="outline" onClick={() => refetch()} disabled={isFetching}><RefreshCw className={cn('h-3.5 w-3.5', isFetching && 'animate-spin')} /> Recheck</Button>
      </div>
      {isError && <p role="alert" className="text-xs text-warning">The last recheck failed; showing the previous result.</p>}
      <div>
        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted">Required before a backup</h3>
        <ul className={cn('grid gap-2', !compact && 'md:grid-cols-2')}>{required.map((c: any) => <Row key={c.id} c={c} />)}</ul>
      </div>
      {!compact && (
        <div>
          <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-muted">Verified by operation</h3>
          <ul className="grid gap-2 md:grid-cols-2">{verification.map((c: any) => <Row key={c.id} c={c} />)}</ul>
        </div>
      )}
      <p className="text-xs text-muted">{data.assessment} Checked <Ago iso={data.checked_at} />. Format validation does not certify a complete recovery backup.</p>
    </div>
  )
}

const RUNTIME_TONE = (v?: string): Tone => (v === 'active' || v === 'configured' || v === 'synchronized' ? 'success' : v === 'inactive' || v === 'missing' || v === 'unsynchronized' ? 'danger' : 'warning')

function formatOffset(ms: number) {
  const s = Math.abs(ms) / 1000
  if (s < 90) return `${Math.round(s)} seconds`
  if (s < 5400) return `${Math.round(s / 60)} minutes`
  return `${(s / 3600).toFixed(1)} hours`
}

/** Runtime health plus appliance time trust (B01). */
export function NcmHealth() {
  const offset = useClockOffset()
  const { data, isError, isPending, refetch } = useQuery<any>({ queryKey: ['ncm', 'health'], queryFn: async () => (await api.get('/ncm/health')).data, refetchInterval: 30000 })
  const skewed = Math.abs(offset) >= 120_000
  return (
    <div className="space-y-2">
      <div className="rounded-md border border-border px-3 py-2 text-xs" aria-label="NCCM runtime health">
        {isError && !data ? (
          <span role="alert" className="flex items-center gap-2 text-warning">Runtime health unavailable. <Button size="sm" variant="outline" onClick={() => refetch()}>Retry</Button></span>
        ) : isPending ? 'Checking runtime health…' : (
          <div className="flex flex-wrap gap-x-5 gap-y-1">
            {[['worker', 'Backup worker'], ['scheduler', 'Schedule timer'], ['delivery', 'Delivery timer'], ['encryption', 'Archive encryption'], ['time_sync', 'Appliance clock']].map(([key, label]) => (
              <span key={key} className="inline-flex items-center gap-1">
                <ToneIcon tone={RUNTIME_TONE(data[key])} />{label}: <strong className="font-medium">{data[key] === 'synchronized' ? 'NTP-synchronized' : data[key] === 'unsynchronized' ? 'not synchronized' : data[key] || 'unknown'}</strong>
              </span>
            ))}
          </div>
        )}
      </div>
      {data?.time_sync === 'unsynchronized' && (
        <div role="alert" className="flex gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
          <Clock className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <span>The appliance clock is not NTP-synchronized (appliance time now {absoluteUtc(data.server_time)}). Schedules, freshness and every recorded timestamp depend on it. Ask an administrator to repair time synchronization; recorded history is kept as captured, not rewritten.</span>
        </div>
      )}
      {skewed && (
        <div role="status" className="flex gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
          <Clock className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <span>Your browser clock is {formatOffset(offset)} {offset > 0 ? 'behind' : 'ahead of'} the appliance. Ages on this page are measured with the appliance clock; hover any time to see the exact appliance (UTC) and local time.</span>
        </div>
      )}
    </div>
  )
}

export function ReadinessCard({ deviceId, onAction }: { deviceId: string; onAction?: (a: ReadinessAction) => void }) {
  return <Card><CardContent className="space-y-3 pt-4"><h2 className="font-semibold">Backup readiness</h2><NcmReadiness deviceId={deviceId} onAction={onAction} /></CardContent></Card>
}
