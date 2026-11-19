import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, Repeat, Wrench } from 'lucide-react'
import { api } from '@/lib/api'
import { regionalTimezone } from '@/lib/regionalTime'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { formatAbsolute } from '@/pages/device/format'
import {
  DAY_PRESETS, WEEKDAYS, defaultRecurrence, describeRecurrence, durationLabel, oneOffPayload, ownWindow,
  recurrenceError, recurringPayload, sameDays, toggleDay, viaLabel, windowTiming,
  type MaintenanceAsset, type MaintenanceWindow, type RecurrenceDraft,
} from './model'

const ONE_OFF_DURATIONS: Array<{ value: string; label: string }> = [
  { value: '1800', label: '30 minutes' },
  { value: '3600', label: '1 hour' },
  { value: '7200', label: '2 hours' },
  { value: '14400', label: '4 hours' },
  { value: '28800', label: '8 hours' },
  { value: '86400', label: '24 hours' },
  { value: 'custom', label: 'Custom end time' },
]

const RECURRING_DURATIONS = [30, 60, 120, 180, 240, 480, 720, 1440]

function todayISO(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** Maintenance windows for one device or server: schedule a one-off or a
 *  recurring window and see (or remove) the ones that cover it. */
export function MaintenanceDialog({
  open, onOpenChange, asset, windows, queryKeys,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  asset: MaintenanceAsset
  windows?: { active: MaintenanceWindow[]; upcoming: MaintenanceWindow[] }
  /** react-query keys to refresh after a change */
  queryKeys: unknown[][]
}) {
  const qc = useQueryClient()
  const orgTz = regionalTimezone()
  const [kind, setKind] = useState<'once' | 'recurring'>('once')
  const [startMode, setStartMode] = useState<'now' | 'later'>('now')
  const [startAt, setStartAt] = useState('')
  const [duration, setDuration] = useState('3600')
  const [endAt, setEndAt] = useState('')
  const [rec, setRec] = useState<RecurrenceDraft>(() => defaultRecurrence(orgTz))
  const [customMinutes, setCustomMinutes] = useState(false)
  const [reason, setReason] = useState('')

  const invalidate = () => { for (const key of queryKeys) qc.invalidateQueries({ queryKey: key }) }
  const noun = asset.kind === 'server' ? 'server' : 'device'

  const computeRange = (): { start: Date; end: Date } | null => {
    const start = startMode === 'now' ? new Date() : (startAt ? new Date(startAt) : null)
    if (!start || Number.isNaN(start.getTime())) return null
    const end = duration === 'custom'
      ? (endAt ? new Date(endAt) : null)
      : new Date(start.getTime() + Number(duration) * 1000)
    if (!end || Number.isNaN(end.getTime()) || end <= start) return null
    return { start, end }
  }
  const range = kind === 'once' ? computeRange() : null
  const recError = kind === 'recurring' ? recurrenceError(rec, todayISO()) : null
  const ready = kind === 'once' ? !!range : !recError

  const create = useMutation({
    mutationFn: async () => {
      if (kind === 'recurring') return api.post('/device-maintenance', recurringPayload(asset, rec, reason))
      const r = computeRange()
      if (!r) throw new Error('invalid range')
      return api.post('/device-maintenance', oneOffPayload(asset, r.start, r.end, reason))
    },
    onSuccess: () => {
      toast.success('Maintenance scheduled', `Alerting and SLA impact are paused for this ${noun} during the window.`)
      setReason('')
      invalidate()
    },
    onError: (e: unknown) => toast.error('Could not schedule maintenance', apiErrorMessage(e)),
  })

  const cancelWin = useMutation({
    mutationFn: async (mid: string) => api.delete(`/device-maintenance/${mid}`),
    onSuccess: () => { toast.success('Maintenance window removed'); invalidate() },
    onError: (e: unknown) => toast.error('Could not remove window', apiErrorMessage(e)),
  })

  const existing = [...(windows?.active || []), ...(windows?.upcoming || [])]
  const selectCls = 'h-9 w-full rounded-md border border-border bg-surface px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40'
  const label = 'mb-1 block text-xs font-medium text-muted'
  const setR = (patch: Partial<RecurrenceDraft>) => setRec((r) => ({ ...r, ...patch }))
  const durationChoice = customMinutes || !RECURRING_DURATIONS.includes(rec.durationMinutes) ? 'custom' : String(rec.durationMinutes)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Wrench className="h-4 w-4 text-primary" />Maintenance — {asset.name}</DialogTitle>
          <DialogDescription>
            {asset.kind === 'server'
              ? 'While a window is active the server keeps reporting and metrics are recorded, but its alert rules are not evaluated and nothing is sent. Alerts that are open stay open.'
              : 'While a window is active the device keeps being polled and metrics are recorded, but status changes, alerts and notifications are suppressed and the window is excluded from uptime, SLA and reports.'}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-1 rounded-md border border-border p-1" role="tablist" aria-label="Window kind">
            {(['once', 'recurring'] as const).map((k) => (
              <button key={k} type="button" role="tab" aria-selected={kind === k}
                className={`rounded px-2 py-1.5 text-sm ${kind === k ? 'bg-primary/10 font-medium text-primary' : 'text-muted hover:text-text'}`}
                onClick={() => setKind(k)}>
                {k === 'once' ? 'One-off' : 'Recurring'}
              </button>
            ))}
          </div>

          {kind === 'once' ? (
            <>
              <div className="grid grid-cols-2 gap-3">
                <label className="block"><span className={label}>Start</span>
                  <select className={selectCls} value={startMode} onChange={(e) => setStartMode(e.target.value as 'now' | 'later')}>
                    <option value="now">Now</option>
                    <option value="later">At a scheduled time</option>
                  </select>
                </label>
                <label className="block"><span className={label}>Duration</span>
                  <select className={selectCls} value={duration} onChange={(e) => setDuration(e.target.value)}>
                    {ONE_OFF_DURATIONS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
                  </select>
                </label>
              </div>
              {startMode === 'later' && (
                <label className="block"><span className={label}>Start time</span><Input type="datetime-local" value={startAt} onChange={(e) => setStartAt(e.target.value)} /></label>
              )}
              {duration === 'custom' && (
                <label className="block"><span className={label}>End time</span><Input type="datetime-local" value={endAt} onChange={(e) => setEndAt(e.target.value)} /></label>
              )}
            </>
          ) : (
            <>
              <div>
                <span className={label}>Days</span>
                <div className="flex flex-wrap gap-1">
                  {WEEKDAYS.map((d) => (
                    <button key={d.n} type="button" aria-pressed={rec.days.includes(d.n)}
                      className={`h-8 min-w-[2.75rem] rounded-md border px-2 text-xs ${rec.days.includes(d.n) ? 'border-primary bg-primary/10 font-medium text-primary' : 'border-border text-muted hover:text-text'}`}
                      onClick={() => setR({ days: toggleDay(rec.days, d.n) })}>
                      {d.short}
                    </button>
                  ))}
                </div>
                <div className="mt-1 flex flex-wrap gap-2">
                  {DAY_PRESETS.map((p) => (
                    <button key={p.key} type="button"
                      className={`text-xs ${sameDays(rec.days, p.days) ? 'font-medium text-primary' : 'text-muted hover:text-text'}`}
                      onClick={() => setR({ days: p.days })}>
                      {p.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <label className="block"><span className={label}>Start time (local)</span>
                  <Input type="time" value={rec.startTime} onChange={(e) => setR({ startTime: e.target.value.slice(0, 5) })} />
                </label>
                <label className="block"><span className={label}>Duration</span>
                  <select className={selectCls} value={durationChoice} onChange={(e) => {
                    if (e.target.value === 'custom') { setCustomMinutes(true); return }
                    setCustomMinutes(false)
                    setR({ durationMinutes: Number(e.target.value) })
                  }}>
                    {RECURRING_DURATIONS.map((m) => <option key={m} value={m}>{durationLabel(m)}</option>)}
                    <option value="custom">Custom (minutes)</option>
                  </select>
                </label>
              </div>
              {durationChoice === 'custom' && (
                <label className="block"><span className={label}>Duration in minutes (1 to 10080, 7 days)</span>
                  <Input type="number" min={1} max={10080} value={rec.durationMinutes}
                    onChange={(e) => setR({ durationMinutes: Math.trunc(Number(e.target.value)) })} />
                </label>
              )}
              <div className="grid grid-cols-2 gap-3">
                <label className="block"><span className={label}>Time zone</span>
                  <Input value={rec.timezone} placeholder={orgTz} onChange={(e) => setR({ timezone: e.target.value })} />
                </label>
                <label className="block"><span className={label}>Repeat until (optional)</span>
                  <Input type="date" value={rec.until} min={todayISO()} onChange={(e) => setR({ until: e.target.value })} />
                </label>
              </div>
            </>
          )}

          <label className="block"><span className={label}>Reason (shown in reports and the audit log)</span>
            <Textarea rows={2} placeholder="e.g. Nightly backup, change #1234" value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          {kind === 'once' && range && (
            <div className="rounded-md border border-border bg-surface2 px-3 py-2 text-sm text-muted">
              Window: <span className="text-text">{formatAbsolute(range.start)}</span> → <span className="text-text">{formatAbsolute(range.end)}</span>
            </div>
          )}
          {kind === 'recurring' && (
            <div className={`rounded-md border px-3 py-2 text-sm ${recError ? 'border-danger/40 text-danger' : 'border-border bg-surface2 text-muted'}`}>
              {recError ?? <><Repeat className="mr-1 inline h-3.5 w-3.5" aria-hidden="true" /><span className="text-text">{describeRecurrence(rec, orgTz)}</span>. A window that runs past midnight belongs to the day it starts.</>}
            </div>
          )}
          {existing.length > 0 && (
            <div>
              <div className={label}>Scheduled windows</div>
              <div className="space-y-1.5">
                {existing.map((w) => {
                  const t = windowTiming(w)
                  const via = viaLabel(w, asset)
                  return (
                    <div key={w.id} className="flex items-center gap-2 rounded-md border border-border bg-surface px-3 py-2 text-sm">
                      <span className={`h-2 w-2 shrink-0 rounded-full ${w.active ? 'bg-primary' : 'bg-muted'}`} aria-hidden="true" />
                      <div className="min-w-0 flex-1">
                        <span className="font-medium">{t.state}</span>{' '}
                        <span className="text-muted">{formatAbsolute(t.from)} → {formatAbsolute(t.to)}{via ? ` · ${via}` : ''}</span>
                        {w.recurring && w.schedule ? <div className="text-muted"><Repeat className="mr-1 inline h-3 w-3" aria-hidden="true" />{w.schedule}</div> : null}
                        {w.reason ? <div className="break-words text-muted">{w.reason}</div> : null}
                      </div>
                      {ownWindow(w, asset) && (
                        <Button variant="outline" size="sm" className="h-7 px-2 text-danger" disabled={cancelWin.isPending} onClick={() => cancelWin.mutate(w.id)}>
                          {w.recurring ? 'Remove' : w.active ? 'End now' : 'Remove'}
                        </Button>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>Close</Button>
          <Button size="sm" disabled={!ready || create.isPending} onClick={() => create.mutate()}>
            {create.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wrench className="h-3.5 w-3.5" />}Schedule maintenance
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
