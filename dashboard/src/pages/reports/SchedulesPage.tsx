/**
 * Reports → Scheduled reports: every schedule with what it sends, when it runs
 * next (in the schedule's own time zone, with UTC), who receives it and how its last run went;
 * run now, edit, duplicate, pause and delete for report managers; and each
 * schedule's run history. The list is shared with Channels → Routing
 * (query key ['report-schedules']).
 *
 * Banners come from GET /report-schedules/status (scheduler heartbeat,
 * overdue schedules, whether report emails can carry a link) and from the
 * schedules themselves (a time in the name that differs from the stored time,
 * with a one-click fix). Classic report types can be converted to their
 * replacement templates. See docs/reports-delivery.md.
 */
import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertOctagon, ArrowRightLeft, CalendarClock, ChevronDown, ChevronRight, Clock, Copy, History, Info, Link2Off, Loader2,
  Lock, MoreHorizontal, Pencil, Play, Plus, Search, Settings2, Trash2, TriangleAlert, X,
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
  useConvertSchedule, useDeleteSchedule, useNotificationChannels, usePatchSchedule, useReportSchedules,
  useRunScheduleNow, useSaveSchedule, useScheduleStatus, useToggleSchedule,
} from '@/features/reports/schedules/api'
import {
  BASE_URL_SETTINGS_PATH, SCOPED_EXPLANATION, buildBody, catalogTitle, clockLabel, filterSchedules, frequencyText,
  isFailing, isLegacySchedule, isMine, isNetflowWithheld, isVisibilityScoped, localTimeLabel, nameTimeMismatch,
  periodLabel, relativeTo, replacementOf, runLabel, runResultMessage, scheduleToForm, scheduleZone, scopeOf, soonestRun,
  templateTitle, toInstant, utcClock, zoneName,
  type NotificationChannel, type ReportSchedule, type StatusFilter,
} from '@/features/reports/schedules/model'
import {
  FormatBadge, RecipientChips, RecipientsSummary, RunStatusBadge, ScopeChips, Tip, WithheldRecipients,
  scheduleErrorText, useNow,
} from '@/features/reports/schedules/ui'
import { RunHistory } from '@/features/reports/schedules/runs'
import { ReportSettingsDialog } from '@/features/reports/schedules/ReportSettingsDialog'

type Ctx = {
  branding: Branding
  catalog: ReportCatalog | undefined
  channels: Map<string, NotificationChannel>
  now: number
  userId: string | undefined
  canManage: boolean
  running: Set<string>
  retiming: Set<string>
  onRun: (s: ReportSchedule) => void
  onRetime: (s: ReportSchedule) => void
  onConvert: (s: ReportSchedule) => void
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
  { id: 'overdue', label: 'Overdue' },
  { id: 'retime', label: 'Time differs from name' },
]

