import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, Download, DownloadCloud, Eye, FileCode, GitCompare, Loader2, Lock, Pencil, Pin, PinOff, RefreshCw, ShieldCheck, Trash2, X,
} from 'lucide-react'
import { useCan } from '@/stores/auth'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Switch } from '@/components/ui/Switch'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { toast } from '@/components/ui/Toast'
import { NcmReadiness, type ReadinessAction } from '@/components/ncm/NcmReadiness'
import { Ago, EmptyState, Fact, Pagination, SectionError, StaleBanner, StateBadge, ToneIcon } from '@/components/ncm/ui'
import { HELP, absoluteUtc, categoryLabel, download, errorDetail, ncmError, scheduleSummary, stateOf } from '@/components/ncm/model'
import { DayPicker, TimezoneSelect } from '@/components/ncm/BulkDialogs'
import { DiffView } from '@/components/ncm/DiffView'
import { TrustDialog } from '@/components/ncm/TrustDialog'
import { DeviceCompliance } from '@/components/ncm/ComplianceWorkspace'
import { DeliveriesList, JobList } from '@/components/ncm/OperationsWorkspace'
import { CrossDeviceCompare } from '@/components/ncm/ArchiveTools'

const TABS = ['overview', 'versions', 'jobs', 'baselines', 'compliance', 'notifications', 'settings'] as const
const TYPE_LABEL: Record<string, string> = { running: 'Running configuration', startup: 'Startup configuration' }

