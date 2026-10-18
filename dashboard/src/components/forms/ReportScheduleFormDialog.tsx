/**
 * Schedule editor: what to send (template + carried scope), which data period
 * each run covers, when it runs (in the schedule's own time zone, the
 * organisation's by default, with the next runs previewed in local time and
 * UTC), and how it is delivered (format, notification channels, direct
 * email recipients). Props are shared with the report viewer and the
 * Schedules page: {open, onOpenChange, schedule?, prefill?}.
 */
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import * as Popover from '@radix-ui/react-popover'
import * as SelectPrimitive from '@radix-ui/react-select'
import {
  AlertCircle, Bell, CalendarClock, Check, CheckCircle2, ChevronsUpDown, Clock, FileText, Info, Loader2, Lock,
  Mail, Play, Search, Send, TriangleAlert, X,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatDateTime, zoneLabel, type Branding } from '@/lib/branding'
import { useAuth, useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Label } from '@/components/ui/Label'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Switch } from '@/components/ui/Switch'
import { Skeleton } from '@/components/ui/Skeleton'
import { QueryFailure } from '@/components/ui/QueryState'
import { toast } from '@/components/ui/Toast'
import { useReportBranding, useReportCatalog } from '@/features/reports/api'
import { reportIcon } from '@/features/reports/icons'
import { isPeriodId, PERIOD_LABEL } from '@/features/reports/periods'
import type { PeriodId, ReportCatalog, ScheduleFormat } from '@/features/reports/types'
import { useNotificationChannels, useRunScheduleNow, useSaveSchedule } from '@/features/reports/schedules/api'
import {
  FORMAT_META, FREQUENCIES, MAX_MONTH_DAY, MAX_RECIPIENTS, PERIOD_GROUPS, SCOPED_EXPLANATION, TIME_STEPS,
  addRecipients, buildBody, clockLabel, coverageText, crossesClockChange, defaultForm, formatsForEngine,
  frequencyText, groupTemplateOptions, isNetflowWithheld, isVisibilityScoped, localTimeLabel, nameTimeMismatch,
  nextRuns, ordinal, relativeTo, runLabel, runWithUtc, scheduleToForm, scheduleZone, scopeOf, searchTemplateOptions,
  suggestedName, suggestedPeriod, templateOptions, templateTitle, templateValue, utcClock, validateSchedule,
  weekdaysFrom, zoneName,
  type Issue, type ReportSchedule, type ScheduleForm, type ScheduleFrequency, type TemplateOption,
} from '@/features/reports/schedules/model'
import { ScopeChips, scheduleErrorText, useNow } from '@/features/reports/schedules/ui'
import { TimezoneSelect } from '@/features/reports/schedules/TimezoneSelect'

export type { ReportSchedule } from '@/features/reports/schedules/model'

export type ReportSchedulePrefill = {
  name?: string
  report_type?: string
  custom_report_id?: string | null
  period?: string
  format?: string
  filters?: Record<string, any>
}

export function ReportScheduleFormDialog({
  open,
  onOpenChange,
  schedule,
  prefill,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The schedule to edit; omit or null to create one. */
  schedule?: ReportSchedule | null
  /** Initial values when creating (schedule == null), e.g. from a report viewer. */
  prefill?: ReportSchedulePrefill
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Mounted per opening, so each opening starts from the schedule or prefill. */}
      {open && <ScheduleEditor schedule={schedule ?? null} prefill={prefill} onClose={() => onOpenChange(false)} />}
    </Dialog>
  )
}

/* ------------------------------------------------------------------ */
/*  Editor                                                             */
/* ------------------------------------------------------------------ */

/** Same look as FormField's label, for radio groups and composite controls. */
const GROUP_LABEL = 'block text-xs font-medium uppercase leading-none tracking-wider text-muted'

const FIELD_ID: Record<string, string> = {
  template: 'sched-template',
  period: 'sched-period',
  time: 'sched-time',
  timezone: 'sched-timezone',
  weekday: 'sched-weekday',
  monthday: 'sched-monthday',
  format: 'sched-format',
  recipients: 'sched-recipients',
  name: 'sched-name',
  delivery: 'sched-channels',
  scope: 'sched-template',
}

const lowerFirst = (t: string) => t.charAt(0).toLowerCase() + t.slice(1)

function initialForm(schedule: ReportSchedule | null, prefill?: ReportSchedulePrefill): ScheduleForm {
  if (schedule) return scheduleToForm(schedule)
  const f = defaultForm()
  if (prefill?.report_type) {
    f.report_type = prefill.report_type
    f.custom_report_id = prefill.report_type === 'custom' ? prefill.custom_report_id ?? null : null
  }
  if (prefill?.name) f.name = prefill.name
  if (isPeriodId(prefill?.period)) f.period = prefill.period
  if (prefill?.format && ['pdf', 'excel', 'csv', 'html', 'none'].includes(prefill.format)) f.format = prefill.format as ScheduleFormat
  if (prefill?.filters) f.filters = { ...prefill.filters }
  return f
}

