/**
 * Reports → Scheduled reports: every schedule with what it sends, when it runs
 * next (organisation timezone), who receives it and how its last run went;
 * run now, edit, duplicate, pause and delete for report managers; and each
 * schedule's run history. The list is shared with Channels → Routing
 * (query key ['report-schedules']).
 */
import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertOctagon, CalendarClock, ChevronDown, ChevronRight, Copy, History, Info, Loader2, Lock, MoreHorizontal,
  Pencil, Play, Plus, Search, Trash2, X,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatDateTime, zoneLabel, type Branding } from '@/lib/branding'
import { useAuth, useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { Skeleton } from '@/components/ui/Skeleton'
import { QueryFailure } from '@/components/ui/QueryState'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { toast } from '@/components/ui/Toast'
import { ReportScheduleFormDialog } from '@/components/forms/ReportScheduleFormDialog'
import { useReportBranding, useReportCatalog } from '@/features/reports/api'
import type { ReportCatalog } from '@/features/reports/types'
import {
  useDeleteSchedule, useNotificationChannels, useReportSchedules, useRunScheduleNow, useSaveSchedule,
  useToggleSchedule,
} from '@/features/reports/schedules/api'
import {
  SCOPED_EXPLANATION, buildBody, filterSchedules, frequencyText, isFailing, isNetflowWithheld, isVisibilityScoped,
  periodLabel, relativeTo, runHealth, runLabel, scheduleToForm, scopeOf, soonestRun, templateTitle, toInstant,
  type NotificationChannel, type ReportSchedule, type StatusFilter,
} from '@/features/reports/schedules/model'
import {
  FormatBadge, RecipientChips, RecipientsSummary, RunHistory, RunStatusBadge, ScopeChips, Tip, scheduleErrorText,
  useNow,
} from '@/features/reports/schedules/ui'

type Ctx = {
  branding: Branding
  catalog: ReportCatalog | undefined
  channels: Map<string, NotificationChannel>
  now: number
  userId: string | undefined
  canManage: boolean
  running: Set<string>
  onRun: (s: ReportSchedule) => void
  onToggle: (s: ReportSchedule) => void
  onEdit: (s: ReportSchedule) => void
  onDuplicate: (s: ReportSchedule) => void
  onDelete: (s: ReportSchedule) => void
}

const STATUS_FILTERS: { id: StatusFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'Active' },
  { id: 'paused', label: 'Paused' },
  { id: 'failing', label: 'Failing' },
]

/** Enabled schedules by next run, then paused ones by name. */
function byNextRun(a: ReportSchedule, b: ReportSchedule): number {
  if (a.enabled !== b.enabled) return a.enabled ? -1 : 1
  const ta = toInstant(a.next_run_at)?.getTime() ?? Infinity
  const tb = toInstant(b.next_run_at)?.getTime() ?? Infinity
  return ta - tb || a.name.localeCompare(b.name)
}

