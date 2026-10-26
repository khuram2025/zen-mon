/**
 * Bulk NCCM operations. Every dialog lists its real targets (including devices
 * hidden by filters), changes only the fields the operator ticks, previews the
 * per-device result before applying, and reports per-device outcomes.
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { DownloadCloud, Eye, KeyRound, Save, Trash2, X } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'
import { Ago, StateBadge, ToneIcon } from './ui'
import { HELP, TIMEZONES, WEEKDAYS, errorDetail, ncmError, scheduleSummary, absoluteUtc, type Tone } from './model'

// --------------------------------------------------------------------------- //
// Shared pieces
// --------------------------------------------------------------------------- //
export function DayPicker({ value, onChange, disabled }: { value: number[]; onChange: (v: number[]) => void; disabled?: boolean }) {
  return (
    <div role="group" aria-label="Scheduled days" className="flex flex-wrap gap-1">
      {WEEKDAYS.map(([label, d]) => {
        const on = value.includes(d)
        return (
          <button key={d} type="button" disabled={disabled} aria-pressed={on} aria-label={label}
            onClick={() => onChange(on ? value.filter((x) => x !== d) : [...value, d].sort())}
            className={cn('min-h-8 min-w-10 rounded px-2 py-1 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
              on ? 'bg-primary text-white' : 'border border-border text-muted hover:text-text', disabled && 'opacity-50')}>
            {label}
          </button>
        )
      })}
    </div>
  )
}

export function TimezoneSelect({ value, onChange, disabled, label = 'Timezone' }: { value: string; onChange: (v: string) => void; disabled?: boolean; label?: string }) {
  const [filter, setFilter] = useState('')
  const zones = useMemo(() => {
    const q = filter.trim().toLowerCase()
    const list = q ? TIMEZONES.filter((z) => z.toLowerCase().includes(q)) : TIMEZONES
    return list.includes(value) ? list.slice(0, 200) : [value, ...list.slice(0, 199)]
  }, [filter, value])
  return (
    <div className="flex gap-2">
      <Input aria-label={`Filter ${label.toLowerCase()} list`} placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} className="w-28" disabled={disabled} />
      <Select value={value} onValueChange={onChange} disabled={disabled}>
        <SelectTrigger aria-label={label} className="flex-1"><SelectValue /></SelectTrigger>
        <SelectContent>{zones.map((z) => <SelectItem key={z} value={z}>{z}</SelectItem>)}</SelectContent>
      </Select>
    </div>
  )
}

const STATUS_TONE: Record<string, Tone> = {
  queued: 'info', already_active: 'info', blocked: 'danger', not_enrolled: 'warning', not_found: 'warning', failed: 'danger',
  will_update: 'info', updated: 'success', unchanged: 'neutral', skipped: 'warning', invalid: 'danger', enrolled: 'success',
}
const STATUS_TEXT: Record<string, string> = {
  queued: 'Queued', already_active: 'Already running or queued', blocked: 'Blocked', not_enrolled: 'Not enrolled',
  not_found: 'Not found / out of scope', failed: 'Failed', will_update: 'Will change', updated: 'Updated', unchanged: 'No change',
  skipped: 'Skipped', invalid: 'Invalid', enrolled: 'Enrolled',
}

function OutcomeBadge({ status }: { status: string }) {
  const tone = STATUS_TONE[status] || 'neutral'
  return (
    <span className={cn('inline-flex items-center gap-1 whitespace-nowrap text-xs font-medium', tone === 'neutral' ? 'text-muted' : `text-${tone}`)}>
      <ToneIcon tone={tone} />{STATUS_TEXT[status] || status}
    </span>
  )
}

function TargetList({ devices, hiddenIds }: { devices: any[]; hiddenIds?: Set<string> }) {
  return (
    <details className="rounded-md border border-border text-xs">
      <summary className="cursor-pointer px-3 py-2 font-medium">
        {devices.length} target device{devices.length === 1 ? '' : 's'}
        {hiddenIds && hiddenIds.size > 0 && <span className="text-warning"> · {hiddenIds.size} hidden by current filters or pages</span>}
      </summary>
      <ul className="max-h-40 overflow-auto border-t border-border">
        {devices.map((d) => (
          <li key={d.device_id} className="flex items-center justify-between gap-2 px-3 py-1.5">
            <span className="min-w-0 truncate">{d.hostname} <span className="font-mono text-muted">{d.ip}</span>{hiddenIds?.has(d.device_id) && <Badge variant="outline" className="ml-1">hidden</Badge>}</span>
            <StateBadge device={d} />
          </li>
        ))}
      </ul>
    </details>
  )
}

function fmtValue(field: string, v: any): string {
  if (v === null || v === undefined || v === '') return field === 'eligible_override' ? 'Automatic' : '—'
  if (typeof v === 'boolean') return field === 'eligible_override' ? (v ? 'Included' : 'Excluded') : v ? 'On' : 'Off'
  if (Array.isArray(v)) return field === 'schedule_days' ? v.map((d) => WEEKDAYS[d][0]).join(', ') || '—' : field === 'notify_channels' ? `${v.length} destination(s)` : v.join(', ')
  return String(v)
}

const FIELD_LABEL: Record<string, string> = {
  enabled: 'Collection', schedule_enabled: 'Scheduled', schedule_type: 'Frequency', schedule_interval_hours: 'Every (h)',
  schedule_time: 'Time', schedule_days: 'Days', schedule_timezone: 'Timezone', keep_versions: 'Keep versions',
  alert_on_change: 'Change alerts', notify_channels: 'Destinations', freshness_hours: 'Freshness (h)',
  eligible_override: 'Coverage scope', config_types: 'Capture types', event_backup: 'Event backups',
}

/** Distinct current values across the selection ("Mixed" when they differ). */
function current(devices: any[], pick: (d: any) => any): string {
  const values = [...new Set(devices.map((d) => JSON.stringify(pick(d) ?? null)))]
  if (!devices.length) return '—'
  if (values.length > 1) return `Mixed (${values.length} values)`
  const v = JSON.parse(values[0])
  return typeof v === 'boolean' ? (v ? 'On' : 'Off') : v === null ? '—' : Array.isArray(v) ? v.join(', ') || '—' : String(v)
}

