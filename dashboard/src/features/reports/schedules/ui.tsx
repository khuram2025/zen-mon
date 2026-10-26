/**
 * Presentation pieces shared by the Schedules page, the schedule editor and
 * the Channels → Routing summary. Colours are theme tokens; every status pairs
 * its colour with an icon and a text label.
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import {
  AppWindow, Bell, Boxes, CheckCircle2, CircleDashed, FileCode2, FileSpreadsheet, FileText, FileType,
  Info, Loader2, Lock, Mail, MapPin, Server, SkipForward, TriangleAlert, Users, XCircle,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { serverNow } from '@/lib/serverClock'
import { describeFailure } from '@/lib/access'
import { FilterChips } from '@/components/netflow/FilterChips'
import { flowFilterFromDraft, type FlowFilterDraft } from '@/lib/netflow/filters'
import { useRumApplications } from '../api'
import {
  SCOPED_EXPLANATION, formatLabel, runHealth, scopeOf,
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
  running: { label: 'Running…', cls: 'border-info/30 bg-info/10 text-info-fg', Icon: Loader2 },
  skipped: { label: 'Skipped', cls: 'border-border bg-surface2 text-text2', Icon: SkipForward },
  never: { label: 'Never run', cls: 'border-border bg-surface2 text-muted', Icon: CircleDashed },
  other: { label: '', cls: 'border-border bg-surface2 text-text2', Icon: Info },
}

export function RunStatusBadge({ status, className }: { status: string | null | undefined; className?: string }) {
  const h = runHealth(status)
  const meta = HEALTH[h]
  const raw = String(status)
  const label = h === 'other' ? raw.charAt(0).toUpperCase() + raw.slice(1) : meta.label
  return (
    <span className={cn('inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-xs font-medium', meta.cls, className)}>
      <meta.Icon className={cn('h-3.5 w-3.5 shrink-0', h === 'running' && 'animate-spin')} aria-hidden />
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

/** Recipients hidden from a caller who does not manage reports: the count only. */
export function WithheldRecipients({ count, channelCount }: { count: number | null | undefined; channelCount?: number }) {
  const n = Number(count) || 0
  const parts = [channelCount ? plural(channelCount, 'channel') : '', n ? plural(n, 'recipient') : ''].filter(Boolean)
  return (
    <span
      className="inline-flex items-center gap-1 whitespace-nowrap text-sm text-text2"
      title="Addresses are shown to people who manage reports."
    >
      <Users className="h-3.5 w-3.5 text-muted" aria-hidden />
      {parts.length ? parts.join(' · ') : 'No recipients'}
      <span className="sr-only">. Addresses are shown to people who manage reports.</span>
    </span>
  )
}

/** "2 channels · 3 emails" with the full list in a tooltip. */
export function RecipientsSummary({
  channelIds, recipients, channels, cc = [],
}: { channelIds: string[]; recipients: string[]; channels: Map<string, NotificationChannel>; cc?: string[] }) {
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
              <div className="text-xs font-medium uppercase tracking-wide text-muted">Channels</div>
              <div>{names.join(', ')}</div>
            </div>
          )}
          {nEm > 0 && (
            <div>
              <div className="text-xs font-medium uppercase tracking-wide text-muted">Email</div>
              <div className="break-all">{recipients.slice(0, 8).join(', ')}{nEm > 8 ? ` and ${nEm - 8} more` : ''}</div>
            </div>
          )}
          {cc.length > 0 && (
            <div>
              <div className="text-xs font-medium uppercase tracking-wide text-muted">CC</div>
              <div className="break-all">{cc.slice(0, 8).join(', ')}{cc.length > 8 ? ` and ${cc.length - 8} more` : ''}</div>
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
  channelIds, recipients, channels, cc = [],
}: { channelIds: string[]; recipients: string[]; channels: Map<string, NotificationChannel>; cc?: string[] }) {
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
      {cc.map((r) => (
        <li key={`cc-${r}`} className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-xs text-text">
          <span className="text-2xs font-semibold uppercase text-muted">CC</span>
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

/** Read-only scope chips: NetFlow filter, selected devices, application; or the withheld notice. */
export function ScopeChips({ filters, withheld, tz }: { filters: ScheduleFilters | null | undefined; withheld?: boolean; tz?: string }) {
  const scope = scopeOf(filters)
  const apps = useRumApplications(!!scope.applicationId)
  const appName = useMemo(
    () => apps.data?.find((a) => a.application_id === scope.applicationId)?.service_name,
    [apps.data, scope.applicationId],
  )
  const nf = useMemo(() => (scope.netflow ? flowFilterFromDraft(netflowDraft(scope.netflow)).filter : null), [scope.netflow])
  if (!scope.any && !withheld) return <span className="text-sm text-muted">Everything the schedule&apos;s owner can see</span>
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {scope.deviceIds.length > 0 && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-xs text-text">
            <Server className="h-3 w-3 text-muted" aria-hidden />
            <span className="text-text2">Devices</span>
            <span className="font-medium tabular-nums">{scope.deviceIds.length} selected</span>
          </span>
        )}
        {scope.groupIds.length > 0 && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-xs text-text">
            <Boxes className="h-3 w-3 text-muted" aria-hidden />
            <span className="text-text2">Groups</span>
            <span className="font-medium tabular-nums">{scope.groupIds.length} selected</span>
          </span>
        )}
        {scope.locations.length > 0 && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-xs text-text">
            <MapPin className="h-3 w-3 text-muted" aria-hidden />
            <span className="text-text2">Locations</span>
            <span className="font-medium">{scope.locations.join(', ')}</span>
          </span>
        )}
        {scope.deviceTypes.length > 0 && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-xs text-text">
            <Server className="h-3 w-3 text-muted" aria-hidden />
            <span className="text-text2">Types</span>
            <span className="font-medium">{scope.deviceTypes.join(', ')}</span>
          </span>
        )}
        {scope.applicationId && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-xs text-text">
            <AppWindow className="h-3 w-3 text-muted" aria-hidden />
            <span className="text-text2">Application</span>
            <span className="font-medium">{appName ?? scope.applicationId}</span>
          </span>
        )}
        {withheld && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-xs text-text2">
            <Lock className="h-3 w-3" aria-hidden /> NetFlow scope hidden from your role
          </span>
        )}
      </div>
      {nf && <FilterChips filter={nf} tz={tz} />}
    </div>
  )
}