export default function SchedulesPage() {
  const can = useCan()
  const user = useAuth((s) => s.user)
  const scoped = isVisibilityScoped(user)
  const catalog = useReportCatalog()
  const { branding, isSuccess: brandingLoaded } = useReportBranding()
  const schedules = useReportSchedules()
  const channelsQ = useNotificationChannels()
  const toggle = useToggleSchedule()
  const runNow = useRunScheduleNow()
  const save = useSaveSchedule()
  const del = useDeleteSchedule()
  const now = useNow(30_000)

  const canManage = can('reports.manage') && !scoped && catalog.data?.can?.manage !== false
  const [q, setQ] = useState('')
  const [status, setStatus] = useState<StatusFilter>('all')
  const [mine, setMine] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [running, setRunning] = useState<Set<string>>(new Set())
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ReportSchedule | null>(null)
  const [deleting, setDeleting] = useState<ReportSchedule | null>(null)

  const list = useMemo(() => [...(schedules.data ?? [])].sort(byNextRun), [schedules.data])
  const channels = useMemo(() => new Map((channelsQ.data ?? []).map((c) => [c.id, c])), [channelsQ.data])
  const titleOf = (s: ReportSchedule) => templateTitle(s, catalog.data)
  const shown = filterSchedules(list, { q, status, mineOnly: mine, userId: user?.id, titleOf })
  const failing = list.filter(isFailing)
  const counts: Record<StatusFilter, number> = {
    all: list.length,
    active: list.filter((s) => s.enabled).length,
    paused: list.filter((s) => !s.enabled).length,
    failing: failing.length,
  }
  const soonest = soonestRun(list, now)
  const hasOwners = list.some((s) => s.created_by)
  const filtered = q.trim() !== '' || status !== 'all' || mine

  const toggleExpanded = (id: string) =>
    setExpanded((cur) => {
      const next = new Set(cur)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const openNew = () => { setEditing(null); setFormOpen(true) }

  const ctx: Ctx = {
    branding,
    catalog: catalog.data,
    channels,
    now,
    userId: user?.id,
    canManage,
    running,
    onEdit: (s) => { setEditing(s); setFormOpen(true) },
    onDelete: (s) => setDeleting(s),
    onToggle: (s) => {
      toggle.mutateAsync(s.id).then(
        (r) => toast.success((r?.enabled ?? !s.enabled) ? 'Schedule enabled' : 'Schedule paused', `"${s.name}"`),
        (e) => toast.error('The schedule was not changed', scheduleErrorText(e)),
      )
    },
    onRun: (s) => {
      setRunning((cur) => new Set(cur).add(s.id))
      runNow.mutateAsync(s.id)
        .then((r) => {
          const st = runHealth(r?.status)
          const n = r?.delivered?.length ?? 0
          if (st === 'failed') toast.error(`"${s.name}" failed`, r?.errors?.join('; ') || 'The report could not be generated.')
          else if (st === 'partial') toast.warning(`"${s.name}" delivered with problems`, r?.errors?.join('; ') || undefined)
          else toast.success(`"${s.name}" generated`, n ? `Delivered to ${n} recipient${n === 1 ? '' : 's'}.` : 'Nobody is set to receive it; open it from the run history.')
        })
        .catch((e) => toast.error(`"${s.name}" did not run`, scheduleErrorText(e)))
        .finally(() => setRunning((cur) => {
          const next = new Set(cur)
          next.delete(s.id)
          return next
        }))
    },
    onDuplicate: (s) => {
      const body = { ...buildBody(scheduleToForm(s)), name: `${s.name} (copy)`.slice(0, 255), enabled: false }
      save.mutateAsync({ body }).then(
        (created) => {
          toast.success('Paused copy created', 'Review it, then enable it when it is ready to send.')
          if (created?.id) { setEditing(created); setFormOpen(true) }
        },
        (e) => toast.error('The schedule was not duplicated', scheduleErrorText(e)),
      )
    },
  }

  const tzLabel = `${branding.timezone.replace(/_/g, ' ')} (${zoneLabel(branding.timezone, now)})`

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-text">
            <CalendarClock className="h-6 w-6 text-primary" aria-hidden />
            Scheduled reports
          </h1>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            Generate reports automatically and deliver them by email and notification channels.
            {brandingLoaded && <> Times are in the organisation time zone, {tzLabel}.</>}
          </p>
        </div>
        {canManage && (
          <Button onClick={openNew} className="shrink-0">
            <Plus className="h-4 w-4" aria-hidden /> New schedule
          </Button>
        )}
      </div>

      {scoped && can('reports.manage') && (
        <div role="note" className="flex items-start gap-2 rounded-lg border border-info/30 bg-info/5 px-4 py-3 text-sm text-info-fg">
          <Lock className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
          <span>{SCOPED_EXPLANATION}</span>
        </div>
      )}

      {failing.length > 0 && status !== 'failing' && (
        <div role="alert" className="flex flex-col gap-3 rounded-lg border border-danger/30 bg-danger/5 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-start gap-2 text-sm text-danger-fg">
            <AlertOctagon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <div className="min-w-0">
              <div className="font-medium">
                {failing.length === 1 ? '1 schedule failed on its last run' : `${failing.length} schedules failed on their last run`}
              </div>
              <div className="truncate text-xs text-text2">
                {failing.slice(0, 3).map((s) => s.name).join(', ')}{failing.length > 3 ? ` and ${failing.length - 3} more` : ''}
              </div>
            </div>
          </div>
          <Button size="sm" variant="outline" className="shrink-0" onClick={() => setStatus('failing')}>Show failing</Button>
        </div>
      )}

      {/* Overview */}
      {schedules.data && list.length > 0 && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat label="Active" value={counts.active} hint={`of ${counts.all} schedule${counts.all === 1 ? '' : 's'}`} />
          <Stat label="Paused" value={counts.paused} hint="Run only when started by hand" />
          <Stat label="Failing" value={counts.failing} hint="Last run failed" tone={counts.failing ? 'danger' : undefined} />
          <Stat
            label="Next delivery"
            value={soonest ? relativeTo(soonest.at, now) : '—'}
            hint={soonest ? soonest.schedule.name : 'No active schedule'}
          />
        </div>
      )}

      <Card className="overflow-hidden">
        {/* Toolbar */}
        <div className="flex flex-col gap-3 border-b border-border p-3 lg:flex-row lg:items-center">
          <div className="relative min-w-0 flex-1 lg:max-w-sm">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" aria-hidden />
            <Input
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search name, template, owner or recipient"
              aria-label="Search schedules"
              className="pl-8"
            />
          </div>
          <div role="group" aria-label="Filter by status" className="flex flex-wrap gap-1">
            {STATUS_FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                aria-pressed={status === f.id}
                onClick={() => setStatus(f.id)}
                className={cn(
                  'inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
                  status === f.id ? 'border-primary bg-primary/10 text-text' : 'border-border bg-surface text-text2 hover:bg-surface2',
                )}
              >
                {f.label}
                <span className={cn('tabular-nums', f.id === 'failing' && counts.failing ? 'text-danger-fg' : 'text-muted')}>{counts[f.id]}</span>
              </button>
            ))}
          </div>
          {hasOwners && (
            <Select value={mine ? 'mine' : 'all'} onValueChange={(v) => setMine(v === 'mine')}>
              <SelectTrigger className="h-8 w-full text-xs lg:w-44" aria-label="Filter by owner"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Any owner</SelectItem>
                <SelectItem value="mine">Created by me</SelectItem>
              </SelectContent>
            </Select>
          )}
          {schedules.data && (
            <span className="text-xs text-muted lg:ml-auto" aria-live="polite">
              Showing <span className="tabular-nums">{shown.length}</span> of <span className="tabular-nums">{list.length}</span>
            </span>
          )}
        </div>

        {/* Body */}
        {schedules.isLoading ? (
          <LoadingRows />
        ) : schedules.isError && !schedules.data ? (
          <div className="p-4">
            <QueryFailure error={schedules.error} what="scheduled reports" onRetry={() => void schedules.refetch()} retrying={schedules.isFetching} />
          </div>
        ) : list.length === 0 ? (
          <EmptyState
            title="No scheduled reports yet"
            body="Pick a report template, choose when it runs and who receives it. Each run is kept in the history with a link to the report."
            action={canManage ? <Button onClick={openNew}><Plus className="h-4 w-4" aria-hidden /> New schedule</Button> : null}
            secondary={<Button asChild variant="outline"><Link to="/reports">Browse the report library</Link></Button>}
          />
        ) : shown.length === 0 ? (
          <EmptyState
            title="No schedules match"
            body={filtered ? 'Try another search or status.' : ''}
            action={<Button variant="outline" onClick={() => { setQ(''); setStatus('all'); setMine(false) }}><X className="h-4 w-4" aria-hidden /> Clear filters</Button>}
          />
        ) : (
          <>
            {schedules.isError && (
              <div className="border-b border-border p-3">
                <QueryFailure compact error={schedules.error} what="scheduled reports" onRetry={() => void schedules.refetch()} retrying={schedules.isFetching} />
              </div>
            )}
            {/* Table: md and up */}
            <div className="hidden md:block">
              <Table>
                <THead className="bg-surface2/60">
                  <Tr className="hover:bg-transparent">
                    <Th className="min-w-[200px]">Schedule</Th>
                    <Th className="hidden xl:table-cell">Frequency</Th>
                    <Th>Recipients</Th>
                    <Th className="hidden xl:table-cell">Format</Th>
                    <Th><span className="xl:hidden">When</span><span className="hidden xl:inline">Next run</span></Th>
                    <Th className="hidden xl:table-cell">Last run</Th>
                    <Th className="hidden xl:table-cell">Owner</Th>
                    <Th className="text-center">On</Th>
                    <Th className="w-24 text-right"><span className="sr-only">Actions</span></Th>
                  </Tr>
                </THead>
                <TBody>
                  {shown.map((s) => (
                    <ScheduleRow key={s.id} s={s} ctx={ctx} open={expanded.has(s.id)} onExpand={() => toggleExpanded(s.id)} />
                  ))}
                </TBody>
              </Table>
            </div>
            {/* Cards: below md */}
            <ul className="divide-y divide-border md:hidden" aria-label="Scheduled reports">
              {shown.map((s) => (
                <ScheduleCard key={s.id} s={s} ctx={ctx} open={expanded.has(s.id)} onExpand={() => toggleExpanded(s.id)} />
              ))}
            </ul>
          </>
        )}
      </Card>

      <ReportScheduleFormDialog open={formOpen} onOpenChange={setFormOpen} schedule={editing} />

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete schedule"
        description={
          <>
            Delete <span className="font-semibold text-text">{deleting?.name}</span>? It stops all future runs. Reports already generated stay available from their links.
          </>
        }
        confirmText="Delete schedule"
        destructive
        loading={del.isPending}
        onConfirm={() => {
          if (!deleting) return
          const s = deleting
          del.mutateAsync(s.id).then(
            () => { toast.success('Schedule deleted', `"${s.name}"`); setDeleting(null) },
            (e) => toast.error('The schedule was not deleted', scheduleErrorText(e)),
          )
        }}
      />
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Pieces                                                             */
/* ------------------------------------------------------------------ */

