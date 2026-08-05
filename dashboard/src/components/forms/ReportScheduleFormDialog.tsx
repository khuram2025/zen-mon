import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, CheckCircle2, Loader2, Mail } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'
import {
  LEGACY_REPORT_TYPES,
  SECTION_ENGINE_KEYS,
  SECTION_REPORT_TYPES,
  useReportCatalog,
} from '@/hooks/useReportCatalog'

export type ReportSchedule = {
  id: string
  name: string
  description?: string | null
  enabled: boolean
  report_type: string
  period: string
  format: string
  filters?: Record<string, any>
  frequency: string
  hour: number
  minute: number
  day_of_week?: number | null
  day_of_month?: number | null
  notify_channels: string[]
  /** Set when report_type === 'custom'. */
  custom_report_id?: string | null
  // Bookkeeping returned by the API (read-only in the form).
  last_run_at?: string | null
  last_status?: string | null
  last_error?: string | null
  next_run_at?: string | null
}

const PERIODS = [
  { id: 'last_24h', label: 'Last 24 hours' },
  { id: 'last_7d', label: 'Last 7 days' },
  { id: 'last_30d', label: 'Last 30 days' },
  { id: 'last_90d', label: 'Last 90 days' },
]
const LEGACY_FORMATS = [
  { id: 'pdf', label: 'PDF attachment' },
  { id: 'excel', label: 'Excel attachment' },
  { id: 'csv', label: 'CSV attachment' },
  { id: 'none', label: 'HTML summary only' },
]
// Section-engine reports (the new keys + custom) only render PDF documents.
const SECTION_FORMATS = [
  { id: 'pdf', label: 'PDF attachment' },
  { id: 'none', label: 'None (summary + link only)' },
]

function SelectGroupLabel({ children }: { children: ReactNode }) {
  return (
    <div className="px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-muted">{children}</div>
  )
}
const FREQS = [
  { id: 'daily', label: 'Daily' },
  { id: 'weekly', label: 'Weekly' },
  { id: 'monthly', label: 'Monthly' },
]
const WEEKDAYS = [
  { id: 1, label: 'Monday' }, { id: 2, label: 'Tuesday' }, { id: 3, label: 'Wednesday' },
  { id: 4, label: 'Thursday' }, { id: 5, label: 'Friday' }, { id: 6, label: 'Saturday' }, { id: 7, label: 'Sunday' },
]

type Form = Omit<ReportSchedule, 'id'>

const DEFAULT: Form = {
  name: '', description: '', enabled: true,
  report_type: 'executive_summary', period: 'last_24h', format: 'pdf',
  filters: {}, frequency: 'daily', hour: 8, minute: 0,
  day_of_week: 1, day_of_month: 1, notify_channels: [], custom_report_id: null,
}

function pad(n: number) {
  return String(n).padStart(2, '0')
}

