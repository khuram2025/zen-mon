/**
 * Maintenance for one monitor (B03, B04, B24).
 *
 * Newer appliances list every window that covers the check — its own,
 * its group's, a tag's, or an all-monitors window — and end a window by
 * recording its effective end (the record is kept for historical reports).
 * Older appliances only have the global list; the page then derives the
 * covering windows itself and ends a check-scoped window the legacy way.
 * Inherited windows are never offered an action that cannot end them.
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock, ExternalLink, Wrench } from 'lucide-react'
import { api } from '@/lib/api'
import { serverNow } from '@/lib/serverClock'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { knownUnsupported, markUnsupported, withCapability } from './capabilities'
import { browserTimezone, exactTime, formatDur, isUnsupported, utcOffsetLabel } from './format'
import type { MaintenanceWindow, ServiceCheckExt } from './types'

export function windowEnd(w: MaintenanceWindow): number {
  return Date.parse(w.effective_ends_at || w.ended_at || w.ends_at)
}

export function isActiveWindow(w: MaintenanceWindow, nowMs: number): boolean {
  if (w.state) return w.state === 'active'
  if (w.cancelled_at) return false
  const s = Date.parse(w.starts_at)
  return Number.isFinite(s) && s <= nowMs && windowEnd(w) > nowMs
}

function coversCheck(w: MaintenanceWindow, check: ServiceCheckExt): boolean {
  if (w.scope_type === 'all') return true
  if (w.scope_type === 'check') return w.scope_check_id === check.id
  if (w.scope_type === 'group') return !!check.group_id && w.scope_group_id === check.group_id
  if (w.scope_type === 'tag') return !!w.scope_tag && (check.tags || []).includes(w.scope_tag)
  return false
}

export function scopeText(w: MaintenanceWindow): string {
  if (w.scope_type === 'check') return 'This monitor'
  if (w.scope_type === 'all') return 'All monitors'
  if (w.scope_type === 'group') return `Via group ${w.scope_label || 'group'}`
  return `Via tag ${w.scope_label || w.scope_tag || ''}`.trim()
}

function inFuture(ms: number, nowMs: number): string {
  const s = (ms - nowMs) / 1000
  if (!Number.isFinite(s)) return ''
  if (s <= 0) return 'now'
  return `in ${formatDur(s)}`
}

/** Windows covering this check. `supported` = the per-check contract endpoint exists. */
export function useCheckMaintenance(check: ServiceCheckExt | undefined, range: { fromISO: string; toISO: string }) {
  const id = check?.id
  const v2 = useQuery<MaintenanceWindow[] | null>({
    queryKey: ['service-check-maintenance-v2', id],
    queryFn: async () => withCapability('check-maintenance', async () => {
      const r = (await api.get(`/service-checks/${id}/maintenance`)).data
      return (Array.isArray(r) ? r : r?.data || []) as MaintenanceWindow[]
    }),
    enabled: !!id,
    refetchInterval: 30_000,
    retry: false,
  })
  const supported = v2.data != null
  const legacy = useQuery<MaintenanceWindow[]>({
    queryKey: ['service-check-maintenance'],
    queryFn: async () => {
      const r = (await api.get('/service-check-maintenance')).data
      return (Array.isArray(r) ? r : r?.data || []) as MaintenanceWindow[]
    },
    enabled: !!id && v2.isFetched && v2.data === null,
    refetchInterval: 30_000,
  })
  const inRange = useQuery<MaintenanceWindow[] | null>({
    queryKey: ['service-check-maintenance-v2', id, range.fromISO, range.toISO],
    queryFn: async () => withCapability('check-maintenance', async () => {
      const r = (await api.get(`/service-checks/${id}/maintenance`, { params: { from: range.fromISO, to: range.toISO } })).data
      return (Array.isArray(r) ? r : r?.data || []) as MaintenanceWindow[]
    }),
    enabled: !!id && supported,
    retry: false,
    staleTime: 30_000,
  })

  const all: MaintenanceWindow[] = supported
    ? v2.data || []
    : check ? (legacy.data || []).filter((w) => coversCheck(w, check)).map((w) => ({ ...w, inherited: w.scope_type !== 'check' })) : []

  const fromTs = Date.parse(range.fromISO)
  const toTs = Date.parse(range.toISO)
  const rangeWindows = supported
    ? inRange.data || []
    : all.filter((w) => Date.parse(w.starts_at) < toTs && windowEnd(w) > fromTs)

  return {
    supported,
    loading: v2.isLoading || (!supported && legacy.isLoading),
    windows: all,
    rangeWindows,
    refetch: () => { void v2.refetch(); void legacy.refetch(); void inRange.refetch() },
  }
}