function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: string; tone?: 'danger' }) {
  return (
    <div className="min-w-0 rounded-lg border border-border bg-surface px-4 py-3 shadow-card dark:shadow-card-dark">
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted">{label}</div>
      <div className={cn('mt-0.5 truncate text-xl font-semibold tabular-nums', tone === 'danger' ? 'text-danger-fg' : 'text-text')}>{value}</div>
      {hint && <div className="truncate text-xs text-muted">{hint}</div>}
    </div>
  )
}

function LoadingRows() {
  return (
    <div className="divide-y divide-border" aria-busy="true" aria-label="Loading scheduled reports">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="flex items-center gap-4 px-4 py-4">
          <div className="flex-1 space-y-2"><Skeleton className="h-4 w-1/3" /><Skeleton className="h-3 w-1/2" /></div>
          <Skeleton className="hidden h-4 w-40 md:block" />
          <Skeleton className="hidden h-4 w-28 md:block" />
          <Skeleton className="h-5 w-9 rounded-full" />
        </div>
      ))}
    </div>
  )
}

function EmptyState({ title, body, action, secondary }: { title: string; body?: string; action?: ReactNode; secondary?: ReactNode }) {
  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-3 px-4 py-14 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10 text-primary">
        <CalendarClock className="h-6 w-6" aria-hidden />
      </div>
      <div>
        <h2 className="text-sm font-semibold text-text">{title}</h2>
        {body && <p className="mt-1 text-sm text-muted">{body}</p>}
      </div>
      {(action || secondary) && <div className="flex flex-wrap justify-center gap-2">{action}{secondary}</div>}
    </div>
  )
}

