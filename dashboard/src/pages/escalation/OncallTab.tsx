/**
 * On-call: schedules (layers rotate ordered members every shift length from a
 * handoff, optionally restricted to days/hours, in the schedule's IANA time
 * zone), a 14-day timeline, who is on call now, and time-boxed overrides.
 */
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ArrowDown, ArrowUp, CalendarClock, Loader2, Pencil, Plus, Trash2, UserRound, X } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { zoneLabel } from '@/lib/branding'
import { useDirtyGuard } from '@/hooks/useDirtyGuard'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { Switch } from '@/components/ui/Switch'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { useAssignees } from '@/components/alerts/AlertBits'
import { fmtAlertDate } from '@/components/alerts/format'
import { TimezoneSelect } from '@/features/reports/schedules/TimezoneSelect'
import { useOnCallNow, useOrgTimezone, useSchedules, useScheduleTimeline } from '@/features/escalation/api'
import {
  ROTATION_PRESETS, WEEKDAYS, coverageGapsInWindow, dayTicks, draftFromSchedule, emptyScheduleDraft, hasScheduleErrors,
  layoutShifts, memberCount, moveItem, newLayer, restrictionText, rotationLabel, rotationOrder, scheduleDirty, schedulePayload,
  timelineWindow, utcToZonedWall, validateSchedule, zonedWallToUtc, type ScheduleErrors,
} from '@/features/escalation/model'
import { USER_COLOR_CLS } from '@/features/escalation/ui'
import type { OncallSchedule, ScheduleDraft, Shift } from '@/features/escalation/types'

const DAYS = 14

