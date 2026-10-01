import { useEffect, useId, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, RotateCcw, Save } from 'lucide-react'
import { api } from '@/lib/api'
import { nfKeys } from '@/lib/netflow/api'
import { apiErrorMessage } from '@/lib/netflow/status'
import { formatDateTime } from '@/lib/netflow/format'
import { displayTimeZone } from '@/lib/netflow/window'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'
import {
  DIRECTION_LABELS,
  ROLE_LABELS,
  type CountDirection,
  type ExporterRole,
  type ExporterSettings,
  type HealthExporterRow,
} from './model'

interface Draft {
  display_name: string
  role: ExporterRole
  count_direction: CountDirection
  expected_interval_s: string
  note: string
}

function toDraft(s: ExporterSettings | null): Draft {
  return {
    display_name: s?.display_name ?? '',
    role: s?.role ?? 'edge',
    count_direction: s?.count_direction ?? 'auto',
    expected_interval_s: s?.expected_interval_s != null ? String(s.expected_interval_s) : '',
    note: s?.note ?? '',
  }
}

/** Validation messages for a draft (empty object = valid). */
export function validateSettingsDraft(d: Draft): Partial<Record<keyof Draft, string>> {
  const err: Partial<Record<keyof Draft, string>> = {}
  if (d.display_name.trim().length > 120) err.display_name = 'At most 120 characters'
  if (d.expected_interval_s.trim()) {
    const n = Number(d.expected_interval_s)
    if (!Number.isInteger(n) || n < 1 || n > 86400) err.expected_interval_s = 'Whole seconds between 1 and 86400'
  }
  if (d.note.length > 2000) err.note = 'At most 2000 characters'
  return err
}

const inputCls =
  'h-9 w-full rounded-md border border-border bg-surface px-3 text-sm text-text focus:outline-none focus:ring-2 focus:ring-primary/40 disabled:opacity-60'

/**
 * Exporter settings (FE-12): display name, counting role and direction, expected export
 * interval. The role decides whether `count=edge` counts the exporter; the expected interval
 * replaces the learned cadence for the receiving/late/silent thresholds.
 */
