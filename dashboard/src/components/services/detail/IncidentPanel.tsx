/**
 * Unified incident investigation: one selected incident with its exact
 * bounds, how it ended, the probes around it, alerts raised during it,
 * maintenance overlap, and operator notes with an assignee.
 */
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, StickyNote, X, ZoomIn } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { explainServiceFailure } from '@/components/services/failureReason'
import { ServiceFailureReason } from '@/components/services/ServiceFailureReason'
import { withCapability } from './capabilities'
import { windowEnd, scopeText } from './maintenance'
import { cleanAlertMessage, exactTime, formatDur, formatMs, isRecoveryAlert } from './format'
import type { AlertsPage, IncidentNote, MaintenanceWindow, MetricResponse, Outage } from './types'

const END_TEXT: Record<NonNullable<Outage['endKind']>, string> = {
  recovered: 'Recovered on the first successful check',
  gap: 'Monitoring evidence stopped — recovery was not observed',
  escalated: 'Escalated to Down',
  window: 'Continues past the selected window',
  ongoing: 'Ongoing',
}

export function IncidentPanel({ checkId, outage, intervalSec, timeoutSec, nowMs, maintenance, onClose, onZoom }: {
  checkId: string
  outage: Outage
  intervalSec: number
  timeoutSec: number
  nowMs: number
  maintenance: MaintenanceWindow[]
  onClose: () => void
  onZoom: (fromISO: string, toISO: string) => void
}) {
  const end = outage.end ?? nowMs
  const spanMs = Math.max(0, end - outage.start)
  const pad = Math.max(2, intervalSec * 3) * 1000
  const startISO = outage.startISO || new Date(outage.start).toISOString()

  // Raw probes across the incident (capped at 6 h after the start so a long outage stays cheap).
  const probeTo = Math.min(end, outage.start + 6 * 3600_000) + pad
  const probes = useQuery<MetricResponse>({
    queryKey: ['incident-probes', checkId, outage.start, probeTo],
    queryFn: async () => (await api.get(`/service-checks/${checkId}/metrics`, {
      params: { from: new Date(outage.start - pad).toISOString(), to: new Date(probeTo).toISOString(), granularity: 'raw', limit: 5000 },
    })).data,
    staleTime: 60_000,
  })
  const points = probes.data?.points || []
  const failed = points.filter((p) => p.is_up === false)
  const firstSuccessAfter = points.find((p) => p.is_up === true && Date.parse(p.timestamp) >= outage.start)

  const alerts = useQuery<AlertsPage>({
    queryKey: ['incident-alerts', checkId, outage.start, end],
    queryFn: async () => (await api.get('/alerts', {
      params: {
        service_check_id: checkId,
        from: new Date(outage.start - 60_000).toISOString(),
        to: new Date(end + 5 * 60_000).toISOString(),
        limit: 50,
      },
    })).data,
    staleTime: 30_000,
  })

  const overlaps = maintenance.filter((w) => Date.parse(w.starts_at) < end && windowEnd(w) > outage.start)
  const durationText = `${outage.clippedStart || outage.clippedEnd ? '≥ ' : ''}${formatDur(outage.durationSec ?? spanMs / 1000)}`
  const path = outage.severityPath && outage.severityPath.length > 1 ? outage.severityPath.map((s) => s[0].toUpperCase() + s.slice(1)).join(' → ') : null

  return (
    <section aria-label="Incident investigation" className="rounded-lg border border-primary/30 bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className={cn(
            'rounded px-1.5 py-px text-[10px] font-semibold uppercase tracking-wider',
            outage.kind === 'down' ? 'bg-danger/15 text-danger' : 'bg-warning/15 text-warning',
          )}>
            {outage.kind === 'down' ? 'Down' : 'Warning'}
          </span>
          <h3 className="text-sm font-semibold">Incident investigation</h3>
          <span className="font-mono text-[11px] text-muted">{durationText}</span>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-7" onClick={() => onZoom(new Date(outage.start - pad * 2).toISOString(), new Date(Math.min(end + pad * 2, nowMs)).toISOString())}>
            <ZoomIn className="h-3 w-3" /> Zoom page to incident
          </Button>
          <button type="button" onClick={onClose} className="rounded p-1 text-muted hover:bg-surface2 hover:text-text" aria-label="Close incident investigation">
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 lg:grid-cols-2">
        <div className="space-y-3">
          <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-1.5 text-[11px]">
            <dt className="text-muted">Started</dt>
            <dd className="font-mono tabular-nums">{exactTime(outage.start)}{outage.clippedStart && <span className="ml-1 font-sans text-muted">(began before the selected window)</span>}</dd>
            <dt className="text-muted">Ended</dt>
            <dd className="font-mono tabular-nums">{outage.end == null ? 'Ongoing' : exactTime(outage.end)}</dd>
            <dt className="text-muted">How it ended</dt>
            <dd>{END_TEXT[outage.endKind || (outage.end == null ? 'ongoing' : 'recovered')]}</dd>
            <dt className="text-muted">Duration</dt>
            <dd className="font-mono tabular-nums">{durationText}</dd>
            {path && (<><dt className="text-muted">Severity path</dt><dd>{path}</dd></>)}
            <dt className="text-muted">Maintenance</dt>
            <dd>
              {overlaps.length === 0 ? 'No overlap' : overlaps.map((w) => (
                <div key={w.id}>{scopeText(w)} · {exactTime(w.starts_at, false)} → {exactTime(windowEnd(w), false)}</div>
              ))}
            </dd>
          </dl>
          {outage.reason && (
            <div className="rounded-md border border-border bg-surface2/30 p-2 text-[11px]">
              <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Reason at start</div>
              <ServiceFailureReason reason={outage.reason} timeoutSeconds={timeoutSec} />
            </div>
          )}
          {outage.reasonEnd && (
            <div className="rounded-md border border-border bg-surface2/30 p-2 text-[11px]">
              <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Reason at end</div>
              <span className="text-text2">{explainServiceFailure(outage.reasonEnd, { timeoutSeconds: timeoutSec })}</span>
            </div>
          )}

          <div>
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">
              Probes around the incident {!probes.isLoading && points.length > 0 && <span className="ml-1 font-mono normal-case text-text2">{failed.length} failed of {points.length}</span>}
            </div>
            {probes.isLoading ? <p className="text-[11px] text-muted">Loading probes…</p>
              : probes.isError ? <p className="text-[11px] text-danger">Could not load probes. <button type="button" className="underline" onClick={() => probes.refetch()}>Retry</button></p>
                : points.length === 0 ? <p className="text-[11px] text-muted">No stored probes around this incident (raw probes are kept for 30 days).</p>
                  : (
                    <div className="max-h-[220px] space-y-1 overflow-y-auto pr-1">
                      {failed.slice(0, 40).map((p, i) => (
                        <div key={`${p.timestamp}-${i}`} className="flex items-start gap-2 text-[11px]">
                          <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-danger" />
                          <span className="shrink-0 font-mono tabular-nums text-muted">{exactTime(p.timestamp)}</span>
                          <span className="min-w-0 flex-1 text-text2">{p.error_message ? explainServiceFailure(p.error_message, { failureStage: p.network_diagnostics?.failure_stage, timeoutSeconds: timeoutSec }) : 'Failed'}</span>
                        </div>
                      ))}
                      {failed.length > 40 && <p className="text-[10px] text-muted">…and {failed.length - 40} more failed probes.</p>}
                      {failed.length === 0 && <p className="text-[11px] text-muted">No failed probes stored in this span.</p>}
                      {firstSuccessAfter && (
                        <div className="flex items-start gap-2 text-[11px]">
                          <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-success" />
                          <span className="shrink-0 font-mono tabular-nums text-muted">{exactTime(firstSuccessAfter.timestamp)}</span>
                          <span className="text-success">First successful probe · {formatMs(firstSuccessAfter.response_ms)}</span>
                        </div>
                      )}
                    </div>
                  )}
          </div>
        </div>

        <div className="space-y-3">
          <div>
            <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted">Alerts during the incident</div>
            {alerts.isLoading ? <p className="text-[11px] text-muted">Loading alerts…</p>
              : alerts.isError ? <p className="text-[11px] text-danger">Could not load alerts.</p>
                : (alerts.data?.data || []).length === 0 ? <p className="text-[11px] text-muted">No alerts were raised in this span.</p>
                  : (
                    <ul className="space-y-1">
                      {(alerts.data?.data || []).map((a) => (
                        <li key={a.id} className="flex items-start gap-2 text-[11px]">
                          <span className={cn('mt-1 h-1.5 w-1.5 shrink-0 rounded-full', isRecoveryAlert(a) ? 'bg-success' : a.status === 'active' ? 'bg-danger' : 'bg-muted')} />
                          <span className="shrink-0 font-mono tabular-nums text-muted">{exactTime(a.triggered_at)}</span>
                          <Link to={`/alerts/${a.id}`} className="min-w-0 flex-1 truncate hover:text-primary hover:underline" title={cleanAlertMessage(a.message)}>
                            {isRecoveryAlert(a) ? 'Recovery · ' : ''}{cleanAlertMessage(a.message)}
                          </Link>
                        </li>
                      ))}
                    </ul>
                  )}
          </div>
          <IncidentNotes checkId={checkId} incidentStartISO={startISO} />
        </div>
      </div>
    </section>
  )
}

