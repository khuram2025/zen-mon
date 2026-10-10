import { AlertTriangle, CheckCircle2, FileText, HardDrive, Loader2, ShieldCheck } from 'lucide-react'
import { cn, relativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { bytes, forecastSummary, issueText, logGroups, spaceSegments, usageTextTone } from './storageMath'
import type { Overview, StorageSection } from './types'
import { useOverview, useStorageStatus } from './useStorage'

const COLORS: Record<string, string> = {
  clickhouse: 'bg-primary', clickhouse_diagnostic: 'bg-warning', clickhouse_inactive: 'bg-info/60',
  clickhouse_detached: 'bg-danger/70', postgres: 'bg-info', backups: 'bg-accent',
  journal: 'bg-success/70', os_logs: 'bg-success', apt_cache: 'bg-info/50', other: 'bg-text2/30',
}

export function OverviewSection({ onNavigate }: { onNavigate: (s: StorageSection) => void }) {
  const { data: overview, isLoading, isError, refetch } = useOverview()
  const { data: status, isError: statusError } = useStorageStatus()
  const issues = status?.issues || []
  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h3 className="text-base font-semibold">Appliance storage</h3>
        <p className="text-xs text-muted">Capacity, automatic protection and data lifecycle in one place.</p></div>
      <div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => onNavigate('retention')}>Manage retention</Button>
        <Button size="sm" onClick={() => onNavigate('disks')}>Expand capacity</Button></div>
    </div>
    {isError && <div role="alert" className="rounded-lg border border-warning/40 p-3 text-sm">Storage measurements are unavailable.
      <Button variant="ghost" size="sm" onClick={() => refetch()}>Retry</Button></div>}
    {statusError && <p role="status" className="text-sm text-warning">Disk health could not be checked. Refresh or review Disks &amp; capacity.</p>}
    {status && issues.length === 0 && <div className="flex items-center gap-2 rounded-lg border border-success/30 bg-success/10 p-3 text-sm text-success">
      <CheckCircle2 className="h-4 w-4" /> Storage is healthy. No action needed.</div>}
    {issues.length > 0 && <div role="list" aria-label="Storage findings" className="space-y-2">{issues.map((issue, i) => {
      const [title, detail] = issueText(issue)
      return <div role="listitem" key={`${issue.code}-${i}`} className={cn('flex flex-wrap items-center gap-3 rounded-lg border p-3', issue.severity === 'critical' ? 'border-danger/40 bg-danger/10' : issue.severity === 'info' ? 'border-info/30 bg-info/5' : 'border-warning/30 bg-warning/5')}>
        <AlertTriangle className="h-4 w-4 shrink-0" /><div className="flex-1"><div className="text-sm font-medium">{title}</div><p className="text-xs text-text2">{detail}</p></div>
        {issue.action && <Button variant="outline" size="sm" onClick={() => onNavigate('disks')}>Review capacity</Button>}
      </div>
    })}</div>}
    {isLoading && <div className="flex items-center gap-2 p-4 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Measuring storage…</div>}
    {overview && <><Capacity overview={overview} /><Protection overview={overview} /><Logs overview={overview} /></>}
  </div>
}

function Capacity({ overview }: { overview: Overview }) {
  const forecast = forecastSummary(overview.forecast)
  return <Card><CardHeader className="pb-3"><CardTitle className="flex items-center gap-2"><HardDrive className="h-4 w-4 text-primary" /> Capacity &amp; usage</CardTitle>
    <p className="text-xs text-muted">Updated {relativeTime(overview.sampled_at)}. Usage excludes the system reserve. {overview.data_shares_os ? 'System and monitoring data share this capacity.' : 'System and monitoring data have separate capacity.'}</p></CardHeader>
    <CardContent className="space-y-5">{overview.mounts.map(m => {
      const segments = spaceSegments(m, overview.consumers || [], overview.data_shares_os)
      const measured = segments.reduce((s, c) => s + c.bytes, 0)
      const capacity = m.used_bytes + m.free_bytes
      // Preserve the real total used/free ratio even when category samples overlap.
      const scale = measured > m.used_bytes && measured > 0 ? m.used_bytes / measured : 1
      return <section key={m.mount} className="space-y-3" aria-label={m.roles.includes('system') && m.roles.includes('data') ? 'Appliance capacity' : m.roles.includes('data') ? 'Monitoring capacity' : 'System capacity'}>
        <div className="flex flex-wrap justify-between gap-2"><span className="text-sm font-semibold">{m.roles.includes('system') && m.roles.includes('data') ? 'Appliance capacity' : m.roles.includes('data') ? 'Monitoring capacity' : 'System capacity'}</span>
          <span className={cn('text-lg font-semibold tabular-nums', usageTextTone(m.usage_percent))}>{m.usage_percent.toFixed(1)}% used</span></div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Available" value={bytes(m.free_bytes)} /><Stat label="Used" value={bytes(m.used_bytes)} />
          <Stat label="Total capacity" value={bytes(m.total_bytes)} /><Stat label="System reserve" value={bytes(m.reserved_bytes)} />
        </div>
        <div className="relative flex h-3 overflow-hidden rounded-full bg-surface2" role="img" aria-label={`${m.usage_percent.toFixed(1)}% used, ${bytes(m.free_bytes)} available`}>
          {segments.map(c => <div key={c.id} className={COLORS[c.id] || COLORS.other} style={{ width: `${capacity > 0 ? c.bytes * scale / capacity * 100 : 0}%` }} title={`${c.label}: ${bytes(c.bytes)}`} />)}
          {overview.auto_purge.enabled && <div className="absolute inset-y-0 w-px bg-text/60" style={{ left: `${overview.auto_purge.threshold_pct}%` }} title={`Automatic cleanup at ${overview.auto_purge.threshold_pct}%`} />}
        </div>
        <div className="grid gap-x-8 gap-y-2 sm:grid-cols-2">{segments.map(c => <div key={c.id} className="flex items-center gap-2 text-xs" title={c.hint || undefined}>
          <span className={cn('h-2 w-2 shrink-0 rounded-full', COLORS[c.id] || COLORS.other)} /><span className="flex-1 text-text2">{c.label}</span><span className="tabular-nums">{bytes(c.bytes)}</span></div>)}</div>
        {measured > m.used_bytes && <p className="text-xs text-muted">Category samples overlap during background cleanup; the capacity total remains authoritative.</p>}
      </section>
    })}
    {(overview.os_usage_error || overview.clickhouse_error) && <p role="status" className="text-xs text-warning">Some category measurements are unavailable. Total capacity remains visible; refresh to retry.</p>}
    {forecast && <div className="rounded-lg border border-border/60 bg-surface2/20 p-3 text-xs text-text2"><p>{forecast.text}</p><p className="mt-1 text-muted">Estimate from stored history. Allow additional room for logs, backups and background maintenance.</p></div>}
    </CardContent></Card>
}

function Protection({ overview }: { overview: Overview }) {
  const wd = overview.watchdog
  const enabled = overview.auto_purge.enabled
  const label = !wd.installed || !wd.alive || wd.last_error ? 'Needs attention' : !enabled ? 'Automatic cleanup off' : wd.state === 'critical' ? 'Critical' : wd.state === 'pressure' ? 'Freeing space' : wd.state === 'growth' ? 'Fast growth' : 'Protected'
  const healthy = wd.alive && !wd.last_error && enabled && (!wd.state || wd.state === 'ok')
  return <Card><CardHeader className="pb-3"><div className="flex flex-wrap justify-between gap-2"><CardTitle className="flex items-center gap-2"><ShieldCheck className="h-4 w-4 text-primary" /> Automatic protection</CardTitle><Badge variant={healthy ? 'success' : 'warning'}>{label}</Badge></div>
    <p className="text-xs text-muted">Checks free space every 30 seconds. Eligible old logs are removed first, then older monitoring history within your retention policy. Protected recovery records are kept.</p></CardHeader>
    <CardContent className="space-y-3"><div className="grid gap-3 sm:grid-cols-4">
      <Stat label="Start cleanup" value={enabled ? `${overview.auto_purge.threshold_pct}% used` : 'Disabled'} />
      <Stat label="Cleanup target" value={`${overview.auto_purge.target_pct}% used`} />
      <Stat label="Minimum history" value={`${overview.auto_purge.min_keep_days} days`} />
      <Stat label="Pending reclaim" value={bytes(wd.pending_bytes || 0)} />
    </div>
    {(wd.filesystems || []).map((fs, i) => <div key={fs.mount} className="rounded-lg border border-border/50 p-3 text-xs text-text2">
      <div className="flex flex-wrap justify-between gap-2"><span className="font-medium">{overview.data_shares_os ? 'Appliance capacity' : `Storage area ${i + 1}`}</span><span>{fs.usage_pct.toFixed(1)}% used</span></div>
      <p className="mt-1">Free-space reserve: {bytes(fs.minimum_free_bytes ?? Math.min(overview.auto_purge.min_free_gib * 1024 ** 3, (fs.used_bytes + fs.avail_bytes) * 0.1))} · adapts to available disk capacity.</p>
      <p className="mt-1">{fs.growth_bytes_per_hour == null ? 'Collecting a growth sample.' : fs.growth_bytes_per_hour <= 0 ? 'No net growth in the recent sample.' : `Recent growth: ${bytes(fs.growth_bytes_per_hour)}/hour.`}{fs.runway_hours != null ? ` Estimated remaining capacity: ${fs.runway_hours.toFixed(1)} hours at this rate.` : ''}</p>
      {(fs.exhausted || fs.insufficient) && <p className="mt-1 text-warning">Cleanup cannot recover enough space. Expand capacity or review retention.</p>}
    </div>)}
    <p className="text-xs text-muted">Last check: {wd.heartbeat ? relativeTime(new Date(wd.heartbeat * 1000).toISOString()) : 'Awaiting first report'}. Capacity may take several minutes to return after cleanup.</p>
    {wd.ingest_paused && <p role="status" className="text-sm text-warning">Agent and application uploads are paused until free space recovers. Expand capacity if cleanup is insufficient.</p>}
    {wd.last_error && <p role="status" className="text-xs text-warning">A recent protection check failed. Contact support if this continues.</p>}
    </CardContent></Card>
}

function Logs({ overview }: { overview: Overview }) {
  const groups = logGroups(overview.diagnostic_logs || [])
  return <Card><CardHeader className="pb-3"><CardTitle className="flex items-center gap-2"><FileText className="h-4 w-4 text-primary" /> Log lifecycle</CardTitle>
    <p className="text-xs text-muted">Appliance logs support troubleshooting. Retained logs can remain large after an update until their retention expires; they are separate from monitoring history.</p></CardHeader>
    <CardContent className="space-y-3">
      {groups.length ? <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr className="text-muted"><th className="pb-2 font-medium">Category</th><th className="pb-2 text-right font-medium">Used</th><th className="pb-2 pl-4 font-medium">Automatic retention</th></tr></thead>
      <tbody>{groups.map((g, i) => <tr key={i} className="border-t border-border/40"><td className="py-2">{g.label}</td><td className="py-2 text-right tabular-nums">{bytes(g.bytes)}</td><td className="py-2 pl-4 text-text2">{g.policy}</td></tr>)}</tbody></table></div> : <p className="text-xs text-muted">{overview.clickhouse_error ? 'Log measurements are currently unavailable.' : 'No retained appliance logs measured.'}</p>}
      <p className="text-xs text-muted">Retained logs are checked hourly after their expiry date. Low-space protection may remove eligible logs earlier; incident and recovery records remain protected.</p>
    </CardContent></Card>
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="rounded-lg border border-border/50 bg-surface2/20 p-3"><div className="text-[11px] text-muted">{label}</div><div className="mt-1 text-base font-semibold tabular-nums">{value}</div></div>
}
