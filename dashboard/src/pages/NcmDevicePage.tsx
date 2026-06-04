import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Save, DownloadCloud, Download, Eye, GitCompare, FileCode, Clock, Loader2 } from 'lucide-react'
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

function statusKey(d: any) {
  if (!d?.enrolled) return 'unconfigured'
  if (d.last_status === 'failed') return 'failed'
  if (d.versions) return 'backed_up'
  return 'pending'
}
const SB: Record<string, { label: string; v: any }> = {
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
  const { deviceId } = useParams()
  const qc = useQueryClient()
  const [view, setView] = useState<'none' | 'config' | 'diff'>('none')
  const [selected, setSelected] = useState<any>(null)
  const [cmp, setCmp] = useState<{ older: any; newer: any } | null>(null)

  const { data: overview } = useQuery<any>({ queryKey: ['ncm', 'overview'], queryFn: async () => (await api.get('/ncm/overview')).data, refetchInterval: 30000 })
  const device = (overview?.data || []).find((d: any) => d.device_id === deviceId)
  const { data: creds } = useQuery<any>({ queryKey: ['ncm', 'credentials'], queryFn: async () => (await api.get('/ncm/credentials')).data })
  const { data: platforms } = useQuery<any>({ queryKey: ['ncm', 'platforms'], queryFn: async () => (await api.get('/ncm/platforms')).data })
  const credentials: any[] = creds?.data || []
  const platformList: any[] = platforms?.data || [{ value: 'autodetect', label: 'Auto-detect' }]

  const { data: versions } = useQuery<any>({ queryKey: ['ncm', 'configs', deviceId], queryFn: async () => (await api.get(`/devices/${deviceId}/configs`)).data, enabled: !!deviceId })
  const vlist: any[] = versions?.data || []
  const { data: diff } = useQuery<any>({ queryKey: ['ncm', 'diff', deviceId, vlist[0]?.id, vlist[1]?.id], queryFn: async () => (await api.get(`/devices/${deviceId}/configs-diff?a=${vlist[1].id}&b=${vlist[0].id}`)).data, enabled: vlist.length >= 2 })
  const { data: viewContent } = useQuery<any>({ queryKey: ['ncm', 'view', selected?.id], queryFn: async () => (await api.get(`/devices/${deviceId}/configs/${selected.id}`)).data, enabled: !!selected })
  const { data: cmpDiff } = useQuery<any>({ queryKey: ['ncm', 'cmp', deviceId, cmp?.older?.id, cmp?.newer?.id], queryFn: async () => (await api.get(`/devices/${deviceId}/configs-diff?a=${cmp!.older.id}&b=${cmp!.newer.id}`)).data, enabled: !!cmp })

  const def = credentials.find((c) => c.is_default)
  const [form, setForm] = useState<any>(null)
  const f = form || {
    credential_id: device?.credential_id || def?.id || (credentials[0]?.id ?? ''),
    platform: device?.platform || 'autodetect',
    enabled: true,
    schedule_enabled: !!device?.schedule_enabled,
    schedule_type: device?.schedule_type || 'interval',
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
  const fetchNow = useMutation({ mutationFn: async () => (await api.post(`/devices/${deviceId}/config-fetch`, null, { timeout: 240000 })).data, onSuccess: (d: any) => { toast.success(d.is_change ? `Backed up via SSH (${d.platform})` : 'No change since last backup'); inv(); qc.invalidateQueries({ queryKey: ['ncm', 'configs', deviceId] }) }, onError: (e: any) => toast.error('SSH backup failed', apiErrorMessage(e)) })

  async function download(v: any) {
    try {
      const c = (await api.get(`/devices/${deviceId}/configs/${v.id}`)).data
      const blob = new Blob([c.content || ''], { type: 'text/plain' })
      const url = URL.createObjectURL(blob); const a = document.createElement('a')
      a.href = url; a.download = `${device?.hostname || 'device'}-${v.captured_at.slice(0, 19).replace(/[:T]/g, '-')}.cfg`; a.click(); URL.revokeObjectURL(url)
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
        <Button variant="outline" disabled={!device.enrolled || fetchNow.isPending} onClick={() => fetchNow.mutate()}>
          {fetchNow.isPending
            ? <><Loader2 className="h-4 w-4 animate-spin" /> Backing up…</>
            : <><DownloadCloud className="h-4 w-4" /> Backup now (SSH)</>}
        </Button>
      </div>

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
                <FormField label="At (server time, 24h)"><Input type="time" value={f.schedule_time} onChange={(e) => set({ schedule_time: e.target.value })} /></FormField>
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
              <p className="text-[11px] text-muted">A job runs hourly and backs up devices when their schedule is due.</p>
            </div>
          )}
          <FormField label="Keep versions" hint="Older versions beyond this are auto-deleted from the server.">
            <Input type="number" min={1} max={100} value={f.keep_versions} onChange={(e) => set({ keep_versions: Number(e.target.value) })} />
          </FormField>
          <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
            <span className="text-xs font-medium uppercase tracking-wider text-muted">Alert on config change</span>
            <Switch checked={f.alert_on_change} onCheckedChange={(v) => set({ alert_on_change: v })} />
          </div>
          {device.last_status === 'failed' && device.last_error && <div className="rounded border border-danger/30 bg-danger/10 p-2 text-[11px] text-danger">Last error: {device.last_error}</div>}
          {device.last_success_at && <div className="text-[11px] text-muted">Last success: {relativeTime(device.last_success_at)}</div>}
          <div className="flex flex-wrap gap-2 pt-1">
            <Button size="sm" disabled={!f.credential_id || saveEnroll.isPending} onClick={() => saveEnroll.mutate()}><Save className="h-3.5 w-3.5" /> Save</Button>
            {device.enrolled && <Button size="sm" variant="ghost" className="text-danger" onClick={() => unenroll.mutate()}>Remove</Button>}
          </div>
        </CardContent></Card>

        {/* versions */}
        <Card><CardContent className="space-y-2 pt-4">
          <div className="flex items-center justify-between">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">Versions ({vlist.length})</div>
            {vlist.length >= 2 && <Button size="sm" variant="outline" onClick={() => { setView(view === 'diff' ? 'none' : 'diff'); setSelected(null) }}><GitCompare className="h-3.5 w-3.5" /> Diff latest{diff ? ` (+${diff.added} −${diff.removed})` : ''}</Button>}
          </div>
          <div className="max-h-72 space-y-1 overflow-auto">
            {vlist.map((v, i) => (
              <div key={v.id} className={`flex items-center justify-between rounded border px-2 py-1 text-xs ${selected?.id === v.id ? 'border-primary/50 bg-primary/5' : 'border-border'}`}>
                <div><div>{relativeTime(v.captured_at)}</div><div className="text-muted">{v.line_count} lines · {v.size_bytes} B · {v.captured_by} · {v.hash}</div></div>
                <div className="flex gap-0.5">
                  {i < vlist.length - 1 && (
                    <Button variant="ghost" size="icon" className="h-7 w-7" title="Compare to previous version" onClick={() => setCmp({ older: vlist[i + 1], newer: v })}><GitCompare className="h-3.5 w-3.5" /></Button>
                  )}
                  <Button variant="ghost" size="icon" className="h-7 w-7" title="View" onClick={() => { setSelected(v); setView('config') }}><Eye className="h-3.5 w-3.5" /></Button>
                  <Button variant="ghost" size="icon" className="h-7 w-7" title="Download" onClick={() => download(v)}><Download className="h-3.5 w-3.5" /></Button>
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
            <div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => download(selected)}><Download className="h-3.5 w-3.5" /> Download</Button><Button size="sm" variant="ghost" onClick={() => setView('none')}>Close</Button></div>
          </div>
          <pre className="max-h-[65vh] overflow-auto rounded-md border border-border bg-surface2/40 p-2 text-[11px] font-mono">{viewContent?.content || 'Loading…'}</pre>
        </CardContent></Card>
      )}
      {view === 'diff' && (
        <Card><CardContent className="space-y-2 pt-4">
          <div className="flex items-center justify-between"><div className="text-sm font-medium">Diff — latest two versions {diff ? <span className="text-xs text-muted">(+{diff.added} −{diff.removed})</span> : ''}</div><Button size="sm" variant="ghost" onClick={() => setView('none')}>Close</Button></div>
          <DiffView diff={diff?.diff || ''} />
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
              {relativeTime(cmp.older.captured_at)} ({cmp.older.hash}) → {relativeTime(cmp.newer.captured_at)} ({cmp.newer.hash})
            </div>
          )}
          {cmpDiff && cmpDiff.identical
            ? <div className="text-xs text-muted">No differences (after masking volatile fields).</div>
            : <DiffView diff={cmpDiff?.diff || 'Loading…'} />}
        </DialogContent>
      </Dialog>
    </div>
  )
}