function ownerLabel(s: ReportSchedule, userId?: string): string {
  if (userId && s.created_by === userId) return 'You'
  return s.owner_name || (s.created_by ? 'Another user' : '—')
}

function NextRun({ s, ctx, compact }: { s: ReportSchedule; ctx: Ctx; compact?: boolean }) {
  if (!s.enabled) return <span className="text-sm text-muted">Paused</span>
  const at = toInstant(s.next_run_at)
  if (!at) return <span className="text-sm text-muted">Not scheduled</span>
  const overdue = at.getTime() < ctx.now - 5 * 60_000
  return (
    <div className={compact ? 'inline' : ''}>
      <div className="whitespace-nowrap text-sm tabular-nums text-text">{runLabel(at, ctx.branding)}</div>
      <div className={cn('text-xs tabular-nums', overdue ? 'text-warning-fg' : 'text-muted')}>
        {overdue ? `Due ${relativeTo(at, ctx.now)}` : relativeTo(at, ctx.now)}
      </div>
    </div>
  )
}

function LastRun({ s, ctx }: { s: ReportSchedule; ctx: Ctx }) {
  const at = toInstant(s.last_run_at)
  if (!at && !s.last_status) return <RunStatusBadge status={null} />
  return (
    <div className="space-y-0.5">
      <RunStatusBadge status={s.last_status} />
      {at && (
        <div className="text-xs tabular-nums text-muted" title={formatDateTime(at, ctx.branding, { zone: true })}>
          {relativeTo(at, ctx.now)}
        </div>
      )}
    </div>
  )
}

