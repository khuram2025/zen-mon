import {
  ClockAlert, Copy, FlaskConical, Globe, Loader2, LockKeyhole, Network, Pause, Pencil, Plug, Radar, ShieldCheck, Trash2, Wrench,
} from 'lucide-react'
import { cn, relativeTime } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  BAND_TEXT, PulseDot, UptimeBars, buildDaySeries, dayTitle, pulseStatusOf, uptimeBand,
} from '@/components/services/uptime'
import { LEVEL_META, formatExact, type DailyRow, type ListRow } from './model'

export const BAR_DAYS = 30

export type LoadState = 'loading' | 'error' | 'ready'

const TYPE_META: Record<string, { label: string; Icon: typeof Globe }> = {
  http: { label: 'HTTP', Icon: Globe },
  tcp: { label: 'TCP', Icon: Plug },
  tls: { label: 'TLS', Icon: ShieldCheck },
  icmp: { label: 'ICMP', Icon: Radar },
  dns: { label: 'DNS', Icon: Network },
}

const TEST_HINT = 'Test now — runs one probe immediately and records its result. It does not change the schedule or the SLA confirmation policy.'

function statusWord(status: string): string {
  if (status === 'up') return 'Up'
  if (status === 'down') return 'Down'
  if (status === 'degraded') return 'Degraded'
  if (status === 'warning') return 'Warning'
  return 'Pending'
}