export function NcmDevicePage() {
  const { deviceId = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const tab = (TABS as readonly string[]).includes(params.get('tab') || '') ? (params.get('tab') as string) : 'overview'
  const qc = useQueryClient()
  const can = useCan()
  const navigate = useNavigate()
  const [trustOpen, setTrustOpen] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [blocked, setBlocked] = useState<any>(null)
  const [editRequest, setEditRequest] = useState(0)

  const summary = useQuery<any>({ queryKey: ['ncm', 'device', deviceId], queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-summary`)).data, refetchInterval: 30000, enabled: !!deviceId })
  const device = summary.data
  const setTab = (t: string) => {
    if (dirty && t !== 'settings' && !window.confirm('Discard unsaved backup settings?')) return
    if (t !== 'settings') setDirty(false)
    const next = new URLSearchParams(params)
    if (t === 'overview') next.delete('tab'); else next.set('tab', t)
    setParams(next, { replace: true })
  }
  const onAction = (a: ReadinessAction) => {
    if (a === 'trust') setTrustOpen(true)
    else { setTab('settings'); if (can('ncm.manage')) setEditRequest((n) => n + 1) }
  }
  const backup = useMutation({
    mutationFn: async () => (await api.post(`/devices/${deviceId}/ncm-jobs`)).data,
    onSuccess: (d: any) => { toast.success(d.queued ? 'Backup queued' : 'A backup is already queued or running', 'Follow it under Jobs & attempts.'); qc.invalidateQueries({ queryKey: ['ncm'] }) },
    onError: (e: any) => {
      const d = errorDetail(e)
      if (d?.code === 'readiness_blocked') setBlocked(d)
      else toast.error('Backup not queued', ncmError(e))
    },
  })

  if (summary.isError && !device) {
    const notFound = (summary.error as any)?.response?.status === 404
    return (
      <div className="space-y-3">
        <BackLink dirty={dirty} />
        <Card><CardContent className="space-y-3 py-10 text-center">
          <p className="text-sm">{notFound ? 'Device not found or not in your access scope.' : 'This device could not be loaded. Its backup state is unknown until it loads.'}</p>
          {!notFound && <Button onClick={() => summary.refetch()}>Retry</Button>}
        </CardContent></Card>
      </div>
    )
  }
  if (!device) return <div className="space-y-3"><BackLink dirty={dirty} /><p className="text-sm text-muted">Loading device…</p></div>
  const s = stateOf(device)
  const canBackup = can('ncm.manage') && device.enrolled && device.ncm_enabled
  return (
    <div className="min-w-0 space-y-4">
      <BackLink dirty={dirty} />
      {summary.isError && <StaleBanner what="this device" updatedAt={summary.dataUpdatedAt} onRetry={() => summary.refetch()} retrying={summary.isFetching} />}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="flex flex-wrap items-center gap-2 text-2xl font-semibold tracking-tight">
            <FileCode className="h-5 w-5 shrink-0 text-primary" aria-hidden /><span className="min-w-0 break-all">{device.hostname}</span>
            <StateBadge device={device} />
            {device.enrolled && device.coverage === 'excluded' && <Badge variant="outline" title={device.eligibility_reason}>Outside coverage scope</Badge>}
          </h1>
          <p className="text-sm text-muted">{[device.ip, device.device_type, device.vendor, device.location].filter(Boolean).join(' · ')}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {can('ncm.trust') && <Button variant="outline" onClick={() => setTrustOpen(true)}><Lock className="h-4 w-4" /> SSH trust</Button>}
          <Button disabled={!canBackup || backup.isPending || summary.isError} onClick={() => backup.mutate()}
            title={!device.enrolled ? 'Enroll the device first (Settings)' : !device.ncm_enabled ? 'Collection is disabled' : undefined}>
            {backup.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <DownloadCloud className="h-4 w-4" />} Backup now
          </Button>
        </div>
      </div>

      <SummaryStrip device={device} onTab={setTab} />

      <Tabs value={tab} onValueChange={setTab}>
        <div className="-mx-1 overflow-x-auto px-1">
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="versions">Backups &amp; versions</TabsTrigger>
            <TabsTrigger value="jobs">Jobs &amp; attempts</TabsTrigger>
            <TabsTrigger value="baselines">Baselines &amp; recovery</TabsTrigger>
            <TabsTrigger value="compliance">Compliance</TabsTrigger>
            <TabsTrigger value="notifications">Notifications</TabsTrigger>
            <TabsTrigger value="settings">Settings{dirty ? ' •' : ''}</TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="overview" className="space-y-4">
          <Card><CardContent className="space-y-3 pt-4"><h2 className="font-semibold">Setup and readiness</h2><NcmReadiness deviceId={deviceId} onAction={onAction} /></CardContent></Card>
          <ChangeEvents deviceId={deviceId} />
        </TabsContent>
        <TabsContent value="versions"><VersionsTab device={device} /></TabsContent>
        <TabsContent value="jobs" className="space-y-4"><IncidentHistory deviceId={deviceId} /><JobList deviceId={deviceId} /></TabsContent>
        <TabsContent value="baselines"><BaselinesTab device={device} /></TabsContent>
        <TabsContent value="compliance"><DeviceCompliance deviceId={deviceId} /></TabsContent>
        <TabsContent value="notifications"><NotificationsTab device={device} onEdit={() => onAction('settings')} /></TabsContent>
        <TabsContent value="settings"><SettingsTab device={device} stale={summary.isError} onDirty={setDirty} editRequest={editRequest} onRemoved={() => navigate('/ncm?tab=devices')} /></TabsContent>
      </Tabs>

      <TrustDialog deviceId={deviceId} open={trustOpen} onOpenChange={setTrustOpen} />
      <Dialog open={!!blocked} onOpenChange={(o) => !o && setBlocked(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Backup not queued</DialogTitle><DialogDescription>{blocked?.message} Nothing was sent to the device.</DialogDescription></DialogHeader>
          <ul className="space-y-2 text-sm">{blocked?.blockers?.map((b: any) => (
            <li key={b.id} className="rounded-md border border-border p-2"><div className="flex items-center gap-2 font-medium"><ToneIcon tone="danger" />{b.label}</div><p className="mt-0.5 text-xs text-muted">{b.detail}</p>
              {b.id === 'trust' && <Button size="sm" variant="link" className="h-auto px-0" onClick={() => { setBlocked(null); setTrustOpen(true) }}>Set up SSH trust</Button>}
              {['profile', 'enabled', 'driver'].includes(b.id) && <Button size="sm" variant="link" className="h-auto px-0" onClick={() => { setBlocked(null); onAction('settings') }}>Open settings</Button>}
            </li>
          ))}</ul>
          <DialogFooter><Button onClick={() => setBlocked(null)}>Close</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function BackLink({ dirty }: { dirty: boolean }) {
  return (
    <Link to="/ncm?tab=devices" className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
      onClick={(e) => { if (dirty && !window.confirm('Discard unsaved backup settings?')) e.preventDefault() }}>
      <ArrowLeft className="h-4 w-4" aria-hidden /> NCCM devices
    </Link>
  )
}

// --------------------------------------------------------------------------- //
// Summary strip
// --------------------------------------------------------------------------- //
function SummaryStrip({ device: d, onTab }: { device: any; onTab: (t: string) => void }) {
  const s = stateOf(d)
  const card = 'min-w-0 rounded-lg border border-border bg-surface p-3 text-left transition-colors hover:border-primary/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50'
  return (
    <div className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <button type="button" className={card} onClick={() => onTab('versions')}>
        <div className="text-xs text-muted">Latest recoverable artifact</div>
        <div className="mt-1 flex items-center gap-1.5 text-sm font-medium"><ToneIcon tone={d.fresh ? 'success' : d.validated_at ? 'warning' : 'danger'} />{d.validated_at ? <>Validated <Ago iso={d.validated_at} /></> : 'No validated capture'}</div>
        <div className="mt-0.5 text-xs text-muted">{(d.config_types || []).map((t: string) => TYPE_LABEL[t]).join(', ')} · {d.versions} stored version{d.versions === 1 ? '' : 's'}</div>
        <div className="mt-0.5 text-xs">{d.last_restore_test ? <span className={d.last_restore_test.result === 'passed' ? 'text-success' : 'text-warning'}>Restore test {d.last_restore_test.result} <Ago iso={d.last_restore_test.tested_at} /></span> : <span className="text-muted">Never restore-tested</span>}</div>
      </button>
      <button type="button" className={card} onClick={() => onTab('jobs')}>
        <div className="text-xs text-muted">Last attempt</div>
        <div className="mt-1 flex items-center gap-1.5 text-sm font-medium">
          <ToneIcon tone={d.last_status === 'success' ? 'success' : d.last_status ? 'danger' : 'neutral'} />
          {d.last_attempt_at ? <>{d.last_status === 'success' ? 'Succeeded' : d.last_status === 'blocked' ? 'Blocked' : 'Failed'} <Ago iso={d.last_attempt_at} /></> : 'No attempt yet'}
        </div>
        {d.last_status && d.last_status !== 'success' && <div className="mt-0.5 line-clamp-2 text-xs text-danger" title={d.last_error}>{categoryLabel(d.last_failure_category)}: {d.last_error}</div>}
        {d.recommended_action && d.last_status !== 'success' && <div className="mt-0.5 line-clamp-2 text-xs text-muted">{d.recommended_action}</div>}
        {d.job_active && <div className="mt-0.5 text-xs text-info">A job is queued or running</div>}
      </button>
      <button type="button" className={card} onClick={() => onTab('settings')}>
        <div className="text-xs text-muted">Schedule</div>
        <div className="mt-1 text-sm font-medium">{d.enrolled ? scheduleSummary(d) : 'Not enrolled'}</div>
        <div className={cn('mt-0.5 text-xs', d.overdue ? 'text-warning' : 'text-muted')}>{!d.enrolled || !d.ncm_enabled ? (d.enrolled ? 'Collection disabled' : '') : d.next_due ? <>{d.overdue ? 'Overdue since ' : 'Next run '}<Ago iso={d.next_due} /> ({absoluteUtc(d.next_due)})</> : 'Manual backups only'}</div>
        {d.clock_anomaly && <div className="mt-0.5 text-xs text-warning">Last scheduled run is in the future — the appliance clock was corrected.</div>}
      </button>
      <div className={cn(card, 'hover:border-border')}>
        <div className="text-xs text-muted">Risk</div>
        <div className="mt-1 flex items-center gap-1.5 text-sm font-medium"><ToneIcon tone={s.tone} />{s.label}</div>
        {d.open_incident ? <div className="mt-0.5 text-xs text-danger">Open incident: {d.open_incident.occurrences} failure{d.open_incident.occurrences === 1 ? '' : 's'} since <Ago iso={d.open_incident.opened_at} /></div> : <div className="mt-0.5 text-xs text-muted">No open backup incident</div>}
        <div className="mt-0.5 text-xs text-muted">{d.compliance ? <>Compliance: <span className={d.compliance.fail ? 'text-danger' : 'text-success'}>{d.compliance.fail} failed</span>, {d.compliance.pass} passed{d.compliance.unknown ? `, ${d.compliance.unknown} unknown` : ''}</> : 'Compliance not assessed'}</div>
      </div>
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Versions (B15, B16, B17, F10)
// --------------------------------------------------------------------------- //
function VersionsTab({ device }: { device: any }) {
  const can = useCan()
  const qc = useQueryClient()
  const deviceId = device.device_id
  const [configType, setConfigType] = useState('running')
  const [protectedOnly, setProtectedOnly] = useState(false)
  const [page, setPage] = useState(1)
  const pageSize = 25
  useEffect(() => { setPage(1) }, [configType, protectedOnly])
  const versions = useQuery<any>({
    queryKey: ['ncm', 'configs', deviceId, configType, protectedOnly, page],
    queryFn: async () => (await api.get(`/devices/${deviceId}/configs`, { params: { config_type: configType, protected_only: protectedOnly, limit: pageSize, offset: (page - 1) * pageSize } })).data,
    refetchInterval: 15000,
  })
  const vlist: any[] = versions.data?.data || []
  const total = versions.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  useEffect(() => { if (page > pageCount) setPage(pageCount) }, [page, pageCount])
  const [viewing, setViewing] = useState<any>(null)
  const [cmp, setCmp] = useState<{ a: any; b: any } | null>(null)
  const [baselineFor, setBaselineFor] = useState<any>(null)
  const [restoreFor, setRestoreFor] = useState<any>(null)
  const [pick, setPick] = useState<string[]>([])
  const pin = useMutation({
    mutationFn: async (v: any) => api.put(`/devices/${deviceId}/configs/${v.id}/pin`, { pinned: !v.pinned }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ncm', 'configs', deviceId] }),
    onError: (e: any) => toast.error('Pin change failed', ncmError(e)),
  })
  async function downloadVersion(v: any, raw = false) {
    try {
      const c = (await api.get(`/devices/${deviceId}/configs/${v.id}`, { params: { raw } })).data
      download(`${device.hostname || 'device'}-${v.config_type}-${v.captured_at.slice(0, 19).replace(/[:T]/g, '-')}${raw ? '-RAW' : '-redacted'}.cfg`, c.content || '')
    } catch (e: any) { toast.error('Download failed', ncmError(e)) }
  }
  const togglePick = (id: string) => setPick((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p.slice(-1), id]))
  const picked = pick.map((id) => vlist.find((v) => v.id === id)).filter(Boolean)
  return (
    <div className="min-w-0 space-y-4">
      <Card className="min-w-0">
        <CardContent className="min-w-0 space-y-3 pt-4">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="mr-auto font-semibold">Stored versions <span className="text-sm font-normal text-muted">({versions.isError && !versions.data ? 'unknown' : total} {protectedOnly ? 'protected' : 'total'})</span></h2>
            <Select value={configType} onValueChange={(v) => { setConfigType(v); setPick([]) }}>
              <SelectTrigger aria-label="Configuration type" className="h-8 w-[200px]"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="running">Running configuration</SelectItem><SelectItem value="startup">Startup configuration</SelectItem></SelectContent>
            </Select>
            <Button size="sm" variant={protectedOnly ? 'default' : 'outline'} aria-pressed={protectedOnly} onClick={() => setProtectedOnly((x) => !x)}><ShieldCheck className="h-3.5 w-3.5" /> Protected recovery points</Button>
            <Button size="sm" variant="outline" disabled={picked.length !== 2} onClick={() => {
              const [a, b] = [...picked].sort((x: any, y: any) => x.captured_at.localeCompare(y.captured_at))
              setCmp({ a, b })
            }}><GitCompare className="h-3.5 w-3.5" /> Compare selected ({picked.length}/2)</Button>
          </div>
          <p className="text-xs text-muted">{HELP.retention}</p>
          {versions.isError && !versions.data ? <SectionError what="Version history" onRetry={() => versions.refetch()} />
            : versions.isPending ? <p className="text-sm text-muted">Loading versions…</p>
            : !vlist.length ? <EmptyState>{protectedOnly ? 'No pinned, baseline or restore-tested versions.' : 'No backups yet. Resolve readiness, then use “Backup now”.'}</EmptyState>
            : (
              <ul className="divide-y divide-border rounded-md border border-border">
                {vlist.map((v, i) => {
                  const prev = vlist[i + 1]
                  return (
                    <li key={v.id} className={cn('flex flex-wrap items-center gap-3 px-3 py-2', pick.includes(v.id) && 'bg-primary/5')}>
                      <input type="checkbox" className="h-4 w-4 accent-primary" aria-label={`Select version captured ${absoluteUtc(v.captured_at)} for comparison`} checked={pick.includes(v.id)} onChange={() => togglePick(v.id)} />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-1.5 text-sm">
                          <Ago iso={v.captured_at} className="font-medium" />
                          {v.is_change ? <Badge variant="warning">Changed</Badge> : <Badge variant="outline">No change</Badge>}
                          {v.validated ? <Badge variant="success"><ShieldCheck className="h-3 w-3" aria-hidden />Validated</Badge> : <Badge variant="warning">Unvalidated ({v.captured_by})</Badge>}
                          {v.active_baseline && <Badge variant="info">Baseline: {v.active_baseline}</Badge>}
                          {v.baseline_protected && !v.active_baseline && <Badge variant="outline" title="Referenced by a retired baseline; kept as protected history">Retired baseline</Badge>}
                          {v.pinned && <Badge variant="outline"><Pin className="h-3 w-3" aria-hidden />Pinned</Badge>}
                          {v.restore_test && <Badge variant={v.restore_test.result === 'passed' ? 'success' : 'warning'}>Restore {v.restore_test.result}</Badge>}
                          {v.change_events > 0 && <Badge variant="outline">{v.change_events} change event{v.change_events === 1 ? '' : 's'}</Badge>}
                        </div>
                        <div className="text-xs text-muted">{absoluteUtc(v.captured_at)} · {v.line_count} lines · {v.size_bytes.toLocaleString()} B · {v.platform || v.captured_by} · {v.artifact_format === 'cli_text' ? 'CLI text' : 'manual import'} · {v.hash}</div>
                      </div>
                      <div className="flex flex-wrap items-center gap-0.5">
                        <Button variant="ghost" size="icon" className="h-8 w-8" aria-label="View configuration" title="View" onClick={() => setViewing(v)}><Eye className="h-4 w-4" /></Button>
                        {prev && <Button variant="ghost" size="icon" className="h-8 w-8" aria-label="Compare with previous version" title="Compare with previous" onClick={() => setCmp({ a: prev, b: v })}><GitCompare className="h-4 w-4" /></Button>}
                        <Button variant="ghost" size="icon" className="h-8 w-8" aria-label="Download redacted copy" title="Download redacted copy" onClick={() => downloadVersion(v)}><Download className="h-4 w-4" /></Button>
                        {can('ncm.baseline') && (v.baseline_protected
                          ? <Button variant="ghost" size="icon" className="h-8 w-8" disabled aria-label="Protected by baseline history; cannot be unpinned" title="Protected by baseline history — it cannot be unpinned"><Lock className="h-4 w-4" /></Button>
                          : <Button variant="ghost" size="icon" className="h-8 w-8" aria-label={v.pinned ? 'Unpin version' : 'Pin version (never pruned)'} title={v.pinned ? 'Unpin' : 'Pin (never pruned)'} onClick={() => pin.mutate(v)}>{v.pinned ? <PinOff className="h-4 w-4" /> : <Pin className="h-4 w-4" />}</Button>)}
                        {can('ncm.baseline') && v.validated && <Button variant="ghost" size="sm" onClick={() => setBaselineFor(v)}>Set baseline</Button>}
                        {can('ncm.baseline') && <Button variant="ghost" size="sm" onClick={() => setRestoreFor(v)}>Record restore test</Button>}
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          <Pagination page={page} pageCount={pageCount} total={total} pageSize={pageSize} label="versions" onPage={setPage} />
        </CardContent>
      </Card>
      {viewing && <ConfigViewer device={device} version={viewing} onClose={() => setViewing(null)} onDownload={downloadVersion} />}
      <details className="rounded-lg border border-border bg-surface p-4">
        <summary className="cursor-pointer font-semibold">Compare with another device</summary>
        <div className="mt-3"><CrossDeviceCompare initialDeviceId={deviceId} /></div>
      </details>
      <CompareDialog deviceId={deviceId} pair={cmp} onClose={() => setCmp(null)} />
      <BaselineDialog deviceId={deviceId} version={baselineFor} onClose={() => setBaselineFor(null)} />
      <RestoreTestDialog deviceId={deviceId} version={restoreFor} onClose={() => setRestoreFor(null)} />
    </div>
  )
}

function ConfigViewer({ device, version, onClose, onDownload }: { device: any; version: any; onClose: () => void; onDownload: (v: any, raw?: boolean) => void }) {
  const can = useCan()
  const content = useQuery<any>({ queryKey: ['ncm', 'view', version.id], queryFn: async () => (await api.get(`/devices/${device.device_id}/configs/${version.id}`)).data, retry: 1 })
  const [find, setFind] = useState('')
  const lines = useMemo(() => (content.data?.content || '').split('\n'), [content.data])
  const matches = find.trim() ? lines.filter((l: string) => l.toLowerCase().includes(find.toLowerCase())).length : 0
  return (
    <Card>
      <CardContent className="space-y-2 pt-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-sm font-medium">{TYPE_LABEL[version.config_type]} · {absoluteUtc(version.captured_at)} <span className="text-xs text-muted">({version.line_count} lines, redacted view)</span></div>
          <div className="flex flex-wrap gap-2">
            <Input aria-label="Find in configuration" placeholder="Find…" value={find} onChange={(e) => setFind(e.target.value)} className="h-8 w-40" />
            {find && <span className="self-center text-xs text-muted">{matches} line{matches === 1 ? '' : 's'}</span>}
            <Button size="sm" variant="outline" onClick={() => onDownload(version)}><Download className="h-3.5 w-3.5" /> Redacted copy</Button>
            {can('ncm.export') && <Button size="sm" variant="outline" onClick={() => onDownload(version, true)}>Export raw recovery copy</Button>}
            <Button size="sm" variant="ghost" onClick={onClose} aria-label="Close configuration viewer"><X className="h-4 w-4" /> Close</Button>
          </div>
        </div>
        {content.isError ? (
          <div role="alert" className="flex items-center justify-between gap-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm text-warning">
            The configuration could not be loaded. <Button size="sm" variant="outline" onClick={() => content.refetch()} disabled={content.isFetching}><RefreshCw className={cn('h-3.5 w-3.5', content.isFetching && 'animate-spin')} /> Retry</Button>
          </div>
        ) : content.isPending ? <p className="text-sm text-muted">Loading configuration…</p> : (
          <pre className="max-h-[65vh] overflow-auto rounded-md border border-border bg-surface2/40 py-2 font-mono text-xs leading-relaxed" aria-label="Configuration content">
            {lines.map((l: string, i: number) => (
              <div key={i} className={cn('flex', find && l.toLowerCase().includes(find.toLowerCase()) && 'bg-warning/20')}>
                <span className="w-12 shrink-0 select-none pr-3 text-right text-muted">{i + 1}</span><span className="whitespace-pre pr-3">{l || ' '}</span>
              </div>
            ))}
          </pre>
        )}
      </CardContent>
    </Card>
  )
}

function CompareDialog({ deviceId, pair, onClose }: { deviceId: string; pair: { a: any; b: any } | null; onClose: () => void }) {
  const [normalized, setNormalized] = useState(true)
  const diff = useQuery<any>({
    queryKey: ['ncm', 'diff', deviceId, pair?.a?.id, pair?.b?.id, normalized],
    queryFn: async () => (await api.get(`/devices/${deviceId}/configs-diff`, { params: { a: pair!.a.id, b: pair!.b.id, normalized } })).data,
    enabled: !!pair,
  })
  return (
    <Dialog open={!!pair} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[92vh] max-w-5xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><GitCompare className="h-4 w-4" aria-hidden /> Configuration changes</DialogTitle>
          {pair && <DialogDescription>{pair.a.label || absoluteUtc(pair.a.captured_at)} → {absoluteUtc(pair.b.captured_at)}</DialogDescription>}
        </DialogHeader>
        <label className="flex items-center gap-2 text-sm"><Switch aria-label="Ignore runtime noise" checked={normalized} onCheckedChange={setNormalized} />Ignore runtime noise (timestamps, counters)</label>
        {diff.isError ? <SectionError what="The comparison" onRetry={() => diff.refetch()} /> : diff.isPending ? <p className="text-sm text-muted">Comparing…</p> : (
          <DiffView diff={diff.data.diff} added={diff.data.added} removed={diff.data.removed} identical={diff.data.identical}
            sensitiveChanged={diff.data.sensitive_changed} redacted={diff.data.redacted} normalized={diff.data.normalized} />
        )}
      </DialogContent>
    </Dialog>
  )
}

function BaselineDialog({ deviceId, version, onClose }: { deviceId: string; version: any; onClose: () => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [reason, setReason] = useState('')
  useEffect(() => { setName(''); setReason('') }, [version])
  const save = useMutation({
    mutationFn: async () => api.post(`/devices/${deviceId}/ncm-baselines`, { version_id: version.id, name, reason }),
    onSuccess: () => { toast.success('Baseline approved', 'The version is now protected from pruning.'); qc.invalidateQueries({ queryKey: ['ncm'] }); onClose() },
    onError: (e: any) => toast.error('Baseline not saved', ncmError(e)),
  })
  return (
    <Dialog open={!!version} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>Approve protected baseline</DialogTitle><DialogDescription>The version captured {version && absoluteUtc(version.captured_at)} becomes the approved reference; drift is measured against it. The previous active baseline is retired and stays protected.</DialogDescription></DialogHeader>
        <FormField label="Name" required><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Post-change 2026-Q4" /></FormField>
        <FormField label="Reason / approval reference" required error={reason && reason.trim().length < 3 ? 'At least 3 characters' : null}><Textarea value={reason} onChange={(e) => setReason(e.target.value)} placeholder="CAB-1234 reviewed by …" /></FormField>
        <DialogFooter><Button variant="outline" onClick={onClose}>Cancel</Button><Button disabled={!name.trim() || reason.trim().length < 3 || save.isPending} onClick={() => save.mutate()}>Approve baseline</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function RestoreTestDialog({ deviceId, version, onClose }: { deviceId: string; version: any; onClose: () => void }) {
  const qc = useQueryClient()
  const today = new Date().toISOString().slice(0, 10)
  const [form, setForm] = useState<any>({})
  useEffect(() => { setForm({ result: 'passed', method: 'lab_device', target: '', scope_notes: '', notes: '', tested_at: today }) }, [version]) // eslint-disable-line react-hooks/exhaustive-deps
  const save = useMutation({
    mutationFn: async () => api.post(`/devices/${deviceId}/ncm-restore-tests`, { ...form, version_id: version.id, tested_at: new Date(`${form.tested_at}T12:00:00Z`).toISOString() }),
    onSuccess: () => { toast.success('Restore test recorded'); qc.invalidateQueries({ queryKey: ['ncm'] }); onClose() },
    onError: (e: any) => toast.error('Not recorded', ncmError(e)),
  })
  const set = (p: any) => setForm((f: any) => ({ ...f, ...p }))
  return (
    <Dialog open={!!version} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader><DialogTitle>Record restore test</DialogTitle><DialogDescription>Record the outcome of a restore you performed (for example on a lab device). This records evidence only — NCCM never writes configuration to devices. Tested versions are protected from pruning.</DialogDescription></DialogHeader>
        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label="Result"><Select value={form.result} onValueChange={(v) => set({ result: v })}><SelectTrigger aria-label="Result"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="passed">Passed — device fully restored</SelectItem><SelectItem value="partial">Partial — some sections missing</SelectItem><SelectItem value="failed">Failed</SelectItem></SelectContent></Select></FormField>
          <FormField label="Method"><Select value={form.method} onValueChange={(v) => set({ method: v })}><SelectTrigger aria-label="Method"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="lab_device">Lab device</SelectItem><SelectItem value="isolated_replica">Isolated replica / VM</SelectItem><SelectItem value="production_rollback">Production rollback</SelectItem><SelectItem value="vendor_tool">Vendor tool validation</SelectItem></SelectContent></Select></FormField>
          <FormField label="Target" required><Input value={form.target || ''} onChange={(e) => set({ target: e.target.value })} placeholder="lab-fw-01 (same model/OS)" /></FormField>
          <FormField label="Test date"><Input type="date" max={today} value={form.tested_at || today} onChange={(e) => set({ tested_at: e.target.value })} /></FormField>
        </div>
        <FormField label="Scope verified" hint="Contexts/VDOMs/vsys, certificates and keys that were (or were not) restored."><Textarea value={form.scope_notes || ''} onChange={(e) => set({ scope_notes: e.target.value })} /></FormField>
        <FormField label="Notes"><Textarea value={form.notes || ''} onChange={(e) => set({ notes: e.target.value })} /></FormField>
        <DialogFooter><Button variant="outline" onClick={onClose}>Cancel</Button><Button disabled={(form.target || '').trim().length < 2 || save.isPending} onClick={() => save.mutate()}>Record</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// --------------------------------------------------------------------------- //
// Baselines & recovery (F03, F11)
// --------------------------------------------------------------------------- //
function BaselinesTab({ device }: { device: any }) {
  const can = useCan()
  const qc = useQueryClient()
  const deviceId = device.device_id
  const baselines = useQuery<any>({ queryKey: ['ncm', 'baselines', deviceId], queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-baselines`)).data })
  const tests = useQuery<any>({ queryKey: ['ncm', 'restore-tests', deviceId], queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-restore-tests`)).data })
  const [retire, setRetire] = useState<any>(null)
  const [reason, setReason] = useState('')
  const [cmp, setCmp] = useState<{ a: any; b: any } | null>(null)
  const doRetire = useMutation({
    mutationFn: async () => api.post(`/devices/${deviceId}/ncm-baselines/${retire.id}/retire`, { reason }),
    onSuccess: () => { toast.success('Baseline retired', 'Its version stays protected as baseline history.'); qc.invalidateQueries({ queryKey: ['ncm'] }); setRetire(null) },
    onError: (e: any) => toast.error('Not retired', ncmError(e)),
  })
  return (
    <div className="min-w-0 space-y-4">
      <Card className="min-w-0">
        <CardContent className="space-y-3 pt-4">
          <h2 className="font-semibold">Baseline lifecycle</h2>
          <p className="text-xs text-muted">Drift compares the newest capture with the active baseline, ignoring runtime noise. Retired baselines stay listed and keep their versions protected.</p>
          {baselines.isError ? <SectionError what="Baselines" onRetry={() => baselines.refetch()} /> : baselines.isPending ? <p className="text-sm text-muted">Loading…</p> : !baselines.data.data.length ? <EmptyState>No baseline approved yet. Approve one from a validated version under Backups &amp; versions.</EmptyState> : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-sm">
                <thead><tr className="text-left text-xs text-muted"><th className="py-1.5 pr-2 font-medium">Name</th><th className="pr-2 font-medium">Type</th><th className="pr-2 font-medium">State</th><th className="pr-2 font-medium">Drift</th><th className="pr-2 font-medium">Approved</th><th className="pr-2 font-medium">Reason</th><th><span className="sr-only">Actions</span></th></tr></thead>
                <tbody>{baselines.data.data.map((b: any) => (
                  <tr key={b.id} className="border-t border-border align-top">
                    <td className="py-2 pr-2 font-medium">{b.name}</td>
                    <td className="pr-2">{TYPE_LABEL[b.config_type]}</td>
                    <td className="pr-2">{b.retired_at ? <Badge variant="outline">Retired <Ago iso={b.retired_at} /></Badge> : <Badge variant="info">Active</Badge>}</td>
                    <td className="pr-2">{b.retired_at ? <span className="text-muted">—</span> : b.drift === 'in_sync' ? <span className="inline-flex items-center gap-1 text-success"><ToneIcon tone="success" />In sync</span> : b.drift === 'drifted' ? <span className="inline-flex items-center gap-1 text-warning"><ToneIcon tone="warning" />Drifted</span> : <span className="text-muted">Unknown</span>}</td>
                    <td className="pr-2">{b.actor_name}<div className="text-xs text-muted"><Ago iso={b.created_at} /> · version {absoluteUtc(b.version_captured_at)}</div></td>
                    <td className="max-w-xs pr-2 text-xs">{b.reason}</td>
                    <td className="whitespace-nowrap text-right">
                      {!b.retired_at && b.drift === 'drifted' && b.latest_version_id && <Button size="sm" variant="ghost" onClick={() => setCmp({ a: { id: b.version_id, label: `Baseline “${b.name}”` }, b: { id: b.latest_version_id, captured_at: new Date().toISOString() } })}>Show drift</Button>}
                      {!b.retired_at && can('ncm.baseline') && <Button size="sm" variant="ghost" onClick={() => { setRetire(b); setReason('') }}>Retire</Button>}
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
      <Card className="min-w-0">
        <CardContent className="space-y-3 pt-4">
          <h2 className="font-semibold">Recovery assurance</h2>
          <dl className="grid gap-3 sm:grid-cols-3">
            <Fact label="Artifact format">CLI configuration text{device.platform_label ? ` (${device.platform_label})` : ''}</Fact>
            <Fact label="Capture scope">{device.support?.state === 'implemented' ? 'See collector scope under NCCM → Collectors' : device.support?.message}</Fact>
            <Fact label="Restore evidence">{device.last_restore_test ? <>{device.last_restore_test.result} <Ago iso={device.last_restore_test.tested_at} /></> : 'None recorded'}</Fact>
          </dl>
          <p className="text-xs text-muted">A validated capture proves the output was complete and in the expected format; only a recorded restore test demonstrates that the device can be recovered from it.</p>
          {tests.isError ? <SectionError what="Restore tests" onRetry={() => tests.refetch()} /> : tests.isPending ? null : !tests.data.data.length ? <EmptyState>No restore test recorded. Record one from a version under Backups &amp; versions after a lab drill.</EmptyState> : (
            <ul className="divide-y divide-border rounded-md border border-border text-sm">{tests.data.data.map((t: any) => (
              <li key={t.id} className="px-3 py-2">
                <div className="flex flex-wrap items-center gap-2"><Badge variant={t.result === 'passed' ? 'success' : t.result === 'partial' ? 'warning' : 'danger'}>{t.result}</Badge><span className="font-medium">{t.target}</span><span className="text-xs text-muted">{t.method.replace(/_/g, ' ')} · {absoluteUtc(t.tested_at)} · by {t.actor_name}</span></div>
                <div className="text-xs text-muted">{TYPE_LABEL[t.config_type]} captured {absoluteUtc(t.version_captured_at)}{t.scope_notes ? ` · Scope: ${t.scope_notes}` : ''}{t.notes ? ` · ${t.notes}` : ''}</div>
              </li>
            ))}</ul>
          )}
        </CardContent>
      </Card>
      <Dialog open={!!retire} onOpenChange={(o) => !o && setRetire(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>Retire baseline “{retire?.name}”</DialogTitle><DialogDescription>Drift will no longer be measured against it. Its version remains protected by baseline history and cannot be pruned or unpinned.</DialogDescription></DialogHeader>
          <FormField label="Reason" required><Textarea value={reason} onChange={(e) => setReason(e.target.value)} /></FormField>
          <DialogFooter><Button variant="outline" onClick={() => setRetire(null)}>Cancel</Button><Button disabled={reason.trim().length < 3 || doRetire.isPending} onClick={() => doRetire.mutate()}>Retire baseline</Button></DialogFooter>
        </DialogContent>
      </Dialog>
      <CompareDialog deviceId={deviceId} pair={cmp} onClose={() => setCmp(null)} />
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Incidents and change events
// --------------------------------------------------------------------------- //
function IncidentHistory({ deviceId }: { deviceId: string }) {
  const q = useQuery<any>({ queryKey: ['ncm', 'incidents', deviceId], queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-incidents`)).data })
  if (q.isError) return <SectionError what="Backup incidents" onRetry={() => q.refetch()} />
  if (q.isPending || !q.data.data.length) return null
  return (
    <Card><CardContent className="space-y-2 pt-4">
      <h2 className="font-semibold">Backup incidents</h2>
      <p className="text-xs text-muted">Repeated failures of this device are correlated into one incident (one alert and one notification); the next successful capture closes it.</p>
      <ul className="divide-y divide-border text-sm">{q.data.data.slice(0, 10).map((i: any) => (
        <li key={i.id} className="flex flex-wrap items-center gap-2 py-2">
          <Badge variant={i.status === 'open' ? 'danger' : 'success'}>{i.status === 'open' ? 'Open' : 'Recovered'}</Badge>
          <span className="font-medium">{categoryLabel(i.failure_category)}</span>
          <span className="text-xs text-muted">{i.occurrences} failure{i.occurrences === 1 ? '' : 's'} · opened <Ago iso={i.opened_at} /> · last <Ago iso={i.last_seen_at} />{i.recovered_at && <> · recovered <Ago iso={i.recovered_at} /></>}</span>
          {i.status === 'open' && i.recommended_action && <span className="w-full text-xs text-muted">{i.recommended_action}</span>}
        </li>
      ))}</ul>
    </CardContent></Card>
  )
}

