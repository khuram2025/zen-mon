import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { History, ListFilter, Loader2, Lock, ScrollText, Timer } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import { QueryFailure } from '@/components/ui/QueryState'
import { Switch } from '@/components/ui/Switch'
import { toast } from '@/components/ui/Toast'
import { describeFailure } from '@/lib/access'
import { cn } from '@/lib/utils'
import { softwareApi, softwareKeys, type SettingsResponse } from '@/pages/inventory/softwareSettings'
import {
  diffSettings, draftDirty, draftFromSettings, FIELD_LABELS, formStatus, freshnessPreview, helpFor, hiddenSummary, isEmptyPatch,
  normaliseWhole, previewSettings, saveToast, toggleHidden, validateDraft, type DraftField, type SettingsDraft,
} from './settingsModel'
import { SettingsEditorSkeleton } from './SettingsEditorSkeleton'
import { categoryLabel } from './softwareModel'

/** One number of the form: label, input, unit, and under it the limits as help text or the reason the value cannot be saved. */
function NumberField({
  field, unit, value, onChange, onSettle, help, error, disabled, className,
}: {
  field: DraftField
  unit: string
  value: string
  onChange: (value: string) => void
  /** The field was left: show the number in its canonical spelling (`024` becomes `24`). */
  onSettle: () => void
  help: string
  error?: string
  disabled: boolean
  className?: string
}) {
  const id = useId()
  return (
    <div className={cn('space-y-1.5', className)} data-field={field}>
      <Label htmlFor={id} className="normal-case tracking-normal text-[12px] font-medium text-text2">{FIELD_LABELS[field]}</Label>
      <div className="flex items-center gap-2">
        <Input
          id={id}
          inputMode="numeric"
          autoComplete="off"
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          onBlur={onSettle}
          aria-invalid={error ? true : undefined}
          aria-describedby={`${id}-help`}
          // A view-only role reads these numbers: they are the page's content, not a control to dim. The shared Input halves
          // a disabled field's opacity (3.4:1 in the light theme); here it keeps full contrast on a muted fill (FV1-N4).
          className={cn('w-28 tabular-nums', disabled && 'disabled:bg-surface2 disabled:opacity-100', error && 'border-danger focus-visible:border-danger focus-visible:ring-danger/30')}
        />
        <span className="text-xs text-muted">{unit}</span>
      </div>
      <p id={`${id}-help`} role={error ? 'alert' : undefined} className={cn('text-[11px] leading-4', error ? 'text-danger-fg' : 'text-muted')}>{error ?? help}</p>
    </div>
  )
}

function Section({ icon, title, description, children }: { icon: ReactNode; title: string; description: string; children: ReactNode }) {
  const id = useId()
  return (
    <Card className="min-w-0">
      <section aria-labelledby={id}>
        <div className="flex items-start gap-3 border-b border-border px-4 py-3">
          <span className="mt-0.5 text-muted" aria-hidden>{icon}</span>
          <div className="min-w-0">
            <h3 id={id} className="text-sm font-semibold tracking-tight">{title}</h3>
            <p className="mt-0.5 text-xs text-muted">{description}</p>
          </div>
        </div>
        <div className="space-y-4 p-4">{children}</div>
      </section>
    </Card>
  )
}

/**
 * The Data & retention form (doc 03 section 3.9, contract 3.13): freshness targets per kind, the stale age, how
 * long change history is kept, the snapshot hour, trends, and the software categories hidden by default. Numbers
 * are checked as they are typed (whole numbers inside the server's limits, shown as help text), Save sends only
 * what changed, a success toast names the effect, and a refusal from the server is shown in its own words. For a
 * user who holds only inventory.view every control is disabled and there is nothing to save.
 */
