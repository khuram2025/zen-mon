import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, Lock, RotateCcw, Save } from 'lucide-react'
import { api } from '@/lib/api'
import { toast } from '@/components/ui/Toast'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { Card, CardContent } from '@/components/ui/Card'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { nfKeys, useNfQuery } from '@/lib/netflow/api'
import { apiErrorMessage } from '@/lib/netflow/status'
import { cn, relativeTime } from '@/lib/utils'
import {
  FIELD_LABEL,
  settingsPatch,
  validateParam,
  type DetectorParam,
  type DetectorSettingsRow,
  type EngineStatus,
} from './model'
import { DetectorStateChip } from './parts'

interface DetectorsResponse {
  quiet_minutes: number
  detectors: DetectorSettingsRow[]
  engine: EngineStatus
  can_manage: boolean
}

type Drafts = Record<string, Record<string, string | boolean>>

function draftValue(p: DetectorParam): string | boolean {
  if (p.kind === 'bool') return !!p.value
  if (p.kind === 'ports') return Array.isArray(p.value) ? (p.value as number[]).join(', ') : String(p.value ?? '')
  return String(p.value ?? '')
}

/** Detector thresholds, enable switches and the auto-resolve quiet period (netflow.manage). */
export function SettingsTab() {
  const q = useNfQuery<DetectorsResponse>('/netflow/detectors', null, { refetchInterval: false })
  return (
    <QueryPanel query={q} label="detection settings" skeleton="rows">
      {(res) => <SettingsForm key={JSON.stringify(res.data.detectors.map((d) => d.params.map((p) => p.value)))} data={res.data} />}
    </QueryPanel>
  )
}

