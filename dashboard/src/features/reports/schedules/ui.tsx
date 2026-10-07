/**
 * Presentation pieces shared by the Schedules page, the schedule editor and
 * the Channels → Routing summary. Colours are theme tokens; every status pairs
 * its colour with an icon and a text label.
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import {
  AppWindow, Bell, CheckCircle2, CircleDashed, ExternalLink, FileCode2, FileSpreadsheet, FileText, FileType,
  Info, Lock, Mail, Server, TriangleAlert, XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { serverNow } from '@/lib/serverClock'
import { describeFailure } from '@/lib/access'
import { formatDateTime, type Branding } from '@/lib/branding'
import { QueryFailure } from '@/components/ui/QueryState'
import { Skeleton } from '@/components/ui/Skeleton'
import { FilterChips } from '@/components/netflow/FilterChips'
import { flowFilterFromDraft, type FlowFilterDraft } from '@/lib/netflow/filters'
import { useScheduleRuns } from './api'
import {
  SCOPED_EXPLANATION, formatLabel, relativeTo, runHealth, runLabel, scopeOf, toInstant,
  type NotificationChannel, type RunHealth, type ScheduleFilters,
} from './model'

/** Current appliance time, refreshed on an interval so relative times stay true. */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => serverNow())
  useEffect(() => {
    const t = window.setInterval(() => setNow(serverNow()), intervalMs)
    return () => window.clearInterval(t)
  }, [intervalMs])
  return now
}

/** Error text for a failed schedule mutation; explains the visibility-scope refusal. */
export function scheduleErrorText(e: unknown): string {
  const f = describeFailure(e)
  if (f.kind === 'forbidden') {
    return /visibility scope/i.test(f.message) ? SCOPED_EXPLANATION : f.message || 'Your role cannot change report schedules.'
  }
  return f.message
}

/* ------------------------------------------------------------------ */
/*  Tooltip                                                            */
/* ------------------------------------------------------------------ */

export function Tip({ content, children, side = 'top' }: { content: ReactNode; children: ReactNode; side?: 'top' | 'bottom' | 'left' | 'right' }) {
  return (
    <TooltipPrimitive.Provider delayDuration={250}>
      <TooltipPrimitive.Root>
        <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
        <TooltipPrimitive.Portal>
          <TooltipPrimitive.Content
            side={side}
            sideOffset={6}
            collisionPadding={8}
            className="z-50 max-w-xs rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs text-text shadow-elevated"
          >
            {content}
          </TooltipPrimitive.Content>
        </TooltipPrimitive.Portal>
      </TooltipPrimitive.Root>
    </TooltipPrimitive.Provider>
  )
}

/* ------------------------------------------------------------------ */
/*  Status and format                                                  */
/* ------------------------------------------------------------------ */

const HEALTH: Record<RunHealth, { label: string; cls: string; Icon: typeof CheckCircle2 }> = {
  success: { label: 'Succeeded', cls: 'border-success/30 bg-success/10 text-success-fg', Icon: CheckCircle2 },
  partial: { label: 'Partial', cls: 'border-warning/35 bg-warning/10 text-warning-fg', Icon: TriangleAlert },
  failed: { label: 'Failed', cls: 'border-danger/30 bg-danger/10 text-danger-fg', Icon: XCircle },
  never: { label: 'Never run', cls: 'border-border bg-surface2 text-muted', Icon: CircleDashed },
  other: { label: '', cls: 'border-border bg-surface2 text-text2', Icon: Info },
}

export function RunStatusBadge({ status, className }: { status: string | null | undefined; className?: string }) {
  const h = runHealth(status)
  const meta = HEALTH[h]
  const label = h === 'other' ? String(status) : meta.label
  return (
    <span className={cn('inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-xs font-medium', meta.cls, className)}>
      <meta.Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
      {label}
    </span>
  )
}

const FORMAT_ICON: Record<string, typeof FileText> = {
  pdf: FileText, excel: FileSpreadsheet, html: FileCode2, csv: FileType, none: Mail,
}

export function FormatBadge({ format }: { format: string }) {
  const Icon = FORMAT_ICON[format] ?? FileText
  return (
    <span className="inline-flex items-center gap-1 whitespace-nowrap rounded-md border border-border bg-surface2/70 px-1.5 py-0.5 text-xs font-medium text-text2">
      <Icon className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />
      {formatLabel(format)}
    </span>
  )
}