function IncidentNotes({ checkId, incidentStartISO }: { checkId: string; incidentStartISO: string }) {
  const qc = useQueryClient()
  const [body, setBody] = useState('')
  const [assignee, setAssignee] = useState('')
  const [error, setError] = useState<string | null>(null)
  const notes = useQuery<IncidentNote[] | null>({
    queryKey: ['service-check-notes', checkId, incidentStartISO],
    queryFn: () => withCapability('notes', async () => {
      const r = (await api.get(`/service-checks/${checkId}/notes`, { params: { incident_started_at: incidentStartISO } })).data
      return (Array.isArray(r) ? r : r?.data || []) as IncidentNote[]
    }),
    retry: false,
  })
  const add = useMutation({
    mutationFn: async () => (await api.post(`/service-checks/${checkId}/notes`, {
      body: body.trim(), incident_started_at: incidentStartISO, assignee: assignee.trim() || null,
    })).data,
    onSuccess: () => {
      setBody('')
      toast.success('Note added')
      qc.invalidateQueries({ queryKey: ['service-check-notes', checkId] })
    },
    onError: (e: any) => setError(apiErrorMessage(e)),
  })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    const text = body.trim()
    if (!text) { setError('Write a note first.'); return }
    if (text.length > 4000) { setError('Notes are limited to 4,000 characters.'); return }
    add.mutate()
  }
  const noteId = `note-body-${incidentStartISO}`
  const assigneeId = `note-assignee-${incidentStartISO}`
  return (
    <div>
      <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">
        <StickyNote className="h-3 w-3" /> Notes & assignment
      </div>
      {notes.isLoading ? <p className="text-[11px] text-muted">Loading notes…</p>
        : notes.isError ? <p className="text-[11px] text-danger">Could not load notes.</p>
          : notes.data === null ? <p className="text-[11px] text-muted">Incident notes are not available on this appliance version.</p>
            : (
              <>
                {(notes.data || []).length === 0 ? <p className="mb-2 text-[11px] text-muted">No notes yet.</p> : (
                  <ul className="mb-2 space-y-1.5">
                    {(notes.data || []).map((n) => (
                      <li key={n.id} className="rounded-md border border-border bg-surface2/30 px-2 py-1.5 text-[11px]">
                        <div className="flex flex-wrap items-baseline justify-between gap-2 text-[10px] text-muted">
                          <span>{n.author_name || 'Unknown'}{n.assignee ? ` · assigned to ${n.assignee}` : ''}</span>
                          <span className="font-mono">{exactTime(n.created_at, false)} ({relativeTime(n.created_at)})</span>
                        </div>
                        <p className="mt-0.5 whitespace-pre-wrap break-words">{n.body}</p>
                      </li>
                    ))}
                  </ul>
                )}
                <form onSubmit={submit} className="space-y-2">
                  <label htmlFor={noteId} className="sr-only">Note</label>
                  <Textarea id={noteId} value={body} onChange={(e) => setBody(e.target.value)} rows={2} maxLength={4000}
                    placeholder="What was found, what was done…" aria-invalid={!!error} aria-describedby={error ? `${noteId}-err` : undefined}
                    className="text-xs" />
                  <div className="flex items-center gap-2">
                    <label htmlFor={assigneeId} className="sr-only">Assignee</label>
                    <Input id={assigneeId} value={assignee} onChange={(e) => setAssignee(e.target.value)} maxLength={255}
                      placeholder="Assign to (optional)" className="h-8 text-xs" />
                    <Button type="submit" size="sm" className="h-8 shrink-0" disabled={add.isPending}>
                      {add.isPending && <Loader2 className="h-3 w-3 animate-spin" />} Add note
                    </Button>
                  </div>
                  {error && <p id={`${noteId}-err`} role="alert" className="text-[11px] text-danger">{error}</p>}
                </form>
              </>
            )}
    </div>
  )
}
