/**
 * Settings -> System status -> Overview: is this appliance healthy?
 *
 * CPU, memory, swap, load and uptime of the appliance itself, its services, its data stores and its software,
 * with the last 24 hours as a chart. Services are named by what they do ("Configuration database", "Web server"):
 * which software is behind them is not shown. Backend: GET /system/status and /system/status/history
 * (server/app/services/appliance_health.py). Storage findings come from the storage status the other sections use.
 * A reading the appliance could not take is shown as "—", never as zero.
 */
import { lazy, Suspense, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  Activity, AlertTriangle, CheckCircle2, Clock, Cpu, Database, HardDrive, Loader2, MemoryStick, Package, RefreshCw, Server,
} from 'lucide-react'
import { cn, relativeTime } from '@/lib/utils'
import { regionalDate } from '@/lib/regionalTime'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { StatusDot } from '@/components/ui/StatusDot'
import { KPI_LABEL, KPI_VALUE, kpiTone } from '@/components/ui/kpiTone'
import { issueText } from '@/components/storage/storageMath'
import { useStorageStatus } from '@/components/storage/useStorage'
import {
  INGESTION_LABEL, SERVICE_STATE, UNAVAILABLE, clockLabel, cpuTime, duration, fmtBytes, historyCoverage, loadTone, pct,
  seriesStats, serviceStateLabel, serviceSummary, storageChange, updateStatusLabel, usageTone,
  type ServiceRow, type SystemHistory, type SystemStatus, type Tone,
} from './healthMath'
import { useSystemHistory, useSystemStatus } from './useSystemStatus'

const HealthHistoryChart = lazy(() => import('./HealthHistoryChart'))

const RANGES = [1, 6, 24] as const
type StorageTarget = 'storage' | 'disks'