function FieldToggle({ id, label, checked, onChange, currentValue, children }: {
  id: string; label: string; checked: boolean; onChange: (v: boolean) => void; currentValue: string; children: ReactNode
}) {
  return (
    <fieldset className={cn('rounded-md border p-3', checked ? 'border-primary/50 bg-primary/5' : 'border-border')}>
      <legend className="sr-only">{label}</legend>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label htmlFor={id} className="flex cursor-pointer items-center gap-2 text-sm font-medium">
          <input id={id} type="checkbox" aria-label={`Change ${label.toLowerCase()}`} className="h-4 w-4 accent-primary" checked={checked} onChange={(e) => onChange(e.target.checked)} />
          {label}{!checked && <span className="text-xs font-normal text-muted">— unchanged</span>}
        </label>
        <span className="text-xs text-muted">Current: {currentValue}</span>
      </div>
      {checked && <div className="mt-3 space-y-2">{children}</div>}
    </fieldset>
  )
}

// --------------------------------------------------------------------------- //
// Bulk settings with preview (B03, B07, B09)
// --------------------------------------------------------------------------- //
/** Starting values for fields the operator may tick (seeded from the first target). */
function defaults(s: any) {
  return {
    enabled: true, schedule_enabled: s.schedule_enabled ?? true, schedule_type: s.schedule_type || 'interval',
    schedule_interval_hours: s.schedule_interval_hours || 24, schedule_time: s.schedule_time || '02:00',
    schedule_days: s.schedule_days?.length ? s.schedule_days : [1, 2, 3, 4, 5], tz_mode: 'preserve', schedule_timezone: 'UTC',
    keep_versions: s.keep_versions || 5, freshness_hours: s.freshness_hours || 24, alert_on_change: true,
    config_types: ['running'], event_backup: true, eligibility: 'auto', notify_channels: [] as string[],
  }
}
export function BulkSettingsDialog({ open, onOpenChange, devices, hiddenIds, onDone }: {
  open: boolean; onOpenChange: (o: boolean) => void; devices: any[]; hiddenIds?: Set<string>; onDone: () => void
}) {
  const enrolled = devices.filter((d) => d.enrolled)
  const [on, setOn] = useState<Record<string, boolean>>({})
  const [v, setV] = useState<any>(() => defaults({}))
  const [preview, setPreview] = useState<any>(null)
  const [applied, setApplied] = useState<any>(null)
  const channels = useQuery<any>({ queryKey: ['ncm', 'notification-channels'], queryFn: async () => (await api.get('/ncm/notification-channels')).data, enabled: open })

  useEffect(() => {
    if (!open) return
    setOn({}); setPreview(null); setApplied(null)
    setV(defaults(enrolled[0] || {}))
    // Seed only when the dialog opens, not on background refreshes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
  useEffect(() => { setPreview(null) }, [on, v])

  const set = (patch: any) => setV((x: any) => ({ ...x, ...patch }))
  const toggle = (k: string) => (b: boolean) => setOn((x) => ({ ...x, [k]: b }))
  const errors: string[] = []
  if (on.schedule && v.schedule_enabled && v.schedule_type !== 'interval' && !/^([01]\d|2[0-3]):[0-5]\d$/.test(v.schedule_time || '')) errors.push('Enter a valid 24-hour time.')
  if (on.schedule && v.schedule_enabled && v.schedule_type === 'weekly' && !v.schedule_days.length) errors.push('Pick at least one day.')
  if (on.schedule && v.schedule_type === 'interval' && !(v.schedule_interval_hours >= 1 && v.schedule_interval_hours <= 720)) errors.push('Interval must be 1–720 hours.')
  if (on.retention && !(v.keep_versions >= 1 && v.keep_versions <= 100)) errors.push('Keep versions must be 1–100.')
  if (on.freshness && !(v.freshness_hours >= 1 && v.freshness_hours <= 720)) errors.push('Freshness must be 1–720 hours.')
  if (on.types && !v.config_types.length) errors.push('Select at least one capture type.')

  const body = (dry: boolean) => {
    const b: any = { device_ids: devices.map((d) => d.device_id), dry_run: dry }
    if (on.enabled) b.enabled = v.enabled
    if (on.schedule) {
      b.schedule_enabled = v.schedule_enabled
      if (v.schedule_enabled) {
        b.schedule_type = v.schedule_type
        if (v.schedule_type === 'interval') b.schedule_interval_hours = v.schedule_interval_hours
        else b.schedule_time = v.schedule_time
        if (v.schedule_type === 'weekly') b.schedule_days = v.schedule_days
      }
    }
    if (on.schedule && v.tz_mode === 'override') b.schedule_timezone = v.schedule_timezone
    if (on.retention) b.keep_versions = v.keep_versions
    if (on.freshness) b.freshness_hours = v.freshness_hours
    if (on.alerts) b.alert_on_change = v.alert_on_change
    if (on.types) b.config_types = v.config_types
    if (on.events) b.event_backup = v.event_backup
    if (on.scope) b.eligibility = v.eligibility
    if (on.channels) b.notify_channels = v.notify_channels
    return b
  }
  const anyField = Object.values(on).some(Boolean)
  const doPreview = useMutation({
    mutationFn: async () => (await api.post('/ncm/bulk-settings', body(true))).data,
    onSuccess: setPreview,
    onError: (e: any) => toast.error('Preview failed', ncmError(e)),
  })
  const apply = useMutation({
    mutationFn: async () => (await api.post('/ncm/bulk-settings', body(false))).data,
    onSuccess: (r: any) => {
      setApplied(r)
      toast.success(`Updated ${r.updated} device${r.updated === 1 ? '' : 's'}`, [r.unchanged && `${r.unchanged} unchanged`, r.skipped && `${r.skipped} skipped (not enrolled)`].filter(Boolean).join(', ') || undefined)
      onDone()
    },
    onError: (e: any) => {
      const d = errorDetail(e)
      if (d?.results) setPreview({ ...d, dry_run: true })
      toast.error('Nothing was changed', ncmError(e))
    },
  })
  const result = applied || preview

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Bulk backup settings</DialogTitle>
          <DialogDescription>Tick only the settings you want to change. Everything else — including each device's collection state and timezone — stays as it is. Preview shows the exact effect per device before anything is saved.</DialogDescription>
        </DialogHeader>
        <TargetList devices={devices} hiddenIds={hiddenIds} />
        {devices.length > enrolled.length && <p className="text-xs text-warning">{devices.length - enrolled.length} selected device(s) are not enrolled and will be skipped.</p>}
        <div className="space-y-2">
          <FieldToggle id="bulk-enabled" label="Collection state" checked={!!on.enabled} onChange={toggle('enabled')} currentValue={current(enrolled, (d) => d.ncm_enabled)}>
            <div className="flex items-center gap-2 text-sm"><Switch aria-label="Collection enabled" checked={v.enabled} onCheckedChange={(x) => set({ enabled: x })} />{v.enabled ? 'Enable collection on every target' : 'Disable collection on every target'}</div>
          </FieldToggle>
          <FieldToggle id="bulk-schedule" label="Schedule" checked={!!on.schedule} onChange={toggle('schedule')} currentValue={current(enrolled, scheduleSummary)}>
            <div className="flex items-center gap-2 text-sm"><Switch aria-label="Scheduled backup" checked={v.schedule_enabled} onCheckedChange={(x) => set({ schedule_enabled: x })} />Scheduled backup {v.schedule_enabled ? 'on' : 'off'}</div>
            {v.schedule_enabled && (
              <div className="grid gap-2 sm:grid-cols-2">
                <FormField label="Frequency">
                  <Select value={v.schedule_type} onValueChange={(x) => set({ schedule_type: x })}>
                    <SelectTrigger aria-label="Frequency"><SelectValue /></SelectTrigger>
                    <SelectContent><SelectItem value="interval">Every N hours</SelectItem><SelectItem value="daily">Daily at a time</SelectItem><SelectItem value="weekly">Weekly on days</SelectItem></SelectContent>
                  </Select>
                </FormField>
                {v.schedule_type === 'interval'
                  ? <FormField label="Every (hours)"><Input type="number" min={1} max={720} value={v.schedule_interval_hours} onChange={(e) => set({ schedule_interval_hours: Number(e.target.value) })} /></FormField>
                  : <FormField label="At (24-hour, device timezone)"><Input type="time" value={v.schedule_time} onChange={(e) => set({ schedule_time: e.target.value })} /></FormField>}
                {v.schedule_type === 'weekly' && <div className="sm:col-span-2"><DayPicker value={v.schedule_days} onChange={(d) => set({ schedule_days: d })} /></div>}
              </div>
            )}
            <div className="space-y-2 rounded-md border border-border p-2">
              <div role="radiogroup" aria-label="Timezone handling" className="flex flex-wrap gap-3 text-sm">
                <label className="flex items-center gap-1.5"><input type="radio" name="tz-mode" checked={v.tz_mode === 'preserve'} onChange={() => set({ tz_mode: 'preserve' })} /> Keep each device's timezone ({current(enrolled, (d) => d.schedule_timezone)})</label>
                <label className="flex items-center gap-1.5"><input type="radio" name="tz-mode" checked={v.tz_mode === 'override'} onChange={() => set({ tz_mode: 'override' })} /> Use one timezone for all</label>
              </div>
              {v.tz_mode === 'override' && <TimezoneSelect value={v.schedule_timezone} onChange={(z) => set({ schedule_timezone: z })} />}
              <p className="text-xs text-muted">{HELP.timezone} {HELP.scheduler}</p>
            </div>
          </FieldToggle>
          <FieldToggle id="bulk-retention" label="Retention" checked={!!on.retention} onChange={toggle('retention')} currentValue={current(enrolled, (d) => d.keep_versions)}>
            <FormField label="Keep changed versions" hint={HELP.retention}><Input type="number" min={1} max={100} value={v.keep_versions} onChange={(e) => set({ keep_versions: Number(e.target.value) })} /></FormField>
          </FieldToggle>
          <FieldToggle id="bulk-freshness" label="Freshness limit" checked={!!on.freshness} onChange={toggle('freshness')} currentValue={current(enrolled, (d) => d.freshness_hours)}>
            <FormField label="Freshness limit (hours)" hint={HELP.freshness}><Input type="number" min={1} max={720} value={v.freshness_hours} onChange={(e) => set({ freshness_hours: Number(e.target.value) })} /></FormField>
          </FieldToggle>
          <FieldToggle id="bulk-types" label="Capture types" checked={!!on.types} onChange={toggle('types')} currentValue={current(enrolled, (d) => d.config_types)}>
            <div className="flex gap-4 text-sm">{['running', 'startup'].map((t) => (
              <label key={t} className="flex items-center gap-1.5"><input type="checkbox" className="h-4 w-4 accent-primary" checked={v.config_types.includes(t)} onChange={(e) => set({ config_types: e.target.checked ? [...v.config_types, t] : v.config_types.filter((x: string) => x !== t) })} />{t === 'running' ? 'Running configuration' : 'Startup configuration'}</label>
            ))}</div>
            <p className="text-xs text-muted">Startup capture is rejected per device when its driver cannot collect it (FortiOS, PAN-OS, Junos); the preview shows which.</p>
          </FieldToggle>
          <FieldToggle id="bulk-alerts" label="Change alerts" checked={!!on.alerts} onChange={toggle('alerts')} currentValue={current(enrolled, (d) => d.alert_on_change)}>
            <div className="flex items-center gap-2 text-sm"><Switch aria-label="Alert on configuration change" checked={v.alert_on_change} onCheckedChange={(x) => set({ alert_on_change: x })} />Alert when a configuration changes</div>
          </FieldToggle>
          <FieldToggle id="bulk-events" label="Event-triggered backups" checked={!!on.events} onChange={toggle('events')} currentValue={current(enrolled, (d) => d.event_backup)}>
            <div className="flex items-center gap-2 text-sm"><Switch aria-label="Back up after configuration-change events" checked={v.event_backup} onCheckedChange={(x) => set({ event_backup: x })} />Back up after syslog/trap configuration-change events</div>
          </FieldToggle>
          <FieldToggle id="bulk-scope" label="Coverage scope" checked={!!on.scope} onChange={toggle('scope')} currentValue={current(enrolled, (d) => d.eligible_override === null ? 'Automatic' : d.eligible_override ? 'Included' : 'Excluded')}>
            <Select value={v.eligibility} onValueChange={(x) => set({ eligibility: x })}>
              <SelectTrigger aria-label="Coverage scope"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="auto">Automatic (network device type and address)</SelectItem><SelectItem value="include">Include in coverage</SelectItem><SelectItem value="exclude">Exclude from coverage</SelectItem></SelectContent>
            </Select>
          </FieldToggle>
          <FieldToggle id="bulk-channels" label="Notification destinations" checked={!!on.channels} onChange={toggle('channels')} currentValue={current(enrolled, (d) => (d.notify_channels || []).length + ' destination(s)')}>
            {channels.isError ? <p className="text-xs text-warning">Destinations could not be loaded; this field cannot be changed right now.</p>
              : channels.isPending ? <p className="text-xs text-muted">Loading destinations…</p>
              : !channels.data?.data?.length ? <p className="text-xs text-muted">No enabled NCCM-supported destination exists. Add an email, SMS, webhook, Slack, Teams, Discord or PagerDuty channel in Alerting.</p>
              : channels.data.data.map((c: any) => (
                <label key={c.id} className="flex items-center gap-2 text-sm"><input type="checkbox" className="h-4 w-4 accent-primary" checked={v.notify_channels.includes(c.id)} onChange={(e) => set({ notify_channels: e.target.checked ? [...v.notify_channels, c.id] : v.notify_channels.filter((x: string) => x !== c.id) })} />{c.name} <span className="text-muted">({c.type})</span></label>
              ))}
            <p className="text-xs text-muted">Replaces each device's destination list with the selection (none selected clears it).</p>
          </FieldToggle>
        </div>
        {errors.length > 0 && <ul role="alert" className="list-disc pl-5 text-xs text-danger">{errors.map((e) => <li key={e}>{e}</li>)}</ul>}
        {result && <BulkSettingsResult result={result} applied={!!applied} />}
        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>{applied ? 'Close' : 'Cancel'}</Button>
          {!applied && <Button variant="outline" disabled={!anyField || !!errors.length || doPreview.isPending} onClick={() => doPreview.mutate()}><Eye className="h-3.5 w-3.5" /> {doPreview.isPending ? 'Previewing…' : 'Preview changes'}</Button>}
          {!applied && <Button disabled={!preview || !preview.will_update || preview.invalid > 0 || apply.isPending} onClick={() => apply.mutate()}>
            <Save className="h-3.5 w-3.5" /> {apply.isPending ? 'Applying…' : preview ? `Apply to ${preview.will_update} device${preview.will_update === 1 ? '' : 's'}` : 'Preview first'}
          </Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function BulkSettingsResult({ result, applied }: { result: any; applied: boolean }) {
  return (
    <section aria-label={applied ? 'Applied changes' : 'Preview'} className="space-y-2">
      <div className="flex flex-wrap gap-3 text-xs">
        <strong className="text-sm">{applied ? 'Result' : 'Preview'}</strong>
        <span>{applied ? result.updated : result.will_update} {applied ? 'updated' : 'will change'}</span>
        <span className="text-muted">{result.unchanged} unchanged</span>
        {result.skipped > 0 && <span className="text-warning">{result.skipped} skipped</span>}
        {result.invalid > 0 && <span className="text-danger">{result.invalid} invalid — fix before applying</span>}
      </div>
      <div className="max-h-72 overflow-auto rounded-md border border-border">
        <table className="w-full min-w-[640px] text-xs">
          <thead className="sticky top-0 bg-surface2 text-left"><tr><th className="p-2">Device</th><th className="p-2">Outcome</th><th className="p-2">Changes</th><th className="p-2">Schedule · next run</th></tr></thead>
          <tbody>
            {result.results.map((r: any) => (
              <tr key={r.device_id} className="border-t border-border align-top">
                <td className="p-2 font-medium">{r.hostname || r.device_id}</td>
                <td className="p-2"><OutcomeBadge status={r.status} />{r.reason && <div className="text-muted">{r.reason}</div>}{r.errors?.map((e: string) => <div key={e} className="text-danger">{e}</div>)}</td>
                <td className="p-2">{r.changes && Object.keys(r.changes).length ? Object.entries(r.changes).map(([k, c]: any) => (
                  <div key={k}>{FIELD_LABEL[k] || k}: <span className="text-muted line-through">{fmtValue(k, c.before)}</span> → <strong>{fmtValue(k, c.after)}</strong></div>
                )) : <span className="text-muted">—</span>}</td>
                <td className="p-2">{r.schedule_summary || '—'}{r.collection_enabled === false && <div className="text-muted">Collection disabled — will not run</div>}{r.next_run_at && r.collection_enabled !== false && <div title={absoluteUtc(r.next_run_at)}>Next: <Ago iso={r.next_run_at} /> ({absoluteUtc(r.next_run_at)})</div>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

// --------------------------------------------------------------------------- //
// Assign profile (keeps collection state unless chosen)
// --------------------------------------------------------------------------- //
export function AssignProfileDialog({ open, onOpenChange, credentials, platforms, devices, hiddenIds, onDone }: {
  open: boolean; onOpenChange: (o: boolean) => void; credentials: any[]; platforms: any[]; devices: any[]; hiddenIds?: Set<string>; onDone: () => void
}) {
  const [credId, setCredId] = useState('')
  const [platform, setPlatform] = useState('__keep__')
  const [state, setState] = useState('keep')
  const [result, setResult] = useState<any>(null)
  useEffect(() => { if (open) { setCredId(credentials.find((c) => c.is_default)?.id || credentials[0]?.id || ''); setPlatform('__keep__'); setState('keep'); setResult(null) } }, [open, credentials])
  const assign = useMutation({
    mutationFn: async () => (await api.post('/ncm/bulk-assign', {
      device_ids: devices.map((d) => d.device_id), credential_id: credId,
      platform: platform === '__keep__' ? null : platform, enabled: state === 'keep' ? null : state === 'enable',
    })).data,
    onSuccess: (r: any) => { setResult(r); toast.success(`Profile assigned to ${r.assigned} device${r.assigned === 1 ? '' : 's'}`); onDone() },
    onError: (e: any) => toast.error('Assign failed', ncmError(e)),
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-lg overflow-y-auto">
        <DialogHeader><DialogTitle>Assign connection profile</DialogTitle><DialogDescription>Sets the SSH profile (and optionally the driver). Schedules, retention and — unless you choose otherwise — each device's collection state are kept. Unenrolled devices are enrolled with collection enabled.</DialogDescription></DialogHeader>
        <TargetList devices={devices} hiddenIds={hiddenIds} />
        <FormField label="Connection profile">
          <Select value={credId} onValueChange={setCredId}>
            <SelectTrigger aria-label="Connection profile"><SelectValue placeholder="Choose a profile" /></SelectTrigger>
            <SelectContent>{credentials.map((c) => <SelectItem key={c.id} value={c.id}>{c.name} ({c.username}){c.is_default ? ' · default' : ''}</SelectItem>)}</SelectContent>
          </Select>
        </FormField>
        <FormField label="Driver">
          <Select value={platform} onValueChange={setPlatform}>
            <SelectTrigger aria-label="Driver"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="__keep__">Keep each device's driver</SelectItem>{platforms.map((p) => <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>)}</SelectContent>
          </Select>
        </FormField>
        <FormField label="Collection state for already enrolled devices">
          <Select value={state} onValueChange={setState}>
            <SelectTrigger aria-label="Collection state"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="keep">Keep each device's current state</SelectItem><SelectItem value="enable">Enable collection</SelectItem><SelectItem value="disable">Disable collection</SelectItem></SelectContent>
          </Select>
        </FormField>
        {result && (
          <ul className="max-h-40 overflow-auto rounded-md border border-border text-xs">
            {result.results.map((r: any) => {
              const d = devices.find((x) => x.device_id === r.device_id)
              return <li key={r.device_id} className="flex justify-between gap-2 border-b border-border px-3 py-1.5 last:border-0"><span>{d?.hostname || r.device_id}</span><span><OutcomeBadge status={r.status} /> · collection {r.enabled ? 'enabled' : 'disabled'}</span></li>
            })}
          </ul>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{result ? 'Close' : 'Cancel'}</Button>
          {!result && <Button disabled={!credId || assign.isPending} onClick={() => assign.mutate()}><KeyRound className="h-3.5 w-3.5" /> {assign.isPending ? 'Assigning…' : `Assign to ${devices.length}`}</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// --------------------------------------------------------------------------- //
// Bulk backup with per-device results (B10)
// --------------------------------------------------------------------------- //
export function BulkBackupDialog({ open, onOpenChange, devices, hiddenIds, onDone, onPrune }: {
  open: boolean; onOpenChange: (o: boolean) => void; devices: any[]; hiddenIds?: Set<string>; onDone: () => void; onPrune?: (ids: string[]) => void
}) {
  const [result, setResult] = useState<any>(null)
  useEffect(() => { if (open) setResult(null) }, [open])
  const run = useMutation({
    mutationFn: async () => (await api.post('/ncm/bulk-jobs', { device_ids: devices.map((d) => d.device_id) })).data,
    onSuccess: (r: any) => {
      setResult(r)
      const accepted = r.queued + r.already_active
      const problems = r.blocked + r.failed + r.not_enrolled + r.not_found
      const detail = [`${r.queued} queued`, r.already_active && `${r.already_active} already active`, r.blocked && `${r.blocked} blocked`, r.not_enrolled && `${r.not_enrolled} not enrolled`, r.not_found && `${r.not_found} not found`, r.failed && `${r.failed} failed`].filter(Boolean).join(', ')
      if (!accepted) toast.error('No backups were queued', detail)
      else if (problems) toast.warning('Some backups were not queued', detail)
      else toast.success('Backups queued', detail)
      const gone = r.results.filter((x: any) => x.status === 'not_found').map((x: any) => x.device_id)
      if (gone.length) onPrune?.(gone)
      onDone()
    },
    onError: (e: any) => toast.error('Bulk backup failed', ncmError(e)),
  })
  const byStatus = useMemo(() => {
    const m = new Map<string, any[]>()
    for (const r of result?.results || []) m.set(r.status, [...(m.get(r.status) || []), r])
    return [...m.entries()]
  }, [result])
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader><DialogTitle>Run backup now</DialogTitle><DialogDescription>Each device is checked against the same readiness rules as a single backup. Devices with a missing prerequisite are reported, not queued.</DialogDescription></DialogHeader>
        <TargetList devices={devices} hiddenIds={hiddenIds} />
        {result && (
          <div className="space-y-2">
            {byStatus.map(([status, rows]) => (
              <details key={status} open={status !== 'queued'} className="rounded-md border border-border text-xs">
                <summary className="cursor-pointer px-3 py-2"><OutcomeBadge status={status} /> <span className="ml-1 text-muted">{rows.length}</span></summary>
                <ul className="border-t border-border">
                  {rows.map((r) => <li key={r.device_id} className="px-3 py-1.5"><span className="font-medium">{r.hostname || r.device_id}</span>{r.reason && <span className="text-muted"> — {r.reason}</span>}</li>)}
                </ul>
              </details>
            ))}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{result ? 'Close' : 'Cancel'}</Button>
          {!result && <Button disabled={!devices.length || run.isPending} onClick={() => run.mutate()}><DownloadCloud className="h-3.5 w-3.5" /> {run.isPending ? 'Queueing…' : `Queue ${devices.length} backup${devices.length === 1 ? '' : 's'}`}</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// --------------------------------------------------------------------------- //
// Selection review (B11)
// --------------------------------------------------------------------------- //
export function SelectionReviewDialog({ open, onOpenChange, devices, hiddenIds, onRemove, onClear }: {
  open: boolean; onOpenChange: (o: boolean) => void; devices: any[]; hiddenIds: Set<string>; onRemove: (id: string) => void; onClear: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader><DialogTitle>Selected devices ({devices.length})</DialogTitle><DialogDescription>Bulk actions apply to every device listed here, including those hidden by the current filters or on other pages.</DialogDescription></DialogHeader>
        <ul className="divide-y divide-border rounded-md border border-border text-sm">
          {devices.map((d) => (
            <li key={d.device_id} className="flex items-center justify-between gap-2 px-3 py-2">
              <div className="min-w-0">
                <div className="truncate font-medium">{d.hostname} {hiddenIds.has(d.device_id) && <Badge variant="outline">hidden by filters</Badge>}</div>
                <div className="font-mono text-xs text-muted">{d.ip}</div>
              </div>
              <div className="flex items-center gap-2"><StateBadge device={d} /><Button variant="ghost" size="icon" className="h-8 w-8" aria-label={`Remove ${d.hostname} from selection`} onClick={() => onRemove(d.device_id)}><X className="h-4 w-4" /></Button></div>
            </li>
          ))}
          {!devices.length && <li className="px-3 py-6 text-center text-muted">Nothing selected.</li>}
        </ul>
        <DialogFooter>
          <Button variant="outline" onClick={() => { onClear(); onOpenChange(false) }} disabled={!devices.length}><Trash2 className="h-3.5 w-3.5" /> Clear selection</Button>
          <Button onClick={() => onOpenChange(false)}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

