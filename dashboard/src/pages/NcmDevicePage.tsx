import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Save, DownloadCloud, Download, Eye, GitCompare, FileCode, Clock, Loader2 } from 'lucide-react'
import { useCan } from '@/stores/auth'
import { api } from '@/lib/api'
import { relativeTime, apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'

const WEEKDAYS = [['Sun', 0], ['Mon', 1], ['Tue', 2], ['Wed', 3], ['Thu', 4], ['Fri', 5], ['Sat', 6]] as const

function statusKey(d: any): string { return d?.backup_state || 'pending' }

const SB: Record<string, { label: string; v: any }> = {
  fresh: { label: 'Fresh / validated', v: 'success' }, stale: { label: 'Stale', v: 'warning' },
  unverified: { label: 'Unverified', v: 'warning' }, excluded: { label: 'Excluded', v: undefined },
  disabled: { label: 'Disabled', v: undefined },
  backed_up: { label: 'Backed up', v: 'success' }, failed: { label: 'Failed', v: 'danger' },
  pending: { label: 'Pending', v: 'warning' }, unconfigured: { label: 'Not configured', v: undefined },
}
function DiffView({ diff }: { diff: string }) {
  const lines = diff ? diff.split('\n') : []
  if (!lines.length) return <div className="text-xs text-muted">Identical.</div>
  return (
    <pre className="max-h-[60vh] overflow-auto rounded-md border border-border bg-surface2/40 text-[11px] leading-relaxed">
      {lines.map((l, i) => {
        const cls = l.startsWith('+') && !l.startsWith('+++') ? 'bg-success/10 text-success'
          : l.startsWith('-') && !l.startsWith('---') ? 'bg-danger/10 text-danger'
          : l.startsWith('@@') ? 'text-primary' : 'text-muted'
        return <div key={i} className={`whitespace-pre-wrap px-2 font-mono ${cls}`}>{l || ' '}</div>
      })}
    </pre>
  )
}

export function NcmDevicePage() {
  const can = useCan()
  const { deviceId } = useParams()
  const qc = useQueryClient()
  const [view, setView] = useState<'none' | 'config' | 'diff'>('none')
  const [selected, setSelected] = useState<any>(null)
  const [cmp, setCmp] = useState<{ older: any; newer: any } | null>(null)
  const [lastRun, setLastRun] = useState<{ ok: boolean; change: boolean; msg: string } | null>(null)

  const { data: overview } = useQuery<any>({ queryKey: ['ncm', 'overview'], queryFn: async () => (await api.get('/ncm/overview')).data, refetchInterval: 30000 })
  const device = (overview?.data || []).find((d: any) => d.device_id === deviceId)
  const { data: creds } = useQuery<any>({ queryKey: ['ncm', 'credentials'], queryFn: async () => (await api.get('/ncm/credentials')).data })
  const { data: platforms } = useQuery<any>({ queryKey: ['ncm', 'platforms'], queryFn: async () => (await api.get('/ncm/platforms')).data })
  const credentials: any[] = creds?.data || []
  const platformList: any[] = platforms?.data || [{ value: 'autodetect', label: 'Auto-detect' }]

  const [configType, setConfigType] = useState('running')
  const [compareA, setCompareA] = useState('')
  const [compareB, setCompareB] = useState('')
  const [baselineName, setBaselineName] = useState('')
  const [baselineReason, setBaselineReason] = useState('')
  const [baselineVersion, setBaselineVersion] = useState<any>(null)
  const { data: jobData } = useQuery<any>({ queryKey: ['ncm', 'jobs', deviceId], queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-jobs`)).data, refetchInterval: 5000, enabled: !!deviceId })
  const { data: baselineData } = useQuery<any>({ queryKey: ['ncm', 'baselines', deviceId], queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-baselines`)).data, enabled: !!deviceId })
  const { data: channelData } = useQuery<any>({ queryKey: ['ncm', 'notification-channels'], queryFn: async () => (await api.get('/ncm/notification-channels')).data, enabled: can('ncm.manage') })
  const { data: deliveryData } = useQuery<any>({ queryKey: ['ncm', 'deliveries', deviceId], queryFn: async () => (await api.get(`/devices/${deviceId}/ncm-deliveries`)).data, refetchInterval: 30000, enabled: !!deviceId })
  const baseline = baselineData?.data?.find((b: any) => !b.retired_at && b.config_type === configType)
  const { data: versions } = useQuery<any>({ queryKey: ['ncm', 'configs', deviceId, configType], queryFn: async () => (await api.get(`/devices/${deviceId}/configs?config_type=${configType}`)).data, enabled: !!deviceId, refetchInterval: 5000 })
  const vlist: any[] = versions?.data || []
  const { data: diff } = useQuery<any>({ queryKey: ['ncm', 'diff', deviceId, vlist[0]?.id, vlist[1]?.id], queryFn: async () => (await api.get(`/devices/${deviceId}/configs-diff?a=${vlist[1].id}&b=${vlist[0].id}`)).data, enabled: vlist.length >= 2 })
  const { data: viewContent } = useQuery<any>({ queryKey: ['ncm', 'view', selected?.id], queryFn: async () => (await api.get(`/devices/${deviceId}/configs/${selected.id}`)).data, enabled: !!selected })
  const { data: cmpDiff } = useQuery<any>({ queryKey: ['ncm', 'cmp', deviceId, cmp?.older?.id, cmp?.newer?.id], queryFn: async () => (await api.get(`/devices/${deviceId}/configs-diff?a=${cmp!.older.id}&b=${cmp!.newer.id}`)).data, enabled: !!cmp })

  const def = credentials.find((c) => c.is_default)
  const [form, setForm] = useState<any>(null)
  const f = form || {
    credential_id: device?.credential_id || def?.id || (credentials[0]?.id ?? ''),
    platform: device?.platform || 'autodetect',
    enabled: device?.ncm_enabled ?? true,
    freshness_hours: device?.freshness_hours || 24,
    eligible_override: device?.eligible_override ?? null,
    notify_channels: device?.notify_channels || [],
    config_types: device?.config_types || ['running'],
    schedule_enabled: !!device?.schedule_enabled,
    schedule_type: device?.schedule_type || 'interval',
    schedule_timezone: device?.schedule_timezone || 'UTC',
    schedule_interval_hours: device?.schedule_interval_hours || 24,
    schedule_time: device?.schedule_time || '02:00',
    schedule_days: device?.schedule_days || [1, 2, 3, 4, 5],
    keep_versions: device?.keep_versions || 5,
    alert_on_change: device?.alert_on_change !== false,
  }
  const set = (patch: any) => setForm({ ...f, ...patch })
  const toggleDay = (d: number) => set({ schedule_days: f.schedule_days.includes(d) ? f.schedule_days.filter((x: number) => x !== d) : [...f.schedule_days, d].sort() })

  const inv = () => qc.invalidateQueries({ queryKey: ['ncm'] })
  const saveEnroll = useMutation({ mutationFn: async () => (await api.put(`/devices/${deviceId}/ncm`, f)).data, onSuccess: () => { toast.success('Backup settings saved'); inv() }, onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)) })
  const unenroll = useMutation({ mutationFn: async () => api.delete(`/devices/${deviceId}/ncm`), onSuccess: () => { toast.success('Removed from backup'); inv() }, onError: (e: any) => toast.error('Failed', apiErrorMessage(e)) })
  const fetchNow = useMutation({
    mutationFn: async () => (await api.post(`/devices/${deviceId}/ncm-jobs`)).data,
    onSuccess: (d: any) => {
      setLastRun({ ok: true, change: false, msg: d.queued ? 'Backup queued; progress appears below' : 'A backup is already queued or running' })
      toast.success(d.queued ? 'Backup queued' : 'Backup already active')
      inv()
    },
    onError: (e: any) => { setLastRun({ ok: false, change: false, msg: apiErrorMessage(e) }); toast.error('SSH backup failed', apiErrorMessage(e)) },
  })

  const approveBaseline = useMutation({ mutationFn: async () => api.post(`/devices/${deviceId}/ncm-baselines`, { version_id: baselineVersion.id, name: baselineName, reason: baselineReason }), onSuccess: () => { setBaselineVersion(null); toast.success('Protected baseline saved'); inv() }, onError: (e: any) => toast.error('Baseline failed', apiErrorMessage(e)) })
  const pin = useMutation({ mutationFn: async (v: any) => api.put(`/devices/${deviceId}/configs/${v.id}/pin`, { pinned: !v.pinned }), onSuccess: inv, onError: (e: any) => toast.error('Pin failed', apiErrorMessage(e)) })
  const cancelJob = useMutation({ mutationFn: async (id: string) => api.post(`/devices/${deviceId}/ncm-jobs/${id}/cancel`), onSuccess: inv, onError: (e: any) => toast.error('Cancel failed', apiErrorMessage(e)) })

  async function download(v: any, raw = false) {
    try {
      const c = (await api.get(`/devices/${deviceId}/configs/${v.id}?raw=${raw}`)).data
      const blob = new Blob([c.content || ''], { type: 'text/plain' })
      const url = URL.createObjectURL(blob); const a = document.createElement('a')
      a.href = url; a.download = `${device?.hostname || 'device'}-${v.captured_at.slice(0, 19).replace(/[:T]/g, '-')}${raw ? '' : '-redacted'}.cfg`; a.click(); URL.revokeObjectURL(url)
    } catch (e: any) { toast.error('Download failed', apiErrorMessage(e)) }
  }

  if (!device) {
    return <div className="space-y-3"><Link to="/ncm" className="inline-flex items-center gap-1 text-sm text-primary"><ArrowLeft className="h-4 w-4" /> Config Backup</Link><Card><CardContent className="py-10 text-center text-muted">Loading device…</CardContent></Card></div>
  }
  const sk = SB[statusKey(device)]

  return (
    <div className="space-y-4">
      <Link to="/ncm" className="inline-flex items-center gap-1 text-sm text-primary hover:underline"><ArrowLeft className="h-4 w-4" /> Config Backup</Link>

      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <FileCode className="h-5 w-5 text-primary" /> {device.hostname}
            {sk.v ? <Badge variant={sk.v}>{sk.label}{device.versions ? ` · ${device.versions}v` : ''}</Badge> : <span className="text-xs text-muted">{sk.label}</span>}
          </h1>
          <p className="text-xs text-muted">{[device.ip, device.device_type, device.vendor, device.location].filter(Boolean).join(' · ')}</p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <Button variant="outline" disabled={!can('ncm.manage') || !device.enrolled || fetchNow.isPending} onClick={() => fetchNow.mutate()}>
            {fetchNow.isPending
              ? <><Loader2 className="h-4 w-4 animate-spin" /> Backing up…</>
              : <><DownloadCloud className="h-4 w-4" /> Backup now (SSH)</>}
          </Button>
          {fetchNow.isPending
            ? <div className="text-[11px] text-muted">Pulling running-config over SSH — large configs take ~30–60s</div>
            : lastRun
              ? <div className={`text-[11px] ${lastRun.ok ? (lastRun.change ? 'text-emerald-600' : 'text-muted') : 'text-rose-600'}`}>
                  {lastRun.ok ? (lastRun.change ? '✓ ' : '✓ ') : '✕ '}{lastRun.msg}
                </div>
              : null}
        </div>
      </div>

      {!!jobData?.data?.length && <Card><CardContent className="space-y-2 pt-4"><div className="text-sm font-semibold">Backup jobs</div>{jobData.data.slice(0, 5).map((j: any) => <div key={j.id} className="flex items-center justify-between text-xs"><span>{relativeTime(j.created_at)} · {j.config_types.join(', ')} · {j.status} · attempt {j.attempts}{j.error_code ? ` · ${j.error_code}` : ''}</span>{can('ncm.manage') && ['queued', 'running'].includes(j.status) && <Button size="sm" variant="ghost" onClick={() => cancelJob.mutate(j.id)}>Cancel</Button>}</div>)}</CardContent></Card>}
      {!!deliveryData?.data?.length && <Card><CardContent className="space-y-1 pt-4"><div className="text-sm font-semibold">Notification delivery</div>{deliveryData.data.slice(0, 5).map((d: any) => <div key={d.id} className="text-xs">{relativeTime(d.created_at)} · {d.status} · {d.attempts} attempts {d.last_error || ''}</div>)}</CardContent></Card>}
      <Dialog open={!!baselineVersion} onOpenChange={(open) => { if (!open) setBaselineVersion(null) }}><DialogContent><DialogHeader><DialogTitle>Approve protected baseline</DialogTitle></DialogHeader><FormField label="Name"><Input value={baselineName} onChange={(e) => setBaselineName(e.target.value)} /></FormField><FormField label="Reason"><Input value={baselineReason} onChange={(e) => setBaselineReason(e.target.value)} /></FormField><Button disabled={!baselineName.trim() || baselineReason.trim().length < 3 || approveBaseline.isPending} onClick={() => approveBaseline.mutate()}>Approve baseline</Button></DialogContent></Dialog>
      <div className="grid gap-4 lg:grid-cols-[340px_1fr]">
        {/* settings */}
        <Card><CardContent className="space-y-2 pt-4">
          <div className="text-xs font-semibold uppercase tracking-wider text-muted">Backup settings</div>
          <FormField label="Connection profile">
            <Select value={f.credential_id} onValueChange={(v) => set({ credential_id: v })}>
              <SelectTrigger><SelectValue placeholder="Select a profile" /></SelectTrigger>
              <SelectContent>{credentials.map((c) => <SelectItem key={c.id} value={c.id}>{c.name} ({c.username})</SelectItem>)}</SelectContent>
            </Select>
          </FormField>
          <FormField label="Platform">
            <Select value={f.platform} onValueChange={(v) => set({ platform: v })}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>{platformList.map((p) => <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>)}</SelectContent>
            </Select>
          </FormField>
          <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
            <span className="flex items-center gap-1 text-xs font-medium uppercase tracking-wider text-muted"><Clock className="h-3.5 w-3.5" /> Scheduled backup</span>
            <Switch checked={f.schedule_enabled} onCheckedChange={(v) => set({ schedule_enabled: v })} />
          </div>
          {f.schedule_enabled && (
            <div className="space-y-2 rounded-md border border-border p-2">
              <FormField label="Timezone (IANA)"><Input value={f.schedule_timezone} onChange={(e) => set({ schedule_timezone: e.target.value })} placeholder="Asia/Riyadh" /></FormField>
              <FormField label="Frequency">
                <Select value={f.schedule_type} onValueChange={(v) => set({ schedule_type: v })}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="interval">Every N hours</SelectItem>
                    <SelectItem value="daily">Daily at a time</SelectItem>
                    <SelectItem value="weekly">Weekly on days</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>
              {f.schedule_type === 'interval' && (
                <FormField label="Every (hours)"><Input type="number" min={1} max={720} value={f.schedule_interval_hours} onChange={(e) => set({ schedule_interval_hours: Number(e.target.value) })} /></FormField>
              )}
              {(f.schedule_type === 'daily' || f.schedule_type === 'weekly') && (
                <FormField label="At (selected timezone, 24h)"><Input type="time" value={f.schedule_time} onChange={(e) => set({ schedule_time: e.target.value })} /></FormField>
              )}
              {f.schedule_type === 'weekly' && (
                <div>
                  <div className="mb-1 text-xs text-muted">On days</div>
                  <div className="flex flex-wrap gap-1">
                    {WEEKDAYS.map(([lbl, d]) => (
                      <button key={d} type="button" onClick={() => toggleDay(d as number)}
                        className={`rounded px-2 py-1 text-xs font-medium ${f.schedule_days.includes(d) ? 'bg-primary text-white' : 'border border-border text-muted hover:text-text'}`}>{lbl}</button>
                    ))}
                  </div>
                </div>
              )}
              <p className="text-[11px] text-muted">Schedules are checked every minute; durable jobs record progress and retry failures.</p>
            </div>
          )}
          <div className="space-y-1 text-xs"><div className="font-medium">Capture types</div>{['running', 'startup'].map((t) => <label key={t} className="mr-3 inline-flex gap-1"><input type="checkbox" checked={f.config_types.includes(t)} onChange={(e) => set({ config_types: e.target.checked ? [...f.config_types, t] : f.config_types.filter((x: string) => x !== t) })} />{t}</label>)}<p className="text-muted">Startup capture is supported on Cisco, Arista, Comware and Huawei drivers.</p></div>
          <div className="space-y-1 text-xs"><div className="font-medium">Notification destinations</div>{(channelData?.data || []).map((c: any) => <label key={c.id} className="flex gap-2"><input type="checkbox" checked={f.notify_channels.includes(c.id)} onChange={(e) => set({ notify_channels: e.target.checked ? [...f.notify_channels, c.id] : f.notify_channels.filter((id: string) => id !== c.id) })} />{c.name} ({c.type})</label>)}{!channelData?.data?.length && <p className="text-muted">No supported channels available. Configure a notification channel in Alerting.</p>}</div>
          <FormField label="Freshness limit (hours)"><Input type="number" min={1} max={720} value={f.freshness_hours} onChange={(e) => set({ freshness_hours: Number(e.target.value) })} /></FormField>
          <FormField label="Backup eligibility"><Select value={f.eligible_override === null ? 'auto' : f.eligible_override ? 'include' : 'exclude'} onValueChange={(v) => set({ eligible_override: v === 'auto' ? null : v === 'include' })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="auto">Automatic network device scope</SelectItem><SelectItem value="include">Include in coverage</SelectItem><SelectItem value="exclude">Exclude from coverage</SelectItem></SelectContent></Select></FormField>
          <FormField label="Keep versions" hint="Retains meaningful changes, pins, the latest snapshot and last validated recovery copy.">
            <Input type="number" min={1} max={100} value={f.keep_versions} onChange={(e) => set({ keep_versions: Number(e.target.value) })} />
          </FormField>
          <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
            <span className="text-xs font-medium uppercase tracking-wider text-muted">Alert on config change</span>
            <Switch checked={f.alert_on_change} onCheckedChange={(v) => set({ alert_on_change: v })} />
          </div>
          {device.last_status === 'failed' && device.last_error && <div className="rounded border border-danger/30 bg-danger/10 p-2 text-[11px] text-danger">Last error: {device.last_error}</div>}
          {device.last_success_at && <div className="text-[11px] text-muted">Last checked: {relativeTime(device.last_success_at)}{device.last_capture && device.last_capture !== device.last_success_at ? ` · last change ${relativeTime(device.last_capture)}` : ''}</div>}
          <div className="flex flex-wrap gap-2 pt-1">
            <Button size="sm" disabled={!can('ncm.manage') || !f.credential_id || saveEnroll.isPending} onClick={() => saveEnroll.mutate()}><Save className="h-3.5 w-3.5" /> Save</Button>
            {can('ncm.manage') && device.enrolled && <Button size="sm" variant="ghost" className="text-danger" onClick={() => unenroll.mutate()}>Remove</Button>}
          </div>
        </CardContent></Card>

        {/* versions */}
        <Card><CardContent className="space-y-2 pt-4">
          <div className="flex items-center justify-between">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">Versions ({vlist.length})</div>
            <Select value={configType} onValueChange={(v) => { setConfigType(v); setSelected(null); setView('none') }}><SelectTrigger className="w-32"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="running">Running</SelectItem><SelectItem value="startup">Startup</SelectItem></SelectContent></Select>
            {baseline && vlist[0] && <Button size="sm" variant="outline" onClick={() => setCmp({ older: { id: baseline.version_id, label: `Baseline: ${baseline.name}` }, newer: vlist[0] })}>Compare baseline</Button>}
            {vlist.length >= 2 && <Button size="sm" variant="outline" onClick={() => { setView(view === 'diff' ? 'none' : 'diff'); setSelected(null) }}><GitCompare className="h-3.5 w-3.5" /> Diff latest{diff ? ` (+${diff.added} −${diff.removed})` : ''}</Button>}
          </div>
          {vlist.length >= 2 && <div className="flex flex-wrap gap-2"><Select value={compareA} onValueChange={setCompareA}><SelectTrigger className="w-44"><SelectValue placeholder="Compare from" /></SelectTrigger><SelectContent>{vlist.map((v) => <SelectItem key={v.id} value={v.id}>{relativeTime(v.captured_at)} · {v.hash}</SelectItem>)}</SelectContent></Select><Select value={compareB} onValueChange={setCompareB}><SelectTrigger className="w-44"><SelectValue placeholder="Compare to" /></SelectTrigger><SelectContent>{vlist.map((v) => <SelectItem key={v.id} value={v.id}>{relativeTime(v.captured_at)} · {v.hash}</SelectItem>)}</SelectContent></Select><Button size="sm" variant="outline" disabled={!vlist.some((v) => v.id === compareA) || !vlist.some((v) => v.id === compareB)} onClick={() => setCmp({ older: vlist.find((v) => v.id === compareA), newer: vlist.find((v) => v.id === compareB) })}>Compare selected</Button></div>}
          <div className="max-h-72 space-y-1 overflow-auto">
            {vlist.map((v, i) => (
              <div key={v.id} className={`flex items-center justify-between rounded border px-2 py-1 text-xs ${selected?.id === v.id ? 'border-primary/50 bg-primary/5' : 'border-border'}`}>
                <div>
                  <div className="flex items-center gap-1.5">
                    {relativeTime(v.captured_at)}
                    {v.pinned
                      ? <span className="rounded-sm bg-muted/15 px-1 py-px text-[10px] font-medium text-muted">Pinned</span>
                      : v.is_change
                        ? <span className="rounded-sm bg-amber-500/15 px-1 py-px text-[10px] font-medium text-amber-600">Changed</span>
                        : <span className="rounded-sm bg-emerald-500/15 px-1 py-px text-[10px] font-medium text-emerald-600">No change</span>}
                  </div>
                  <div className="text-muted">{v.config_type} · {v.validated ? 'Validated' : 'Unverified'} · {v.line_count} lines · {v.size_bytes} B · {v.captured_by} · {v.hash}</div>
                </div>
                <div className="flex gap-0.5">
                  {can('ncm.baseline') && <Button variant="ghost" size="sm" onClick={() => pin.mutate(v)}>{v.pinned ? 'Unpin' : 'Pin'}</Button>}
                  {can('ncm.baseline') && v.validated && <Button variant="ghost" size="sm" onClick={() => { setBaselineVersion(v); setBaselineName(''); setBaselineReason('') }}>Set baseline</Button>}
                  {i < vlist.length - 1 && (
                    <Button variant="ghost" size="icon" className="h-7 w-7" title="Compare to previous version" onClick={() => setCmp({ older: vlist[i + 1], newer: v })}><GitCompare className="h-3.5 w-3.5" /></Button>
                  )}
                  <Button variant="ghost" size="icon" className="h-7 w-7" title="View" onClick={() => { setSelected(v); setView('config') }}><Eye className="h-3.5 w-3.5" /></Button>
                  <Button variant="ghost" size="icon" className="h-7 w-7" title="Download redacted copy" onClick={() => download(v)}><Download className="h-3.5 w-3.5" /></Button>
                </div>
              </div>
            ))}
            {!vlist.length && <div className="text-xs text-muted">No backups yet. Set a profile and use “Backup now”.</div>}
          </div>
        </CardContent></Card>
      </div>

      {view === 'config' && selected && (
        <Card><CardContent className="space-y-2 pt-4">
          <div className="flex items-center justify-between">
            <div className="text-sm font-medium">Config · {relativeTime(selected.captured_at)} <span className="text-xs text-muted">({selected.line_count} lines)</span></div>
            <div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => download(selected)}><Download className="h-3.5 w-3.5" /> Redacted copy</Button>{can('ncm.export') && <Button size="sm" variant="outline" onClick={() => download(selected, true)}>Export raw recovery copy</Button>}<Button size="sm" variant="ghost" onClick={() => setView('none')}>Close</Button></div>
          </div>
          <pre className="max-h-[65vh] overflow-auto rounded-md border border-border bg-surface2/40 p-2 text-[11px] font-mono">{viewContent?.content || 'Loading…'}</pre>
        </CardContent></Card>
      )}
      {view === 'diff' && (
        <Card><CardContent className="space-y-2 pt-4">
          <div className="flex items-center justify-between"><div className="text-sm font-medium">Diff — latest two versions {diff ? <span className="text-xs text-muted">(+{diff.added} −{diff.removed})</span> : ''}</div><Button size="sm" variant="ghost" onClick={() => setView('none')}>Close</Button></div>
          {diff ? <DiffView diff={diff.diff} /> : <div className="text-xs text-muted">Loading comparison…</div>}
        </CardContent></Card>
      )}

      {/* per-version comparison popup */}
      <Dialog open={!!cmp} onOpenChange={(o) => !o && setCmp(null)}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <GitCompare className="h-4 w-4" /> Config changes
              {cmpDiff ? <span className="text-xs font-normal text-muted">(+{cmpDiff.added} −{cmpDiff.removed})</span> : null}
            </DialogTitle>
          </DialogHeader>
          {cmp && (
            <div className="text-[11px] text-muted">
              {cmp.older.label || `${relativeTime(cmp.older.captured_at)} (${cmp.older.hash})`} → {relativeTime(cmp.newer.captured_at)} ({cmp.newer.hash})
            </div>
          )}
          {cmpDiff && cmpDiff.identical
            ? <div className="text-xs text-muted">No configuration differences after excluding known runtime metadata.</div>
            : <DiffView diff={cmpDiff?.diff || 'Loading…'} />}
        </DialogContent>
      </Dialog>
    </div>
  )
}