function when(iso: string | null | undefined): string {
  if (!iso) return UNAVAILABLE
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? regionalDate(d, 'datetime', [], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : UNAVAILABLE
}

export function HealthSection({ onNavigate }: { onNavigate: (section: StorageTarget) => void }) {
  const { data: status, isLoading, isError, isFetching, refetch } = useSystemStatus()
  const { data: storage } = useStorageStatus()
  const [hours, setHours] = useState<(typeof RANGES)[number]>(24)
  const history = useSystemHistory(hours)

  // Storage findings that need the operator, in the same words as the Storage section.
  const storageIssues = (storage?.issues || []).filter((i) => i.severity === 'critical' || i.severity === 'warning')

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold">Appliance health</h3>
          <p className="text-xs text-muted">
            CPU, memory, services and data stores of this appliance itself, not of the devices it monitors.
            {status ? ` Updated ${relativeTime(status.sampled_at)}.` : ''}
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={() => { refetch(); history.refetch() }} disabled={isFetching}>
          <RefreshCw className={cn(isFetching && 'animate-spin')} /> Refresh
        </Button>
      </div>

      {isError && !status && (
        <div role="alert" className="flex flex-wrap items-center gap-3 rounded-lg border border-warning/40 p-3 text-sm">
          Appliance health could not be read. <Button variant="ghost" size="sm" onClick={() => refetch()}>Retry</Button>
        </div>
      )}
      {isLoading && <div className="flex items-center gap-2 p-4 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Reading appliance health…</div>}

      {status && (
        <>
          <Verdict status={status} storageIssues={storageIssues.map((i) => ({ severity: i.severity as 'critical' | 'warning', text: issueText(i) }))} onNavigate={onNavigate} stale={isError} />
          <Tiles status={status} onNavigate={onNavigate} />
          <HistoryCard history={history.data} loading={history.isLoading} failed={history.isError} hours={hours} onHours={setHours} />
          <Services services={status.services} />
          <div className="grid gap-4 lg:grid-cols-2">
            <DataStores status={status} onNavigate={onNavigate} />
            <Software status={status} />
          </div>
        </>
      )}
    </div>
  )
}

function Verdict({ status, storageIssues, onNavigate, stale }: {
  status: SystemStatus
  storageIssues: { severity: 'critical' | 'warning'; text: [string, string] }[]
  onNavigate: (s: StorageTarget) => void
  stale: boolean
}) {
  const healthy = status.findings.length === 0 && storageIssues.length === 0
  const summary = serviceSummary(status.services)
  return (
    <div className="space-y-2">
      {stale && <p role="status" className="text-xs text-warning-fg">The last refresh failed; showing the reading from {relativeTime(status.sampled_at)}.</p>}
      {healthy ? (
        <div className="flex items-center gap-2 rounded-lg border border-success/30 bg-success/10 p-3 text-sm text-success-fg">
          <CheckCircle2 className="h-4 w-4 shrink-0" />
          <span>
            The appliance is healthy. {summary.running} of {summary.total} core services are running
            {summary.unknown > 0 ? ` (${summary.unknown} could not be checked)` : ''}.
          </span>
        </div>
      ) : (
        <div role="list" aria-label="Appliance findings" className="space-y-2">
          {status.findings.map((f) => (
            <div role="listitem" key={f.code} className={cn('flex items-center gap-3 rounded-lg border p-3 text-sm', f.severity === 'critical' ? 'border-danger/40 bg-danger/10' : 'border-warning/30 bg-warning/5')}>
              <AlertTriangle className={cn('h-4 w-4 shrink-0', f.severity === 'critical' ? 'text-danger-fg' : 'text-warning-fg')} />
              <span className="flex-1">{f.message}</span>
            </div>
          ))}
          {storageIssues.map((i, n) => (
            <div role="listitem" key={`storage-${n}`} className={cn('flex flex-wrap items-center gap-3 rounded-lg border p-3 text-sm', i.severity === 'critical' ? 'border-danger/40 bg-danger/10' : 'border-warning/30 bg-warning/5')}>
              <AlertTriangle className={cn('h-4 w-4 shrink-0', i.severity === 'critical' ? 'text-danger-fg' : 'text-warning-fg')} />
              <div className="flex-1"><div className="font-medium">{i.text[0]}</div><p className="text-xs text-text2">{i.text[1]}</p></div>
              <Button variant="outline" size="sm" onClick={() => onNavigate('storage')}>Open storage</Button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Tile({ label, value, tone, icon, children, onClick, actionLabel }: {
  label: string; value: string; tone: Tone; icon: ReactNode; children?: ReactNode; onClick?: () => void; actionLabel?: string
}) {
  const t = kpiTone(tone)
  return (
    <Card data-kpi={label} className={cn('relative flex h-full flex-col overflow-hidden', onClick && 'transition hover:border-primary/40 focus-within:border-primary/60')}>
      <span aria-hidden className={cn('absolute inset-x-0 top-0 h-[3px]', t.bar)} />
      <div className="flex items-start justify-between gap-2 px-4 pt-3">
        <div className="min-w-0">
          <div className={KPI_LABEL}>{label}</div>
          <div className={cn('mt-1.5', KPI_VALUE, value === UNAVAILABLE && 'text-muted')}>
            {onClick
              ? <button type="button" onClick={onClick} aria-label={actionLabel} className="after:absolute after:inset-0 after:content-[''] hover:underline focus-visible:outline-none">{value}</button>
              : value}
          </div>
        </div>
        <span aria-hidden className={cn('flex h-8 w-8 shrink-0 items-center justify-center rounded-lg [&_svg]:h-4 [&_svg]:w-4', t.chip)}>{icon}</span>
      </div>
      <div className="flex-1 px-4 pb-3 pt-1.5 text-xs leading-4 text-muted">{children}</div>
    </Card>
  )
}

function Tiles({ status, onNavigate }: { status: SystemStatus; onNavigate: (s: StorageTarget) => void }) {
  const { cpu, memory, disk, host } = status
  const load = cpu.load
  const noSwap = memory.swap_total_bytes === 0
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 2xl:grid-cols-6">
      <Tile label="CPU" value={pct(cpu.usage_pct)} tone={usageTone(cpu.usage_pct, 75, 90)} icon={<Cpu />}>
        {cpu.usage_pct == null ? 'CPU use could not be measured.' : (
          <>
            {cpu.window_s && cpu.window_s >= 60 ? 'Average of the last minute' : 'Measured just now'}
            {cpu.cores ? ` across ${cpu.cores} core${cpu.cores === 1 ? '' : 's'}` : ''}.
            {cpu.iowait_pct != null && cpu.iowait_pct >= 5 ? <span className="text-warning-fg"> {pct(cpu.iowait_pct)} waiting for disk.</span> : null}
            {cpu.steal_pct != null && cpu.steal_pct >= 5 ? <span className="text-warning-fg"> {pct(cpu.steal_pct)} taken by the hypervisor.</span> : null}
          </>
        )}
      </Tile>
      <Tile label="Memory" value={pct(memory.used_pct)} tone={usageTone(memory.used_pct, 90, 97)} icon={<MemoryStick />}>
        {memory.used_bytes == null ? 'Memory use could not be read.' : `${fmtBytes(memory.used_bytes)} of ${fmtBytes(memory.total_bytes)} in use; ${fmtBytes(memory.available_bytes)} available.`}
      </Tile>
      <Tile label="Swap" value={noSwap ? 'None' : pct(memory.swap_used_pct)} tone={noSwap ? 'muted' : usageTone(memory.swap_used_pct, 50, 80)} icon={<Server />}>
        {noSwap ? 'No swap space is configured.' : memory.swap_used_bytes == null ? 'Swap use could not be read.' : `${fmtBytes(memory.swap_used_bytes)} of ${fmtBytes(memory.swap_total_bytes)} in use.`}
      </Tile>
      <Tile label="Load" value={load ? load[0].toFixed(2) : UNAVAILABLE} tone={loadTone(load, cpu.cores)} icon={<Activity />}>
        {load ? `1-minute average; ${load[1].toFixed(2)} over 5 min, ${load[2].toFixed(2)} over 15 min.${cpu.cores ? ` ${cpu.cores}.00 would be every core busy.` : ''}` : 'Load could not be read.'}
      </Tile>
      <Tile label="Storage" value={disk ? pct(disk.used_pct) : UNAVAILABLE} tone={usageTone(disk?.used_pct, 85, 95)} icon={<HardDrive />} onClick={() => onNavigate('storage')} actionLabel="Open storage">
        {disk ? `${fmtBytes(disk.free_bytes)} available on the data volume.` : 'Storage use could not be read.'}
      </Tile>
      <Tile label="Uptime" value={duration(host.uptime_s)} tone={host.uptime_s == null ? 'muted' : 'primary'} icon={<Clock />}>
        {host.boot_time ? `Started ${when(host.boot_time)}.` : 'Start time could not be read.'}
      </Tile>
    </div>
  )
}

function HistoryCard({ history, loading, failed, hours, onHours }: {
  history: SystemHistory | undefined; loading: boolean; failed: boolean; hours: number; onHours: (h: (typeof RANGES)[number]) => void
}) {
  const points = history?.points || []
  const cpu = seriesStats(points, 'cpu', 'cpu_max')
  const mem = seriesStats(points, 'mem', 'mem_max')
  const coverage = history ? historyCoverage(history) : null
  const change = history ? storageChange(history.disk_change) : null
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2"><Activity className="h-4 w-4 text-primary" /> Last {hours === 1 ? 'hour' : `${hours} hours`}</CardTitle>
          <div role="group" aria-label="History range" className="flex overflow-hidden rounded-md border border-border text-xs">
            {RANGES.map((h) => (
              <button key={h} type="button" onClick={() => onHours(h)} aria-pressed={hours === h}
                className={cn('px-2.5 py-1 font-medium transition-colors', hours === h ? 'bg-primary/10 text-primary' : 'text-text2 hover:bg-surface2')}>
                {h} h
              </button>
            ))}
          </div>
        </div>
        <p className="text-xs text-muted">One sample a minute, kept for 24 hours. A break in a line means nothing was sampled then (the appliance or its web application was not running).</p>
      </CardHeader>
      <CardContent className="space-y-3">
        {loading && !history && <div className="flex h-40 items-center justify-center gap-2 text-xs text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading history…</div>}
        {(failed && !history) || (history && !history.available) ? (
          <p role="status" className="rounded-lg border border-border/60 bg-surface2/20 p-3 text-xs text-text2">
            History is unavailable right now (the service that keeps it is not answering). The current values above are live.
          </p>
        ) : null}
        {history?.available && points.length === 0 && (
          <p role="status" className="rounded-lg border border-border/60 bg-surface2/20 p-3 text-xs text-text2">
            History is being collected. The first points appear within a few minutes of an update that adds this page.
          </p>
        )}
        {history?.available && points.length > 0 && (
          <>
            <Suspense fallback={<div className="h-60" />}><HealthHistoryChart points={points} hours={hours} bucketS={history.bucket_s} /></Suspense>
            <dl className="grid gap-x-6 gap-y-1 text-xs text-text2 sm:grid-cols-2">
              <div className="flex justify-between gap-3"><dt>CPU average / busiest minute</dt><dd className="tabular-nums">{cpu ? `${pct(cpu.mean)} / ${pct(cpu.peak)}` : UNAVAILABLE}</dd></div>
              <div className="flex justify-between gap-3"><dt>Memory average / highest</dt><dd className="tabular-nums">{mem ? `${pct(mem.mean)} / ${pct(mem.peak)}` : UNAVAILABLE}</dd></div>
            </dl>
            <p className="text-xs text-muted">
              {coverage && !coverage.complete ? `Collected for ${duration(coverage.covered_s)} of this range so far. ` : ''}
              {change || ''}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function ServiceRows({ rows }: { rows: ServiceRow[] }) {
  return (
    <>
      {rows.map((s) => {
        const st = SERVICE_STATE[s.state] || SERVICE_STATE.unknown
        const running = s.state === 'running'
        return (
          <tr key={s.id} className="border-t border-border/40" data-service={s.id}>
            <th scope="row" className="py-2 pr-3 text-left font-normal">
              <span className="font-medium text-text">{s.label}</span>
            </th>
            <td className="py-2 pr-3">
              <span className={cn('inline-flex items-center gap-2 whitespace-nowrap', st.text)}><StatusDot status={st.dot} /> {serviceStateLabel(s)}</span>
            </td>
            <td className="py-2 pr-3 text-right tabular-nums">{running ? duration(s.uptime_s) : UNAVAILABLE}</td>
            <td className="py-2 pr-3 text-right tabular-nums">{running ? fmtBytes(s.memory_bytes) : UNAVAILABLE}</td>
            <td className="py-2 pr-3 text-right tabular-nums">{running ? cpuTime(s.cpu_seconds) : UNAVAILABLE}</td>
            <td className={cn('py-2 text-right tabular-nums', s.restarts ? 'font-medium text-warning-fg' : '')}>{s.restarts == null ? UNAVAILABLE : s.restarts}</td>
          </tr>
        )
      })}
    </>
  )
}

function Services({ services }: { services: ServiceRow[] }) {
  const core = services.filter((s) => s.core)
  const extra = services.filter((s) => !s.core)
  const th = 'pb-2 text-2xs font-semibold uppercase tracking-wider text-muted'
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2"><Server className="h-4 w-4 text-primary" /> Services</CardTitle>
        <p className="text-xs text-muted">The programs that make up the appliance. The databases are asked directly, so one that runs but does not answer shows as down.</p>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-xs">
            <thead>
              <tr>
                <th scope="col" className={cn(th, 'text-left')}>Service</th>
                <th scope="col" className={cn(th, 'text-left')}>State</th>
                <th scope="col" className={cn(th, 'text-right')}>Running for</th>
                <th scope="col" className={cn(th, 'text-right')}>Memory</th>
                <th scope="col" className={cn(th, 'text-right')} title="Processor time used since the service started">CPU time</th>
                <th scope="col" className={cn(th, 'text-right')} title="Automatic restarts since the appliance started">Restarts</th>
              </tr>
            </thead>
            <tbody>
              <ServiceRows rows={core} />
              {extra.length > 0 && (
                <>
                  <tr className="border-t border-border/40"><td colSpan={6} className="pb-1 pt-3 text-2xs font-semibold uppercase tracking-wider text-muted">Additional services</td></tr>
                  <ServiceRows rows={extra} />
                </>
              )}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-t border-border/40 py-2 first:border-t-0 first:pt-0">
      <div className="min-w-0"><div className="text-xs font-medium text-text">{label}</div>{hint && <div className="text-2xs text-muted">{hint}</div>}</div>
      <div className="shrink-0 text-right text-xs tabular-nums text-text2">{children}</div>
    </div>
  )
}

const TONE_TEXT: Record<Tone, string> = { primary: 'text-text2', warning: 'text-warning-fg', danger: 'text-danger-fg', muted: 'text-muted' }

function DataStores({ status, onNavigate }: { status: SystemStatus; onNavigate: (s: StorageTarget) => void }) {
  const ingestion = status.ingestion.monitoring ? INGESTION_LABEL[status.ingestion.monitoring] : null
  const paused = status.ingestion.uploads_paused
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2"><Database className="h-4 w-4 text-primary" /> Data</CardTitle>
        <p className="text-xs text-muted">How much each data store holds and whether results are being taken in.</p>
      </CardHeader>
      <CardContent>
        {status.data_stores.map((d) => (
          <Row key={d.id} label={d.label} hint={d.holds}>
            {d.size_bytes == null ? <span className="text-muted">Unavailable</span> : <>{fmtBytes(d.size_bytes)}<span className="text-muted"> {d.kind === 'memory' ? 'in memory' : 'on disk'}</span></>}
          </Row>
        ))}
        <Row label="Monitoring results" hint={ingestion?.hint || 'The state of result processing could not be read.'}>
          <span className={ingestion ? TONE_TEXT[ingestion.tone] : 'text-muted'}>{ingestion ? ingestion.label : 'Unavailable'}</span>
        </Row>
        <Row label="Agent and application uploads" hint="Paused only while free space is critically low; availability checks are never paused.">
          <span className={paused == null ? 'text-muted' : paused ? 'text-warning-fg' : 'text-text2'}>{paused == null ? 'Unavailable' : paused ? 'Paused' : 'Accepted'}</span>
        </Row>
        <div className="pt-2"><Button variant="outline" size="sm" onClick={() => onNavigate('storage')}><HardDrive /> Storage details</Button></div>
      </CardContent>
    </Card>
  )
}

function Software({ status }: { status: SystemStatus }) {
  const { software, host, clock } = status
  const ck = clockLabel(clock)
  const last = software.last_update
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2"><Package className="h-4 w-4 text-primary" /> Software and system</CardTitle>
        <p className="text-xs text-muted">What is installed and what it runs on.</p>
      </CardHeader>
      <CardContent>
        <Row label="ZenPlus version" hint={software.installed_at ? `Installed ${when(software.installed_at)}` : undefined}>
          {software.version || <span className="text-muted">Unavailable</span>}
        </Row>
        <Row label="Last update" hint={last?.at ? when(last.at) : undefined}>
          {last ? <span className={last.status === 'success' ? '' : 'text-warning-fg'}>{last.version || UNAVAILABLE} {updateStatusLabel(last.status)}</span> : <span className="text-muted">No update recorded</span>}
        </Row>
        <Row label="Processor" hint={host.cpu_model || undefined}>{host.cores ? `${host.cores} core${host.cores === 1 ? '' : 's'}` : <span className="text-muted">Unavailable</span>}</Row>
        <Row label="Clock" hint={clock.timezone ? `Time zone ${clock.timezone}` : undefined}><span className={TONE_TEXT[ck.tone]}>{ck.text}</span></Row>
        <div className="pt-2"><Button asChild variant="outline" size="sm"><Link to="/settings/general?tab=updates">Updates</Link></Button></div>
      </CardContent>
    </Card>
  )
}