export function OncallTab({ canManage, focusId }: { canManage: boolean; focusId: string | null }) {
  const qc = useQueryClient()
  const schedules = useSchedules()
  const [selected, setSelected] = useState<string | null>(focusId)
  const [editing, setEditing] = useState<OncallSchedule | 'new' | null>(null)
  const [deleting, setDeleting] = useState<OncallSchedule | null>(null)
  const list = schedules.data ?? []
  const current = list.find((s) => s.id === selected) ?? list[0] ?? null

  useEffect(() => { if (focusId) setSelected(focusId) }, [focusId])

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/oncall/schedules/${id}`),
    onSuccess: () => { toast.success('Schedule deleted'); setDeleting(null); setSelected(null); qc.invalidateQueries({ queryKey: ['escalation'] }) },
    onError: (e) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  if (schedules.isLoading) return <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]"><Skeleton className="h-64" /><Skeleton className="h-64" /></div>
  if (schedules.isError && !schedules.data) return <QueryFailure error={schedules.error} what="on-call schedules" onRetry={() => void schedules.refetch()} retrying={schedules.isFetching} />

  return (
    <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">Schedules</h2>
          {canManage && <Button size="sm" onClick={() => setEditing('new')}><Plus className="h-3.5 w-3.5" aria-hidden /> New schedule</Button>}
        </div>
        {list.length === 0 ? (
          <Card><CardContent className="space-y-1 p-6 text-center">
            <CalendarClock className="mx-auto h-6 w-6 text-muted" aria-hidden />
            <p className="text-sm font-medium">No on-call schedules</p>
            <p className="text-xs text-muted">A schedule rotates people through shifts; escalation steps page whoever is on call when they fire.</p>
          </CardContent></Card>
        ) : (
          <ul className="space-y-2">
            {list.map((s) => <ScheduleListItem key={s.id} s={s} active={current?.id === s.id} onSelect={() => setSelected(s.id)} />)}
          </ul>
        )}
      </div>
      <div className="min-w-0">
        {current ? (
          <ScheduleDetail s={current} canManage={canManage} onEdit={() => setEditing(current)} onDelete={() => setDeleting(current)} />
        ) : (
          <Card><CardContent className="p-6 text-sm text-muted">Select or create a schedule.</CardContent></Card>
        )}
      </div>
      {editing && <ScheduleEditorDialog key={editing === 'new' ? 'new' : editing.id} schedule={editing === 'new' ? null : editing} onClose={() => setEditing(null)} onSaved={(id) => id && setSelected(id)} />}
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => { if (!o) setDeleting(null) }}
        title={`Delete “${deleting?.name ?? ''}”?`}
        description="Escalation steps that target this schedule will resolve to nobody and use their policy's fallback route."
        confirmText="Delete schedule"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </div>
  )
}

function ScheduleListItem({ s, active, onSelect }: { s: OncallSchedule; active: boolean; onSelect: () => void }) {
  // The list response embeds who is on call now; older servers need a request per schedule.
  const fetched = useOnCallNow(s.now ? null : s.id)
  const now = s.now ? { isLoading: false, data: s.now } : fetched
  const members = memberCount(s)
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={active || undefined}
        className={cn('w-full rounded-lg border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60', active ? 'border-primary/50 bg-primary/5' : 'border-border bg-surface hover:bg-surface2/50', !s.enabled && 'opacity-60')}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-sm font-semibold">{s.name}</span>
          {!s.enabled ? <Badge variant="outline">Disabled</Badge> : members === 0 ? <Badge variant="warning">No members</Badge> : null}
        </div>
        <div className="mt-0.5 text-xs text-muted">{s.timezone} · {s.layers.length} layer{s.layers.length === 1 ? '' : 's'} · {members} member{members === 1 ? '' : 's'}</div>
        <div className="mt-1.5 flex items-center gap-1.5 text-xs">
          <UserRound className="h-3.5 w-3.5 text-primary" aria-hidden />
          {now.isLoading ? <span className="text-muted">…</span>
            : now.data?.users.length ? <span><b>{now.data.users.map((u) => u.name).join(', ')}</b>{now.data.until ? <span className="text-muted"> until {fmtAlertDate(now.data.until)}</span> : null}</span>
              : <span className="text-warning">Nobody on call now</span>}
        </div>
      </button>
    </li>
  )
}

function ScheduleDetail({ s, canManage, onEdit, onDelete }: { s: OncallSchedule; canManage: boolean; onEdit: () => void; onDelete: () => void }) {
  const users = useAssignees()
  const nameOf = (id: string) => users.data?.find((u) => u.id === id)?.name ?? 'Unknown user'
  const fetched = useOnCallNow(s.id)
  const now = fetched.data ? fetched : s.now ? { ...fetched, isLoading: false, isError: false, data: s.now } : fetched
  const usedBy = s.used_by ?? []
  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <h2 className="text-base font-semibold">{s.name}</h2>
              <p className="text-xs text-muted">{s.description || 'No description'} · {s.timezone} ({zoneLabel(s.timezone)})</p>
            </div>
            {canManage && (
              <div className="flex gap-2">
                <Button size="sm" variant="outline" onClick={onEdit}><Pencil className="h-3.5 w-3.5" aria-hidden /> Edit</Button>
                <Button
                  size="sm" variant="ghost" className="text-muted hover:text-danger" onClick={onDelete} disabled={usedBy.length > 0}
                  aria-label={`Delete schedule ${s.name}`} title={usedBy.length ? `Used by ${usedBy.join(', ')} — remove it from those policies first` : undefined}
                >
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                </Button>
              </div>
            )}
          </div>
          <div className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-sm" role="status">
            <span className="font-medium">On call now: </span>
            {now.isLoading ? '…' : now.isError ? <span className="text-danger">unavailable</span> : now.data?.users.length ? (
              <>{now.data.users.map((u) => u.name).join(', ')}{now.data.until && <span className="text-muted"> — until {fmtAlertDate(now.data.until)}</span>}</>
            ) : <span className="text-warning">nobody — steps targeting this schedule use the policy fallback</span>}
          </div>
          {usedBy.length > 0 && <p className="text-xs text-muted">Used by escalation policies: {usedBy.join(', ')}</p>}
          <div className="grid gap-2 md:grid-cols-2">
            {s.layers.map((l, i) => {
              const upcoming = rotationOrder(l.members, l.shift_hours, l.handoff_at, Date.now(), 1)[0]
              return (
                <div key={l.id ?? i} className="rounded-md border border-border bg-surface2/30 p-2.5 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">{l.name}</span>
                    <span className="text-xs text-muted">{rotationLabel(l.shift_hours)}{i === s.layers.length - 1 && s.layers.length > 1 ? ' · wins overlaps' : ''}</span>
                  </div>
                  <div className="text-xs text-muted">Handoff {l.handoff_at ? `${utcToZonedWall(l.handoff_at, s.timezone).time} (${s.timezone})` : '—'}{upcoming ? ` · this shift: ${nameOf(upcoming.user_id)}` : ''}</div>
                  <ol className="mt-1 flex flex-wrap gap-1">
                    {l.members.map((m, k) => <li key={`${m}-${k}`} className="rounded border border-border bg-surface px-1.5 py-px text-xs">{k + 1}. {nameOf(m)}</li>)}
                    {!l.members.length && <li className="text-xs text-warning">No members</li>}
                  </ol>
                  {l.restrict_to.length > 0 && <div className="mt-1 text-xs text-text2">On duty: {l.restrict_to.map(restrictionText).join('; ')}</div>}
                </div>
              )
            })}
          </div>
        </CardContent>
      </Card>
      <TimelineCard s={s} />
      <OverridesCard s={s} canManage={canManage} nameOf={nameOf} users={users.data ?? []} />
    </div>
  )
}

/* ── 14-day timeline ──────────────────────────────────────────────────── */

function TimelineCard({ s }: { s: OncallSchedule }) {
  const [{ from, to }] = useState(() => timelineWindow(Date.now(), DAYS))
  const tl = useScheduleTimeline(s.id, from, to)
  return (
    <Card>
      <CardContent className="space-y-2 p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-sm font-semibold">Next {DAYS} days</h3>
          <span className="text-xs text-muted">Times in your browser&apos;s zone; hover a shift for {s.timezone}.</span>
        </div>
        {tl.isLoading ? <Skeleton className="h-24 w-full" />
          : tl.isError ? <QueryFailure compact error={tl.error} what="the timeline" onRetry={() => void tl.refetch()} retrying={tl.isFetching} />
            : <ShiftTimeline shifts={tl.data ?? []} from={from} to={to} tz={s.timezone} now={Date.now()} />}
      </CardContent>
    </Card>
  )
}

export function ShiftTimeline({ shifts, from, to, tz, now }: { shifts: Shift[]; from: number; to: number; tz: string; now: number }) {
  const rows = useMemo(() => layoutShifts(shifts, from, to), [shifts, from, to])
  const ticks = useMemo(() => dayTicks(from, Math.round((to - from) / 86400_000)), [from, to])
  const gaps = useMemo(() => coverageGapsInWindow(shifts, from, to).filter((g) => g.end - g.start >= 60_000), [shifts, from, to])
  const nowPct = now >= from && now <= to ? ((now - from) / (to - from)) * 100 : null
  const legend = useMemo(() => {
    const m = new Map<string, { name: string; color: number }>()
    for (const r of rows) for (const b of r.bars) if (!m.has(b.user_id ?? b.user_name)) m.set(b.user_id ?? b.user_name, { name: b.user_name, color: b.colorIndex })
    return [...m.values()]
  }, [rows])
  const span = (a: string, b: string) => `${fmtAlertDate(a)} → ${fmtAlertDate(b)} · ${utcToZonedWall(a, tz).date} ${utcToZonedWall(a, tz).time}–${utcToZonedWall(b, tz).time} ${tz}`
  if (!rows.length) return <p className="rounded-md border border-dashed border-warning/40 p-3 text-sm text-warning">No shifts in the next {ticks.length} days — this schedule resolves to nobody.</p>
  return (
    <div className="space-y-2">
      <div className="overflow-x-auto">
        <div className="min-w-[720px]">
          <div className="relative ml-28 h-5 border-b border-border text-[10px] text-muted">
            {ticks.map((t) => (
              <span key={t.at} className="absolute top-0 -translate-x-0 whitespace-nowrap pl-0.5" style={{ left: `${t.leftPct}%` }}>
                {new Date(t.at).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric' })}
              </span>
            ))}
          </div>
          {rows.map((r) => (
            <div key={r.key} className="flex items-center border-b border-border/50 py-1">
              <div className="w-28 shrink-0 truncate pr-2 text-xs font-medium" title={r.label}>{r.label}</div>
              <div className="relative h-7 flex-1 rounded bg-surface2/40" role="list" aria-label={`${r.label} shifts`}>
                {ticks.map((t) => <span key={t.at} className="absolute top-0 h-full border-l border-border/40" style={{ left: `${t.leftPct}%` }} aria-hidden />)}
                {r.bars.map((b) => (
                  <span
                    key={b.key}
                    role="listitem"
                    className={cn('absolute top-0.5 flex h-6 items-center overflow-hidden rounded px-1 text-[10px] font-medium text-white', USER_COLOR_CLS[b.colorIndex], b.override && 'ring-2 ring-warning')}
                    style={{ left: `${b.leftPct}%`, width: `${Math.max(b.widthPct, 0.3)}%` }}
                    title={`${b.user_name}${b.override ? ' (override)' : ''}: ${span(b.start, b.end)}`}
                    aria-label={`${b.user_name}${b.override ? ' (override)' : ''} from ${fmtAlertDate(b.start)} to ${fmtAlertDate(b.end)}`}
                  >
                    <span className="truncate">{b.user_name}</span>
                  </span>
                ))}
                {nowPct != null && <span className="absolute -top-1 h-9 border-l-2 border-danger" style={{ left: `${nowPct}%` }} aria-hidden />}
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
        {legend.map((l) => <span key={l.name} className="inline-flex items-center gap-1"><span className={cn('h-2.5 w-2.5 rounded-sm', USER_COLOR_CLS[l.color])} aria-hidden />{l.name}</span>)}
        <span className="inline-flex items-center gap-1"><span className="h-2.5 w-0.5 bg-danger" aria-hidden />now</span>
      </div>
      {gaps.length > 0 && (
        <p className="flex items-start gap-1 text-xs text-warning"><AlertTriangle className="mt-px h-3 w-3 shrink-0" aria-hidden />
          Nobody on call {gaps.slice(0, 3).map((g) => `${fmtAlertDate(new Date(g.start).toISOString())} → ${fmtAlertDate(new Date(g.end).toISOString())}`).join('; ')}{gaps.length > 3 ? ` and ${gaps.length - 3} more` : ''}.
        </p>
      )}
    </div>
  )
}

/* ── Overrides ────────────────────────────────────────────────────────── */

function toLocalInput(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function OverridesCard({ s, canManage, nameOf, users }: { s: OncallSchedule; canManage: boolean; nameOf: (id: string) => string; users: Array<{ id: string; name: string }> }) {
  const qc = useQueryClient()
  const [userId, setUserId] = useState('')
  const [start, setStart] = useState(() => toLocalInput(Date.now()))
  const [end, setEnd] = useState(() => toLocalInput(Date.now() + 8 * 3600_000))
  const [reason, setReason] = useState('')
  const startMs = Date.parse(start)
  const endMs = Date.parse(end)
  const invalid = !userId ? 'Choose who covers' : !(endMs > startMs) ? 'The end must be after the start' : null
  const refresh = () => qc.invalidateQueries({ queryKey: ['escalation'] })

  const create = useMutation({
    mutationFn: async () => (await api.post(`/oncall/schedules/${s.id}/overrides`, {
      user_id: userId, starts_at: new Date(startMs).toISOString(), ends_at: new Date(endMs).toISOString(), reason: reason.trim() || null,
    })).data,
    onSuccess: () => { toast.success('Override added', `${nameOf(userId)} covers ${fmtAlertDate(new Date(startMs).toISOString())} → ${fmtAlertDate(new Date(endMs).toISOString())}.`); setReason(''); refresh() },
    onError: (e) => toast.error('Could not add the override', apiErrorMessage(e)),
  })
  const remove = useMutation({
    mutationFn: async (oid: string) => api.delete(`/oncall/schedules/${s.id}/overrides/${oid}`),
    onSuccess: () => { toast.success('Override removed'); refresh() },
    onError: (e) => toast.error('Could not remove the override', apiErrorMessage(e)),
  })
  const overrides = [...(s.overrides ?? [])].filter((o) => Date.parse(o.ends_at) > Date.now()).sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at))

  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div>
          <h3 className="text-sm font-semibold">Overrides</h3>
          <p className="text-xs text-muted">A substitute for a time window (holiday, sick leave). Overrides win over every layer.</p>
        </div>
        {overrides.length === 0 ? <p className="text-xs text-muted">No current or upcoming overrides.</p> : (
          <ul className="divide-y divide-border rounded-md border border-border">
            {overrides.map((o) => (
              <li key={o.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                <span className="font-medium">{o.user_name || nameOf(o.user_id)}</span>
                <span className="text-xs text-muted">{fmtAlertDate(o.starts_at)} → {fmtAlertDate(o.ends_at)}</span>
                {o.reason && <span className="text-xs text-text2">— {o.reason}</span>}
                {canManage && o.id && (
                  <Button size="icon" variant="ghost" className="ml-auto h-7 w-7 text-muted hover:text-danger" disabled={remove.isPending} onClick={() => remove.mutate(o.id)} aria-label={`Remove override for ${o.user_name || nameOf(o.user_id)}`}>
                    <X className="h-3.5 w-3.5" aria-hidden />
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
        {canManage && (
          <form className="grid gap-2 rounded-md border border-border bg-surface2/30 p-3 sm:grid-cols-2 xl:grid-cols-[1fr_1fr_1fr_1fr_auto]" onSubmit={(e) => { e.preventDefault(); if (!invalid) create.mutate() }}>
            <FormField label="Who covers">
              <select aria-label="Override user" value={userId} onChange={(e) => setUserId(e.target.value)} className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm">
                <option value="">Choose…</option>
                {users.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
              </select>
            </FormField>
            <FormField label="From (your time)"><Input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} /></FormField>
            <FormField label="Until (your time)"><Input type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} /></FormField>
            <FormField label="Reason"><Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Covering annual leave" maxLength={200} /></FormField>
            <div className="flex items-end">
              <Button type="submit" size="sm" disabled={!!invalid || create.isPending} title={invalid ?? undefined}>
                {create.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Plus className="h-3.5 w-3.5" aria-hidden />} Add override
              </Button>
            </div>
          </form>
        )}
      </CardContent>
    </Card>
  )
}

/* ── Schedule editor ──────────────────────────────────────────────────── */

function ScheduleEditorDialog({ schedule, onClose, onSaved }: { schedule: OncallSchedule | null; onClose: () => void; onSaved: (id: string | null) => void }) {
  const qc = useQueryClient()
  const orgTz = useOrgTimezone()
  const users = useAssignees()
  const [saved] = useState<ScheduleDraft>(() => (schedule ? draftFromSchedule(schedule) : emptyScheduleDraft(orgTz)))
  const [draft, setDraft] = useState<ScheduleDraft>(saved)
  const [showErrors, setShowErrors] = useState(false)
  const dirty = scheduleDirty(draft, saved)
  const errors: ScheduleErrors = validateSchedule(draft)
  const shown = showErrors ? errors : {}

  const save = useMutation({
    mutationFn: async () => {
      const body = schedulePayload(draft)
      return schedule ? (await api.put(`/oncall/schedules/${schedule.id}`, body)).data : (await api.post('/oncall/schedules', body)).data
    },
    onSuccess: (data) => {
      toast.success(schedule ? 'Schedule saved' : 'Schedule created')
      qc.invalidateQueries({ queryKey: ['escalation'] })
      onSaved(schedule?.id ?? (typeof data?.id === 'string' ? data.id : null))
      onClose()
    },
    onError: (e) => toast.error('Save failed', apiErrorMessage(e)),
  })
  const guard = useDirtyGuard(dirty, save.isPending, (o) => { if (!o) onClose() })

  const setLayer = (key: string, patch: Partial<ScheduleDraft['layers'][number]>) =>
    setDraft((d) => ({ ...d, layers: d.layers.map((l) => (l.key === key ? { ...l, ...patch } : l)) }))
  const nameOf = (id: string) => users.data?.find((u) => u.id === id)?.name ?? 'Unknown user'
  const at = new Date()

  return (
    <Dialog open onOpenChange={guard.onOpenChange}>
      <DialogContent className="w-[calc(100vw-2rem)] max-w-4xl">
        <DialogHeader>
          <DialogTitle>{schedule ? `Edit “${schedule.name}”` : 'New on-call schedule'}</DialogTitle>
          <DialogDescription>Each layer hands over to the next member every shift length, starting at its handoff. When layers overlap, the one listed last wins.</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_280px_auto]">
            <FormField label="Name" required error={shown.name}>
              <Input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="e.g. Network primary on-call" maxLength={255} />
            </FormField>
            <FormField label="Time zone" error={shown.timezone}>
              <TimezoneSelect value={draft.timezone} orgTz={orgTz} at={at} onChange={(tz) => setDraft({ ...draft, timezone: tz || orgTz })} />
            </FormField>
            <div className="flex items-end gap-2 pb-1">
              <Switch checked={draft.enabled} onCheckedChange={(v) => setDraft({ ...draft, enabled: v })} aria-label="Schedule enabled" />
              <span className="text-sm">{draft.enabled ? 'Enabled' : 'Disabled'}</span>
            </div>
          </div>
          <FormField label="Description">
            <Textarea rows={2} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
          </FormField>

          <section aria-labelledby="layers-h" className="space-y-2">
            <div className="flex items-center justify-between">
              <h3 id="layers-h" className="text-sm font-semibold">Layers</h3>
              {shown.layers && <span className="text-xs text-danger">{shown.layers}</span>}
            </div>
            {draft.layers.map((l, li) => {
              const err = shown.layerErrors?.[l.key]
              const handoffIso = zonedWallToUtc(l.handoff_date, l.handoff_time, draft.timezone)
              const next = rotationOrder(l.members, Number(l.shift_hours), handoffIso, Date.now(), 3)
              return (
                <div key={l.key} className={cn('space-y-3 rounded-lg border p-3', err ? 'border-danger/50' : 'border-border bg-surface2/30')}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Input className="h-8 max-w-[220px]" aria-label={`Layer ${li + 1} name`} value={l.name} onChange={(e) => setLayer(l.key, { name: e.target.value })} maxLength={100} />
                    <span className="text-xs text-muted">{li === draft.layers.length - 1 && draft.layers.length > 1 ? 'Highest — wins overlaps' : `Layer ${li + 1}`}</span>
                    <div className="ml-auto flex gap-1">
                      <Button type="button" size="icon" variant="ghost" className="h-7 w-7" aria-label={`Move layer ${l.name} up`} disabled={li === 0} onClick={() => setDraft((d) => ({ ...d, layers: moveItem(d.layers, li, -1) }))}><ArrowUp className="h-3.5 w-3.5" aria-hidden /></Button>
                      <Button type="button" size="icon" variant="ghost" className="h-7 w-7" aria-label={`Move layer ${l.name} down`} disabled={li === draft.layers.length - 1} onClick={() => setDraft((d) => ({ ...d, layers: moveItem(d.layers, li, 1) }))}><ArrowDown className="h-3.5 w-3.5" aria-hidden /></Button>
                      <Button type="button" size="icon" variant="ghost" className="h-7 w-7 text-muted hover:text-danger" aria-label={`Remove layer ${l.name}`} disabled={draft.layers.length <= 1} onClick={() => setDraft((d) => ({ ...d, layers: d.layers.filter((x) => x.key !== l.key) }))}><Trash2 className="h-3.5 w-3.5" aria-hidden /></Button>
                    </div>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                    <FormField label="Rotation">
                      <select aria-label={`Layer ${li + 1} rotation`} value={l.rotation} className="h-9 w-full rounded-md border border-border bg-surface px-2 text-sm"
                        onChange={(e) => {
                          const preset = ROTATION_PRESETS.find((p) => p.value === e.target.value)!
                          setLayer(l.key, { rotation: preset.value, shift_hours: preset.hours ? String(preset.hours) : l.shift_hours })
                        }}>
                        {ROTATION_PRESETS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
                      </select>
                    </FormField>
                    <FormField label="Shift length (hours)">
                      <Input type="number" min={1} max={8760} value={l.shift_hours} disabled={l.rotation !== 'custom'} onChange={(e) => setLayer(l.key, { shift_hours: e.target.value })} />
                    </FormField>
                    <FormField label={`Handoff date (${draft.timezone})`}>
                      <Input type="date" value={l.handoff_date} onChange={(e) => setLayer(l.key, { handoff_date: e.target.value })} />
                    </FormField>
                    <FormField label="Handoff time">
                      <Input type="time" value={l.handoff_time} onChange={(e) => setLayer(l.key, { handoff_time: e.target.value })} />
                    </FormField>
                  </div>

                  <div className="space-y-1.5">
                    <div className="text-xs font-medium uppercase tracking-wider text-muted">Members, in rotation order</div>
                    <ol className="space-y-1">
                      {l.members.map((m, mi) => (
                        <li key={`${m}-${mi}`} className="flex items-center gap-2 rounded-md border border-border bg-surface px-2 py-1 text-sm">
                          <span className="w-5 text-xs tabular-nums text-muted">{mi + 1}.</span>
                          <span className="min-w-0 flex-1 truncate">{nameOf(m)}</span>
                          <Button type="button" size="icon" variant="ghost" className="h-6 w-6" aria-label={`Move ${nameOf(m)} up`} disabled={mi === 0} onClick={() => setLayer(l.key, { members: moveItem(l.members, mi, -1) })}><ArrowUp className="h-3 w-3" aria-hidden /></Button>
                          <Button type="button" size="icon" variant="ghost" className="h-6 w-6" aria-label={`Move ${nameOf(m)} down`} disabled={mi === l.members.length - 1} onClick={() => setLayer(l.key, { members: moveItem(l.members, mi, 1) })}><ArrowDown className="h-3 w-3" aria-hidden /></Button>
                          <Button type="button" size="icon" variant="ghost" className="h-6 w-6 text-muted hover:text-danger" aria-label={`Remove ${nameOf(m)} from ${l.name}`} onClick={() => setLayer(l.key, { members: l.members.filter((_, k) => k !== mi) })}><X className="h-3 w-3" aria-hidden /></Button>
                        </li>
                      ))}
                    </ol>
                    <select
                      aria-label={`Add a member to ${l.name}`}
                      value=""
                      onChange={(e) => { if (e.target.value) setLayer(l.key, { members: [...l.members, e.target.value] }) }}
                      className="h-8 w-full max-w-xs rounded-md border border-dashed border-border bg-surface px-2 text-sm text-muted"
                    >
                      <option value="">+ Add member…</option>
                      {(users.data ?? []).filter((u) => !l.members.includes(u.id)).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
                    </select>
                    {next.length > 0 && (
                      <p className="text-xs text-muted">Next shifts: {next.map((n) => `${nameOf(n.user_id)} from ${fmtAlertDate(new Date(n.start).toISOString())}`).join(' · ')}</p>
                    )}
                  </div>

                  <RestrictionsEditor layerName={l.name} value={l.restrict_to} onChange={(restrict_to) => setLayer(l.key, { restrict_to })} />
                  {err && <p className="text-xs text-danger">{err}</p>}
                </div>
              )
            })}
            <Button type="button" variant="outline" size="sm" onClick={() => setDraft((d) => ({ ...d, layers: [...d.layers, newLayer(d)] }))}>
              <Plus className="h-3.5 w-3.5" aria-hidden /> Add layer
            </Button>
          </section>
        </div>
        <DialogFooter>
          {dirty && <span className="mr-auto self-center text-xs text-warning">Unsaved changes</span>}
          <Button variant="outline" onClick={() => guard.onOpenChange(false)} disabled={save.isPending}>Cancel</Button>
          <Button onClick={() => { if (hasScheduleErrors(errors)) { setShowErrors(true); return } save.mutate() }} disabled={save.isPending || (!!schedule && !dirty)}>
            {save.isPending && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />} {schedule ? 'Save schedule' : 'Create schedule'}
          </Button>
        </DialogFooter>
        {guard.confirm}
      </DialogContent>
    </Dialog>
  )
}

function RestrictionsEditor({ layerName, value, onChange }: { layerName: string; value: ScheduleDraft['layers'][number]['restrict_to']; onChange: (v: ScheduleDraft['layers'][number]['restrict_to']) => void }) {
  const update = (i: number, patch: Partial<(typeof value)[number]>) => onChange(value.map((r, k) => (k === i ? { ...r, ...patch } : r)))
  return (
    <div className="space-y-1.5">
      <div className="text-xs font-medium uppercase tracking-wider text-muted">On duty</div>
      {value.length === 0 && <p className="text-xs text-muted">Around the clock. Add a restriction for business-hours coverage.</p>}
      {value.map((r, i) => (
        <div key={i} className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface px-2 py-1.5">
          <div className="flex gap-0.5" role="group" aria-label={`${layerName} restriction ${i + 1} days`}>
            {WEEKDAYS.map((d) => {
              const on = r.days.includes(d.n)
              return (
                <button key={d.n} type="button" aria-pressed={on} onClick={() => update(i, { days: on ? r.days.filter((x) => x !== d.n) : [...r.days, d.n].sort((a, b) => a - b) })}
                  className={cn('h-7 w-9 rounded text-[11px] font-medium', on ? 'bg-primary text-on-primary' : 'bg-surface2 text-muted')}>
                  {d.short}
                </button>
              )
            })}
          </div>
          <Input type="time" className="h-8 w-28" aria-label={`${layerName} restriction ${i + 1} start`} value={r.start} onChange={(e) => update(i, { start: e.target.value })} />
          <span className="text-xs text-muted">to</span>
          <Input type="time" className="h-8 w-28" aria-label={`${layerName} restriction ${i + 1} end`} value={r.end} onChange={(e) => update(i, { end: e.target.value })} />
          <span className="text-xs text-muted">{r.end < r.start ? '(ends next day)' : ''}</span>
          <Button type="button" size="icon" variant="ghost" className="ml-auto h-7 w-7 text-muted hover:text-danger" aria-label={`Remove ${layerName} restriction ${i + 1}`} onClick={() => onChange(value.filter((_, k) => k !== i))}><X className="h-3.5 w-3.5" aria-hidden /></Button>
        </div>
      ))}
      <Button type="button" size="sm" variant="ghost" onClick={() => onChange([...value, { days: [0, 1, 2, 3, 4], start: '08:00', end: '17:00' }])}>
        <Plus className="h-3.5 w-3.5" aria-hidden /> Add restriction
      </Button>
    </div>
  )
}