/* ------------------------------------------------------------------ */
/*  Recipients                                                         */
/* ------------------------------------------------------------------ */

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

export function channelName(id: string, map: Map<string, NotificationChannel>): string {
  return map.get(id)?.name ?? 'Deleted channel'
}

/** "2 channels · 3 emails" with the full list in a tooltip. */
export function RecipientsSummary({
  channelIds, recipients, channels,
}: { channelIds: string[]; recipients: string[]; channels: Map<string, NotificationChannel> }) {
  const nCh = channelIds.length
  const nEm = recipients.length
  if (!nCh && !nEm) {
    return (
      <span className="inline-flex items-center gap-1 whitespace-nowrap text-xs text-warning-fg">
        <TriangleAlert className="h-3.5 w-3.5" aria-hidden /> No recipients
      </span>
    )
  }
  const names = channelIds.map((id) => channelName(id, channels))
  const label = [nCh ? plural(nCh, 'channel') : '', nEm ? plural(nEm, 'email') : ''].filter(Boolean).join(' · ')
  return (
    <Tip
      content={
        <div className="space-y-1.5">
          {nCh > 0 && (
            <div>
              <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Channels</div>
              <div>{names.join(', ')}</div>
            </div>
          )}
          {nEm > 0 && (
            <div>
              <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Email</div>
              <div className="break-all">{recipients.slice(0, 8).join(', ')}{nEm > 8 ? ` and ${nEm - 8} more` : ''}</div>
            </div>
          )}
        </div>
      }
    >
      <button
        type="button"
        className="inline-flex items-center gap-2 rounded text-sm text-text2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
        aria-label={`Recipients: ${label}`}
      >
        {nCh > 0 && (
          <span className="inline-flex items-center gap-1 tabular-nums"><Bell className="h-3.5 w-3.5 text-muted" aria-hidden />{nCh}</span>
        )}
        {nEm > 0 && (
          <span className="inline-flex items-center gap-1 tabular-nums"><Mail className="h-3.5 w-3.5 text-muted" aria-hidden />{nEm}</span>
        )}
        <span className="sr-only">{label}</span>
      </button>
    </Tip>
  )
}

/** Full recipient list as chips (details panel, mobile cards). */
export function RecipientChips({
  channelIds, recipients, channels,
}: { channelIds: string[]; recipients: string[]; channels: Map<string, NotificationChannel> }) {
  if (!channelIds.length && !recipients.length) {
    return <span className="text-sm text-muted">Nobody — runs appear in the history only.</span>
  }
  return (
    <ul className="flex flex-wrap gap-1.5" aria-label="Recipients">
      {channelIds.map((id) => {
        const ch = channels.get(id)
        return (
          <li
            key={`c-${id}`}
            className={cn(
              'inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs',
              ch ? 'border-border bg-surface2/70 text-text' : 'border-danger/30 bg-danger/5 text-danger-fg',
            )}
          >
            <Bell className="h-3 w-3" aria-hidden />
            {ch ? ch.name : 'Deleted channel'}
            {ch && ch.enabled === false && <span className="text-muted">(disabled)</span>}
          </li>
        )
      })}
      {recipients.map((r) => (
        <li key={`e-${r}`} className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-xs text-text">
          <Mail className="h-3 w-3" aria-hidden />
          <span className="break-all">{r}</span>
        </li>
      ))}
    </ul>
  )
}

/* ------------------------------------------------------------------ */
/*  Scope                                                              */
/* ------------------------------------------------------------------ */

function netflowDraft(scope: Record<string, unknown>): FlowFilterDraft {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(scope)) {
    if (v == null || v === '') continue
    out[k] = Array.isArray(v) ? v.map(String).join(',') : String(v)
  }
  return out as FlowFilterDraft
}

type RumApp = { application_id: string; service_name: string }