/** POST …/end on newer appliances; the legacy DELETE only for a check-scoped window. */
export function useEndMaintenance(checkId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ window: w, legacyAllowed }: { window: MaintenanceWindow; legacyAllowed: boolean }) => {
      if (!knownUnsupported('maintenance-end')) {
        try {
          return (await api.post(`/service-check-maintenance/${w.id}/end`, { reason: 'Ended from service detail' })).data
        } catch (e) {
          if (!isUnsupported(e)) throw e
          markUnsupported('maintenance-end')
        }
      }
      if (!legacyAllowed || w.scope_type !== 'check') {
        throw new Error('This appliance cannot end an inherited window from here. Use the Maintenance page.')
      }
      await api.delete(`/service-check-maintenance/${w.id}`)
      return null
    },
    onSuccess: () => {
      toast.success('Maintenance window ended')
      qc.invalidateQueries({ queryKey: ['service-check', checkId] })
      qc.invalidateQueries({ queryKey: ['service-check-maintenance-v2', checkId] })
      qc.invalidateQueries({ queryKey: ['service-check-maintenance'] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      qc.invalidateQueries({ queryKey: ['service-sla', checkId] })
    },
    onError: (e: any) => toast.error('Could not end maintenance', apiErrorMessage(e)),
  })
}

function useImpactCount(w: MaintenanceWindow | null) {
  return useQuery<number | null>({
    queryKey: ['maintenance-impact', w?.scope_type, w?.scope_group_id, w?.scope_tag],
    queryFn: async () => {
      if (!w) return null
      const params: Record<string, string | number> = { limit: 1 }
      if (w.scope_type === 'group' && w.scope_group_id) params.group_id = w.scope_group_id
      else if (w.scope_type === 'tag' && w.scope_tag) params.tag = w.scope_tag
      else if (w.scope_type !== 'all') return null
      const r = (await api.get('/service-checks', { params })).data
      return typeof r?.meta?.total === 'number' ? r.meta.total : null
    },
    enabled: !!w && w.scope_type !== 'check',
    staleTime: 30_000,
  })
}