function ScheduleEditor({
  schedule, prefill, onClose,
}: { schedule: ReportSchedule | null; prefill?: ReportSchedulePrefill; onClose: () => void }) {
  const can = useCan()
  const user = useAuth((s) => s.user)
  const scoped = isVisibilityScoped(user)
  const catalog = useReportCatalog()
  const { branding } = useReportBranding()
  const channels = useNotificationChannels()
  const save = useSaveSchedule()
  const runNow = useRunScheduleNow()
  const now = useNow(30_000)

  const isEdit = !!schedule?.id
  const withheld = isEdit && isNetflowWithheld(schedule)
  const [form, setForm] = useState<ScheduleForm>(() => initialForm(schedule, prefill))
  const [nameTouched, setNameTouched] = useState(isEdit || !!prefill?.name)
  const [periodTouched, setPeriodTouched] = useState(isEdit || isPeriodId(prefill?.period))
  const [serverError, setServerError] = useState<string | null>(null)
  const [attempted, setAttempted] = useState(false)
  const set = <K extends keyof ScheduleForm>(k: K, v: ScheduleForm[K]) => setForm((f) => ({ ...f, [k]: v }))

  const options = useMemo(() => templateOptions(catalog.data), [catalog.data])
  const value = templateValue(form.report_type, form.custom_report_id)
  const template = options.find((o) => o.value === value) ?? null
  // The schedule's own zone ('' = the organisation's); every time in the
  // editor is shown in it.
  const tz = scheduleZone(form, branding.timezone)
  const zb = useMemo(() => ({ ...branding, timezone: tz }), [branding, tz])

  // A name derived from the template and frequency until the user types one.
  useEffect(() => {
    if (nameTouched || !template) return
    setForm((f) => ({ ...f, name: suggestedName(template.title, f.frequency) }))
  }, [template?.value, form.frequency, nameTouched]) // eslint-disable-line react-hooks/exhaustive-deps

  // Formats follow the template's engine.
  const formats = formatsForEngine(template?.engine ?? 'sections')
  useEffect(() => {
    if (template && !formats.includes(form.format)) setForm((f) => ({ ...f, format: 'pdf' }))
  }, [template?.engine]) // eslint-disable-line react-hooks/exhaustive-deps

  const chooseTemplate = (o: TemplateOption) =>
    setForm((f) => ({ ...f, report_type: o.report_type, custom_report_id: o.custom_report_id }))
  const chooseFrequency = (freq: ScheduleFrequency) =>
    setForm((f) => ({ ...f, frequency: freq, period: periodTouched ? f.period : suggestedPeriod(freq) }))

  const original = schedule
    ? { dom: schedule.day_of_month ?? null, time: { hour: schedule.hour, minute: schedule.minute } }
    : { dom: null, time: null }
  const { errors, warnings } = validateSchedule(form, {
    template,
    originalDayOfMonth: original.dom,
    originalTime: original.time,
    timezone: tz,
  })
  const valid = errors.length === 0

  const runs = useMemo(() => nextRuns(form, tz, 3, new Date(now)), [form.frequency, form.hour, form.minute, form.day_of_week, form.day_of_month, tz, now]) // eslint-disable-line react-hooks/exhaustive-deps
  const coverage = isPeriodId(form.period) ? coverageText(form.period, runs[0] ?? new Date(now), zb) : ''
  const nameWarning = nameTimeMismatch(form.name.trim(), form.hour, form.minute)

  const submit = async (andRun: boolean) => {
    setAttempted(true)
    setServerError(null)
    if (!valid) {
      document.getElementById(FIELD_ID[errors[0].field] ?? 'sched-name')?.focus()
      return
    }
    try {
      const saved = await save.mutateAsync({
        id: isEdit ? schedule!.id : null,
        body: buildBody(form, { keepStoredFilters: withheld }),
      })
      toast.success(isEdit ? 'Schedule saved' : 'Schedule created', `"${form.name.trim()}" ${form.enabled ? `runs ${lowerFirst(frequencyText(form, branding.time_format))} ${zoneName(tz)}` : 'is paused'}.`)
      onClose()
      const id = saved?.id ?? schedule?.id
      if (andRun && id) {
        toast.info('Generating report', 'You will be told when it has been delivered.')
        runNow.mutateAsync(id).then(
          (r) => {
            const st = (r?.status || '').toLowerCase()
            const delivered = r?.delivered?.length ?? 0
            if (st === 'failed') toast.error('Run failed', r?.errors?.join('; ') || 'The report could not be generated.')
            else if (st === 'partial') toast.warning('Delivered with problems', r?.errors?.join('; ') || undefined)
            else toast.success('Report generated', delivered ? `Delivered to ${delivered} recipient${delivered === 1 ? '' : 's'}.` : 'Open it from the run history.')
          },
          (e) => toast.error('Run failed', scheduleErrorText(e)),
        )
      }
    } catch (e) {
      setServerError(scheduleErrorText(e))
    }
  }

  if (!can('reports.manage') || scoped || catalog.data?.can?.manage === false) {
    return (
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Lock className="h-4 w-4 text-muted" aria-hidden /> Scheduling unavailable</DialogTitle>
          <DialogDescription>
            {scoped ? SCOPED_EXPLANATION : 'Your role can view reports but cannot schedule them. Ask an administrator for the “Manage reports” permission.'}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>Close</Button>
        </DialogFooter>
      </DialogContent>
    )
  }

  const pending = save.isPending
  const tzName = `${zoneName(tz)} (${zoneLabel(tz, runs[0] ?? now)})`

  return (
    <DialogContent className="max-w-5xl pb-0 sm:pb-0">
      <DialogHeader className="pr-8">
        <DialogTitle className="flex items-center gap-2">
          <CalendarClock className="h-5 w-5 text-primary" aria-hidden />
          {isEdit ? 'Edit schedule' : 'New schedule'}
        </DialogTitle>
        <DialogDescription>
          Generate a report automatically and deliver it by email and notification channels. This schedule runs in {tzName}{form.timezone ? '' : ', the organisation time zone'}.
        </DialogDescription>
      </DialogHeader>

      {serverError && (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger-fg">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <div><span className="font-medium">The schedule was not saved.</span> {serverError}</div>
        </div>
      )}

      <form
        id="report-schedule-form"
        noValidate
        onSubmit={(e) => { e.preventDefault(); void submit(false) }}
        className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_300px]"
      >
        <div className="min-w-0 space-y-7">
          {/* 1. What */}
          <Step n={1} title="Report" description="The template to generate on each run.">
            <TemplatePicker
              id={FIELD_ID.template}
              catalog={catalog.data}
              options={options}
              value={value}
              fallbackTitle={form.report_type ? templateTitle({ report_type: form.report_type, custom_report_id: form.custom_report_id }, catalog.data) : ''}
              loading={catalog.isLoading}
              invalid={attempted && errors.some((e) => e.field === 'template')}
              onChange={chooseTemplate}
            />
            {catalog.isError && !catalog.data && (
              <QueryFailure compact error={catalog.error} what="the report templates" onRetry={() => void catalog.refetch()} retrying={catalog.isFetching} />
            )}
            {template && <TemplateDetails template={template} />}
            {(scopeOf(form.filters).any || withheld) && (
              <div className="rounded-md border border-border bg-surface2/40 px-3 py-2.5">
                <div className="mb-1.5 flex items-center justify-between gap-2">
                  <span className="text-xs font-medium text-text2">Scope carried by this schedule</span>
                  {withheld ? (
                    <span className="inline-flex items-center gap-1 text-[11px] text-muted"><Lock className="h-3 w-3" aria-hidden /> Kept unchanged</span>
                  ) : (
                    <button
                      type="button"
                      className="rounded text-[11px] font-medium text-text2 underline-offset-2 hover:text-text hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                      onClick={() => setForm((f) => {
                        const { netflow: _n, device_ids: _d, application_id: _a, ...rest } = f.filters
                        return { ...f, filters: rest }
                      })}
                    >
                      Remove scope
                    </button>
                  )}
                </div>
                <ScopeChips filters={form.filters} withheld={withheld} tz={tz} />
                <p className="mt-1.5 text-[11px] text-muted">
                  {withheld
                    ? 'This schedule has a NetFlow scope your role may not see. Saving keeps the stored scope exactly as it is.'
                    : 'Each run covers the data period below with exactly this scope, limited to what the schedule’s owner may see.'}
                </p>
              </div>
            )}
          </Step>

          {/* 2. Data period */}
          <Step n={2} title="Data period" description="The window of data each run reports on.">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,260px)_1fr] sm:items-start">
              <FormField label="Period">
                <Select
                  value={form.period}
                  onValueChange={(v) => { setPeriodTouched(true); set('period', v as PeriodId) }}
                >
                  <SelectTrigger id={FIELD_ID.period}><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {PERIOD_GROUPS.map((g) => (
                      <SelectGroup key={g.title}>
                        <SelectPrimitive.Label className="px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
                          {g.title}
                        </SelectPrimitive.Label>
                        {g.periods.map((p) => (
                          <SelectItem key={p.id} value={p.id}>
                            {p.label}{p.id === suggestedPeriod(form.frequency) ? ' · suggested' : ''}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
              <div className="space-y-1.5 sm:pt-6">
                <p className="flex items-start gap-1.5 text-sm text-text2" aria-live="polite">
                  <CalendarClock className="mt-0.5 h-4 w-4 shrink-0 text-muted" aria-hidden />
                  <span>
                    <span className="text-muted">{runs[0] ? 'The next run covers ' : 'Covers '}</span>
                    <span className="font-medium tabular-nums text-text">{coverage}</span>
                  </span>
                </p>
                {form.period !== suggestedPeriod(form.frequency) && (
                  <p className="text-xs text-muted">
                    Suggested for {form.frequency} schedules: {PERIOD_LABEL[suggestedPeriod(form.frequency)]}.{' '}
                    <button
                      type="button"
                      className="rounded font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                      onClick={() => { setPeriodTouched(true); set('period', suggestedPeriod(form.frequency)) }}
                    >
                      Use it
                    </button>
                  </p>
                )}
              </div>
            </div>
          </Step>

          {/* 3. When */}
          <Step n={3} title="Schedule" description={`When the report is generated: local time in ${tzName}.`}>
            <div className="space-y-1.5">
              <span className={GROUP_LABEL} id="sched-frequency-label">Repeats</span>
              <ChoiceGroup
                labelledBy="sched-frequency-label"
                variant="segmented"
                value={form.frequency}
                options={FREQUENCIES.map((f) => ({ id: f.id, label: f.label }))}
                onChange={(v) => chooseFrequency(v as ScheduleFrequency)}
              />
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {form.frequency === 'weekly' && (
                <div className="space-y-1.5 sm:col-span-2">
                  <span className={GROUP_LABEL} id="sched-weekday-label">Day of week</span>
                  <ChoiceGroup
                    id={FIELD_ID.weekday}
                    labelledBy="sched-weekday-label"
                    variant="chips"
                    value={String(form.day_of_week)}
                    options={weekdaysFrom(branding.week_start).map((d) => ({ id: String(d.id), label: d.short, aria: d.long }))}
                    onChange={(v) => set('day_of_week', Number(v))}
                  />
                </div>
              )}
              {form.frequency === 'monthly' && (
                <FormField
                  label="Day of month"
                  hint={`Days 29–31 are not offered so the report runs every month. For a month-end report, run on day 1 with “Previous month”.`}
                >
                  <Select value={String(form.day_of_month)} onValueChange={(v) => set('day_of_month', Number(v))}>
                    <SelectTrigger id={FIELD_ID.monthday}><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {Array.from({ length: MAX_MONTH_DAY }, (_, i) => i + 1).map((d) => (
                        <SelectItem key={d} value={String(d)}>{ordinal(d)}</SelectItem>
                      ))}
                      {form.day_of_month > MAX_MONTH_DAY && (
                        <SelectItem value={String(form.day_of_month)}>{ordinal(form.day_of_month)} (last day in shorter months)</SelectItem>
                      )}
                    </SelectContent>
                  </Select>
                </FormField>
              )}
              <FormField label="Time of day" hint={localTimeLabel(form, tz, new Date(now), branding.time_format)}>
                <Select
                  value={`${String(form.hour).padStart(2, '0')}:${String(form.minute).padStart(2, '0')}`}
                  onValueChange={(v) => {
                    const [h, m] = v.split(':').map(Number)
                    setForm((f) => ({ ...f, hour: h, minute: m }))
                  }}
                >
                  <SelectTrigger id={FIELD_ID.time} className="tabular-nums"><SelectValue /></SelectTrigger>
                  <SelectContent className="max-h-72">
                    {form.minute % 15 !== 0 && (
                      <SelectItem value={`${String(form.hour).padStart(2, '0')}:${String(form.minute).padStart(2, '0')}`}>
                        {clockLabel(form.hour, form.minute, branding.time_format)} (current)
                      </SelectItem>
                    )}
                    {TIME_STEPS.map((t) => (
                      <SelectItem key={t.value} value={t.value}>{clockLabel(t.hour, t.minute, branding.time_format)}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormField>
              <FormField
                label="Time zone"
                hint={form.timezone
                  ? (form.timezone === branding.timezone ? 'Same as the organisation; kept if the organisation zone changes later.' : `Differs from the organisation time zone (${zoneName(branding.timezone)}).`)
                  : 'The organisation time zone, saved with the schedule.'}
                error={attempted ? errors.find((e) => e.field === 'timezone')?.message : null}
              >
                <TimezoneSelect
                  id={FIELD_ID.timezone}
                  value={form.timezone}
                  orgTz={branding.timezone}
                  at={runs[0] ?? new Date(now)}
                  onChange={(v) => set('timezone', v)}
                  invalid={attempted && errors.some((e) => e.field === 'timezone')}
                />
              </FormField>
            </div>
            <NextRunsPreview runs={runs} branding={zb} now={now} />
          </Step>

          {/* 4. Delivery */}
          <Step n={4} title="Delivery" description="How the report is sent and who receives it.">
            <div className="space-y-1.5">
              <span className={GROUP_LABEL} id="sched-format-label">Attachment</span>
              <ChoiceGroup
                id={FIELD_ID.format}
                labelledBy="sched-format-label"
                variant="cards"
                value={form.format}
                options={formats.map((f) => ({ id: f, label: FORMAT_META[f].label, hint: FORMAT_META[f].hint }))}
                onChange={(v) => set('format', v as ScheduleFormat)}
              />
            </div>
            <ChannelPicker
              selected={form.notify_channels}
              query={channels}
              onChange={(ids) => set('notify_channels', ids)}
            />
            <RecipientInput
              id={FIELD_ID.recipients}
              value={form.recipients}
              onChange={(r) => set('recipients', r)}
            />
          </Step>

          {/* 5. Details */}
          <Step n={5} title="Details">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
              <FormField
                label="Schedule name"
                required
                error={attempted ? errors.find((e) => e.field === 'name')?.message : null}
                hint={nameWarning
                  ? <span className="inline-flex items-start gap-1 text-warning-fg"><TriangleAlert className="mt-px h-3 w-3 shrink-0" aria-hidden /> The name says {nameWarning}; the report runs at {clockLabel(form.hour, form.minute, branding.time_format)} {zoneName(tz)}.</span>
                  : !nameTouched && template ? 'Suggested from the template; type to change it.' : undefined}
              >
                <Input
                  id={FIELD_ID.name}
                  value={form.name}
                  maxLength={255}
                  onChange={(e) => { setNameTouched(true); set('name', e.target.value) }}
                  placeholder="Weekly availability for the NOC"
                />
              </FormField>
              <div className="flex h-9 items-center gap-3 rounded-md border border-border px-3 sm:mt-6">
                <Switch
                  id="sched-enabled"
                  checked={form.enabled}
                  onCheckedChange={(v) => set('enabled', v)}
                  aria-describedby="sched-enabled-hint"
                />
                <label htmlFor="sched-enabled" className="text-sm font-medium text-text">Enabled</label>
                <span id="sched-enabled-hint" className="sr-only">
                  {form.enabled ? 'Runs automatically on schedule' : 'Paused: runs only when started by hand'}
                </span>
              </div>
            </div>
            <FormField label="Description" hint="Optional. Shown in the schedules list.">
              <Textarea
                rows={2}
                maxLength={1000}
                value={form.description}
                onChange={(e) => set('description', e.target.value)}
                placeholder="Sent to the NOC distribution list every Monday morning."
              />
            </FormField>
          </Step>
        </div>

        <ScheduleSummary
          form={form}
          template={template}
          fallbackTitle={templateTitle({ report_type: form.report_type, custom_report_id: form.custom_report_id }, catalog.data)}
          coverage={coverage}
          nextRun={runs[0] ?? null}
          branding={zb}
          now={now}
          errors={errors}
          warnings={warnings}
          channelNames={new Map((channels.data ?? []).map((c) => [c.id, c.name]))}
        />
      </form>

      <DialogFooter className="mb-0 items-center sm:mb-0 sm:justify-between">
        <p className="mr-auto hidden text-xs text-muted sm:block" aria-live="polite">
          {valid
            ? <span className="inline-flex items-center gap-1 text-success-fg"><CheckCircle2 className="h-3.5 w-3.5" aria-hidden /> Ready to save</span>
            : <span className="inline-flex items-center gap-1 text-warning-fg"><TriangleAlert className="h-3.5 w-3.5" aria-hidden /> {errors.length} {errors.length === 1 ? 'item needs' : 'items need'} attention</span>}
        </p>
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" onClick={onClose} disabled={pending}>Cancel</Button>
          <Button type="button" variant="outline" className="hidden sm:inline-flex" disabled={!valid || pending} onClick={() => void submit(true)}>
            <Play className="h-4 w-4" aria-hidden /> Save and run now
          </Button>
          <Button type="submit" form="report-schedule-form" disabled={!valid || pending}>
            {pending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Check className="h-4 w-4" aria-hidden />}
            {isEdit ? 'Save schedule' : 'Create schedule'}
          </Button>
        </div>
      </DialogFooter>
    </DialogContent>
  )
}

/* ------------------------------------------------------------------ */
/*  Layout                                                             */
/* ------------------------------------------------------------------ */

function Step({ n, title, description, children }: { n: number; title: string; description?: string; children: ReactNode }) {
  const id = useId()
  return (
    <section aria-labelledby={id} className="space-y-3">
      <div className="flex items-start gap-3">
        <span
          aria-hidden
          className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xs font-semibold tabular-nums text-primary"
        >
          {n}
        </span>
        <div className="min-w-0">
          <h3 id={id} className="text-sm font-semibold text-text">{title}</h3>
          {description && <p className="text-xs text-muted">{description}</p>}
        </div>
      </div>
      <div className="space-y-3 sm:pl-9">{children}</div>
    </section>
  )
}

type Choice = { id: string; label: string; hint?: string; aria?: string }

/** Single-choice radio group with arrow-key navigation (roving tab stop). */
function ChoiceGroup({
  id, labelledBy, value, options, onChange, variant,
}: {
  id?: string
  labelledBy: string
  value: string
  options: Choice[]
  onChange: (id: string) => void
  variant: 'segmented' | 'chips' | 'cards'
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])
  const current = Math.max(0, options.findIndex((o) => o.id === value))
  const move = (i: number) => {
    const n = (i + options.length) % options.length
    onChange(options[n].id)
    refs.current[n]?.focus()
  }
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); move(i + 1) }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); move(i - 1) }
    else if (e.key === 'Home') { e.preventDefault(); move(0) }
    else if (e.key === 'End') { e.preventDefault(); move(options.length - 1) }
  }
  return (
    <div
      role="radiogroup"
      aria-labelledby={labelledBy}
      className={cn(
        variant === 'segmented' && 'inline-flex rounded-lg border border-border bg-surface2/60 p-0.5',
        variant === 'chips' && 'flex flex-wrap gap-1.5',
        variant === 'cards' && 'grid grid-cols-2 gap-2 xl:grid-cols-4',
      )}
    >
      {options.map((o, i) => {
        const on = o.id === value
        return (
          <button
            key={o.id}
            id={i === current ? id : undefined}
            ref={(el) => { refs.current[i] = el }}
            type="button"
            role="radio"
            aria-checked={on}
            aria-label={o.aria}
            tabIndex={i === current ? 0 : -1}
            onClick={() => onChange(o.id)}
            onKeyDown={(e) => onKey(e, i)}
            className={cn(
              'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
              variant === 'segmented' && cn(
                'min-w-[84px] rounded-md px-3 py-1.5 text-sm font-medium',
                on ? 'bg-surface text-text shadow-sm' : 'text-muted hover:text-text',
              ),
              variant === 'chips' && cn(
                'h-8 min-w-[48px] rounded-md border px-2.5 text-sm font-medium',
                on ? 'border-primary bg-primary/10 text-primary' : 'border-border bg-surface text-text2 hover:border-primary/40 hover:text-text',
              ),
              variant === 'cards' && cn(
                'flex flex-col items-start rounded-lg border px-3 py-2 text-left',
                on ? 'border-primary bg-primary/5 ring-1 ring-primary/40' : 'border-border bg-surface hover:border-primary/40',
              ),
            )}
          >
            {variant === 'cards' ? (
              <>
                <span className="flex w-full items-center justify-between gap-2 text-sm font-medium text-text">
                  {o.label}
                  {on && <CheckCircle2 className="h-3.5 w-3.5 text-primary" aria-hidden />}
                </span>
                {o.hint && <span className="mt-0.5 text-[11px] leading-snug text-muted">{o.hint}</span>}
              </>
            ) : o.label}
          </button>
        )
      })}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Template picker                                                    */
/* ------------------------------------------------------------------ */

function TemplatePicker({
  id, catalog, options, value, fallbackTitle, loading, invalid, onChange,
}: {
  id: string
  catalog: ReportCatalog | undefined
  options: TemplateOption[]
  value: string
  fallbackTitle: string
  loading: boolean
  invalid: boolean
  onChange: (o: TemplateOption) => void
}) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const listId = useId()
  const selected = options.find((o) => o.value === value) ?? null

  const groups = useMemo(() => {
    const pool = options.filter((o) => o.available || o.value === value)
    return groupTemplateOptions(searchTemplateOptions(pool, q), catalog)
  }, [options, value, q, catalog])
  const flat = useMemo(() => groups.flatMap((g) => g.options), [groups])
  const optId = (i: number) => `${listId}-opt-${i}`

  useEffect(() => {
    if (!open) return
    const i = flat.findIndex((o) => o.value === value)
    setActive(i >= 0 && !q ? i : 0)
  }, [open, q]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (open) document.getElementById(optId(active))?.scrollIntoView({ block: 'nearest' })
  }, [active, open]) // eslint-disable-line react-hooks/exhaustive-deps

  const pick = (o: TemplateOption) => { onChange(o); setOpen(false); setQ('') }
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(flat.length - 1, a + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(0, a - 1)) }
    else if (e.key === 'Home' && e.ctrlKey) { e.preventDefault(); setActive(0) }
    else if (e.key === 'End' && e.ctrlKey) { e.preventDefault(); setActive(flat.length - 1) }
    else if (e.key === 'Enter') { e.preventDefault(); if (flat[active]) pick(flat[active]) }
  }

  const Icon = reportIcon(selected?.icon)
  let index = -1
  return (
    <Popover.Root open={open} onOpenChange={(o) => { setOpen(o); if (!o) setQ('') }}>
      <div className="space-y-1.5">
        <span className={GROUP_LABEL} id={`${id}-label`}>Template<span className="ml-0.5 text-danger">*</span></span>
        <Popover.Trigger asChild>
          <button
            id={id}
            type="button"
            disabled={loading}
            aria-haspopup="listbox"
            aria-expanded={open}
            aria-labelledby={`${id}-label ${id}`}
            aria-invalid={invalid || undefined}
            className={cn(
              'flex min-h-[44px] w-full items-center gap-3 rounded-md border bg-surface px-3 py-2 text-left text-sm hover:bg-surface2/60',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 disabled:cursor-wait disabled:opacity-60',
              invalid ? 'border-danger' : 'border-border',
            )}
          >
            {loading ? (
              <span className="flex items-center gap-2 text-muted"><Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Loading templates…</span>
            ) : selected ? (
              <>
                <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <Icon className="h-4 w-4" aria-hidden />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium text-text">{selected.title}</span>
                  <span className="block truncate text-xs text-muted">{selected.categoryTitle}</span>
                </span>
              </>
            ) : value ? (
              <span className="flex min-w-0 flex-1 items-center gap-2 text-warning-fg">
                <TriangleAlert className="h-4 w-4 shrink-0" aria-hidden />
                <span className="truncate">{fallbackTitle || 'Template'} — not available</span>
              </span>
            ) : (
              <span className="flex-1 text-muted">Choose a report template…</span>
            )}
            <ChevronsUpDown className="ml-auto h-4 w-4 shrink-0 text-muted" aria-hidden />
          </button>
        </Popover.Trigger>
      </div>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={4}
          collisionPadding={12}
          className="z-50 flex max-h-[min(460px,var(--radix-popover-content-available-height))] w-[var(--radix-popover-trigger-width)] min-w-[300px] flex-col overflow-hidden rounded-md border border-border bg-surface shadow-elevated"
        >
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <Search className="h-4 w-4 shrink-0 text-muted" aria-hidden />
            <input
              autoFocus
              role="combobox"
              aria-expanded
              aria-controls={listId}
              aria-autocomplete="list"
              aria-activedescendant={flat[active] ? optId(active) : undefined}
              aria-label="Search report templates"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={onKey}
              placeholder="Search templates"
              className="h-7 w-full bg-transparent text-sm text-text outline-none placeholder:text-muted"
            />
          </div>
          <div id={listId} role="listbox" aria-label="Report templates" className="min-h-0 flex-1 overflow-y-auto p-1">
            {flat.length === 0 && (
              <div className="px-3 py-6 text-center text-sm text-muted">No template matches “{q}”.</div>
            )}
            {groups.map((g) => (
              <div key={g.id} role="group" aria-labelledby={`${listId}-g-${g.id}`}>
                <div id={`${listId}-g-${g.id}`} className="sticky top-0 bg-surface px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-muted">
                  {g.title}
                </div>
                {g.options.map((o) => {
                  index += 1
                  const i = index
                  const OIcon = reportIcon(o.icon)
                  const isSel = o.value === value
                  return (
                    <div
                      key={o.value}
                      id={optId(i)}
                      role="option"
                      aria-selected={isSel}
                      onMouseEnter={() => setActive(i)}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => pick(o)}
                      className={cn(
                        'flex cursor-pointer items-start gap-2.5 rounded-md px-2 py-2',
                        i === active && 'bg-surface2',
                      )}
                    >
                      <OIcon className={cn('mt-0.5 h-4 w-4 shrink-0', isSel ? 'text-primary' : 'text-muted')} aria-hidden />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-1.5">
                          <span className={cn('truncate text-sm font-medium', isSel ? 'text-primary' : 'text-text')}>{o.title}</span>
                          {o.engine === 'legacy' && <span className="shrink-0 rounded border border-border px-1 text-[10px] text-muted">Classic</span>}
                          {!o.available && <span className="shrink-0 rounded border border-warning/40 px-1 text-[10px] text-warning-fg">Unavailable</span>}
                        </span>
                        {o.description && <span className="line-clamp-1 text-xs text-muted">{o.description}</span>}
                        {o.custom && o.owner && <span className="block text-[11px] text-muted">by {o.owner}</span>}
                      </span>
                      {isSel && <Check className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden />}
                    </div>
                  )
                })}
              </div>
            ))}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}

