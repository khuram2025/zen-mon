/**
 * Report settings (GET / PUT /api/v1/reports/settings): link expiry, run and
 * attachment retention, the attachment size limit and delivery retries.
 * Reading needs reports.view; saving needs reports.manage.
 */
import { useState } from 'react'
import { AlertCircle, Check, Loader2, Settings2 } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Skeleton } from '@/components/ui/Skeleton'
import { QueryFailure } from '@/components/ui/QueryState'
import { toast } from '@/components/ui/Toast'
import { useReportSettings, useSaveReportSettings } from './api'
import {
  REPORT_SETTING_DEFAULTS, REPORT_SETTING_FIELDS, retryPlanText, validateReportSettings, type ReportSettings,
} from './model'
import { scheduleErrorText } from './ui'

export function ReportSettingsDialog({ open, onOpenChange, canEdit }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  canEdit: boolean
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && <SettingsEditor canEdit={canEdit} onClose={() => onOpenChange(false)} />}
    </Dialog>
  )
}

type Draft = Record<keyof ReportSettings, string>

const toDraft = (s: Partial<ReportSettings> | undefined): Draft =>
  Object.fromEntries(REPORT_SETTING_FIELDS.map((f) => [f.key, String(s?.[f.key] ?? REPORT_SETTING_DEFAULTS[f.key])])) as Draft

function SettingsEditor({ canEdit, onClose }: { canEdit: boolean; onClose: () => void }) {
  const q = useReportSettings()
  const save = useSaveReportSettings()
  const [draft, setDraft] = useState<Draft | null>(null)
  const [serverError, setServerError] = useState<string | null>(null)
  const value = draft ?? (q.data ? toDraft(q.data) : null)
  const errors = value ? validateReportSettings(value) : {}
  const valid = Object.keys(errors).length === 0
  const dirty = !!draft && !!q.data && REPORT_SETTING_FIELDS.some((f) => Number(draft[f.key]) !== q.data![f.key])

  const submit = async () => {
    if (!value || !valid) return
    setServerError(null)
    const body = Object.fromEntries(REPORT_SETTING_FIELDS.map((f) => [f.key, Number(value[f.key])])) as ReportSettings
    try {
      await save.mutateAsync(body)
      toast.success('Report settings saved')
      onClose()
    } catch (e) {
      setServerError(scheduleErrorText(e))
    }
  }

  return (
    <DialogContent className="max-w-lg">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2"><Settings2 className="h-5 w-5 text-primary" aria-hidden /> Report settings</DialogTitle>
        <DialogDescription>
          How long report links work, how long runs and attachments are kept, and how delivery handles large attachments and failures.
          These apply to every schedule.
        </DialogDescription>
      </DialogHeader>
      {serverError && (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger-fg">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <div><span className="font-medium">The settings were not saved.</span> {serverError}</div>
        </div>
      )}
      {q.isLoading ? (
        <div className="space-y-3" aria-busy="true" aria-label="Loading report settings">
          {REPORT_SETTING_FIELDS.map((f) => <Skeleton key={f.key} className="h-14 w-full" />)}
        </div>
      ) : q.isError && !q.data ? (
        <QueryFailure error={q.error} what="the report settings" onRetry={() => void q.refetch()} retrying={q.isFetching} />
      ) : value && (
        <form
          id="report-settings-form"
          noValidate
          onSubmit={(e) => { e.preventDefault(); void submit() }}
          className="space-y-4"
        >
          {REPORT_SETTING_FIELDS.map((f) => (
            <FormField
              key={f.key}
              label={f.label}
              hint={f.key === 'retry_attempts' && !errors[f.key] ? `${retryPlanText(Number(value[f.key]))} ${f.hint}` : f.hint}
              error={errors[f.key] ?? null}
            >
              <div className="flex items-center gap-2">
                <Input
                  id={`report-setting-${f.key}`}
                  type="number"
                  inputMode="numeric"
                  min={f.min}
                  max={f.max}
                  step={1}
                  value={value[f.key]}
                  disabled={!canEdit}
                  aria-invalid={errors[f.key] ? true : undefined}
                  onChange={(e) => setDraft({ ...value, [f.key]: e.target.value })}
                  className="w-28 tabular-nums"
                />
                <span className="text-sm text-muted">{f.unit}</span>
                {f.key === 'link_expiry_days' && value[f.key].trim() === '0' && <span className="text-xs text-warning-fg">Links never expire</span>}
              </div>
            </FormField>
          ))}
          {!canEdit && <p className="text-xs text-muted">Viewing only: changing these needs the “Manage reports” permission.</p>}
        </form>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose} disabled={save.isPending}>{canEdit ? 'Cancel' : 'Close'}</Button>
        {canEdit && (
          <Button type="submit" form="report-settings-form" disabled={!value || !valid || !dirty || save.isPending}>
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Check className="h-4 w-4" aria-hidden />}
            Save settings
          </Button>
        )}
      </DialogFooter>
    </DialogContent>
  )
}
