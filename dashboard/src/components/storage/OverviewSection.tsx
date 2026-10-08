import {
  AlertOctagon, AlertTriangle, CheckCircle2, HardDrive, Info, Loader2, TrendingUp,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import {
  SEVERITY_STYLE, bytes, forecastSummary, spaceSegments, usageTextTone,
} from './storageMath'
import type { MountInfo, Overview, StorageIssue, StorageSection, StorageStatus } from './types'
import { useOverview, useStorageStatus } from './useStorage'

const SEGMENT_COLORS: Record<string, string> = {
  clickhouse: 'bg-primary',
  postgres: 'bg-info',
  backups: 'bg-accent',
  journal: 'bg-warning',
  apt_cache: 'bg-success',
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
        <CapacityCard overview={overview} onNavigate={onNavigate} />
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
        {overview.data_shares_os && (
          <p className="text-xs text-muted">
            Monitoring data shares one volume with the operating system on this appliance, so
            every file on it counts toward the usage thresholds below. Adding a disk extends
            this volume.
          </p>
        )}
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
          <span className="ml-2">{bytes(mount.used_bytes)} used of {bytes(capacity)}</span>
          <span className="ml-2 text-muted">· {bytes(mount.free_bytes)} free</span>
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