function SettingsForm({ data }: { data: DetectorsResponse }) {
  const qc = useQueryClient()
  const [drafts, setDrafts] = useState<Drafts>({})
  const [enabled, setEnabled] = useState<Record<string, boolean>>({})
  const [quiet, setQuiet] = useState(String(data.quiet_minutes))
  const readOnly = !data.can_manage

  const errors: Record<string, string> = {}
  const edits: Record<string, Record<string, unknown>> = {}
  for (const d of data.detectors) {
    for (const p of d.params) {
      const raw = drafts[d.kind]?.[p.key]
      if (raw === undefined) continue
      const r = validateParam(p, raw)
      if ('error' in r) errors[`${d.kind}.${p.key}`] = r.error
      else (edits[d.kind] ??= {})[p.key] = r.value
    }
    if (enabled[d.kind] !== undefined) (edits[d.kind] ??= {}).enabled = enabled[d.kind]
  }
  const quietN = Number(quiet)
  const quietError = !Number.isInteger(quietN) || quietN < 10 || quietN > 10080 ? 'Between 10 and 10,080 minutes' : null
  const patch = settingsPatch(data.detectors, edits)
  const dirty = Object.keys(patch.detectors).length > 0 || (!quietError && quietN !== data.quiet_minutes)
  const invalid = Object.keys(errors).length > 0 || !!quietError

  const save = useMutation({
    mutationFn: async () => (await api.put('/netflow/detectors', { ...patch, quiet_minutes: quietN })).data,
    onSuccess: () => {
      toast.success('Detection settings saved', 'They apply from the next evaluated 5-minute period.')
      setDrafts({})
      setEnabled({})
      void qc.invalidateQueries({ queryKey: nfKeys.all })
    },
    onError: (e) => toast.error('Settings were not saved', apiErrorMessage(e)),
  })

  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault()
        if (!invalid && dirty) save.mutate()
      }}
    >
      {readOnly && (
        <p className="flex items-center gap-2 rounded-md border border-border bg-surface2 px-3 py-2 text-xs text-text2">
          <Lock className="h-3.5 w-3.5" aria-hidden /> Read only: changing detection settings needs the netflow.manage permission.
        </p>
      )}
      <Card>
        <CardContent className="grid gap-3 p-4 min-[640px]:grid-cols-2">
          <label className="block text-xs font-medium text-text">
            Resolve findings automatically after
            <span className="mt-1 flex items-center gap-2">
              <Input
                type="number"
                inputMode="numeric"
                className="h-9 w-28"
                value={quiet}
                onChange={(e) => setQuiet(e.target.value)}
                disabled={readOnly}
                aria-invalid={!!quietError}
                aria-describedby="nf-quiet-help"
              />
              <span className="text-text2">minutes without a new occurrence</span>
            </span>
            {quietError && <span className="mt-1 block text-danger-fg">{quietError}</span>}
            <span id="nf-quiet-help" className="mt-1 block font-normal text-text2">A resolved finding reopens if its detector matches again.</span>
          </label>
          <div className="text-xs text-text2">
            <p className="font-medium text-text">Detection engine</p>
            <p className="mt-1">
              {data.engine.status === 'never'
                ? 'Has not evaluated any traffic yet.'
                : `${data.engine.status === 'running' ? 'Running' : 'Not running'} — last evaluation ${relativeTime(data.engine.last_run_at)}.`}{' '}
              Each 5-minute period is evaluated once, two minutes after it ends, by one API worker at a time.
            </p>
          </div>
        </CardContent>
      </Card>
      <ul className="space-y-2">
        {data.detectors.map((d) => {
          const on = enabled[d.kind] ?? d.enabled
          return (
            <li key={d.kind} className="rounded-lg border border-border bg-surface p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-sm font-semibold text-text">{d.label}</h3>
                    {d.status && <DetectorStateChip state={d.status} />}
                  </div>
                  <p className="mt-0.5 text-xs text-text2">{d.description}</p>
                  <p className="mt-0.5 text-[11px] text-text2">
                    <span className="font-medium text-text">Method:</span> {d.method}
                  </p>
                  {(d.required_fields ?? []).length > 0 && (
                    <p className="text-[11px] text-text2">Needs: {(d.required_fields ?? []).map((f) => FIELD_LABEL[f] ?? f).join(', ')}</p>
                  )}
                  {d.reason && <p className="mt-0.5 text-[11px] text-warning-fg">{d.reason}</p>}
                </div>
                <label className="flex shrink-0 items-center gap-2 text-xs text-text">
                  <Switch
                    checked={on}
                    disabled={readOnly}
                    onCheckedChange={(v) => setEnabled((s) => ({ ...s, [d.kind]: v }))}
                    aria-label={`${d.label} enabled`}
                  />
                  {on ? 'On' : 'Off'}
                </label>
              </div>
              <div className="mt-2 grid grid-cols-1 gap-2 min-[480px]:grid-cols-2 lg:grid-cols-4">
                {d.params.map((p) => {
                  const id = `nf-param-${d.kind}-${p.key}`
                  const value = drafts[d.kind]?.[p.key] ?? draftValue(p)
                  const err = errors[`${d.kind}.${p.key}`]
                  const set = (v: string | boolean) => setDrafts((s) => ({ ...s, [d.kind]: { ...(s[d.kind] ?? {}), [p.key]: v } }))
                  return (
                    <div key={p.key} className={cn(p.kind === 'ports' && 'min-[480px]:col-span-2')}>
                      <label htmlFor={id} className="block text-[11px] font-medium text-text">
                        {p.label}
                        {p.unit ? ` (${p.unit})` : ''}
                      </label>
                      {p.kind === 'severity' ? (
                        <select
                          id={id}
                          className="mt-1 h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text"
                          value={String(value)}
                          onChange={(e) => set(e.target.value)}
                          disabled={readOnly}
                        >
                          <option value="critical">Critical</option>
                          <option value="warning">Warning</option>
                          <option value="info">Info</option>
                        </select>
                      ) : p.kind === 'bool' ? (
                        <div className="mt-1.5">
                          <Switch id={id} checked={value === true} onCheckedChange={(v) => set(v)} disabled={readOnly} />
                        </div>
                      ) : (
                        <Input
                          id={id}
                          className="mt-1 h-9"
                          inputMode={p.kind === 'ports' ? 'text' : 'decimal'}
                          value={String(value)}
                          onChange={(e) => set(e.target.value)}
                          disabled={readOnly}
                          aria-invalid={!!err}
                        />
                      )}
                      {err ? (
                        <span className="mt-0.5 block text-[11px] text-danger-fg">{err}</span>
                      ) : (
                        <span className="mt-0.5 block text-[11px] text-text2">Default {Array.isArray(p.default) ? (p.default as number[]).join(', ') : String(p.default)}</span>
                      )}
                    </div>
                  )
                })}
              </div>
            </li>
          )
        })}
      </ul>
      {!readOnly && (
        <div className="sticky bottom-2 z-10 flex flex-wrap items-center justify-end gap-2 rounded-lg border border-border bg-surface/95 p-2 shadow-card backdrop-blur">
          <span className="mr-auto text-xs text-text2">{invalid ? 'Fix the highlighted values' : dirty ? 'Unsaved changes' : 'No changes'}</span>
          <Button type="button" variant="outline" size="sm" disabled={!dirty || save.isPending} onClick={() => { setDrafts({}); setEnabled({}); setQuiet(String(data.quiet_minutes)) }}>
            <RotateCcw aria-hidden /> Discard
          </Button>
          <Button type="submit" size="sm" disabled={!dirty || invalid || save.isPending}>
            {save.isPending ? <Loader2 className="animate-spin" aria-hidden /> : <Save aria-hidden />} Save settings
          </Button>
        </div>
      )}
    </form>
  )
}