function RowActions({ s, ctx, open, onExpand }: { s: ReportSchedule; ctx: Ctx; open: boolean; onExpand: () => void }) {
  const editable = ctx.canManage && s.can_edit !== false
  const busy = ctx.running.has(s.id)
  const withheld = isNetflowWithheld(s)
  return (
    <div className="flex items-center justify-end gap-1">
      {editable && (
        <Tip content={busy ? 'Generating…' : 'Run now: generate and deliver immediately'}>
          <Button
            size="icon"
            variant="ghost"
            className="h-8 w-8"
            onClick={() => ctx.onRun(s)}
            disabled={busy}
            aria-label={busy ? `Running ${s.name}` : `Run ${s.name} now`}
          >
            {busy ? <Loader2 className="animate-spin" aria-hidden /> : <Play aria-hidden />}
          </Button>
        </Tip>
      )}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="icon" variant="ghost" className="h-8 w-8" aria-label={`More actions for ${s.name}`}>
            <MoreHorizontal aria-hidden />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          {editable && (
            <DropdownMenuItem onSelect={() => ctx.onEdit(s)}><Pencil aria-hidden /> Edit</DropdownMenuItem>
          )}
          {editable && (
            <DropdownMenuItem onSelect={() => ctx.onDuplicate(s)} disabled={withheld}>
              <Copy aria-hidden /> {withheld ? 'Duplicate (NetFlow scope hidden)' : 'Duplicate'}
            </DropdownMenuItem>
          )}
          <DropdownMenuItem onSelect={onExpand}>
            <History aria-hidden /> {open ? 'Hide run history' : 'Run history'}
          </DropdownMenuItem>
          {editable && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem destructive onSelect={() => ctx.onDelete(s)}><Trash2 aria-hidden /> Delete</DropdownMenuItem>
            </>
          )}
          {!editable && (
            <div className="flex items-start gap-1.5 px-2.5 py-1.5 text-[11px] text-muted">
              <Info className="mt-px h-3 w-3 shrink-0" aria-hidden />
              {ctx.canManage ? 'You cannot change this schedule.' : 'Viewing only: your role cannot change schedules.'}
            </div>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}

function EnabledSwitch({ s, ctx }: { s: ReportSchedule; ctx: Ctx }) {
  const editable = ctx.canManage && s.can_edit !== false
  return (
    <Switch
      checked={s.enabled}
      onCheckedChange={() => ctx.onToggle(s)}
      disabled={!editable}
      aria-label={`${s.enabled ? 'Pause' : 'Enable'} schedule ${s.name}`}
      title={editable ? undefined : 'Your role cannot change schedules'}
    />
  )
}

function ScheduleRow({ s, ctx, open, onExpand }: { s: ReportSchedule; ctx: Ctx; open: boolean; onExpand: () => void }) {
  const detailsId = `schedule-details-${s.id}`
  return (
    <Fragment>
      <Tr className={cn('align-top', !s.enabled && 'text-text2', open && 'bg-surface2/40')}>
        <Td>
          <div className="flex items-start gap-2">
            <button
              type="button"
              onClick={onExpand}
              aria-expanded={open}
              aria-controls={detailsId}
              aria-label={`${open ? 'Hide' : 'Show'} details and run history for ${s.name}`}
              className="mt-0.5 rounded p-0.5 text-muted hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
            >
              {open ? <ChevronDown className="h-4 w-4" aria-hidden /> : <ChevronRight className="h-4 w-4" aria-hidden />}
            </button>
            <div className="min-w-0 break-words">
              <div className="font-medium text-text">{s.name}</div>
              <div className="text-xs text-muted">
                {templateTitle(s, ctx.catalog)} · {periodLabel(s.period)}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-1.5 text-xs text-muted xl:hidden">
                <RunStatusBadge status={s.last_status} />
                {toInstant(s.last_run_at) && <span className="tabular-nums">{relativeTo(s.last_run_at, ctx.now)}</span>}
                <span>· by {ownerLabel(s, ctx.userId)}</span>
              </div>
            </div>
          </div>
        </Td>
        <Td className="hidden text-sm text-text2 xl:table-cell">{frequencyText(s, ctx.branding.time_format)}</Td>
        <Td>
          <div className="space-y-1">
            <RecipientsSummary channelIds={s.notify_channels ?? []} recipients={s.recipients ?? []} channels={ctx.channels} />
            <div className="xl:hidden"><FormatBadge format={s.format} /></div>
          </div>
        </Td>
        <Td className="hidden xl:table-cell"><FormatBadge format={s.format} /></Td>
        <Td>
          <div className="mb-0.5 text-xs text-text2 xl:hidden">{frequencyText(s, ctx.branding.time_format)}</div>
          <NextRun s={s} ctx={ctx} />
        </Td>
        <Td className="hidden xl:table-cell"><LastRun s={s} ctx={ctx} /></Td>
        <Td className="hidden text-sm text-text2 xl:table-cell">{ownerLabel(s, ctx.userId)}</Td>
        <Td className="text-center"><EnabledSwitch s={s} ctx={ctx} /></Td>
        <Td><RowActions s={s} ctx={ctx} open={open} onExpand={onExpand} /></Td>
      </Tr>
      {open && (
        <Tr className="hover:bg-transparent">
          <Td colSpan={9} className="bg-surface2/30 p-0">
            <div id={detailsId}><ScheduleDetails s={s} ctx={ctx} /></div>
          </Td>
        </Tr>
      )}
    </Fragment>
  )
}

function ScheduleCard({ s, ctx, open, onExpand }: { s: ReportSchedule; ctx: Ctx; open: boolean; onExpand: () => void }) {
  const detailsId = `schedule-card-details-${s.id}`
  return (
    <li className="space-y-3 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-medium text-text">{s.name}</div>
          <div className="text-xs text-muted">{templateTitle(s, ctx.catalog)} · {periodLabel(s.period)}</div>
        </div>
        <EnabledSwitch s={s} ctx={ctx} />
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        <div className="col-span-2">
          <dt className="text-[11px] uppercase tracking-wide text-muted">Frequency</dt>
          <dd className="text-text2">{frequencyText(s, ctx.branding.time_format)}</dd>
        </div>
        <div>
          <dt className="text-[11px] uppercase tracking-wide text-muted">Next run</dt>
          <dd><NextRun s={s} ctx={ctx} /></dd>
        </div>
        <div>
          <dt className="text-[11px] uppercase tracking-wide text-muted">Last run</dt>
          <dd><LastRun s={s} ctx={ctx} /></dd>
        </div>
        <div className="col-span-2">
          <dt className="text-[11px] uppercase tracking-wide text-muted">Delivery</dt>
          <dd className="mt-1 space-y-1.5">
            <div><FormatBadge format={s.format} /></div>
            <RecipientChips channelIds={s.notify_channels ?? []} recipients={s.recipients ?? []} channels={ctx.channels} />
          </dd>
        </div>
      </dl>
      <div className="flex items-center justify-between gap-2">
        <Button size="sm" variant="outline" onClick={onExpand} aria-expanded={open} aria-controls={detailsId}>
          <History className="h-3.5 w-3.5" aria-hidden /> {open ? 'Hide history' : 'Run history'}
        </Button>
        <RowActions s={s} ctx={ctx} open={open} onExpand={onExpand} />
      </div>
      {open && <div id={detailsId} className="-mx-4 border-t border-border"><ScheduleDetails s={s} ctx={ctx} /></div>}
    </li>
  )
}

function ScheduleDetails({ s, ctx }: { s: ReportSchedule; ctx: Ctx }) {
  const withheld = isNetflowWithheld(s)
  const scope = scopeOf(s.filters)
  return (
    <div className="grid grid-cols-1 gap-5 p-4 lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)]">
      <dl className="space-y-3 text-sm">
        {s.description && (
          <div>
            <dt className="text-[11px] uppercase tracking-wide text-muted">Description</dt>
            <dd className="text-text2">{s.description}</dd>
          </div>
        )}
        <div>
          <dt className="text-[11px] uppercase tracking-wide text-muted">Recipients</dt>
          <dd className="mt-1">
            <RecipientChips channelIds={s.notify_channels ?? []} recipients={s.recipients ?? []} channels={ctx.channels} />
          </dd>
        </div>
        {(scope.any || withheld) && (
          <div>
            <dt className="text-[11px] uppercase tracking-wide text-muted">Scope</dt>
            <dd className="mt-1"><ScopeChips filters={s.filters} withheld={withheld} tz={ctx.branding.timezone} /></dd>
          </div>
        )}
        <div>
          <dt className="text-[11px] uppercase tracking-wide text-muted">Owner</dt>
          <dd className="text-text2">{ownerLabel(s, ctx.userId)}</dd>
        </div>
        {s.last_error && isFailing(s) && (
          <div className="rounded-md border border-danger/30 bg-danger/5 px-3 py-2">
            <dt className="text-[11px] font-medium uppercase tracking-wide text-danger-fg">Last error</dt>
            <dd className="mt-0.5 break-words text-xs text-text">{s.last_error}</dd>
          </div>
        )}
      </dl>
      <div className="min-w-0">
        <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted">
          <History className="h-3.5 w-3.5" aria-hidden /> Run history
        </h3>
        <RunHistory scheduleId={s.id} branding={ctx.branding} now={ctx.now} />
      </div>
    </div>
  )
}
