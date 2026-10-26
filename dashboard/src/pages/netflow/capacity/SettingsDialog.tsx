import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { SlidersHorizontal } from 'lucide-react'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { toast } from '@/components/ui/Toast'
import { cn } from '@/lib/utils'
import { formatDateTime } from '@/lib/netflow/format'
import { apiErrorMessage } from '@/lib/netflow/status'
import { displayTimeZone } from '@/lib/netflow/window'
import { WEEKDAYS, type CapacityMethod, type CapacitySettings } from './model'
import { selectCls } from './shared'

type Draft = Omit<CapacitySettings, 'stored' | 'updated_at' | 'updated_by'>

function numberField(v: string): number {
  return v.trim() === '' ? NaN : Number(v)
}

/**
 * The capacity methodology (CR-08): what is measured and how, always readable; editable with
 * the NetFlow manage permission. Saving is audited on the server (old and new values).
 */
export function SettingsDialog({
  open,
  onOpenChange,
  settings,
  method,
  canManage,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  settings: CapacitySettings
  method: CapacityMethod | null
  canManage: boolean
}) {
  const qc = useQueryClient()
  const [d, setD] = useState<Draft>(settings)
  const [text, setText] = useState({ warn: '', crit: '', cov: '', bcov: '' })
  useEffect(() => {
    if (!open) return
    setD(settings)
    setText({ warn: String(settings.warn_pct), crit: String(settings.crit_pct), cov: String(settings.min_coverage_pct), bcov: String(settings.billing_min_coverage_pct) })
  }, [open, settings])

  const warn = numberField(text.warn)
  const crit = numberField(text.crit)
  const cov = numberField(text.cov)
  const bcov = numberField(text.bcov)
  const errors: Record<string, string> = {}
  if (!(warn >= 1 && warn <= 100)) errors.warn = 'Between 1 and 100'
  if (!(crit >= 1 && crit <= 100)) errors.crit = 'Between 1 and 100'
  if (!errors.warn && !errors.crit && warn >= crit) errors.warn = 'Must be below critical'
  if (!(cov >= 50 && cov <= 100)) errors.cov = 'Between 50 and 100'
  if (!(bcov >= 50 && bcov <= 100)) errors.bcov = 'Between 50 and 100'
  if (d.business_hours.days.length === 0) errors.days = 'Pick at least one day'
  if (d.business_hours.start_hour === d.business_hours.end_hour % 24) errors.hours = 'Start and end must differ'

  const save = useMutation({
    mutationFn: async () =>
      api.put('/netflow/capacity/settings', {
        percentile: d.percentile,
        direction_mode: d.direction_mode,
        warn_pct: warn,
        crit_pct: crit,
        min_coverage_pct: cov,
        billing_min_coverage_pct: bcov,
        business_hours: d.business_hours,
      }),
    onSuccess: () => {
      toast.success('Capacity settings saved', 'The change is recorded in the audit log.')
      qc.invalidateQueries({ queryKey: ['netflow'] })
      onOpenChange(false)
    },
    onError: (e) => toast.error('Could not save the settings', apiErrorMessage(e)),
  })

  const bh = d.business_hours
  const ro = !canManage
  const field = (label: string, key: keyof typeof text, err?: string) => (
    <label className="space-y-1 text-xs font-medium text-text">
      {label}
      <Input inputMode="decimal" value={text[key]} disabled={ro} aria-invalid={!!err}
        onChange={(e) => setText((t) => ({ ...t, [key]: e.target.value }))} />
      {err && <span className="block text-xs font-normal text-danger-fg">{err}</span>}
    </label>
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <SlidersHorizontal className="h-4 w-4 text-primary" aria-hidden />
            Capacity methodology
          </DialogTitle>
          <DialogDescription>
            How utilization, percentiles and billing figures are calculated.
            {settings.updated_at && (
              <> Last changed {settings.updated_by ? `by ${settings.updated_by} ` : ''}{formatDateTime(settings.updated_at, displayTimeZone(), { zone: 'offset' })}.</>
            )}
          </DialogDescription>
        </DialogHeader>

        <ul className="space-y-1.5 rounded-md border border-border bg-surface2/40 p-3 text-xs leading-relaxed text-text">
          <li><strong>Samples:</strong> complete 5-minute slots of the period; partial slots at the edges are left out.</li>
          <li><strong>Idle vs gap:</strong> a slot is zero when the exporter delivered records in it; slots in which the exporter delivered nothing are collection gaps and are excluded, not counted as zero.</li>
          <li>
            <strong>Percentile:</strong>{' '}
            {method?.percentile_definition ??
              `Samples sorted ascending; the top ${100 - settings.percentile}% are discarded and the highest remaining sample is the ${settings.percentile}th percentile.`}{' '}
            Stated only with at least {method?.min_samples ?? Math.ceil(100 / (100 - settings.percentile))} samples and {settings.min_coverage_pct}% coverage, otherwise the peak is shown and labelled.
          </li>
          <li><strong>Low coverage:</strong> when coverage is too low for a percentile, the peak can still show a warning, critical or over-capacity state, but never calls an interface healthy (it reads &quot;Insufficient data&quot;).</li>
          <li><strong>Directions:</strong> in = received, out = transmitted, each against its own speed; the headline is the higher one. Combined in + out is throughput only.</li>
          <li><strong>Long flows:</strong> bytes are spread over each record&apos;s duration (capped at one hour).</li>
          <li><strong>Duplicate export:</strong> exporters that send both ingress- and egress-monitored records of the same packets count ingress records only (per exporter counting direction).</li>
          <li><strong>Speed:</strong> configured circuit speed, then NetFlow interface override, then SNMP ifSpeed (sFlow counter ifSpeed when not SNMP-polled); unknown speed means unknown utilization.</li>
        </ul>

        <fieldset disabled={ro} className="space-y-4">
          <legend className="sr-only">Settings</legend>
          <div className="grid grid-cols-1 gap-3 min-[520px]:grid-cols-2">
            <label className="space-y-1 text-xs font-medium text-text">
              Percentile
              <select className={cn(selectCls, 'w-full')} value={d.percentile} onChange={(e) => setD({ ...d, percentile: Number(e.target.value) })}>
                {[95, 90, 85, 80, 75].map((p) => <option key={p} value={p}>{p}th percentile</option>)}
              </select>
            </label>
            <label className="space-y-1 text-xs font-medium text-text">
              Billing direction mode
              <select className={cn(selectCls, 'w-full')} value={d.direction_mode}
                onChange={(e) => setD({ ...d, direction_mode: e.target.value as Draft['direction_mode'] })}>
                <option value="separate">Separate: bill the higher direction</option>
                <option value="merged">Merged: in + out per sample</option>
              </select>
            </label>
            {field('Warning at utilization (%)', 'warn', errors.warn)}
            {field('Critical at utilization (%)', 'crit', errors.crit)}
            {field('Minimum coverage to state a percentile (%)', 'cov', errors.cov)}
            {field('Minimum coverage for billing-grade (%)', 'bcov', errors.bcov)}
          </div>

          <div className="space-y-2 rounded-md border border-border p-3">
            <label className="flex items-center gap-2 text-xs font-medium text-text">
              <input type="checkbox" checked={bh.enabled} onChange={(e) => setD({ ...d, business_hours: { ...bh, enabled: e.target.checked } })} />
              Bill business hours only by default
            </label>
            <p className="text-xs text-text2">The Capacity page can also switch to these hours at any time.</p>
            <div className="grid grid-cols-2 gap-3 min-[520px]:grid-cols-3">
              <label className="space-y-1 text-xs font-medium text-text">
                From
                <select className={cn(selectCls, 'w-full')} value={bh.start_hour} onChange={(e) => setD({ ...d, business_hours: { ...bh, start_hour: Number(e.target.value) } })}>
                  {Array.from({ length: 24 }, (_, h) => <option key={h} value={h}>{String(h).padStart(2, '0')}:00</option>)}
                </select>
              </label>
              <label className="space-y-1 text-xs font-medium text-text">
                Until
                <select className={cn(selectCls, 'w-full')} value={bh.end_hour} onChange={(e) => setD({ ...d, business_hours: { ...bh, end_hour: Number(e.target.value) } })}>
                  {Array.from({ length: 24 }, (_, h) => h + 1).map((h) => <option key={h} value={h}>{String(h % 24).padStart(2, '0')}:00</option>)}
                </select>
              </label>
              <label className="col-span-2 space-y-1 text-xs font-medium text-text min-[520px]:col-span-1">
                Time zone
                <Input value={bh.tz} onChange={(e) => setD({ ...d, business_hours: { ...bh, tz: e.target.value } })} placeholder="Asia/Riyadh" />
              </label>
            </div>
            {errors.hours && <p className="text-xs text-danger-fg">{errors.hours}</p>}
            <fieldset>
              <legend className="mb-1 text-xs font-medium text-text">Days</legend>
              <div className="flex flex-wrap gap-2">
                {WEEKDAYS.map((w) => (
                  <label key={w.iso} className="inline-flex items-center gap-1 text-xs text-text">
                    <input
                      type="checkbox"
                      checked={bh.days.includes(w.iso)}
                      onChange={(e) =>
                        setD({ ...d, business_hours: { ...bh, days: e.target.checked ? [...bh.days, w.iso].sort() : bh.days.filter((x) => x !== w.iso) } })
                      }
                    />
                    {w.short}
                  </label>
                ))}
              </div>
              {errors.days && <p className="text-xs text-danger-fg">{errors.days}</p>}
            </fieldset>
          </div>
        </fieldset>
        {ro && <p role="status" className="text-xs text-text2">Changing the methodology needs the NetFlow manage permission.</p>}

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>{ro ? 'Close' : 'Cancel'}</Button>
          {!ro && (
            <Button size="sm" disabled={save.isPending || Object.keys(errors).length > 0} onClick={() => save.mutate()}>
              {save.isPending ? 'Saving…' : 'Save'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
