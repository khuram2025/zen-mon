import { useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, Lock, Plus, Router, ShieldOff, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { toast } from '@/components/ui/Toast'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { nfKeys, useNfQuery } from '@/lib/netflow/api'
import { formatDateTime } from '@/lib/netflow/format'
import { apiErrorMessage } from '@/lib/netflow/status'
import { exceptionBody, exceptionExporters, exceptionScope, type DetectorException, type DetectorSettingsRow } from './model'
import { ToneChip } from './parts'

interface ExceptionsResponse {
  items: DetectorException[]
  can_manage: boolean
  /** How many exporters a tag-scoped viewer's new exception applies to; null = unrestricted. */
  scope_exporters: number | null
}

const EMPTY = { kind: '', src: '', dst: '', port: '', reason: '', expires: '', exporters: '' }

/**
 * Approved patterns (FE-06): a match is kept and shown as suppressed with the exception's reason,
 * never dropped, so an approved monitoring host remains visible and auditable.
 */
export function ExceptionsTab({ tz }: { tz: string }) {
  const q = useNfQuery<ExceptionsResponse>('/netflow/detector-exceptions', null, { refetchInterval: false })
  const detectors = useNfQuery<{ detectors: DetectorSettingsRow[] }>('/netflow/detectors', null, { refetchInterval: false })
  const qc = useQueryClient()
  const [draft, setDraft] = useState(EMPTY)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [toDelete, setToDelete] = useState<DetectorException | null>(null)
  const canManage = q.data?.data.can_manage ?? false
  const scopeCount = q.data?.data.scope_exporters ?? null

  const create = useMutation({
    mutationFn: async (body: Record<string, unknown>) => (await api.post('/netflow/detector-exceptions', body)).data,
    onSuccess: (r: { suppressed_findings?: number }) => {
      toast.success('Exception added', r.suppressed_findings ? `${r.suppressed_findings} active finding(s) are now shown as suppressed` : undefined)
      setDraft(EMPTY)
      void qc.invalidateQueries({ queryKey: nfKeys.all })
    },
    onError: (e) => toast.error('The exception was not added', apiErrorMessage(e)),
  })
  const remove = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/netflow/detector-exceptions/${id}`)).data,
    onSuccess: (r: { reopened_findings?: number }) => {
      toast.success('Exception removed', r.reopened_findings ? `${r.reopened_findings} finding(s) reopened` : undefined)
      setToDelete(null)
      void qc.invalidateQueries({ queryKey: nfKeys.all })
    },
    onError: (e) => toast.error('The exception was not removed', apiErrorMessage(e)),
  })

  const submit = () => {
    const r = exceptionBody(draft)
    if ('errors' in r) {
      setErrors(r.errors)
      return
    }
    setErrors({})
    create.mutate(r.body)
  }

  const kinds = detectors.data?.data.detectors ?? []
  return (
    <div className="space-y-3">
      {q.data && !canManage && (
        <p className="flex items-center gap-2 rounded-md border border-border bg-surface2 px-3 py-2 text-xs text-text2">
          <Lock className="h-3.5 w-3.5" aria-hidden /> Read only: adding or removing exceptions needs the netflow.manage permission.
        </p>
      )}
      {canManage && (
        <Card>
          <CardContent className="p-4">
            <h3 className="text-sm font-semibold text-text">Add an exception</h3>
            <p className="mt-0.5 text-xs text-text2">
              For approved traffic such as monitoring pings, vulnerability scanners or backups. Matching findings stay listed as suppressed with this reason.
            </p>
            <form
              className="mt-3 grid grid-cols-1 gap-2 min-[560px]:grid-cols-2 lg:grid-cols-3"
              onSubmit={(e) => {
                e.preventDefault()
                submit()
              }}
            >
              <Labeled id="nf-x-kind" label="Detector">
                <select id="nf-x-kind" className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm text-text" value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value })}>
                  <option value="">Every detector</option>
                  {kinds.map((k) => (
                    <option key={k.kind} value={k.kind}>{k.label}</option>
                  ))}
                </select>
              </Labeled>
              <Labeled id="nf-x-src" label="Source address or CIDR" error={errors.src}>
                <Input id="nf-x-src" className="h-9" value={draft.src} onChange={(e) => setDraft({ ...draft, src: e.target.value })} placeholder="10.20.1.5" aria-invalid={!!errors.src} />
              </Labeled>
              <Labeled id="nf-x-dst" label="Destination address or CIDR" error={errors.dst}>
                <Input id="nf-x-dst" className="h-9" value={draft.dst} onChange={(e) => setDraft({ ...draft, dst: e.target.value })} placeholder="optional" aria-invalid={!!errors.dst} />
              </Labeled>
              <Labeled id="nf-x-port" label="Destination port" error={errors.port}>
                <Input id="nf-x-port" className="h-9" inputMode="numeric" value={draft.port} onChange={(e) => setDraft({ ...draft, port: e.target.value })} placeholder="optional" aria-invalid={!!errors.port} />
              </Labeled>
              <Labeled id="nf-x-exp" label="Expires (your local time, optional)" error={errors.expires}>
                <Input id="nf-x-exp" className="h-9" type="datetime-local" value={draft.expires} onChange={(e) => setDraft({ ...draft, expires: e.target.value })} aria-invalid={!!errors.expires} />
              </Labeled>
              <Labeled id="nf-x-reason" label="Reason" error={errors.reason}>
                <Input id="nf-x-reason" className="h-9" value={draft.reason} onChange={(e) => setDraft({ ...draft, reason: e.target.value })} maxLength={500} placeholder="Approved monitoring host" aria-invalid={!!errors.reason} />
              </Labeled>
              <Labeled
                id="nf-x-exporters"
                label="Exporters (optional)"
                error={errors.exporters}
                hint={
                  scopeCount === null
                    ? 'Leave empty to apply it on every exporter.'
                    : `Leave empty to apply it on the ${scopeCount.toLocaleString('en-US')} exporter${scopeCount === 1 ? '' : 's'} in your scope; it never applies outside it.`
                }
              >
                <Input id="nf-x-exporters" className="h-9" value={draft.exporters} onChange={(e) => setDraft({ ...draft, exporters: e.target.value })} placeholder="e.g. 10.0.0.1, 10.0.0.2" aria-invalid={!!errors.exporters} aria-describedby="nf-x-exporters-h" />
              </Labeled>
              <div className="flex items-end min-[560px]:col-span-2 lg:col-span-3">
                <Button type="submit" size="sm" disabled={create.isPending}>
                  {create.isPending ? <Loader2 className="animate-spin" aria-hidden /> : <Plus aria-hidden />} Add exception
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      )}
      <Card>
        <CardContent className="p-3">
          <QueryPanel query={q} label="detector exceptions" skeleton="rows" empty="No exceptions: every match is reported as found" isEmpty={(d) => d.data.items.length === 0}>
            {(res) => (
              <ul className="divide-y divide-border">
                {res.data.items.map((x) => (
                  <li key={x.id} className="flex flex-col gap-2 py-2.5 min-[640px]:flex-row min-[640px]:items-start min-[640px]:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <ShieldOff className="h-4 w-4 text-text2" aria-hidden />
                        <span className="text-sm font-medium text-text">{x.kind_label ?? 'Every detector'}</span>
                        <span className="break-all font-mono text-xs text-text">{exceptionScope(x)}</span>
                        {x.expired ? <ToneChip tone="neutral">Expired</ToneChip> : x.expires_at ? <ToneChip tone="info">Until {formatDateTime(x.expires_at, tz, { seconds: false })}</ToneChip> : <ToneChip tone="neutral">No expiry</ToneChip>}
                      </div>
                      <p className="mt-0.5 flex items-start gap-1 break-words text-xs text-text">
                        <Router className="mt-px h-3.5 w-3.5 shrink-0 text-text2" aria-hidden />
                        <span>
                          <span className="sr-only">Applies on: </span>
                          <ExporterScope x={x} />
                        </span>
                      </p>
                      <p className="mt-0.5 break-words text-xs text-text2">
                        {x.reason} · added by {x.created_by?.username ?? 'unknown'} {x.created_at ? `on ${formatDateTime(x.created_at, tz, { seconds: false })}` : ''} ·{' '}
                        {x.matches.toLocaleString('en-US')} finding{x.matches === 1 ? '' : 's'} suppressed by it
                      </p>
                    </div>
                    {canManage && x.can_change && (
                      <Button size="sm" variant="outline" onClick={() => setToDelete(x)} aria-label={`Remove exception ${x.reason}`}>
                        <Trash2 aria-hidden /> Remove
                      </Button>
                    )}
                    {canManage && !x.can_change && (
                      <p className="flex max-w-56 items-start gap-1.5 text-xs text-text2">
                        <Lock className="mt-px h-3 w-3 shrink-0" aria-hidden />
                        {x.scope === 'global' ? 'Applies on every exporter: only an administrator can remove it.' : 'Also applies outside your scope: only an administrator can remove it.'}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </QueryPanel>
        </CardContent>
      </Card>
      <ConfirmDialog
        open={!!toDelete}
        onOpenChange={(o) => !o && setToDelete(null)}
        title="Remove this exception?"
        description={toDelete ? `Findings it suppressed (${toDelete.matches}) return to open, and future matches are reported again.` : undefined}
        confirmText="Remove"
        destructive
        loading={remove.isPending}
        onConfirm={() => {
          if (toDelete) remove.mutate(toDelete.id)
        }}
      />
    </div>
  )
}

/** The exporter scope with the addresses in monospace (same wording as exceptionExporters). */
function ExporterScope({ x }: { x: DetectorException }) {
  const text = exceptionExporters(x)
  const shown = x.scope === 'exporters' && x.exporters ? x.exporters.slice(0, 3) : []
  const list = shown.join(', ')
  const at = list ? text.indexOf(list) : -1
  if (at < 0) return <>{text}</>
  return (
    <>
      {text.slice(0, at)}
      <span className="font-mono">{list}</span>
      {text.slice(at + list.length)}
    </>
  )
}

function Labeled({ id, label, error, hint, children }: { id: string; label: string; error?: string; hint?: string; children: ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="block text-xs font-medium text-text">{label}</label>
      <div className="mt-1">{children}</div>
      {error && <span id={hint ? `${id}-h` : undefined} className="mt-0.5 block text-xs text-danger-fg">{error}</span>}
      {hint && !error && <span id={`${id}-h`} className="mt-0.5 block text-xs text-text2">{hint}</span>}
    </div>
  )
}