/** One row per active window, with the action that window actually supports. */
export function MaintenanceWindowList({ windows, supported, nowMs, onEnd, ending }: {
  windows: MaintenanceWindow[]
  supported: boolean
  nowMs: number
  onEnd: (w: MaintenanceWindow) => void
  ending: boolean
}) {
  const [confirm, setConfirm] = useState<MaintenanceWindow | null>(null)
  const impact = useImpactCount(confirm)
  const impactText = confirm
    ? impact.isLoading ? 'Counting affected monitors…'
      : impact.data != null ? `This ends maintenance for ${impact.data} monitor${impact.data === 1 ? '' : 's'} you can see${confirm.scope_type === 'all' ? ' (every monitor)' : ''}, not only this one.`
        : 'This window covers other monitors as well; ending it affects all of them.'
    : ''

  if (windows.length === 0) {
    return (
      <p className="text-[12px] text-muted">
        The window covering this monitor could not be identified from here.{' '}
        <Link to="/services/maintenance" className="font-medium text-primary hover:underline">Open Maintenance</Link> to review it.
      </p>
    )
  }
  return (
    <div className="space-y-1.5">
      {windows.map((w) => {
        const inherited = w.scope_type !== 'check'
        return (
          <div key={w.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
            <span className="rounded border border-info/40 bg-info/10 px-1.5 py-px text-[10px] font-semibold uppercase tracking-wider text-info">
              {scopeText(w)}
            </span>
            <span className="tabular-nums">
              until {exactTime(windowEnd(w), false)} <span className="text-muted">({inFuture(windowEnd(w), nowMs)})</span>
            </span>
            {w.reason && <span className="min-w-0 truncate text-muted" title={w.reason}>{w.reason}</span>}
            <span className="ml-auto flex items-center gap-2">
              {!inherited ? (
                <Button size="sm" className="h-7" disabled={ending} onClick={() => onEnd(w)}>
                  {ending ? 'Ending…' : 'End now'}
                </Button>
              ) : supported ? (
                <Button size="sm" variant="outline" className="h-7" disabled={ending} onClick={() => setConfirm(w)}>
                  End for all covered monitors…
                </Button>
              ) : null}
              {inherited && (
                <Link to="/services/maintenance" className="inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline">
                  Managed in Maintenance <ExternalLink className="h-3 w-3" />
                </Link>
              )}
            </span>
          </div>
        )
      })}
      <ConfirmDialog
        open={!!confirm}
        onOpenChange={(o) => { if (!o) setConfirm(null) }}
        title={confirm ? `End ${scopeText(confirm).replace(/^Via /, '')} maintenance?` : 'End maintenance?'}
        description={`${impactText} Alerts resume immediately for every covered monitor. The window's record is kept for SLA reporting.`}
        confirmText="End window"
        destructive
        loading={ending}
        onConfirm={() => { if (confirm) onEnd(confirm); setConfirm(null) }}
      />
    </div>
  )
}

export function MaintenanceBanner({ windows, supported, slaExcludesMaintenance, nowMs, onEnd, ending }: {
  windows: MaintenanceWindow[]
  supported: boolean
  slaExcludesMaintenance: boolean
  nowMs: number
  onEnd: (w: MaintenanceWindow) => void
  ending: boolean
}) {
  return (
    <div role="status" className="rounded-lg border border-info/40 bg-info/10 px-3 py-2.5">
      <div className="flex items-start gap-3">
        <Wrench className="mt-0.5 h-4 w-4 shrink-0 text-info" />
        <div className="min-w-0 flex-1 space-y-2">
          <div>
            <span className="text-xs font-semibold uppercase tracking-wider text-info">In maintenance</span>
            <p className="mt-0.5 text-[12.5px] leading-relaxed">
              Probes still run. Alerts are suppressed.
              {slaExcludesMaintenance ? ' Maintenance time is excluded from SLA and availability figures.' : ''}
            </p>
          </div>
          <MaintenanceWindowList windows={windows} supported={supported} nowMs={nowMs} onEnd={onEnd} ending={ending} />
        </div>
      </div>
    </div>
  )
}

const toLocalInput = (ms: number) => {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** Start a check-scoped window. Presets use a server-side start where supported. */
export function useStartMaintenance(checkId: string, onDone: () => void) {
  const qc = useQueryClient()
  const done = (title: string, detail?: string) => {
    toast.success(title, detail)
    onDone()
    qc.invalidateQueries({ queryKey: ['service-check', checkId] })
    qc.invalidateQueries({ queryKey: ['service-check-maintenance-v2', checkId] })
    qc.invalidateQueries({ queryKey: ['service-check-maintenance'] })
    qc.invalidateQueries({ queryKey: ['service-checks'] })
  }
  const preset = useMutation({
    mutationFn: async (minutes: number) => {
      const reason = `Manual ${minutes >= 60 ? `${minutes / 60}h` : `${minutes} min`} window from service detail`
      try {
        return (await api.post('/service-check-maintenance', { scope_type: 'check', scope_check_id: checkId, duration_minutes: minutes, reason })).data
      } catch (e: any) {
        // Older appliances require explicit bounds; use the appliance clock for them.
        if (e?.response?.status !== 422) throw e
        const start = serverNow()
        return (await api.post('/service-check-maintenance', {
          scope_type: 'check', scope_check_id: checkId,
          starts_at: new Date(start).toISOString(), ends_at: new Date(start + minutes * 60_000).toISOString(), reason,
        })).data
      }
    },
    onSuccess: () => done('Maintenance window started'),
    onError: (e: any) => toast.error('Failed to start maintenance', apiErrorMessage(e)),
  })
  const custom = useMutation({
    mutationFn: async (args: { startsAtISO: string; endsAtISO: string }) =>
      (await api.post('/service-check-maintenance', {
        scope_type: 'check', scope_check_id: checkId, starts_at: args.startsAtISO, ends_at: args.endsAtISO,
        reason: 'Custom maintenance window from service detail',
      })).data,
    onSuccess: (_d, args) => {
      const start = Date.parse(args.startsAtISO)
      done(start > serverNow() + 60_000 ? 'Maintenance window scheduled' : 'Maintenance window started',
        `${exactTime(start, false)} → ${exactTime(args.endsAtISO, false)}`)
    },
    onError: (e: any) => toast.error('Failed to schedule maintenance', apiErrorMessage(e)),
  })
  return { preset, custom }
}

export function MaintenanceDialog({
  open, onOpenChange, activeWindows, supported, nowMs, onStart, onStartCustom, onEnd, starting, ending,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  activeWindows: MaintenanceWindow[]
  supported: boolean
  nowMs: number
  onStart: (minutes: number) => void
  onStartCustom: (startsAtISO: string, endsAtISO: string) => void
  onEnd: (w: MaintenanceWindow) => void
  starting: boolean
  ending: boolean
}) {
  const [mode, setMode] = useState<'preset' | 'custom'>('preset')
  const [startsAt, setStartsAt] = useState(() => toLocalInput(serverNow()))
  const [endsAt, setEndsAt] = useState(() => toLocalInput(serverNow() + 3_600_000))
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setMode('preset')
      setError(null)
      setStartsAt(toLocalInput(serverNow()))
      setEndsAt(toLocalInput(serverNow() + 3_600_000))
    }
  }, [open])

  const presets = [
    { label: '15 min', minutes: 15 },
    { label: '1 hour', minutes: 60 },
    { label: '4 hours', minutes: 240 },
    { label: '24 hours', minutes: 1440 },
  ]

  const submitCustom = () => {
    setError(null)
    if (!startsAt || !endsAt) { setError('Enter both a start and an end time.'); return }
    const s = new Date(startsAt).getTime()
    const e = new Date(endsAt).getTime()
    if (!Number.isFinite(s) || !Number.isFinite(e)) { setError('Enter valid start and end times.'); return }
    if (e <= s) { setError('The end time must be after the start time.'); return }
    if (e <= serverNow()) { setError('The window would already be over by the appliance clock.'); return }
    onStartCustom(new Date(s).toISOString(), new Date(e).toISOString())
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Wrench className="h-4 w-4 text-primary" /> Maintenance for this monitor
          </DialogTitle>
          <DialogDescription className="text-xs">
            Maintenance suppresses alerts for this monitor. Probes keep running and their results stay in diagnostics.
          </DialogDescription>
        </DialogHeader>
        {activeWindows.length > 0 && (
          <div className="rounded-md border border-info/30 bg-info/5 p-2.5">
            <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-info">Active now</div>
            <MaintenanceWindowList windows={activeWindows} supported={supported} nowMs={nowMs} onEnd={onEnd} ending={ending} />
          </div>
        )}
        <div className="flex gap-0.5 rounded-md bg-surface2 p-0.5" role="group" aria-label="Window type">
          {(['preset', 'custom'] as const).map((m) => (
            <button
              key={m}
              type="button"
              aria-pressed={mode === m}
              onClick={() => setMode(m)}
              className={cn('flex-1 rounded px-2 py-1 text-[11px] font-medium', mode === m ? 'bg-surface text-text' : 'text-muted')}
            >
              {m === 'preset' ? 'Start now' : 'Schedule'}
            </button>
          ))}
        </div>
        {mode === 'preset' ? (
          <div className="grid grid-cols-2 gap-2">
            {presets.map((p) => (
              <Button key={p.minutes} variant="outline" size="sm" disabled={starting} onClick={() => onStart(p.minutes)}>
                <Clock className="h-3 w-3" />{p.label}
              </Button>
            ))}
          </div>
        ) : (
          <div className="space-y-3">
            <div>
              <label htmlFor="maint-start" className="mb-1 block text-[10px] uppercase tracking-wider text-muted">Starts at</label>
              <input id="maint-start" type="datetime-local" value={startsAt} onChange={(e) => setStartsAt(e.target.value)}
                aria-invalid={!!error} aria-describedby={error ? 'maint-error' : 'maint-tz'}
                className="w-full rounded-md border border-border bg-surface2 px-2 py-1.5 text-xs" />
            </div>
            <div>
              <label htmlFor="maint-end" className="mb-1 block text-[10px] uppercase tracking-wider text-muted">Ends at</label>
              <input id="maint-end" type="datetime-local" value={endsAt} onChange={(e) => setEndsAt(e.target.value)}
                aria-invalid={!!error} aria-describedby={error ? 'maint-error' : 'maint-tz'}
                className="w-full rounded-md border border-border bg-surface2 px-2 py-1.5 text-xs" />
            </div>
            <p id="maint-tz" className="text-[10px] text-muted">Times are in {browserTimezone()} ({utcOffsetLabel()}).</p>
            {error && <div id="maint-error" role="alert" className="rounded-md border border-danger/40 bg-danger/10 px-2 py-1.5 text-[11px] text-danger">{error}</div>}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={starting}>Close</Button>
          {mode === 'custom' && (
            <Button size="sm" onClick={submitCustom} disabled={starting}>{starting ? 'Scheduling…' : 'Schedule'}</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
