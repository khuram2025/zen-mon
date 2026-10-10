import {
  AlertOctagon, AlertTriangle, CheckCircle2, FileText, HardDrive, Info, Loader2, ShieldCheck, TrendingUp,
} from 'lucide-react'
import { cn, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import {
  SEVERITY_STYLE, bytes, forecastSummary, spaceSegments, usageTextTone,
} from './storageMath'
import type { DiagnosticLog, MountInfo, Overview, StorageIssue, StorageSection, StorageStatus } from './types'
import { useOverview, useStorageStatus } from './useStorage'

const SEGMENT_COLORS: Record<string, string> = {
  clickhouse: 'bg-primary',
  clickhouse_diagnostic: 'bg-danger/70',
  clickhouse_inactive: 'bg-warning/70',
  clickhouse_detached: 'bg-warning',
  postgres: 'bg-info',
  backups: 'bg-accent',
  journal: 'bg-success/70',
  os_logs: 'bg-success',
  apt_cache: 'bg-info/50',
  other: 'bg-text2/30',
}

const ACTION_LABEL: Record<NonNullable<StorageIssue['action']>, { label: string; section: StorageSection }> = {
  expand: { label: 'Expand storage', section: 'disks' },
  claim: { label: 'Claim space', section: 'disks' },
  extend: { label: 'Allocate', section: 'disks' },
  add: { label: 'Add disk', section: 'disks' },
  rescan: { label: 'Disks & volumes', section: 'disks' },
  remove_missing: { label: 'Review', section: 'disks' },
}

export function OverviewSection({ onNavigate }: { onNavigate: (s: StorageSection) => void }) {
  const { data: overview, isLoading: overviewLoading } = useOverview()
  const { data: status } = useStorageStatus()

  return (
    <div className="space-y-4">
      <HealthCard status={status} onNavigate={onNavigate} />
      {overviewLoading || !overview ? (
        <Card>
          <CardContent className="flex items-center gap-2 py-6 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> Measuring storage…
          </CardContent>
        </Card>
      ) : (
        <>
          <CapacityCard overview={overview} onNavigate={onNavigate} />
          <ProtectionCard overview={overview} />
          <DiagnosticLogsCard overview={overview} />
        </>
      )}
    </div>
  )
}

function HealthCard({ status, onNavigate }: { status?: StorageStatus; onNavigate: (s: StorageSection) => void }) {
  if (!status) {
    return (
      <Card>
        <CardContent className="flex items-center gap-2 py-5 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> Checking disks and volumes…
        </CardContent>
      </Card>
    )
  }
  const issues = status.issues || []
  if (issues.length === 0) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-success/30 bg-success/10 px-4 py-3 text-sm text-success">
        <CheckCircle2 className="h-4 w-4 shrink-0" /> Storage is healthy. No action needed.
      </div>
    )
  }
  return (
    <div className="space-y-2" role="list" aria-label="Storage findings">
      {issues.map((issue, i) => {
        const style = SEVERITY_STYLE[issue.severity]
        const Icon = issue.severity === 'critical' ? AlertOctagon
          : issue.severity === 'info' ? Info : AlertTriangle
        const action = issue.action ? ACTION_LABEL[issue.action] : null
        return (
          <div key={`${issue.code}-${i}`} role="listitem"
            className={cn('flex flex-wrap items-center gap-3 rounded-lg border px-4 py-2.5', style.box)}>
            <Icon className={cn('h-4 w-4 shrink-0', style.text)} />
            <div className="min-w-0 flex-1">
              <div className={cn('text-sm font-medium', style.text)}>{issue.title}</div>
              {issue.detail && <div className="text-xs text-text2">{issue.detail}</div>}
            </div>
            {action && (
              <Button size="sm" variant="outline" onClick={() => onNavigate(action.section)}>
                {action.label}
              </Button>
            )}
          </div>
        )
      })}
    </div>
  )
}