export function ExporterSettingsForm({ exporter, settings, row }: {
  exporter: string
  settings: ExporterSettings | null
  row?: HealthExporterRow | null
}) {
  const can = useCan()
  const canManage = can('netflow.manage')
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Draft>(() => toDraft(settings))
  useEffect(() => setDraft(toDraft(settings)), [settings])
  const errors = validateSettingsDraft(draft)
  const dirty = JSON.stringify(draft) !== JSON.stringify(toDraft(settings))
  const ids = { name: useId(), role: useId(), dir: useId(), interval: useId(), note: useId() }
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }))
  const enc = encodeURIComponent(exporter)

  const save = useMutation({
    mutationFn: async () =>
      api.put(`/netflow/exporters/${enc}/settings`, {
        display_name: draft.display_name.trim() || null,
        role: draft.role,
        count_direction: draft.count_direction,
        expected_interval_s: draft.expected_interval_s.trim() ? Number(draft.expected_interval_s) : null,
        note: draft.note.trim() || null,
      }),
    onSuccess: () => {
      toast.success('Exporter settings saved')
      qc.invalidateQueries({ queryKey: nfKeys.all })
    },
    onError: (e) => toast.error('Could not save the settings', apiErrorMessage(e)),
  })
  const reset = useMutation({
    mutationFn: async () => api.delete(`/netflow/exporters/${enc}/settings`),
    onSuccess: () => {
      toast.success('Settings reset to defaults')
      qc.invalidateQueries({ queryKey: nfKeys.all })
    },
    onError: (e) => toast.error('Could not reset the settings', apiErrorMessage(e)),
  })
  const disabled = !canManage || save.isPending || reset.isPending
  const tz = displayTimeZone()

  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault()
        if (!Object.keys(errors).length && dirty) save.mutate()
      }}
    >
      {!canManage && (
        <p className="rounded-md border border-border bg-surface2/40 px-3 py-2 text-xs text-text2">
          Changing exporter settings needs the NetFlow manage permission.
        </p>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <label htmlFor={ids.name} className="text-xs font-medium text-text">Display name</label>
          <input id={ids.name} className={inputCls} value={draft.display_name} maxLength={120} disabled={disabled}
            onChange={(e) => set('display_name', e.target.value)} placeholder="e.g. WAN edge — Riyadh" aria-invalid={!!errors.display_name} />
          {errors.display_name && <p className="text-[11px] text-danger-fg">{errors.display_name}</p>}
        </div>
        <div className="space-y-1">
          <label htmlFor={ids.interval} className="text-xs font-medium text-text">Expected export interval (seconds)</label>
          <input id={ids.interval} className={inputCls} inputMode="numeric" value={draft.expected_interval_s} disabled={disabled}
            onChange={(e) => set('expected_interval_s', e.target.value)}
            placeholder={row?.learned_interval_s ? `learned: ${Math.round(row.learned_interval_s)}` : 'learned automatically'}
            aria-invalid={!!errors.expected_interval_s} aria-describedby={`${ids.interval}-h`} />
          <p id={`${ids.interval}-h`} className="text-[11px] text-text2">
            {errors.expected_interval_s ? <span className="text-danger-fg">{errors.expected_interval_s}</span> :
              'Leave empty to use the cadence learned from this exporter. Late after 2 intervals + 1 min, silent after 10 (at least 2 and 15 min).'}
          </p>
        </div>
        <div className="space-y-1">
          <label htmlFor={ids.role} className="text-xs font-medium text-text">Counting role</label>
          <select id={ids.role} className={inputCls} value={draft.role} disabled={disabled}
            onChange={(e) => set('role', e.target.value as ExporterRole)} aria-describedby={`${ids.role}-h`}>
            {(Object.keys(ROLE_LABELS) as ExporterRole[]).map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
          </select>
          <p id={`${ids.role}-h`} className="text-[11px] text-text2">
            Edge counting (the “Counted once at the edge” mode) adds up only edge exporters, so traffic seen again on core
            routers is not counted twice. All observations stay stored and visible.
          </p>
        </div>
        <div className="space-y-1">
          <label htmlFor={ids.dir} className="text-xs font-medium text-text">Direction counted in edge mode</label>
          <select id={ids.dir} className={inputCls} value={draft.count_direction} disabled={disabled}
            onChange={(e) => set('count_direction', e.target.value as CountDirection)} aria-describedby={`${ids.dir}-h`}>
            {(Object.keys(DIRECTION_LABELS) as CountDirection[]).map((d) => <option key={d} value={d}>{DIRECTION_LABELS[d]}</option>)}
          </select>
          <p id={`${ids.dir}-h`} className="text-[11px] text-text2">
            Automatic keeps ingress-monitored records when the exporter reports both directions, otherwise every record.
          </p>
        </div>
      </div>
      <div className="space-y-1">
        <label htmlFor={ids.note} className="text-xs font-medium text-text">Note</label>
        <textarea id={ids.note} rows={2} maxLength={2000} value={draft.note} disabled={disabled}
          onChange={(e) => set('note', e.target.value)}
          className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-primary/40 disabled:opacity-60" />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" size="sm" disabled={disabled || !dirty || Object.keys(errors).length > 0}>
          {save.isPending ? <Loader2 className="animate-spin" aria-hidden /> : <Save aria-hidden />} Save settings
        </Button>
        {settings && (
          <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => reset.mutate()}>
            <RotateCcw aria-hidden /> Reset to defaults
          </Button>
        )}
        {settings?.updated_at && (
          <span className="text-[11px] text-text2">
            Last changed {formatDateTime(settings.updated_at, tz, { seconds: false })}
            {settings.updated_by_name ? ` by ${settings.updated_by_name}` : ''}
          </span>
        )}
      </div>
    </form>
  )
}