/** Read-only scope chips: NetFlow filter, selected devices, application; or the withheld notice. */
export function ScopeChips({ filters, withheld, tz }: { filters: ScheduleFilters | null | undefined; withheld?: boolean; tz?: string }) {
  const scope = scopeOf(filters)
  const apps = useQuery<{ applications: RumApp[] }>({
    queryKey: ['reports', 'rum-applications'],
    queryFn: async () => (await api.get('/reports/rum-applications')).data,
    enabled: !!scope.applicationId,
    staleTime: 5 * 60_000,
  })
  const appName = useMemo(
    () => apps.data?.applications?.find((a) => a.application_id === scope.applicationId)?.service_name,
    [apps.data, scope.applicationId],
  )
  const nf = useMemo(() => (scope.netflow ? flowFilterFromDraft(netflowDraft(scope.netflow)).filter : null), [scope.netflow])
  if (!scope.any && !withheld) return <span className="text-sm text-muted">Everything the schedule&apos;s owner can see</span>
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {scope.deviceIds.length > 0 && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-[11px] text-text">
            <Server className="h-3 w-3 text-muted" aria-hidden />
            <span className="text-text2">Devices</span>
            <span className="font-medium tabular-nums">{scope.deviceIds.length} selected</span>
          </span>
        )}
        {scope.applicationId && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-[11px] text-text">
            <AppWindow className="h-3 w-3 text-muted" aria-hidden />
            <span className="text-text2">Application</span>
            <span className="font-medium">{appName ?? scope.applicationId}</span>
          </span>
        )}
        {withheld && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-[11px] text-text2">
            <Lock className="h-3 w-3" aria-hidden /> NetFlow scope hidden from your role
          </span>
        )}
      </div>
      {nf && <FilterChips filter={nf} tz={tz} />}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Run history                                                        */
/* ------------------------------------------------------------------ */

export function RunHistory({ scheduleId, branding, now }: { scheduleId: string; branding: Branding; now: number }) {
  const q = useScheduleRuns(scheduleId)
  if (q.isLoading) {
    return (
      <div className="space-y-2" aria-busy="true" aria-label="Loading run history">
        {[0, 1, 2].map((i) => <Skeleton key={i} className="h-11 w-full" />)}
      </div>
    )
  }
  if (q.isError && !q.data) {
    return <QueryFailure compact error={q.error} what="the run history" onRetry={() => void q.refetch()} retrying={q.isFetching} />
  }
  const runs = q.data?.runs ?? []
  if (!runs.length) {
    return <p className="rounded-md border border-dashed border-border px-3 py-4 text-center text-sm text-muted">No runs yet. Runs appear here after the first scheduled or manual run.</p>
  }
  return (
    <ol className="divide-y divide-border overflow-hidden rounded-md border border-border bg-surface" aria-label="Recent runs">
      {runs.map((run) => {
        const at = run.generated_at
        return (
          <li key={run.id} className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <RunStatusBadge status={run.status} />
                {at ? (
                  <span className="text-sm tabular-nums text-text">
                    {formatDateTime(at, branding, { zone: true })}
                    <span className="ml-1.5 text-xs text-muted">{relativeTo(at, now)}</span>
                  </span>
                ) : (
                  <span className="text-sm text-muted">Time not recorded</span>
                )}
              </div>
              {run.delivered_to?.length > 0 ? (
                <div className="text-xs text-text2">
                  <span className="text-muted">Delivered to </span>
                  {run.delivered_to.join(', ')}
                </div>
              ) : (
                <div className="text-xs text-muted">Not delivered to anyone</div>
              )}
              {run.error && <div className="break-words text-xs text-danger-fg">{run.error}</div>}
            </div>
            {run.token ? (
              <a
                href={`/api/v1/reports/shared/${encodeURIComponent(run.token)}`}
                target="_blank"
                rel="noreferrer"
                className="inline-flex shrink-0 items-center gap-1 self-start rounded text-xs font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 sm:self-center"
              >
                <ExternalLink className="h-3.5 w-3.5" aria-hidden /> Open report
                <span className="sr-only"> generated {at ? runLabel(toInstant(at) ?? new Date(0), branding) : ''} (opens in a new tab)</span>
              </a>
            ) : (
              <span
                className="inline-flex shrink-0 items-center gap-1 self-start text-xs text-muted sm:self-center"
                title="Report links are shown to the schedule's owner and to people who manage reports"
              >
                <Lock className="h-3.5 w-3.5" aria-hidden /> Link restricted
                <span className="sr-only">: report links are shown to the schedule&apos;s owner and to people who manage reports</span>
              </span>
            )}
          </li>
        )
      })}
    </ol>
  )
}