export function SettingsEditor({ canManage, enabled = true }: { canManage: boolean; enabled?: boolean }) {
  const qc = useQueryClient()
  const query = useQuery({ queryKey: softwareKeys.settings, queryFn: softwareApi.settings, staleTime: 30_000, enabled })
  const data = query.data
  // With the settings already in the cache the form is there on the first render (no flash of a skeleton).
  const [draft, setDraft] = useState<SettingsDraft | null>(() => (data ? draftFromSettings(data.settings) : null))
  const [saveError, setSaveError] = useState<string | null>(null)
  const baseline = useRef<SettingsResponse['settings'] | null>(data ? data.settings : null)

  // Adopt what the server says, unless the person is in the middle of editing it.
  useEffect(() => {
    if (!data) return
    setDraft((current) => (current === null || (baseline.current !== null && !draftDirty(current, baseline.current)) ? draftFromSettings(data.settings) : current))
    baseline.current = data.settings
  }, [data])

  const save = useMutation({
    mutationFn: (patch: ReturnType<typeof diffSettings>) => softwareApi.saveSettings(patch),
    onSuccess: (res) => {
      setSaveError(null)
      qc.setQueryData(softwareKeys.settings, { ...res, changed: [], adjusted: [] })
      baseline.current = res.settings
      setDraft(draftFromSettings(res.settings))
      // The summary, the catalog and the trends all read these settings.
      void qc.invalidateQueries({ predicate: (q) => q.queryKey[0] === 'inventory' && q.queryKey[1] !== 'settings' })
      const t = saveToast(res)
      if (t.kind === 'warning') toast.warning(t.title, t.description)
      else if (t.kind === 'info') toast.info(t.title, t.description)
      else toast.success(t.title, t.description)
    },
    // A 422 carries the reason as text; show it as the server wrote it.
    onError: (e) => setSaveError(describeFailure(e).message),
  })

  const check = useMemo(() => (data && draft ? validateDraft(draft, data.limits) : null), [data, draft])
  const dirty = !!(data && draft && draftDirty(draft, data.settings))
  const invalid = !!check && check.values === null

  if (!enabled || (query.isLoading && !data)) return <SettingsEditorSkeleton />
  if (query.isError && !data) {
    return <QueryFailure error={query.error} what="the inventory settings" onRetry={() => void query.refetch()} retrying={query.isFetching} />
  }
  if (!data || !draft || !check) return <SettingsEditorSkeleton />

  // Functional updates: two changes in quick succession (a typed value, then a click) must both survive.
  const update = (fn: (d: SettingsDraft) => SettingsDraft) => { setDraft((d) => (d ? fn(d) : d)); setSaveError(null) }
  const set = (patch: Partial<SettingsDraft>) => update((d) => ({ ...d, ...patch }))
  const locked = !canManage || save.isPending
  const submit = () => {
    if (!canManage || !check.values) return
    const patch = diffSettings(data.settings, check.values)
    if (isEmptyPatch(patch)) {
      // The form only differs in spelling (`024` for 24): say so, and show the number the way it is stored.
      setDraft(draftFromSettings(data.settings))
      toast.info('Nothing to change', 'These values are already in force.')
      return
    }
    save.mutate(patch)
  }
  const preview = previewSettings(draft, data.settings)
  const numeric = (field: DraftField, unit: string) => (
    <NumberField
      field={field}
      unit={unit}
      value={draft[field]}
      onChange={(v) => set({ [field]: v } as Partial<SettingsDraft>)}
      onSettle={() => update((d) => (normaliseWhole(d[field]) === d[field] ? d : { ...d, [field]: normaliseWhole(d[field]) }))}
      help={helpFor(field, data.limits, data.defaults)}
      error={check.errors[field]}
      disabled={locked}
    />
  )

  return (
    <form
      onSubmit={(e) => { e.preventDefault(); submit() }}
      noValidate
      aria-label="Data and retention settings"
      className="space-y-4"
      data-settings-form=""
      data-read-only={canManage ? undefined : ''}
    >
      {query.isError && (
        <div>
          <QueryFailure compact what="the inventory settings" error={query.error} onRetry={() => void query.refetch()} retrying={query.isFetching} />
          <p className="mt-1 text-[11px] text-muted">Showing the settings that loaded last; they may be out of date.</p>
        </div>
      )}
      <fieldset disabled={locked} className="grid min-w-0 gap-4 lg:grid-cols-2">
        <legend className="sr-only">Inventory data and retention</legend>
        <Section
          icon={<Timer className="h-4 w-4" />}
          title="Freshness targets"
          description="How old an asset's last inventory may be before it counts as Aging or Stale. They drive the freshness badges, the freshness filters and the line under the tabs."
        >
          <div className="grid gap-4 sm:grid-cols-2">
            {numeric('fresh_server', 'hours')}
            {numeric('fresh_device', 'hours')}
          </div>
          {numeric('stale_days', 'days')}
          <p className="rounded-md bg-surface2/60 px-3 py-2 text-xs text-text2" data-preview="freshness">{freshnessPreview(preview)}</p>
        </Section>

        <Section
          icon={<History className="h-4 w-4" />}
          title="History and trends"
          description="How long the change history is kept, when the daily snapshot is taken (15 minutes past that hour) and whether the Overview draws trends from those snapshots."
        >
          {numeric('change_retention_days', 'days')}
          {numeric('snapshot_hour', "o'clock")}
          <div className="flex items-start gap-3 rounded-md border border-border p-3">
            <Switch
              id="trends-enabled"
              checked={draft.trends_enabled}
              onCheckedChange={(on) => set({ trends_enabled: on })}
              disabled={locked}
              className="mt-0.5"
            />
            <div className="min-w-0">
              <Label htmlFor="trends-enabled" className="normal-case tracking-normal text-[13px] font-medium text-text">Show trends on the Overview</Label>
              <p className="mt-0.5 text-[11px] leading-4 text-muted">
                Sparklines appear once three days of snapshots exist. Switching this off only hides them: snapshots are still recorded, so the history is there when you switch it on again.
              </p>
            </div>
          </div>
        </Section>

        <div className="lg:col-span-2">
          <Section
            icon={<ListFilter className="h-4 w-4" />}
            title="Software catalog defaults"
            description="Categories the Software catalog and the Overview leave out unless asked. Hidden products stay stored and counted."
          >
            <fieldset className="min-w-0">
              <legend className="mb-2 text-[12px] font-medium text-text2">Hidden by default</legend>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 md:grid-cols-3 xl:grid-cols-4">
                {data.categories.map((c) => (
                  <label key={c} className={cn('flex items-center gap-2 rounded-md px-2 py-1 text-[13px] text-text', !locked && 'cursor-pointer hover:bg-surface2/60')}>
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-[rgb(var(--primary))]"
                      checked={draft.hide.includes(c)}
                      disabled={locked}
                      onChange={(e) => { const on = e.target.checked; update((d) => ({ ...d, hide: toggleHidden(d.hide, c, on, data.categories) })) }}
                    />
                    {categoryLabel(c)}
                  </label>
                ))}
              </div>
              <p className="mt-2 text-[11px] leading-4 text-muted" data-hidden-summary="">{hiddenSummary(draft.hide)}</p>
            </fieldset>
          </Section>
        </div>
      </fieldset>

      {!canManage ? null : (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-border bg-surface px-4 py-3 shadow-card" data-settings-actions="">
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" size="sm" disabled={!dirty || invalid || save.isPending}>
              {save.isPending && <Loader2 className="animate-spin" aria-hidden />}
              Save changes
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={!dirty || save.isPending}
              onClick={() => { setDraft(draftFromSettings(data.settings)); setSaveError(null) }}
            >
              Discard
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={save.isPending}
              onClick={() => { setDraft(draftFromSettings(data.defaults)); setSaveError(null) }}
              title="Fills the form with the factory values. Nothing changes until you save."
            >
              Restore defaults
            </Button>
          </div>
          <p className="text-xs text-muted" role="status" aria-live="polite" data-form-status="">{formStatus({ dirty, invalid, saving: save.isPending })}</p>
          <p className="flex items-center gap-1.5 text-xs text-muted sm:ml-auto">
            <ScrollText className="h-3.5 w-3.5 shrink-0" aria-hidden />
            Changes to these settings are recorded in the audit log.
          </p>
          {saveError && (
            <p role="alert" className="w-full rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger-fg" data-save-error="">
              The appliance did not save the settings: {saveError}
            </p>
          )}
        </div>
      )}
      {!canManage && (
        <p className="flex items-center gap-1.5 text-xs text-muted">
          <Lock className="h-3.5 w-3.5 shrink-0" aria-hidden />
          Changes to these settings are recorded in the audit log.
        </p>
      )}
    </form>
  )
}

export default SettingsEditor