export function MonitorRow({
  check: c, stale, nowMs, selected, onToggle, uptime24h, uptimeState, dailyRows, barsState,
  canOperate, testing, onOpen, onTest, onEdit, onClone, onDelete,
}: {
  check: ListRow
  stale: boolean
  nowMs: number
  selected: boolean
  onToggle: () => void
  uptime24h: number | null | undefined
  uptimeState: LoadState
  dailyRows?: DailyRow[]
  barsState: LoadState
  canOperate: boolean
  testing: boolean
  onOpen: () => void
  onTest: () => void
  onEdit: () => void
  onClone: () => void
  onDelete: () => void
}) {
  const t = TYPE_META[c.check_type] || TYPE_META.http
  const level = LEVEL_META[c.level]
  const target = c.target_url || `${c.target_host}${c.target_port ? `:${c.target_port}` : ''}`
  const neverChecked = !c.last_check_at
  const pulse = stale ? 'unknown' : pulseStatusOf(c.status, c.enabled)

  let statusText: string
  let statusTone: string
  if (!c.enabled) {
    statusText = c.status === 'unknown' ? 'Paused' : `Paused · last ${statusWord(c.status)}`
    statusTone = 'text-muted'
  } else if (stale) {
    statusText = neverChecked ? 'No result yet' : c.status === 'unknown' ? 'No recent result' : `Last known ${statusWord(c.status)}`
    statusTone = 'text-warning'
  } else {
    statusText = statusWord(c.status)
    statusTone = c.status === 'up' ? 'text-success'
      : c.status === 'down' ? 'text-danger'
        : c.status === 'unknown' ? 'text-muted'
          : 'text-warning'
  }

  const lastExact = formatExact(c.last_check_at)
  const expected = c.status === 'down' && c.down_check_interval ? c.down_check_interval : c.check_interval
  const staleTitle = neverChecked
    ? `No probe result has been recorded yet. Expected every ${expected}s.`
    : `Evidence is stale: the last result was ${relativeTime(c.last_check_at)} (${lastExact}); expected every ${expected}s. The status shown is the last known state.`

  const cells = buildDaySeries(dailyRows || [], BAR_DAYS, nowMs).map((d) => ({ key: d.key, pct: d.pct, title: dayTitle(d) }))
  const band = uptimeBand(uptime24h)

  const bars = barsState === 'loading'
    ? <div className="h-full w-full animate-pulse rounded-[3px] bg-surface3/50" aria-label="Loading daily history" />
    : barsState === 'error'
      ? (
        <div className="flex h-full w-full items-center justify-center rounded-[3px] border border-dashed border-border text-2xs text-muted" title="The daily availability request failed; this is not a monitoring gap.">
          History unavailable
        </div>
      )
      : <UptimeBars cells={cells} className="h-full" />

  return (
    <div
      role="link"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => { if (e.key === 'Enter' && e.target === e.currentTarget) onOpen() }}
      className={cn(
        'group flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3 transition-colors hover:bg-surface2/40 focus:outline-none focus-visible:bg-surface2/60 lg:flex-nowrap',
        selected && 'bg-primary/[0.04]',
      )}
    >
      <span className="w-4 shrink-0" onClick={(e) => e.stopPropagation()}>
        {canOperate && (
          <input type="checkbox" checked={selected} onChange={onToggle} className="h-3.5 w-3.5 accent-primary" aria-label={`Select ${c.name}`} />
        )}
      </span>

      {/* Identity */}
      <div className="flex min-w-0 flex-1 basis-[calc(100%-1.75rem)] items-center gap-3 lg:w-[30%] lg:min-w-[220px] lg:flex-none lg:basis-auto">
        <PulseDot status={pulse} size="md" />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <span className="min-w-0 max-w-full truncate text-sm font-semibold text-text">{c.name}</span>
            <span
              className="inline-flex shrink-0 items-center gap-1 rounded border border-border bg-surface2/60 px-1.5 py-px text-2xs font-semibold uppercase tracking-wider text-muted"
              title={level ? `${level.label} — ${level.hint}` : undefined}
            >
              <t.Icon className="h-2.5 w-2.5" />{t.label}{level && <span className="text-muted/70">· {level.short}</span>}
            </span>
            {!c.enabled && (
              <span className="inline-flex shrink-0 items-center gap-1 rounded bg-surface3/70 px-1.5 py-px text-2xs font-semibold uppercase tracking-wider text-muted" title="Paused — not probing or alerting">
                <Pause className="h-2.5 w-2.5" />Paused
              </span>
            )}
            {stale && (
              <span className="inline-flex shrink-0 items-center gap-1 rounded bg-warning/10 px-1.5 py-px text-2xs font-semibold uppercase tracking-wider text-warning" title={staleTitle}>
                <ClockAlert className="h-2.5 w-2.5" />Stale
              </span>
            )}
            {c.in_maintenance && (
              <span className="inline-flex shrink-0 items-center gap-1 rounded bg-info/10 px-1.5 py-px text-2xs font-semibold uppercase tracking-wider text-info" title="In maintenance — alerts suppressed">
                <Wrench className="h-2.5 w-2.5" />Maint
              </span>
            )}
            {c.credential_id && (
              <LockKeyhole className="h-3 w-3 shrink-0 text-success" aria-label="Authenticated check" />
            )}
            {c.check_type === 'tls' && c.tls_days_remaining != null && c.tls_days_remaining < 30 && (
              <span className={cn(
                'shrink-0 rounded px-1.5 py-px font-mono text-2xs font-bold',
                c.tls_days_remaining < 7 ? 'bg-danger/15 text-danger' : 'bg-warning/15 text-warning',
              )}>
                cert {c.tls_days_remaining}d
              </span>
            )}
          </div>
          <div className="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs">
            <span className={cn('shrink-0 font-medium', statusTone)} title={stale ? staleTitle : undefined}>{statusText}</span>
            <span className="text-border-strong">·</span>
            <span className={cn('shrink-0 tabular-nums', stale ? 'text-warning' : 'text-muted')} title={`Last result: ${lastExact}`}>
              {neverChecked ? 'never checked' : relativeTime(c.last_check_at)}
            </span>
            <span className="text-border-strong">·</span>
            <span className="min-w-0 truncate font-mono text-muted" title={target}>{target}</span>
          </div>
        </div>
      </div>

      {/* 30-day bars: compact strip on narrow screens, column on desktop */}
      <div className="h-3 w-full pl-7 lg:hidden">{bars}</div>
      <div className="hidden h-7 min-w-0 flex-1 lg:block">{bars}</div>

      <div className="flex w-full items-center justify-end gap-3 pl-7 lg:w-auto lg:pl-0">
        {/* 24h uptime */}
        <span
          className={cn('w-16 shrink-0 text-right font-mono text-xs font-medium tabular-nums', uptimeState === 'ready' ? BAND_TEXT[band] : 'text-muted')}
          title={uptimeState === 'error'
            ? '24 h availability could not be loaded'
            : uptimeState === 'ready' && uptime24h == null ? 'No confirmed evidence in the last 24 h' : 'Confirmed availability, last 24 h'}
        >
          {uptimeState === 'loading'
            ? <span className="ml-auto block h-3 w-12 animate-pulse rounded bg-surface3/60" />
            : uptimeState === 'error' ? '?' : uptime24h != null ? `${uptime24h.toFixed(2)}%` : '—'}
        </span>

        {/* Response */}
        <span className="w-[64px] shrink-0 text-right font-mono text-xs tabular-nums text-text2" title="Last response time">
          {c.last_response_ms != null ? `${c.last_response_ms.toFixed(0)} ms` : '—'}
        </span>

        {/* Interval */}
        <span className="hidden w-14 shrink-0 text-right text-xs tabular-nums text-muted sm:inline" title={`Checked every ${c.check_interval}s · last result ${lastExact}`}>
          {c.check_interval}s
        </span>

        {/* Actions */}
        {canOperate && (
          <div className="ml-auto flex w-[128px] shrink-0 justify-end gap-0.5 opacity-70 transition-opacity group-hover:opacity-100 lg:ml-0" onClick={(e) => e.stopPropagation()}>
            <Button variant="ghost" size="icon" className="h-7 w-7" title={TEST_HINT} aria-label={`Test ${c.name} now`} onClick={onTest} disabled={testing}>
              {testing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FlaskConical className="h-3.5 w-3.5" />}
            </Button>
            <Button variant="ghost" size="icon" className="h-7 w-7" title="Edit" aria-label={`Edit ${c.name}`} onClick={onEdit}>
              <Pencil className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="icon" className="h-7 w-7" title="Duplicate" aria-label={`Duplicate ${c.name}`} onClick={onClone}>
              <Copy className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" title="Delete" aria-label={`Delete ${c.name}`} onClick={onDelete}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        )}
      </div>
    </div>
  )
}