function TemplateDetails({ template }: { template: TemplateOption }) {
  const scopes = template.filterable.map((f) => (f === 'devices' ? 'devices' : f === 'applications' ? 'application' : 'NetFlow'))
  return (
    <div className="rounded-md bg-surface2/50 px-3 py-2 text-xs text-text2">
      {template.description && <p className="text-sm text-text2">{template.description}</p>}
      <p className="mt-1 text-muted">
        {template.custom ? 'Organisation template' : template.engine === 'legacy' ? 'Classic report' : 'Built-in template'}
        {' · '}{template.categoryTitle}
        {scopes.length > 0 && <> · can be limited by {scopes.join(', ')}</>}
      </p>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Next runs                                                          */
/* ------------------------------------------------------------------ */

function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

function NextRunsPreview({ runs, branding, now }: { runs: Date[]; branding: Branding; now: number }) {
  const tz = branding.timezone
  const local = browserZone()
  const showLocal = runs[0] && zoneLabel(local, runs[0]) !== zoneLabel(tz, runs[0])
  return (
    <div className="rounded-md border border-border bg-surface2/40 px-3 py-2.5">
      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-text2">
        <Clock className="h-3.5 w-3.5 text-muted" aria-hidden /> Next runs
      </div>
      {runs.length === 0 ? (
        <p className="text-sm text-muted">No upcoming run could be calculated.</p>
      ) : (
        <ol className="space-y-1" aria-label="Next three runs">
          {runs.map((r, i) => (
            <li key={r.getTime()} className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 text-sm">
              <span className="tabular-nums text-text">
                {runLabel(r, branding)} <span className="text-xs text-muted">{zoneLabel(tz, r)}</span>
                {zoneLabel(tz, r) !== 'UTC' && (
                  <span className="ml-1.5 text-xs text-muted">· {utcClock(r)}</span>
                )}
                {i > 0 && crossesClockChange(tz, r, runs[i - 1]) && (
                  <span className="ml-2 rounded border border-warning/40 px-1 text-[10px] text-warning-fg">Clock change</span>
                )}
              </span>
              <span className="text-xs tabular-nums text-muted">{relativeTo(r, now)}</span>
            </li>
          ))}
        </ol>
      )}
      {showLocal && (
        <p className="mt-1.5 text-[11px] text-muted">
          Your time: {formatDateTime(runs[0], { ...branding, timezone: local }, { zone: true })}
        </p>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Delivery                                                           */
/* ------------------------------------------------------------------ */

function ChannelPicker({
  selected, query, onChange,
}: {
  selected: string[]
  query: ReturnType<typeof useNotificationChannels>
  onChange: (ids: string[]) => void
}) {
  const all = query.data ?? []
  const byId = new Map(all.map((c) => [c.id, c]))
  const shown = [
    ...all.filter((c) => c.enabled !== false || selected.includes(c.id)),
    ...selected.filter((id) => !byId.has(id)).map((id) => ({ id, name: 'Deleted channel', type: 'missing', enabled: false })),
  ]
  const toggle = (id: string) => onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id])
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <span className={GROUP_LABEL} id="sched-channels-label">Notification channels</span>
        <span className="text-[11px] tabular-nums text-muted">{selected.length} selected</span>
      </div>
      {query.isLoading ? (
        <div className="flex gap-2"><Skeleton className="h-9 w-32" /><Skeleton className="h-9 w-28" /><Skeleton className="h-9 w-36" /></div>
      ) : query.isError && !query.data ? (
        <QueryFailure compact error={query.error} what="notification channels" onRetry={() => void query.refetch()} retrying={query.isFetching} />
      ) : shown.length === 0 ? (
        <div className="flex items-start gap-2 rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs text-text2">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />
          <span>
            No notification channels yet. Add direct email recipients below, or{' '}
            <Link to="/channels" className="font-medium text-primary hover:underline">create a channel</Link>.
          </span>
        </div>
      ) : (
        <div id="sched-channels" role="group" aria-labelledby="sched-channels-label" className="flex flex-wrap gap-2">
          {shown.map((c) => {
            const on = selected.includes(c.id)
            const missing = c.type === 'missing'
            return (
              <button
                key={c.id}
                type="button"
                aria-pressed={on}
                onClick={() => toggle(c.id)}
                className={cn(
                  'inline-flex h-9 items-center gap-2 rounded-md border px-3 text-sm transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
                  on ? 'border-primary bg-primary/10 text-text' : 'border-border bg-surface text-text2 hover:border-primary/40 hover:text-text',
                  missing && 'border-danger/40 text-danger-fg',
                )}
              >
                {c.type === 'email' ? <Mail className="h-3.5 w-3.5 text-muted" aria-hidden /> : <Bell className="h-3.5 w-3.5 text-muted" aria-hidden />}
                <span className="max-w-[180px] truncate">{c.name}</span>
                {!missing && (
                  <span className="rounded bg-surface2 px-1 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted">{c.type}</span>
                )}
                {c.enabled === false && !missing && <span className="text-[11px] text-muted">disabled</span>}
                {on && <Check className="h-3.5 w-3.5 text-primary" aria-hidden />}
              </button>
            )
          })}
        </div>
      )}
      <p className="text-[11px] text-muted">
        Email channels receive the attachment; chat and webhook channels receive a summary with a link to the report.
      </p>
    </div>
  )
}

function RecipientInput({ id, value, onChange }: { id: string; value: string[]; onChange: (r: string[]) => void }) {
  const [text, setText] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const hintId = `${id}-hint`
  const msgId = `${id}-msg`

  const commit = (raw: string) => {
    if (!raw.trim()) { setMessage(null); return }
    const r = addRecipients(value, raw, MAX_RECIPIENTS)
    if (r.next.length !== value.length) onChange(r.next)
    const bits: string[] = []
    if (r.invalid.length) bits.push(`Not a valid email address: ${r.invalid.join(', ')}`)
    if (r.duplicates.length) bits.push(`Already added: ${r.duplicates.join(', ')}`)
    if (r.overflow.length) bits.push(`The limit is ${MAX_RECIPIENTS} recipients; not added: ${r.overflow.length}`)
    setMessage(bits.length ? bits.join('. ') + '.' : null)
    setText(r.invalid.join(', '))
  }
  const remove = (addr: string) => onChange(value.filter((x) => x !== addr))

  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <Label htmlFor={id}>Email recipients</Label>
        <span className={cn('text-[11px] tabular-nums', value.length >= MAX_RECIPIENTS ? 'text-warning-fg' : 'text-muted')}>
          {value.length} / {MAX_RECIPIENTS}
        </span>
      </div>
      <div
        className={cn(
          'flex min-h-[40px] flex-wrap items-center gap-1.5 rounded-md border bg-surface px-2 py-1.5',
          'focus-within:border-primary focus-within:ring-2 focus-within:ring-primary/40',
          message ? 'border-danger' : 'border-border',
        )}
        onClick={() => document.getElementById(id)?.focus()}
      >
        {value.length > 0 && (
          <ul className="contents" aria-label="Email recipients added">
            {value.map((addr) => (
              <li key={addr} className="inline-flex max-w-full items-center gap-1 rounded-md border border-border bg-surface2 py-0.5 pl-2 pr-0.5 text-xs text-text">
                <Mail className="h-3 w-3 shrink-0 text-muted" aria-hidden />
                <span className="break-all">{addr}</span>
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); remove(addr) }}
                  aria-label={`Remove ${addr}`}
                  className="rounded p-0.5 text-muted hover:bg-surface3 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        )}
        <input
          id={id}
          type="email"
          inputMode="email"
          autoComplete="off"
          multiple
          value={text}
          disabled={value.length >= MAX_RECIPIENTS && !text}
          aria-describedby={message ? `${msgId} ${hintId}` : hintId}
          aria-invalid={message ? true : undefined}
          onChange={(e) => { setText(e.target.value); if (message) setMessage(null) }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ',' || e.key === ';' || (e.key === ' ' && text.trim())) {
              e.preventDefault()
              commit(text)
            } else if (e.key === 'Backspace' && !text && value.length) {
              remove(value[value.length - 1])
            }
          }}
          onPaste={(e) => {
            const pasted = e.clipboardData.getData('text')
            if (/[\s,;]/.test(pasted.trim())) { e.preventDefault(); commit(`${text} ${pasted}`) }
          }}
          onBlur={() => commit(text)}
          placeholder={value.length ? 'Add another address' : 'name@example.com'}
          className="h-7 min-w-[180px] flex-1 border-0 bg-transparent px-1 text-sm text-text shadow-none outline-none ring-0 placeholder:text-muted focus:outline-none focus:ring-0 focus-visible:outline-none disabled:cursor-not-allowed"
        />
      </div>
      {message && <p id={msgId} role="alert" className="text-[11px] text-danger-fg">{message}</p>}
      <p id={hintId} className="text-[11px] text-muted">
        People outside the notification channels. Press Enter or comma after each address, or paste a list. Up to {MAX_RECIPIENTS}.
      </p>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Summary                                                            */
/* ------------------------------------------------------------------ */

function ScheduleSummary({
  form, template, fallbackTitle, coverage, nextRun, branding, now, errors, warnings, channelNames,
}: {
  form: ScheduleForm
  template: TemplateOption | null
  fallbackTitle: string
  coverage: string
  nextRun: Date | null
  branding: Branding
  now: number
  errors: Issue[]
  warnings: Issue[]
  channelNames: Map<string, string>
}) {
  const nCh = form.notify_channels.length
  const nEm = form.recipients.length
  const delivery = !nCh && !nEm
    ? 'Not delivered — the run history keeps each report.'
    : `${FORMAT_META[form.format]?.label ?? form.format} to ${[
        nCh ? `${nCh} channel${nCh === 1 ? '' : 's'}` : '',
        nEm ? `${nEm} email recipient${nEm === 1 ? '' : 's'}` : '',
      ].filter(Boolean).join(' and ')}`
  const focus = (field: string) => document.getElementById(FIELD_ID[field] ?? '')?.focus()
  return (
    <aside aria-label="Schedule summary" className="lg:sticky lg:top-0 lg:self-start">
      <div className="space-y-4 rounded-lg border border-border bg-surface2/40 p-4">
        <div>
          <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Summary</div>
          <div className="mt-1 text-sm font-semibold text-text">{form.name.trim() || 'Untitled schedule'}</div>
          <div className="mt-0.5 flex items-center gap-1.5 text-xs">
            {form.enabled
              ? <span className="inline-flex items-center gap-1 text-success-fg"><CheckCircle2 className="h-3.5 w-3.5" aria-hidden /> Enabled</span>
              : <span className="inline-flex items-center gap-1 text-muted"><Info className="h-3.5 w-3.5" aria-hidden /> Paused</span>}
          </div>
        </div>
        <dl className="space-y-2.5 text-sm">
          <SummaryRow icon={FileText} label="Report">
            {template?.title || fallbackTitle || <span className="text-muted">Not chosen</span>}
            <span className="block text-xs text-muted">{PERIOD_LABEL[form.period] ?? form.period}</span>
          </SummaryRow>
          <SummaryRow icon={CalendarClock} label="Runs">
            {frequencyText(form, branding.time_format)}
            <span className="block text-xs tabular-nums text-muted">{localTimeLabel(form, branding.timezone, new Date(now), branding.time_format)}</span>
            {nextRun && form.enabled && (
              <span className="block text-xs tabular-nums text-muted">Next {runWithUtc(nextRun, branding)} · {relativeTo(nextRun, now)}</span>
            )}
          </SummaryRow>
          {coverage && (
            <SummaryRow icon={Clock} label="Covers">
              <span className="tabular-nums">{coverage}</span>
            </SummaryRow>
          )}
          <SummaryRow icon={Send} label="Delivery">
            {delivery}
            {nCh > 0 && (
              <span className="block truncate text-xs text-muted">
                {form.notify_channels.map((id) => channelNames.get(id) ?? 'Deleted channel').join(', ')}
              </span>
            )}
          </SummaryRow>
        </dl>
        {(errors.length > 0 || warnings.length > 0) && (
          <div className="space-y-2 border-t border-border pt-3">
            {errors.length > 0 && (
              <ul className="space-y-1" aria-label="Items to fix">
                {errors.map((e, i) => (
                  <li key={`e${i}`}>
                    <button
                      type="button"
                      onClick={() => focus(e.field)}
                      className="flex w-full items-start gap-1.5 rounded text-left text-xs text-danger-fg hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                    >
                      <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden /> {e.message}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {warnings.length > 0 && (
              <ul className="space-y-1" aria-label="Warnings">
                {warnings.map((w, i) => (
                  <li key={`w${i}`} className="flex items-start gap-1.5 text-xs text-warning-fg">
                    <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden /> {w.message}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </aside>
  )
}

function SummaryRow({ icon: Icon, label, children }: { icon: typeof FileText; label: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-2">
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted" aria-hidden />
      <div className="min-w-0">
        <dt className="text-[11px] font-medium uppercase tracking-wide text-muted">{label}</dt>
        <dd className="text-text">{children}</dd>
      </div>
    </div>
  )
}