function CapacityCard({ overview, onNavigate }: { overview: Overview; onNavigate: (s: StorageSection) => void }) {
  const forecast = forecastSummary(overview.forecast)
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <HardDrive className="h-4 w-4 text-primary" /> Capacity
        </CardTitle>
        <p className="text-xs text-muted">
          {overview.data_shares_os
            ? <>/data shares the root filesystem on this appliance (there is no separate data volume), so every file on it counts toward the usage thresholds. Adding a disk extends this filesystem. </>
            : null}
          Usage is used ÷ (used + available), as <span className="font-mono">df</span> reports it.
          Sampled {relativeTime(overview.sampled_at)}.
        </p>
      </CardHeader>
      <CardContent className="space-y-5">
        {overview.mounts.map((m) => (
          <MountUsage key={m.mount} mount={m} overview={overview} />
        ))}

        {overview.clickhouse_error && (
          <div className="flex items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" /> {overview.clickhouse_error}
          </div>
        )}

        {forecast && (
          <div className={cn(
            'flex flex-wrap items-center gap-3 rounded-lg border px-3 py-2.5 text-sm',
            forecast.tone === 'ok' ? 'border-border/60 bg-surface2/30'
              : forecast.tone === 'warning' ? 'border-warning/40 bg-warning/10' : 'border-danger/40 bg-danger/10',
          )}>
            <TrendingUp className={cn('h-4 w-4 shrink-0',
              forecast.tone === 'ok' ? 'text-primary' : forecast.tone === 'warning' ? 'text-warning' : 'text-danger')} />
            <span className="min-w-0 flex-1 text-text2">{forecast.text}</span>
            <Button size="sm" variant="ghost" onClick={() => onNavigate('retention')}>Retention</Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function MountUsage({ mount, overview }: { mount: MountInfo; overview: Overview }) {
  const pct = Math.min(100, mount.usage_percent || 0)
  const segments = spaceSegments(mount, overview.consumers || [], overview.data_shares_os)
  const capacity = mount.used_bytes + mount.free_bytes
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <span className="text-sm font-semibold">{mount.label}</span>
          <span className="font-mono text-[11px] text-muted">
            {mount.roles.includes('data') && mount.mount === '/' ? '/ and /data' : mount.mount}
          </span>
        </div>
        <div className="text-xs tabular-nums text-text2">
          <span className={cn('text-base font-semibold', usageTextTone(pct))}>{pct.toFixed(1)}%</span>
          <span className="ml-2">{bytes(mount.used_bytes)} used</span>
          <span className="ml-2">· {bytes(mount.free_bytes)} available</span>
          {mount.reserved_bytes > 0 && (
            <span className="ml-2 text-muted" title="Blocks reserved for the root user: neither used nor available to ZenPlus">
              · {bytes(mount.reserved_bytes)} reserved
            </span>
          )}
          <span className="ml-2 text-muted">· {bytes(mount.total_bytes)} filesystem</span>
        </div>
      </div>
      <div
        className="flex h-3 w-full overflow-hidden rounded-full bg-surface2"
        role="img"
        aria-label={`${mount.label}: ${pct.toFixed(1)}% used`}
      >
        {segments.map((s) => (
          <div
            key={s.id}
            title={`${s.label}: ${bytes(s.bytes)}`}
            className={cn('h-full', SEGMENT_COLORS[s.id] || 'bg-primary')}
            style={{ width: `${Math.max(0.4, (s.bytes / Math.max(1, capacity)) * 100)}%` }}
          />
        ))}
      </div>
      {/* Threshold markers make the warning levels visible on the bar. */}
      <div className="relative mt-0.5 h-2" aria-hidden>
        {[85, 95].map((t) => (
          <span key={t} className={cn('absolute top-0 h-2 w-px', t === 95 ? 'bg-danger/60' : 'bg-warning/60')}
            style={{ left: `${t}%` }} />
        ))}
      </div>
      <div className="mt-1 grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
        {segments.map((s) => (
          <div key={s.id} className="flex items-center justify-between gap-2 text-xs">
            <span className="flex min-w-0 items-center gap-2 text-text2">
              <span className={cn('h-2.5 w-2.5 shrink-0 rounded-sm', SEGMENT_COLORS[s.id] || 'bg-primary')} />
              <span className="truncate" title={s.hint ? `${s.label} — ${s.hint}` : s.label}>{s.label}</span>
            </span>
            <span className="shrink-0 tabular-nums text-text">{bytes(s.bytes)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function ProtectionCard({ overview }: { overview: Overview }) {
  const wd = overview.watchdog
  const fs = wd.filesystems?.[0]
  const growth = overview.clickhouse?.growth || []
  const stateTone = !wd.alive ? 'warning'
    : wd.state === 'critical' ? 'danger' : wd.state === 'pressure' || wd.state === 'growth' ? 'warning' : 'success'
  const stateLabel = !wd.installed ? 'Not installed' : !wd.alive ? 'Not reporting'
    : ({ ok: 'Healthy', growth: 'Fast growth', pressure: 'Freeing space', critical: 'Critical' } as const)[wd.state || 'ok']
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-primary" /> Low-space protection
          </CardTitle>
          <Badge variant={stateTone as any}>{stateLabel}</Badge>
        </div>
        <p className="text-xs text-muted">
          An independent watchdog checks free space every 30 seconds, even when the API or PostgreSQL is
          down. Under pressure it removes, in order: leftover copies of diagnostic logs, the oldest
          diagnostic log days, whole diagnostic logs, then the least valuable monitoring data older than
          the minimum keep window. If nothing more is eligible it pauses host-agent and APM uploads
          (agents keep them) before the disk fills.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {wd.installed ? (
          <div className="grid gap-3 sm:grid-cols-4">
            <Stat label="Last check" value={wd.heartbeat ? relativeTime(new Date(wd.heartbeat * 1000).toISOString()) : '—'}
              sub={wd.alive ? 'Watchdog running' : 'Check zenplus-storage-watchdog'} warn={!wd.alive} />
            <Stat label="Growth" value={fs?.growth_bytes_per_hour != null ? `${bytes(Math.max(0, fs.growth_bytes_per_hour))}/h` : '—'}
              sub={fs?.runway_hours != null ? `Full in ~${fs.runway_hours < 48 ? `${fs.runway_hours.toFixed(1)} h` : `${Math.round(fs.runway_hours / 24)} days`} at this rate`
                : 'Not growing (15-minute sample)'} warn={fs?.state === 'growth'} />
            <Stat label="Pending deletion" value={bytes(wd.pending_bytes || 0)}
              sub="Scheduled, not yet returned to disk" />
            <Stat label="Last purge" value={wd.last_pass ? bytes(wd.last_pass.scheduled_bytes) : 'None'}
              sub={wd.last_pass ? `${wd.last_pass.action_count} action(s) ${relativeTime(new Date(wd.last_pass.at * 1000).toISOString())}` : 'No purge needed so far'} />
          </div>
        ) : (
          <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            The watchdog is not installed on this appliance yet; the API applies the same policy every 5 minutes
            while it is running. It is installed by the storage setup that runs on upgrade.
          </div>
        )}
        {wd.ingest_paused && (
          <div className="flex items-center gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            <AlertOctagon className="h-4 w-4 shrink-0" /> Host-agent and APM ingestion is paused until free space recovers.
          </div>
        )}
        {fs && fs.reasons.length > 0 && (
          <div className="text-xs text-text2">Current trigger: {fs.reasons.join('; ')}</div>
        )}
        {wd.last_error && (
          <div className="text-xs text-warning">Last error {relativeTime(new Date(wd.last_error.at * 1000).toISOString())}: {wd.last_error.message}</div>
        )}
        {growth.length > 0 && (
          <div>
            <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted">Fastest-growing tables (last hour)</div>
            <div className="space-y-1">
              {growth.slice(0, 6).map((g) => (
                <div key={g.table} className="flex items-center justify-between gap-3 text-xs">
                  <span className="truncate font-mono text-text2">{g.table}</span>
                  <span className="shrink-0 tabular-nums">+{bytes(g.bytes_per_hour)}/h <span className="text-muted">· {bytes(g.bytes)}</span></span>
                </div>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function Stat({ label, value, sub, warn }: { label: string; value: string; sub: string; warn?: boolean }) {
  return (
    <div className="rounded-lg border border-border/50 bg-surface2/20 p-3">
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted">{label}</div>
      <div className={cn('mt-0.5 text-lg font-semibold tabular-nums', warn && 'text-warning')}>{value}</div>
      <div className="text-[11px] text-muted">{sub}</div>
    </div>
  )
}

function DiagnosticLogsCard({ overview }: { overview: Overview }) {
  const logs = overview.diagnostic_logs || []
  if (logs.length === 0) return null
  const total = logs.reduce((a, l) => a + l.bytes, 0)
  const unbounded = logs.filter((l) => !l.legacy && !l.ttl_active)
  const legacy = logs.filter((l) => l.legacy)
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2">
          <FileText className="h-4 w-4 text-primary" /> ClickHouse diagnostic logs
          <span className="text-sm font-normal text-muted">· {bytes(total)}</span>
        </CardTitle>
        <p className="text-xs text-muted">
          ClickHouse's own query, trace and server logs. They are not monitoring data and are bounded by
          retention: warnings and errors only, daily partitions, removed after the period shown. They are
          the first data removed under disk pressure (error and crash logs are kept).
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {unbounded.length > 0 && (
          <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            {unbounded.length} log table(s) have no retention yet ({bytes(unbounded.reduce((a, l) => a + l.bytes, 0))}).
            Retention applies when ClickHouse restarts with the bounded logging configuration (done on upgrade);
            the previous tables are then kept as legacy copies and removed once expired.
          </div>
        )}
        <div className="max-h-72 overflow-y-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-muted">
                <th className="py-1 font-medium">Table</th>
                <th className="py-1 text-right font-medium">Size</th>
                <th className="py-1 text-right font-medium">Kept</th>
                <th className="py-1 text-right font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((l: DiagnosticLog) => (
                <tr key={l.table} className="border-t border-border/40">
                  <td className="py-1 font-mono">{l.table}</td>
                  <td className="py-1 text-right tabular-nums">{bytes(l.bytes)}</td>
                  <td className="py-1 text-right tabular-nums text-text2">{l.retention_days} days</td>
                  <td className="py-1 text-right">
                    {l.legacy ? <Badge variant="warning">legacy copy</Badge>
                      : l.protected ? <Badge variant="default">kept</Badge>
                      : l.ttl_active ? <Badge variant="success">bounded</Badge>
                      : <Badge variant="warning">no TTL</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {legacy.length > 0 && (
          <p className="text-[11px] text-muted">
            Legacy copies ({bytes(legacy.reduce((a, l) => a + l.bytes, 0))}) are left behind by ClickHouse upgrades,
            configuration changes or recoveries. The watchdog removes them once older than their log's retention, or
            first under disk pressure.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