function ChangeEvents({ deviceId }: { deviceId: string }) {
  const q = useQuery<any>({ queryKey: ['ncm', 'change-events', deviceId], queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-change-events`)).data })
  return (
    <Card><CardContent className="space-y-2 pt-4">
      <h2 className="font-semibold">Configuration-change events</h2>
      <p className="text-xs text-muted">Syslog/trap messages announcing a change trigger a backup after a 2-minute quiet period. A user is shown only when the device message names one; nothing is inferred from the configuration difference.</p>
      {q.isError ? <SectionError what="Change events" onRetry={() => q.refetch()} /> : q.isPending ? null : !q.data.data.length ? <p className="text-sm text-muted">No change events received. Point device syslog at the appliance to enable event-triggered backups.</p> : (
        <ul className="divide-y divide-border text-sm">{q.data.data.slice(0, 15).map((e: any) => (
          <li key={e.id} className="flex flex-wrap items-center gap-2 py-1.5">
            <Badge variant="outline">{e.source}</Badge><span>{e.summary}</span>
            {e.reported_user && <span className="text-xs">by <strong>{e.reported_user}</strong> (reported by device)</span>}
            <span className="text-xs text-muted"><Ago iso={e.received_at} /> · {e.job_id ? (e.version_id ? 'captured' : 'backup queued') : e.processed_at ? 'skipped (device not ready)' : 'waiting for quiet period'}</span>
          </li>
        ))}</ul>
      )}
    </CardContent></Card>
  )
}

// --------------------------------------------------------------------------- //
// Notifications (B12)
// --------------------------------------------------------------------------- //
function NotificationsTab({ device, onEdit }: { device: any; onEdit: () => void }) {
  const can = useCan()
  const details: any[] = device.notify_channel_details || []
  return (
    <div className="min-w-0 space-y-4">
      <Card><CardContent className="space-y-2 pt-4">
        <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">Destinations</h2>{can('ncm.manage') && <Button size="sm" variant="outline" onClick={onEdit}><Pencil className="h-3.5 w-3.5" /> Change</Button>}</div>
        {!details.length ? <p className="text-sm text-muted">No destination is configured for this device: failures and changes raise alerts in ZenPlus but send no notification.</p> : (
          <ul className="space-y-1 text-sm">{details.map((c) => (
            <li key={c.id} className="flex items-center gap-2"><ToneIcon tone={c.enabled ? 'success' : 'warning'} />{c.name}{c.type && <span className="text-muted">({c.type})</span>}{!c.enabled && <span className="text-xs text-warning">disabled — nothing is delivered</span>}</li>
          ))}</ul>
        )}
        <p className="text-xs text-muted">Backup failures notify once per incident and once on recovery; configuration changes notify per change.</p>
      </CardContent></Card>
      <DeliveriesList deviceId={device.device_id} />
    </div>
  )
}

// --------------------------------------------------------------------------- //
// Settings with explicit edit / cancel / dirty lifecycle (B19, B22, B12)
// --------------------------------------------------------------------------- //
function toForm(d: any) {
  return {
    credential_id: d.credential_id || '', platform: d.platform || 'autodetect', enabled: d.enrolled ? d.ncm_enabled : true,
    freshness_hours: d.freshness_hours || 24, eligible_override: d.eligible_override ?? null, notify_channels: d.notify_channels || [],
    config_types: d.config_types || ['running'], schedule_enabled: !!d.schedule_enabled, schedule_type: d.schedule_type || 'interval',
    schedule_timezone: d.schedule_timezone || 'UTC', schedule_interval_hours: d.schedule_interval_hours || 24, schedule_time: d.schedule_time || '02:00',
    schedule_days: d.schedule_days || [1, 2, 3, 4, 5], keep_versions: d.keep_versions || 5, alert_on_change: d.alert_on_change !== false,
    event_backup: d.event_backup !== false,
  }
}

function SettingsTab({ device, stale, onDirty, editRequest, onRemoved }: { device: any; stale: boolean; onDirty: (d: boolean) => void; editRequest: number; onRemoved: () => void }) {
  const can = useCan()
  const qc = useQueryClient()
  const canEdit = can('ncm.manage')
  const [editing, setEditing] = useState(!device.enrolled && canEdit)
  const [form, setForm] = useState<any>(() => toForm(device))
  const [removeOpen, setRemoveOpen] = useState(false)
  const persisted = useMemo(() => JSON.stringify(toForm(device)), [device])
  const dirty = editing && JSON.stringify(form) !== persisted
  useEffect(() => { onDirty(dirty) }, [dirty, onDirty])
  useEffect(() => { if (!editing) setForm(toForm(device)) }, [persisted, editing]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (editRequest && canEdit) setEditing(true) }, [editRequest, canEdit])
  useEffect(() => {
    if (!dirty) return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])
  const creds = useQuery<any>({ queryKey: ['ncm', 'credentials'], queryFn: async () => (await api.get('/ncm/credentials')).data, enabled: editing })
  const platforms = useQuery<any>({ queryKey: ['ncm', 'platforms'], queryFn: async () => (await api.get('/ncm/platforms')).data, enabled: editing })
  const channels = useQuery<any>({ queryKey: ['ncm', 'notification-channels'], queryFn: async () => (await api.get('/ncm/notification-channels')).data, enabled: editing && canEdit })
  const driver = (platforms.data?.data || []).find((p: any) => p.value === form.platform)
  const supported: string[] = driver?.config_types || ['running', 'startup']
  const set = (p: any) => setForm((f: any) => ({ ...f, ...p }))
  const errors: Record<string, string> = {}
  if (!form.credential_id) errors.credential_id = 'Select a connection profile.'
  if (!form.config_types.length) errors.config_types = 'Select at least one capture type.'
  if (driver?.capability_resolved && form.config_types.some((t: string) => !supported.includes(t))) errors.config_types = `${driver.label} cannot capture ${form.config_types.filter((t: string) => !supported.includes(t)).join(', ')} configuration.`
  if (form.schedule_enabled && form.schedule_type === 'interval' && !(form.schedule_interval_hours >= 1 && form.schedule_interval_hours <= 720)) errors.interval = '1–720 hours.'
  if (form.schedule_enabled && form.schedule_type !== 'interval' && !/^([01]\d|2[0-3]):[0-5]\d$/.test(form.schedule_time || '')) errors.time = 'Enter a 24-hour time.'
  if (form.schedule_enabled && form.schedule_type === 'weekly' && !form.schedule_days.length) errors.days = 'Pick at least one day.'
  if (!(form.keep_versions >= 1 && form.keep_versions <= 100)) errors.keep = '1–100.'
  if (!(form.freshness_hours >= 1 && form.freshness_hours <= 720)) errors.freshness = '1–720 hours.'
  const save = useMutation({
    mutationFn: async () => (await api.put(`/devices/${device.device_id}/ncm`, form)).data,
    onSuccess: async () => { toast.success('Backup settings saved'); await qc.invalidateQueries({ queryKey: ['ncm'] }); setEditing(false) },
    onError: (e: any) => toast.error('Settings not saved', ncmError(e)),
  })
  const remove = useMutation({
    mutationFn: async () => api.delete(`/devices/${device.device_id}/ncm`),
    onSuccess: () => { toast.success('Removed from backup', 'Stored versions, baselines and audit history were kept.'); qc.invalidateQueries({ queryKey: ['ncm'] }); onRemoved() },
    onError: (e: any) => toast.error('Not removed', ncmError(e)),
  })

  if (!editing) {
    const cred = device.credential_name
    return (
      <Card><CardContent className="space-y-4 pt-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-semibold">Backup settings</h2>
          {canEdit ? <div className="flex gap-2">
            <Button size="sm" onClick={() => setEditing(true)} disabled={stale}><Pencil className="h-3.5 w-3.5" /> {device.enrolled ? 'Edit settings' : 'Enroll device'}</Button>
            {device.enrolled && <Button size="sm" variant="outline" className="text-danger" onClick={() => setRemoveOpen(true)}><Trash2 className="h-3.5 w-3.5" /> Remove from backup</Button>}
          </div> : <span className="text-xs text-muted">Read-only: you need the NCM manage permission to change settings.</span>}
        </div>
        {!device.enrolled ? <p className="text-sm text-muted">This device is not enrolled for configuration backup.</p> : (
          <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <Fact label="Collection">{device.ncm_enabled ? 'Enabled' : 'Disabled'}</Fact>
            <Fact label="Connection profile">{cred || <span className="text-warning">None</span>} {device.auth_method && <span className="text-xs text-muted">({device.auth_method === 'key' ? 'SSH key' : 'password'}, port {device.credential_port})</span>}</Fact>
            <Fact label="Driver">{device.platform_label || 'Auto-detect (capability resolved at first capture)'}</Fact>
            <Fact label="Capture types">{(device.config_types || []).map((t: string) => TYPE_LABEL[t]).join(', ')}</Fact>
            <Fact label="Schedule">{scheduleSummary(device)}{device.next_due && device.ncm_enabled && <div className="text-xs text-muted">Next <Ago iso={device.next_due} /></div>}</Fact>
            <Fact label="Freshness limit">{device.freshness_hours} h</Fact>
            <Fact label="Retention">{device.keep_versions} changed versions (+ protected)</Fact>
            <Fact label="Change alerts">{device.alert_on_change ? 'On' : 'Off'} · event backups {device.event_backup ? 'on' : 'off'}</Fact>
            <Fact label="Coverage scope">{device.eligible_override === null ? 'Automatic' : device.eligible_override ? 'Included' : 'Excluded'} — {device.eligibility_reason}</Fact>
          </dl>
        )}
        <ConfirmDialog open={removeOpen} onOpenChange={setRemoveOpen} destructive loading={remove.isPending} confirmText="Remove from backup" title={`Remove ${device.hostname} from backup?`}
          description={<span className="space-y-2 text-sm"><span className="block">Scheduled and event backups stop and queued jobs are cancelled. The device will count as “not enrolled”{device.eligible ? ' and stays in the coverage denominator as unprotected' : ''}.</span><span className="block">Stored versions ({device.versions}), baselines, restore evidence and the audit trail are kept and remain viewable.</span></span>}
          onConfirm={() => remove.mutate()} />
      </CardContent></Card>
    )
  }

  const credList: any[] = creds.data?.data || []
  const selectedCred = credList.find((c) => c.id === form.credential_id)
  return (
    <Card><CardContent className="space-y-4 pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-semibold">{device.enrolled ? 'Edit backup settings' : 'Enroll device'} {dirty && <Badge variant="warning" className="ml-1">Unsaved changes</Badge>}</h2>
      </div>
      {stale && <p role="alert" className="text-xs text-warning">Device data could not be refreshed; saving is disabled until it reloads.</p>}
      <div className="grid gap-4 lg:grid-cols-2">
        <fieldset className="space-y-3 rounded-md border border-border p-3">
          <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-muted">Collection</legend>
          <label className="flex items-center gap-2 text-sm"><Switch aria-label="Collection enabled" checked={form.enabled} onCheckedChange={(v) => set({ enabled: v })} />Collection {form.enabled ? 'enabled' : 'disabled'}</label>
          <FormField label="Connection profile" error={errors.credential_id} hint={selectedCred ? `${selectedCred.auth_method === 'key' ? `SSH key ${selectedCred.key_type || ''}` : selectedCred.has_password ? 'Password saved' : 'No password saved'} · ${selectedCred.username}@port ${selectedCred.port}` : 'Profiles are managed under NCCM → Connection profiles.'}>
            {creds.isError ? <SectionError what="Profiles" onRetry={() => creds.refetch()} /> : (
              <Select value={form.credential_id} onValueChange={(v) => set({ credential_id: v })} disabled={creds.isPending}>
                <SelectTrigger aria-label="Connection profile"><SelectValue placeholder={creds.isPending ? 'Loading…' : 'Select a profile'} /></SelectTrigger>
                <SelectContent>{credList.map((c) => <SelectItem key={c.id} value={c.id}>{c.name} ({c.username})</SelectItem>)}</SelectContent>
              </Select>
            )}
          </FormField>
          <FormField label="Driver" hint={driver ? (driver.capability_resolved ? driver.scope : 'Capability is unresolved until the first capture detects the platform; startup capture may then be rejected.') : undefined}>
            {platforms.isError ? <SectionError what="The driver list" onRetry={() => platforms.refetch()} /> : (
              <Select value={form.platform} onValueChange={(v) => set({ platform: v })} disabled={platforms.isPending}>
                <SelectTrigger aria-label="Driver"><SelectValue placeholder="Loading drivers…" /></SelectTrigger>
                <SelectContent>{(platforms.data?.data || [{ value: form.platform, label: form.platform }]).map((p: any) => <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>)}</SelectContent>
              </Select>
            )}
          </FormField>
          <div>
            <div className="mb-1 text-xs font-medium uppercase tracking-wider text-muted">Capture types</div>
            <div className="flex flex-wrap gap-4 text-sm">{['running', 'startup'].map((t) => {
              const unavailable = driver?.capability_resolved && !supported.includes(t)
              return (
                <label key={t} className={cn('flex items-center gap-1.5', unavailable && 'text-muted')} title={unavailable ? `${driver.label} cannot capture this type` : undefined}>
                  <input type="checkbox" className="h-4 w-4 accent-primary" disabled={unavailable && !form.config_types.includes(t)} checked={form.config_types.includes(t)}
                    onChange={(e) => set({ config_types: e.target.checked ? [...form.config_types, t] : form.config_types.filter((x: string) => x !== t) })} />
                  {TYPE_LABEL[t]}{unavailable && ' (not supported by this driver)'}
                </label>
              )
            })}</div>
            {errors.config_types && <p className="mt-1 text-[11px] text-danger">{errors.config_types}</p>}
          </div>
          <FormField label="Coverage scope" hint="Scope decides whether the device counts in coverage; it never blocks a deliberate backup.">
            <Select value={form.eligible_override === null ? 'auto' : form.eligible_override ? 'include' : 'exclude'} onValueChange={(v) => set({ eligible_override: v === 'auto' ? null : v === 'include' })}>
              <SelectTrigger aria-label="Coverage scope"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="auto">Automatic (network device type and address)</SelectItem><SelectItem value="include">Include in coverage</SelectItem><SelectItem value="exclude">Exclude from coverage</SelectItem></SelectContent>
            </Select>
          </FormField>
        </fieldset>
        <fieldset className="space-y-3 rounded-md border border-border p-3">
          <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-muted">Schedule &amp; retention</legend>
          <label className="flex items-center gap-2 text-sm"><Switch aria-label="Scheduled backup" checked={form.schedule_enabled} onCheckedChange={(v) => set({ schedule_enabled: v })} />Scheduled backup {form.schedule_enabled ? 'on' : 'off'}</label>
          {form.schedule_enabled && (
            <div className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <FormField label="Frequency"><Select value={form.schedule_type} onValueChange={(v) => set({ schedule_type: v })}><SelectTrigger aria-label="Frequency"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="interval">Every N hours</SelectItem><SelectItem value="daily">Daily at a time</SelectItem><SelectItem value="weekly">Weekly on days</SelectItem></SelectContent></Select></FormField>
                {form.schedule_type === 'interval'
                  ? <FormField label="Every (hours)" error={errors.interval}><Input type="number" min={1} max={720} value={form.schedule_interval_hours} onChange={(e) => set({ schedule_interval_hours: Number(e.target.value) })} /></FormField>
                  : <FormField label="At (24-hour)" error={errors.time}><Input type="time" value={form.schedule_time} onChange={(e) => set({ schedule_time: e.target.value })} /></FormField>}
              </div>
              {form.schedule_type === 'weekly' && <div><DayPicker value={form.schedule_days} onChange={(d) => set({ schedule_days: d })} />{errors.days && <p className="mt-1 text-[11px] text-danger">{errors.days}</p>}</div>}
              {form.schedule_type !== 'interval' && <div><div className="mb-1 text-xs font-medium uppercase tracking-wider text-muted">Timezone</div><TimezoneSelect value={form.schedule_timezone} onChange={(z) => set({ schedule_timezone: z })} /><p className="mt-1 text-[11px] text-muted">{HELP.timezone}</p></div>}
              <p className="text-[11px] text-muted">{HELP.scheduler}</p>
            </div>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            <FormField label="Keep changed versions" error={errors.keep} hint={HELP.retention}><Input type="number" min={1} max={100} value={form.keep_versions} onChange={(e) => set({ keep_versions: Number(e.target.value) })} /></FormField>
            <FormField label="Freshness limit (hours)" error={errors.freshness} hint={HELP.freshness}><Input type="number" min={1} max={720} value={form.freshness_hours} onChange={(e) => set({ freshness_hours: Number(e.target.value) })} /></FormField>
          </div>
        </fieldset>
        <fieldset className="space-y-3 rounded-md border border-border p-3 lg:col-span-2">
          <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-muted">Alerts &amp; notifications</legend>
          <div className="flex flex-wrap gap-6 text-sm">
            <label className="flex items-center gap-2"><Switch aria-label="Alert on configuration change" checked={form.alert_on_change} onCheckedChange={(v) => set({ alert_on_change: v })} />Alert on configuration change</label>
            <label className="flex items-center gap-2"><Switch aria-label="Back up after change events" checked={form.event_backup} onCheckedChange={(v) => set({ event_backup: v })} />Back up after syslog/trap change events</label>
          </div>
          <div>
            <div className="mb-1 text-xs font-medium uppercase tracking-wider text-muted">Notification destinations</div>
            {channels.isError ? <SectionError what="Notification destinations" onRetry={() => channels.refetch()} />
              : channels.isPending ? <p className="text-sm text-muted">Loading destinations…</p>
              : (
                <div className="space-y-1">
                  {channels.data.data.map((c: any) => (
                    <label key={c.id} className="flex items-center gap-2 text-sm"><input type="checkbox" className="h-4 w-4 accent-primary" checked={form.notify_channels.includes(c.id)} onChange={(e) => set({ notify_channels: e.target.checked ? [...form.notify_channels, c.id] : form.notify_channels.filter((x: string) => x !== c.id) })} />{c.name} <span className="text-muted">({c.type})</span></label>
                  ))}
                  {!channels.data.data.length && <p className="text-sm text-muted">No enabled channel of a supported type exists. <Link to="/channels" className="text-primary underline">Configure channels</Link>.</p>}
                  {channels.data.unsupported?.length > 0 && <p className="text-xs text-muted">Not usable for NCCM: {channels.data.unsupported.map((c: any) => `${c.name} (${c.type})`).join(', ')}.</p>}
                </div>
              )}
          </div>
        </fieldset>
      </div>
      <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <Button disabled={!!Object.keys(errors).length || save.isPending || stale || (device.enrolled && !dirty)} onClick={() => save.mutate()}>{save.isPending ? 'Saving…' : device.enrolled ? 'Save changes' : 'Enroll device'}</Button>
        <Button variant="outline" disabled={save.isPending} onClick={() => { setForm(toForm(device)); setEditing(false) }}>Cancel</Button>
        {Object.keys(errors).length > 0 && <span className="text-xs text-danger">Fix {Object.keys(errors).length} field{Object.keys(errors).length === 1 ? '' : 's'} before saving.</span>}
        {dirty && <span className="text-xs text-warning">Unsaved changes are lost if you leave.</span>}
      </div>
    </CardContent></Card>
  )
}