export function ReportScheduleFormDialog({
  open,
  onOpenChange,
  schedule,
  prefill,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  schedule: ReportSchedule | null
  /** Initial report type when creating (schedule == null), e.g. from a report viewer. */
  prefill?: { report_type?: string; custom_report_id?: string | null }
}) {
  const qc = useQueryClient()
  const [form, setForm] = useState<Form>(DEFAULT)
  const isEdit = !!schedule?.id

  useEffect(() => {
    if (!open) return
    if (!schedule) {
      setForm({
        ...DEFAULT,
        ...(prefill?.report_type ? { report_type: prefill.report_type } : {}),
        custom_report_id: prefill?.custom_report_id ?? null,
      })
      return
    }
    setForm({
      name: schedule.name || '',
      description: schedule.description || '',
      enabled: schedule.enabled ?? true,
      report_type: schedule.report_type || 'executive_summary',
      period: schedule.period || 'last_24h',
      format: schedule.format || 'pdf',
      filters: schedule.filters || {},
      frequency: schedule.frequency || 'daily',
      hour: schedule.hour ?? 8,
      minute: schedule.minute ?? 0,
      day_of_week: schedule.day_of_week ?? 1,
      day_of_month: schedule.day_of_month ?? 1,
      notify_channels: schedule.notify_channels || [],
      custom_report_id: schedule.custom_report_id ?? null,
    })
    // prefill is only read when creating; keeping it out of the deps avoids
    // resetting the form on every parent render (callers pass inline objects).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schedule, open])

  // Shared key with ChannelsPage/RoutingTab — return the same array shape.
  const { data: allChannels = [] } = useQuery<any[]>({
    queryKey: ['settings', 'channels'],
    queryFn: async () => {
      const r = (await api.get('/settings/channels')).data
      return Array.isArray(r) ? r : r?.data || []
    },
    enabled: open,
  })
  const enabledChannels = useMemo(() => allChannels.filter((c) => c.enabled !== false), [allChannels])

  const { data: catalog } = useReportCatalog({ enabled: open })
  const customReports = catalog?.custom ?? []

  const isSectionEngine = SECTION_ENGINE_KEYS.has(form.report_type)
  const formatOptions = isSectionEngine ? SECTION_FORMATS : LEGACY_FORMATS

  // The Select carries custom reports as `custom:{id}`.
  const typeValue =
    form.report_type === 'custom' && form.custom_report_id
      ? `custom:${form.custom_report_id}`
      : form.report_type
  const onTypeChange = (v: string) =>
    setForm((s) => {
      const isCustom = v.startsWith('custom:')
      const report_type = isCustom ? 'custom' : v
      const custom_report_id = isCustom ? v.slice('custom:'.length) : null
      const valid = (SECTION_ENGINE_KEYS.has(report_type) ? SECTION_FORMATS : LEGACY_FORMATS).map((f) => f.id)
      return {
        ...s,
        report_type,
        custom_report_id,
        format: valid.includes(s.format) ? s.format : 'pdf',
      }
    })
  // Selected custom schedule whose report was deleted — keep it representable.
  const orphanCustomId =
    form.report_type === 'custom' &&
    form.custom_report_id &&
    !customReports.some((c) => c.id === form.custom_report_id)
      ? form.custom_report_id
      : null

  const toggleChannel = (id: string) =>
    setForm((st) => ({
      ...st,
      notify_channels: st.notify_channels.includes(id)
        ? st.notify_channels.filter((c) => c !== id)
        : [...st.notify_channels, id],
    }))

  const save = useMutation({
    mutationFn: async () => {
      const payload: any = {
        name: form.name.trim(),
        description: form.description || null,
        enabled: form.enabled,
        report_type: form.report_type,
        period: form.period,
        format: form.format,
        filters: form.filters || {},
        frequency: form.frequency,
        hour: form.hour,
        minute: form.minute,
        notify_channels: form.notify_channels,
        day_of_week: form.frequency === 'weekly' ? form.day_of_week : null,
        day_of_month: form.frequency === 'monthly' ? form.day_of_month : null,
        custom_report_id: form.report_type === 'custom' ? form.custom_report_id : null,
      }
      if (isEdit) return (await api.put(`/report-schedules/${schedule!.id}`, payload)).data
      return (await api.post('/report-schedules', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Schedule updated' : 'Schedule created')
      qc.invalidateQueries({ queryKey: ['report-schedules'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const timeValue = `${pad(form.hour)}:${pad(form.minute)}`

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[92vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit scheduled report' : 'New scheduled report'}</DialogTitle>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            save.mutate()
          }}
        >
          <div className="grid grid-cols-1 gap-4 md:grid-cols-[1fr_auto] md:items-end">
            <FormField label="Schedule name" required>
              <Input
                required
                value={form.name}
                onChange={(e) => setForm((s) => ({ ...s, name: e.target.value }))}
                placeholder="Daily Executive Summary"
              />
            </FormField>
            <div className="flex h-9 items-center justify-between gap-3 rounded-md border border-border px-3">
              <span className="text-xs font-medium text-muted">Enabled</span>
              <Switch checked={form.enabled} onCheckedChange={(enabled) => setForm((s) => ({ ...s, enabled }))} />
            </div>
          </div>

          <FormField label="Description" hint="Optional">
            <Textarea
              rows={2}
              value={form.description || ''}
              onChange={(e) => setForm((s) => ({ ...s, description: e.target.value }))}
              placeholder="Sent to the NOC distribution list every morning."
            />
          </FormField>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <FormField label="Report type">
              <Select value={typeValue} onValueChange={onTypeChange}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectGroupLabel>Persona reports</SelectGroupLabel>
                  {LEGACY_REPORT_TYPES.map((t) => <SelectItem key={t.id} value={t.id}>{t.label}</SelectItem>)}
                  <SelectGroupLabel>Operations &amp; applications</SelectGroupLabel>
                  {SECTION_REPORT_TYPES.map((t) => <SelectItem key={t.id} value={t.id}>{t.label}</SelectItem>)}
                  {(customReports.length > 0 || orphanCustomId) && (
                    <>
                      <SelectGroupLabel>Custom reports</SelectGroupLabel>
                      {customReports.map((c) => (
                        <SelectItem key={c.id} value={`custom:${c.id}`}>{c.name}</SelectItem>
                      ))}
                      {orphanCustomId && (
                        <SelectItem value={`custom:${orphanCustomId}`}>Custom (report deleted)</SelectItem>
                      )}
                    </>
                  )}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Period covered">
              <Select value={form.period} onValueChange={(v) => setForm((s) => ({ ...s, period: v }))}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {PERIODS.map((t) => <SelectItem key={t.id} value={t.id}>{t.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Attachment">
              <Select value={form.format} onValueChange={(v) => setForm((s) => ({ ...s, format: v }))}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {formatOptions.map((t) => <SelectItem key={t.id} value={t.id}>{t.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </FormField>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <FormField label="Frequency">
              <Select value={form.frequency} onValueChange={(v) => setForm((s) => ({ ...s, frequency: v }))}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {FREQS.map((t) => <SelectItem key={t.id} value={t.id}>{t.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Time of day" hint="Appliance timezone">
              <Input
                type="time"
                value={timeValue}
                onChange={(e) => {
                  const [h, m] = e.target.value.split(':').map((x) => parseInt(x, 10))
                  setForm((s) => ({ ...s, hour: Number.isFinite(h) ? h : 8, minute: Number.isFinite(m) ? m : 0 }))
                }}
              />
            </FormField>
            {form.frequency === 'weekly' && (
              <FormField label="Day of week">
                <Select value={String(form.day_of_week ?? 1)} onValueChange={(v) => setForm((s) => ({ ...s, day_of_week: parseInt(v, 10) }))}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {WEEKDAYS.map((d) => <SelectItem key={d.id} value={String(d.id)}>{d.label}</SelectItem>)}
                  </SelectContent>
                </Select>
              </FormField>
            )}
            {form.frequency === 'monthly' && (
              <FormField label="Day of month" hint="1–31">
                <Input
                  type="number"
                  min={1}
                  max={31}
                  value={form.day_of_month ?? 1}
                  onChange={(e) => setForm((s) => ({ ...s, day_of_month: Math.min(31, Math.max(1, parseInt(e.target.value, 10) || 1)) }))}
                />
              </FormField>
            )}
          </div>

          <FormField
            label="Deliver to channels"
            hint="Email channels receive the full report as an attachment. Other channels receive a KPI summary with a link to the report."
          >
            {enabledChannels.length === 0 ? (
              <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-xs text-warning">
                No enabled channels configured yet. Create one on the Channels page first.
              </div>
            ) : (
              <div className="flex flex-wrap gap-2">
                {enabledChannels.map((c) => {
                  const active = form.notify_channels.includes(c.id)
                  return (
                    <button
                      key={c.id}
                      type="button"
                      onClick={() => toggleChannel(c.id)}
                      className={cn(
                        'inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors',
                        active ? 'border-primary bg-primary/10 text-primary' : 'border-border bg-bg text-muted hover:border-primary/40 hover:text-text',
                      )}
                    >
                      {c.type === 'email' ? <Mail className="h-3.5 w-3.5" /> : <Bell className="h-3.5 w-3.5" />}
                      {c.name}
                      <span className="rounded bg-surface2 px-1 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-muted">
                        {c.type}
                      </span>
                      {active && <CheckCircle2 className="h-3.5 w-3.5" />}
                    </button>
                  )
                })}
              </div>
            )}
          </FormField>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button
              type="submit"
              disabled={save.isPending || !form.name.trim() || (form.report_type === 'custom' && !form.custom_report_id)}
            >
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
              {isEdit ? 'Save schedule' : 'Create schedule'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