/** Filters shown only while something matches them. */
const CONDITIONAL_FILTERS = new Set<StatusFilter>(['overdue', 'retime'])

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
  const patch = usePatchSchedule()
  const convert = useConvertSchedule()
  const statusQ = useScheduleStatus()
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
  const [converting, setConverting] = useState<ReportSchedule | null>(null)
  const [retiming, setRetiming] = useState<Set<string>>(new Set())
  const [settingsOpen, setSettingsOpen] = useState(false)

  const list = useMemo(() => [...(schedules.data ?? [])].sort(byNextRun), [schedules.data])
  const channels = useMemo(() => new Map((channelsQ.data ?? []).map((c) => [c.id, c])), [channelsQ.data])
  const titleOf = (s: ReportSchedule) => templateTitle(s, catalog.data)
  const shown = filterSchedules(list, { q, status, mineOnly: mine, userId: user?.id, titleOf })
  const failing = list.filter(isFailing)
  const overdue = list.filter((s) => s.overdue)
  const retime = list.filter((s) => s.suggested_time)
  const counts: Record<StatusFilter, number> = {
    all: list.length,
    active: list.filter((s) => s.enabled).length,
    paused: list.filter((s) => !s.enabled).length,
    failing: failing.length,
    overdue: overdue.length,
    retime: retime.length,
  }
  const st = statusQ.data
  const schedulerDown = st?.scheduler_healthy === false
  const overdueCount = Math.max(st?.overdue_count ?? 0, overdue.length)
  const lastTick = toInstant(st?.scheduler_last_tick_at)
  const soonest = soonestRun(list, now)
  const hasOwners = list.some((s) => s.owner_id)
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
    retiming,
    onEdit: (s) => { setEditing(s); setFormOpen(true) },
    onConvert: (s) => setConverting(s),
    onRetime: (s) => {
      const t = s.suggested_time
      if (!t) return
      setRetiming((cur) => new Set(cur).add(s.id))
      const tz = zoneName(scheduleZone(s, branding.timezone))
      patch.mutateAsync({ id: s.id, body: { hour: t.hour, minute: t.minute } })
        .then(
          () => toast.success('Time changed', `"${s.name}" now runs at ${clockLabel(t.hour, t.minute, branding.time_format)} ${tz}.`),
          (e) => toast.error('The time was not changed', scheduleErrorText(e)),
        )
        .finally(() => setRetiming((cur) => {
          const next = new Set(cur)
          next.delete(s.id)
          return next
        }))
    },
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
          const msg = runResultMessage(r, s.name)
          toast[msg.tone](msg.title, msg.body)
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
            {brandingLoaded && <> Each schedule runs in its own time zone, shown with UTC; new schedules use the organisation time zone, {tzLabel}.</>}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {catalog.data?.can?.manage && (
            <Button variant="outline" onClick={() => setSettingsOpen(true)}>
              <Settings2 className="h-4 w-4" aria-hidden /> Report settings
            </Button>
          )}
          {canManage && (
            <Button onClick={openNew}>
              <Plus className="h-4 w-4" aria-hidden /> New schedule
            </Button>
          )}
        </div>
      </div>

      {(schedulerDown || overdueCount > 0) && (
        <div role="alert" className="flex flex-col gap-3 rounded-lg border border-danger/30 bg-danger/5 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-start gap-2 text-sm text-danger-fg">
            <Clock className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <div className="min-w-0">
              <div className="font-medium">
                {schedulerDown
                  ? lastTick
                    ? `The report scheduler has not run since ${formatDateTime(lastTick, branding, { zone: true })} (${relativeTo(lastTick, now)}).`
                    : 'The report scheduler has not run yet.'
                  : `${overdueCount === 1 ? `1 ${scoped ? 'of your schedules is' : 'schedule is'}` : `${overdueCount} ${scoped ? 'of your schedules are' : 'schedules are'}`} overdue: due more than 10 minutes ago and not started.`}
              </div>
              <div className="text-xs text-text2">
                {schedulerDown
                  ? `Scheduled reports are not being generated${overdueCount ? `; ${overdueCount} ${scoped ? `of your schedule${overdueCount === 1 ? ' is' : 's are'}` : `schedule${overdueCount === 1 ? ' is' : 's are'}`} overdue` : ''}. Check that the ZenPlus services are running, or ask an administrator.`
                  : 'The scheduler is running; these runs may be stuck behind a slow report. They start on the next pass, or use Run now.'}
              </div>
            </div>
          </div>
          {overdue.length > 0 && status !== 'overdue' && (
            <Button size="sm" variant="outline" className="shrink-0" onClick={() => setStatus('overdue')}>Show overdue</Button>
          )}
        </div>
      )}

      {st?.share_links_available === false && (
        <div role="note" className="flex flex-col gap-3 rounded-lg border border-warning/35 bg-warning/5 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-start gap-2 text-sm text-text2">
            <Link2Off className="mt-0.5 h-4 w-4 shrink-0 text-warning-fg" aria-hidden />
            <div className="min-w-0">
              <div className="font-medium text-text">Report emails can&apos;t include a link: set the Appliance URL in Settings → General → Company.</div>
              <div className="text-xs">
                Until then emails say where to find the report in ZenPlus, and “Summary only” cannot be chosen for new or changed schedules.
              </div>
            </div>
          </div>
          {can('settings.manage') && (
            <Button asChild size="sm" variant="outline" className="shrink-0"><Link to={BASE_URL_SETTINGS_PATH}>Open settings</Link></Button>
          )}
        </div>
      )}

      {retime.length > 0 && status !== 'retime' && (
        <div role="note" className="flex flex-col gap-3 rounded-lg border border-warning/35 bg-warning/5 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-start gap-2 text-sm text-text2">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-warning-fg" aria-hidden />
            <div className="min-w-0">
              <div className="font-medium text-text">
                {retime.length === 1 ? '1 schedule runs' : `${retime.length} schedules run`} at a different time than {retime.length === 1 ? 'its' : 'their'} name says
              </div>
              <div className="truncate text-xs">
                {retime.slice(0, 3).map((s) => s.name).join(', ')}{retime.length > 3 ? ` and ${retime.length - 3} more` : ''}
              </div>
            </div>
          </div>
          <Button size="sm" variant="outline" className="shrink-0" onClick={() => setStatus('retime')}>Review</Button>
        </div>
      )}

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
            {STATUS_FILTERS.filter((f) => !CONDITIONAL_FILTERS.has(f.id) || counts[f.id] > 0 || status === f.id).map((f) => (
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
                <span className={cn('tabular-nums', (f.id === 'failing' || f.id === 'overdue') && counts[f.id] ? 'text-danger-fg' : f.id === 'retime' && counts.retime ? 'text-warning-fg' : 'text-muted')}>{counts[f.id]}</span>
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
      <ReportSettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} canEdit={canManage} />

      <ConfirmDialog
        open={!!converting}
        onOpenChange={(o) => !o && setConverting(null)}
        title="Convert to a template"
        description={converting && (
          <>
            Switch <span className="font-semibold text-text">{converting.name}</span> from the retired classic report to the{' '}
            <span className="font-semibold text-text">{catalogTitle(replacementOf(converting, catalog.data) ?? '', catalog.data)}</span> template?
            Recipients, timing, period and format stay the same; the content and figures come from the template from the next run on.
          </>
        )}
        confirmText="Convert"
        loading={convert.isPending}
        onConfirm={() => {
          if (!converting) return
          const s = converting
          convert.mutateAsync(s.id).then(
            (r) => { toast.success('Schedule converted', `"${s.name}" now uses ${templateTitle(r ?? s, catalog.data)}.`); setConverting(null) },
            (e) => toast.error('The schedule was not converted', scheduleErrorText(e)),
          )
        }}
      />

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
      <div className="text-xs font-medium uppercase tracking-wide text-muted">{label}</div>
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
  if (isMine(s, userId)) return 'You'
  return s.owner_name || (s.owner_id ? 'Another user' : '—')
}

/** The branding with the schedule's own zone: its times are shown in it. */
function zoned(s: ReportSchedule, ctx: Ctx): Branding {
  const tz = scheduleZone(s, ctx.branding.timezone)
  return tz === ctx.branding.timezone ? ctx.branding : { ...ctx.branding, timezone: tz }
}

/** "Daily at 08:00" over "08:00 Asia/Riyadh (05:00 UTC)", in the organisation's time format. */
function ScheduleTime({ s, ctx, className }: { s: ReportSchedule; ctx: Ctx; className?: string }) {
  const tz = scheduleZone(s, ctx.branding.timezone)
  const label = localTimeLabel(s, tz, new Date(ctx.now), ctx.branding.time_format)
  return (
    <div className={className}>
      <div>{frequencyText(s, ctx.branding.time_format)}</div>
      <div className="text-xs tabular-nums text-muted">{label}</div>
    </div>
  )
}

/** A time written in the schedule's name that disagrees with when it runs, with a one-click fix. */
function NameTimeWarning({ s, ctx }: { s: ReportSchedule; ctx: Ctx }) {
  const named = s.name_time_mismatch ?? nameTimeMismatch(s.name, s.hour, s.minute)
  if (!named) return null
  const tz = zoneName(scheduleZone(s, ctx.branding.timezone))
  const fmt = ctx.branding.time_format
  const t = s.suggested_time
  const editable = ctx.canManage && s.can_edit !== false
  const busy = ctx.retiming.has(s.id)
  return (
    <div role="note" className="mt-1 space-y-1 text-xs text-warning-fg">
      <div className="flex items-start gap-1">
        <TriangleAlert className="mt-px h-3 w-3 shrink-0" aria-hidden />
        <span>
          The name says {named}; it runs at {clockLabel(s.hour, s.minute, fmt)} {tz}. Check the time or the name.
        </span>
      </div>
      {t && editable && (
        <Tip content={`Changes the stored time from ${clockLabel(s.hour, s.minute, fmt)} to ${clockLabel(t.hour, t.minute, fmt)} ${tz}. Nothing else changes; the next run moves accordingly.`}>
          <Button
            size="sm"
            variant="outline"
            className="ml-4 h-6 px-2 text-xs"
            disabled={busy}
            onClick={() => ctx.onRetime(s)}
          >
            {busy ? <Loader2 className="h-3 w-3 animate-spin" aria-hidden /> : <Clock className="h-3 w-3" aria-hidden />}
            Run at {clockLabel(t.hour, t.minute, fmt)} {tz} instead
          </Button>
        </Tip>
      )}
    </div>
  )
}

/** "Classic" and "Overdue" markers next to the name. */
function ScheduleBadges({ s, ctx }: { s: ReportSchedule; ctx: Ctx }) {
  const legacy = isLegacySchedule(s, ctx.catalog)
  if (!legacy && !s.overdue) return null
  const replacement = legacy ? replacementOf(s, ctx.catalog) : null
  return (
    <span className="ml-1.5 inline-flex flex-wrap items-center gap-1 align-middle">
      {s.overdue && (
        <span
          className="inline-flex items-center gap-1 rounded border border-danger/30 bg-danger/10 px-1 py-px text-2xs font-medium text-danger-fg"
          title="Due more than 10 minutes ago and not started"
        >
          <Clock className="h-2.5 w-2.5" aria-hidden /> Overdue
        </span>
      )}
      {legacy && (
        <span
          className="rounded border border-border px-1 py-px text-2xs font-medium text-muted"
          title={`A retired classic report type. It keeps running${replacement ? `; convert it to ${catalogTitle(replacement, ctx.catalog)}` : ''}.`}
        >
          Classic
        </span>
      )}
    </span>
  )
}

/** Recipients, or only their number when the addresses are withheld from the caller. */
function Recipients({ s, ctx, summary }: { s: ReportSchedule; ctx: Ctx; summary?: boolean }) {
  const channelIds = s.notify_channels ?? []
  if (s.recipients_withheld) {
    if (summary) return <WithheldRecipients count={s.recipient_count} channelCount={channelIds.length} />
    return (
      <div className="space-y-1.5">
        {channelIds.length > 0 && <RecipientChips channelIds={channelIds} recipients={[]} channels={ctx.channels} />}
        <WithheldRecipients count={s.recipient_count} />
      </div>
    )
  }
  const cc = s.group_send ? s.cc ?? [] : []
  return summary
    ? <RecipientsSummary channelIds={channelIds} recipients={s.recipients ?? []} channels={ctx.channels} cc={cc} />
    : <RecipientChips channelIds={channelIds} recipients={s.recipients ?? []} channels={ctx.channels} cc={cc} />
}

/** Email options that differ from the defaults. */
function EmailOptionsSummary({ s }: { s: ReportSchedule }) {
  const items = [
    s.group_send ? 'One email to all recipients' : '',
    s.subject_template ? `Subject: ${s.subject_template}` : '',
    s.reply_to ? `Replies to ${s.reply_to}` : '',
    s.skip_when_empty ? 'Not sent when the report has no data' : '',
  ].filter(Boolean)
  if (!items.length) return null
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-muted">Email options</dt>
      <dd className="space-y-0.5 break-words text-text2">{items.map((t) => <div key={t}>{t}</div>)}</dd>
    </div>
  )
}

function NextRun({ s, ctx, compact }: { s: ReportSchedule; ctx: Ctx; compact?: boolean }) {
  if (!s.enabled) return <span className="text-sm text-muted">Paused</span>
  const at = toInstant(s.next_run_at)
  if (!at) return <span className="text-sm text-muted">Not scheduled</span>
  const overdue = s.overdue ?? at.getTime() < ctx.now - 5 * 60_000
  const b = zoned(s, ctx)
  const zone = zoneLabel(b.timezone, at)
  return (
    <div className={compact ? 'inline' : ''}>
      <div className="whitespace-nowrap text-sm tabular-nums text-text">
        {runLabel(at, b)} <span className="text-xs text-muted">{zone}</span>
      </div>
      <div className={cn('text-xs tabular-nums', overdue ? 'text-warning-fg' : 'text-muted')}>
        {zone !== 'UTC' && <>{utcClock(at)} · </>}
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
  const legacy = isLegacySchedule(s, ctx.catalog)
  const replacement = legacy ? replacementOf(s, ctx.catalog) : null
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
          {editable && legacy && replacement && (
            <DropdownMenuItem onSelect={() => ctx.onConvert(s)}>
              <ArrowRightLeft aria-hidden /> Convert to {catalogTitle(replacement, ctx.catalog)}
            </DropdownMenuItem>
          )}
          {editable && (
            <DropdownMenuItem onSelect={() => ctx.onDuplicate(s)} disabled={withheld || legacy}>
              <Copy aria-hidden /> {withheld ? 'Duplicate (NetFlow scope hidden)' : legacy ? 'Duplicate (classic type retired)' : 'Duplicate'}
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
            <div className="flex items-start gap-1.5 px-2.5 py-1.5 text-xs text-muted">
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
              <div className="font-medium text-text">{s.name}<ScheduleBadges s={s} ctx={ctx} /></div>
              <div className="text-xs text-muted">
                {templateTitle(s, ctx.catalog)} · {periodLabel(s.period)}
              </div>
              <NameTimeWarning s={s} ctx={ctx} />
              <div className="mt-1 flex flex-wrap items-center gap-x-1.5 text-xs text-muted xl:hidden">
                <RunStatusBadge status={s.last_status} />
                {toInstant(s.last_run_at) && <span className="tabular-nums">{relativeTo(s.last_run_at, ctx.now)}</span>}
                <span>· by {ownerLabel(s, ctx.userId)}</span>
              </div>
            </div>
          </div>
        </Td>
        <Td className="hidden text-sm text-text2 xl:table-cell"><ScheduleTime s={s} ctx={ctx} /></Td>
        <Td>
          <div className="space-y-1">
            <Recipients s={s} ctx={ctx} summary />
            <div className="xl:hidden"><FormatBadge format={s.format} /></div>
          </div>
        </Td>
        <Td className="hidden xl:table-cell"><FormatBadge format={s.format} /></Td>
        <Td>
          <ScheduleTime s={s} ctx={ctx} className="mb-1 text-xs text-text2 xl:hidden" />
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
          <div className="font-medium text-text">{s.name}<ScheduleBadges s={s} ctx={ctx} /></div>
          <div className="text-xs text-muted">{templateTitle(s, ctx.catalog)} · {periodLabel(s.period)}</div>
          <NameTimeWarning s={s} ctx={ctx} />
        </div>
        <EnabledSwitch s={s} ctx={ctx} />
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        <div className="col-span-2">
          <dt className="text-xs uppercase tracking-wide text-muted">Frequency</dt>
          <dd className="text-text2"><ScheduleTime s={s} ctx={ctx} /></dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wide text-muted">Next run</dt>
          <dd><NextRun s={s} ctx={ctx} /></dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wide text-muted">Last run</dt>
          <dd><LastRun s={s} ctx={ctx} /></dd>
        </div>
        <div className="col-span-2">
          <dt className="text-xs uppercase tracking-wide text-muted">Delivery</dt>
          <dd className="mt-1 space-y-1.5">
            <div><FormatBadge format={s.format} /></div>
            <Recipients s={s} ctx={ctx} />
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
            <dt className="text-xs uppercase tracking-wide text-muted">Description</dt>
            <dd className="text-text2">{s.description}</dd>
          </div>
        )}
        <div>
          <dt className="text-xs uppercase tracking-wide text-muted">Recipients</dt>
          <dd className="mt-1">
            <Recipients s={s} ctx={ctx} />
          </dd>
        </div>
        <EmailOptionsSummary s={s} />
        {(scope.any || withheld) && (
          <div>
            <dt className="text-xs uppercase tracking-wide text-muted">Scope</dt>
            <dd className="mt-1"><ScopeChips filters={s.filters} withheld={withheld} tz={ctx.branding.timezone} /></dd>
          </div>
        )}
        <div>
          <dt className="text-xs uppercase tracking-wide text-muted">Owner</dt>
          <dd className="text-text2">{ownerLabel(s, ctx.userId)}</dd>
        </div>
        {s.last_error && isFailing(s) && (
          <div className="rounded-md border border-danger/30 bg-danger/5 px-3 py-2">
            <dt className="text-xs font-medium uppercase tracking-wide text-danger-fg">Last error</dt>
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
