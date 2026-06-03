import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { FileCode, RefreshCw, Save, GitCompare, Eye, X, KeyRound, Settings2, DownloadCloud, Plus, Trash2, Pencil } from 'lucide-react'
import { api } from '@/lib/api'
import { relativeTime, apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Textarea } from '@/components/ui/Textarea'
import { Switch } from '@/components/ui/Switch'
import { FormField } from '@/components/ui/FormField'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/Dialog'
import { toast } from '@/components/ui/Toast'

function DiffView({ diff }: { diff: string }) {
  const lines = diff ? diff.split('\n') : []
  if (!lines.length) return <div className="text-xs text-muted">Configs are identical.</div>
  return (
    <pre className="max-h-[420px] overflow-auto rounded-md border border-border bg-surface2/40 text-[11px] leading-relaxed">
      {lines.map((l, i) => {
        const cls =
          l.startsWith('+') && !l.startsWith('+++') ? 'bg-success/10 text-success'
            : l.startsWith('-') && !l.startsWith('---') ? 'bg-danger/10 text-danger'
            : l.startsWith('@@') ? 'text-primary' : 'text-muted'
        return <div key={i} className={`whitespace-pre-wrap px-2 font-mono ${cls}`}>{l || ' '}</div>
      })}
    </pre>
  )
}

function statusBadge(d: any) {
  if (d.last_status === 'success') return <Badge variant="success">success</Badge>
  if (d.last_status === 'failed') return <Badge variant="danger">failed</Badge>
  if (d.versions) return <Badge variant="info">stored</Badge>
  return <span className="text-xs text-muted">never</span>
}

export function NcmPage() {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<any>(null)
  const [pasteFor, setPasteFor] = useState<any>(null)
  const [pasteText, setPasteText] = useState('')
  const [viewVersion, setViewVersion] = useState<any>(null)
  const [configFor, setConfigFor] = useState<any>(null)
  const [enroll, setEnroll] = useState<{ credential_id: string; platform: string; enabled: boolean }>({ credential_id: '', platform: 'autodetect', enabled: true })
  const [profilesOpen, setProfilesOpen] = useState(false)

  const { data: overview, isFetching, refetch } = useQuery<any>({
    queryKey: ['ncm', 'overview'],
    queryFn: async () => (await api.get('/ncm/overview')).data,
    refetchInterval: 30000,
  })
  const devices: any[] = overview?.data || []

  const { data: creds } = useQuery<any>({
    queryKey: ['ncm', 'credentials'],
    queryFn: async () => (await api.get('/ncm/credentials')).data,
  })
  const credentials: any[] = creds?.data || []

  const { data: platforms } = useQuery<any>({
    queryKey: ['ncm', 'platforms'],
    queryFn: async () => (await api.get('/ncm/platforms')).data,
  })
  const platformList: any[] = platforms?.data || [{ value: 'autodetect', label: 'Auto-detect' }]

  const { data: versions } = useQuery<any>({
    queryKey: ['ncm', 'configs', selected?.device_id],
    queryFn: async () => (await api.get(`/devices/${selected.device_id}/configs`)).data,
    enabled: !!selected,
  })
  const vlist: any[] = versions?.data || []

  const { data: diff } = useQuery<any>({
    queryKey: ['ncm', 'diff', selected?.device_id, vlist[0]?.id, vlist[1]?.id],
    queryFn: async () => (await api.get(`/devices/${selected.device_id}/configs-diff?a=${vlist[1].id}&b=${vlist[0].id}`)).data,
    enabled: !!selected && vlist.length >= 2,
  })

  const { data: viewContent } = useQuery<any>({
    queryKey: ['ncm', 'view', viewVersion?.id],
    queryFn: async () => (await api.get(`/devices/${selected.device_id}/configs/${viewVersion.id}`)).data,
    enabled: !!viewVersion && !!selected,
  })

  useEffect(() => {
    if (configFor) {
      const def = credentials.find((c) => c.is_default)
      setEnroll({
        credential_id: configFor.credential_id || def?.id || (credentials[0]?.id ?? ''),
        platform: configFor.platform || 'autodetect',
        enabled: configFor.enrolled ? true : true,
      })
    }
  }, [configFor]) // eslint-disable-line react-hooks/exhaustive-deps

  const saveEnroll = useMutation({
    mutationFn: async () => (await api.put(`/devices/${configFor.device_id}/ncm`, enroll)).data,
    onSuccess: () => { toast.success('Device configured for backup'); qc.invalidateQueries({ queryKey: ['ncm'] }); setConfigFor(null) },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })
  const unenroll = useMutation({
    mutationFn: async (id: string) => api.delete(`/devices/${id}/ncm`),
    onSuccess: () => { toast.success('Removed from backup'); qc.invalidateQueries({ queryKey: ['ncm'] }); setConfigFor(null) },
    onError: (e: any) => toast.error('Failed', apiErrorMessage(e)),
  })
  const fetchNow = useMutation({
    mutationFn: async (id: string) => (await api.post(`/devices/${id}/config-fetch`)).data,
    onSuccess: (d: any) => { toast.success(d.is_change ? `Config backed up via SSH (${d.platform})` : 'No change since last backup'); qc.invalidateQueries({ queryKey: ['ncm'] }) },
    onError: (e: any) => toast.error('SSH backup failed', apiErrorMessage(e)),
  })
  const backupPaste = useMutation({
    mutationFn: async () => (await api.post(`/devices/${pasteFor.device_id}/config-backup`, { content: pasteText, source_note: 'manual paste' })).data,
    onSuccess: (d: any) => { toast.success(d.is_change ? 'Config version saved' : 'No change since last backup'); qc.invalidateQueries({ queryKey: ['ncm'] }); setPasteFor(null); setPasteText('') },
    onError: (e: any) => toast.error('Backup failed', apiErrorMessage(e)),
  })

  const coverage = overview ? Math.round((overview.backed_up / Math.max(1, overview.total_devices)) * 100) : 0

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <FileCode className="h-5 w-5 text-primary" /> Config Backup (NCM)
          </h1>
          <p className="text-xs text-muted">Versioned device configuration archive with change diffs, over SSH</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => setProfilesOpen(true)}><KeyRound className="h-3.5 w-3.5" /> Connection Profiles</Button>
          <Button variant="outline" size="sm" onClick={() => refetch()}><RefreshCw className={`h-3.5 w-3.5 ${isFetching ? 'animate-spin' : ''}`} /> Refresh</Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Card><CardContent className="py-3"><div className="text-xs text-muted">Devices</div><div className="text-2xl font-semibold">{overview?.total_devices ?? 0}</div></CardContent></Card>
        <Card><CardContent className="py-3"><div className="text-xs text-muted">Enrolled</div><div className="text-2xl font-semibold text-primary">{overview?.enrolled ?? 0}</div></CardContent></Card>
        <Card><CardContent className="py-3"><div className="text-xs text-muted">Backed up</div><div className="text-2xl font-semibold text-success">{overview?.backed_up ?? 0}</div></CardContent></Card>
        <Card><CardContent className="py-3"><div className="text-xs text-muted">Coverage</div><div className="text-2xl font-semibold">{coverage}%</div></CardContent></Card>
      </div>

      {!credentials.length && (
        <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-xs text-warning">
          No connection profiles yet. Create one (<span className="font-medium">Connection Profiles</span>) with the device CLI/SSH username &amp; password, then configure devices for automatic backup.
        </div>
      )}

      <Card>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th>Device</Th>
                  <Th>Type / Vendor</Th>
                  <Th>Backup profile</Th>
                  <Th>Status</Th>
                  <Th>Last backup</Th>
                  <Th className="text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {devices.map((d) => (
                  <Tr key={d.device_id}>
                    <Td><div className="font-medium">{d.hostname}</div><div className="font-mono text-xs text-muted">{d.ip}</div></Td>
                    <Td className="text-xs text-muted">{d.device_type || '—'}{d.vendor ? ` · ${d.vendor}` : ''}</Td>
                    <Td className="text-xs">
                      {d.enrolled
                        ? <div><div>{d.credential_name || <span className="text-warning">no profile</span>}</div><div className="text-muted">{d.platform}</div></div>
                        : <span className="text-muted">not configured</span>}
                    </Td>
                    <Td>{statusBadge(d)}{d.versions ? <span className="ml-1 text-xs text-muted">· {d.versions}v</span> : null}</Td>
                    <Td className="text-xs text-muted">{d.last_capture ? `${relativeTime(d.last_capture)} · ${d.last_by}` : '—'}</Td>
                    <Td>
                      <div className="flex justify-end gap-1">
                        {d.enrolled && (
                          <Button variant="outline" size="sm" title="Pull running-config over SSH now"
                            disabled={fetchNow.isPending} onClick={() => fetchNow.mutate(d.device_id)}>
                            <DownloadCloud className="h-3.5 w-3.5" /> Backup now
                          </Button>
                        )}
                        <Button variant="ghost" size="sm" onClick={() => setConfigFor(d)}>
                          <Settings2 className="h-3.5 w-3.5" /> {d.enrolled ? 'Configure' : 'Add to backup'}
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => setSelected(d)} disabled={!d.versions}>
                          <GitCompare className="h-3.5 w-3.5" /> History
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
                {!devices.length && <Tr><Td colSpan={6} className="py-10 text-center text-muted">No devices.</Td></Tr>}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      {selected && (
        <Card>
          <CardContent className="space-y-3 pt-4">
            <div className="flex items-center justify-between">
              <h3 className="font-semibold">{selected.hostname} — version history</h3>
              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setSelected(null)}><X className="h-4 w-4" /></Button>
            </div>
            <div className="grid gap-3 md:grid-cols-[260px_1fr]">
              <div className="space-y-1">
                {vlist.map((v) => (
                  <div key={v.id} className="flex items-center justify-between rounded border border-border px-2 py-1 text-xs">
                    <div><div>{relativeTime(v.captured_at)}</div><div className="text-muted">{v.line_count} lines · {v.captured_by} · {v.hash}</div></div>
                    <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setViewVersion(v)}><Eye className="h-3.5 w-3.5" /></Button>
                  </div>
                ))}
              </div>
              <div>
                <div className="mb-1 text-xs font-medium text-muted">Diff (latest two versions){diff ? ` · +${diff.added} −${diff.removed}` : ''}</div>
                {vlist.length < 2 ? <div className="text-xs text-muted">Need at least 2 versions to diff.</div> : <DiffView diff={diff?.diff || ''} />}
              </div>
            </div>
            <div className="flex justify-end">
              <Button variant="ghost" size="sm" onClick={() => { setPasteFor(selected); setPasteText('') }}><Save className="h-3.5 w-3.5" /> Add version (paste)</Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Configure / enroll device */}
      <Dialog open={!!configFor} onOpenChange={(o) => !o && setConfigFor(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader><DialogTitle>Backup configuration — {configFor?.hostname}</DialogTitle></DialogHeader>
          <div className="space-y-3">
            <FormField label="Connection profile">
              <Select value={enroll.credential_id} onValueChange={(v) => setEnroll({ ...enroll, credential_id: v })}>
                <SelectTrigger><SelectValue placeholder="Select a profile" /></SelectTrigger>
                <SelectContent>
                  {credentials.map((c) => <SelectItem key={c.id} value={c.id}>{c.name} ({c.username})</SelectItem>)}
                </SelectContent>
              </Select>
            </FormField>
            <FormField label="Platform" hint="Vendor CLI driver; Auto-detect probes the device.">
              <Select value={enroll.platform} onValueChange={(v) => setEnroll({ ...enroll, platform: v })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {platformList.map((p) => <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>)}
                </SelectContent>
              </Select>
            </FormField>
            <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
              <span className="text-xs font-medium uppercase tracking-wider text-muted">Enabled</span>
              <Switch checked={enroll.enabled} onCheckedChange={(v) => setEnroll({ ...enroll, enabled: v })} />
            </div>
          </div>
          <DialogFooter className="justify-between">
            {configFor?.enrolled
              ? <Button variant="ghost" className="text-danger" onClick={() => unenroll.mutate(configFor.device_id)}>Remove from backup</Button>
              : <span />}
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setConfigFor(null)}>Cancel</Button>
              <Button disabled={!enroll.credential_id || saveEnroll.isPending} onClick={() => saveEnroll.mutate()}>Save</Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Connection profiles manager */}
      <ProfilesDialog open={profilesOpen} onOpenChange={setProfilesOpen} credentials={credentials} platformList={platformList} />

      {/* paste backup */}
      <Dialog open={!!pasteFor} onOpenChange={(o) => !o && setPasteFor(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Add config version (paste) — {pasteFor?.hostname}</DialogTitle></DialogHeader>
          <p className="text-xs text-muted">Paste a running-config to archive a version manually. Use “Backup now” for automatic SSH retrieval.</p>
          <Textarea className="h-64 font-mono text-xs" value={pasteText} onChange={(e) => setPasteText(e.target.value)} placeholder={'hostname ...\ninterface ...'} />
          <DialogFooter>
            <Button variant="outline" onClick={() => setPasteFor(null)}>Cancel</Button>
            <Button disabled={!pasteText.trim() || backupPaste.isPending} onClick={() => backupPaste.mutate()}>Save version</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* view config */}
      <Dialog open={!!viewVersion} onOpenChange={(o) => !o && setViewVersion(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>Config · {viewVersion && relativeTime(viewVersion.captured_at)}</DialogTitle></DialogHeader>
          <pre className="max-h-[60vh] overflow-auto rounded-md border border-border bg-surface2/40 p-2 text-[11px] font-mono">{viewContent?.content || 'Loading…'}</pre>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function ProfilesDialog({ open, onOpenChange, credentials }: { open: boolean; onOpenChange: (o: boolean) => void; credentials: any[]; platformList: any[] }) {
  const qc = useQueryClient()
  const empty = { name: '', username: '', password: '', enable_password: '', port: 22, protocol: 'ssh', is_default: false }
  const [form, setForm] = useState<any>(empty)
  const [editing, setEditing] = useState<any>(null)

  const save = useMutation({
    mutationFn: async () => {
      const body = { ...form, password: form.password || (editing ? undefined : ''), enable_password: form.enable_password || undefined }
      if (editing) return (await api.put(`/ncm/credentials/${editing.id}`, body)).data
      return (await api.post('/ncm/credentials', body)).data
    },
    onSuccess: () => { toast.success(editing ? 'Profile updated' : 'Profile created'); qc.invalidateQueries({ queryKey: ['ncm'] }); setForm(empty); setEditing(null) },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })
  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/ncm/credentials/${id}`),
    onSuccess: () => { toast.success('Profile deleted'); qc.invalidateQueries({ queryKey: ['ncm'] }) },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) { setForm(empty); setEditing(null) } onOpenChange(o) }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader><DialogTitle>Connection profiles (CLI credentials)</DialogTitle></DialogHeader>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-1">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">Profiles</div>
            {credentials.map((c) => (
              <div key={c.id} className="flex items-center justify-between rounded border border-border px-2 py-1.5 text-xs">
                <div>
                  <div className="font-medium">{c.name}{c.is_default && <Badge variant="info" className="ml-1">default</Badge>}</div>
                  <div className="text-muted">{c.protocol}:{c.port} · {c.username} · used by {c.used_by}</div>
                </div>
                <div className="flex gap-0.5">
                  <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => { setEditing(c); setForm({ name: c.name, username: c.username, password: '', enable_password: '', port: c.port, protocol: c.protocol, is_default: c.is_default }) }}><Pencil className="h-3.5 w-3.5" /></Button>
                  <Button variant="ghost" size="icon" className="h-7 w-7 text-danger" onClick={() => del.mutate(c.id)}><Trash2 className="h-3.5 w-3.5" /></Button>
                </div>
              </div>
            ))}
            {!credentials.length && <div className="text-xs text-muted">No profiles yet.</div>}
          </div>
          <div className="space-y-2">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">{editing ? 'Edit profile' : 'New profile'}</div>
            <FormField label="Name"><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Core switches SSH" /></FormField>
            <div className="grid grid-cols-2 gap-2">
              <FormField label="Username"><Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></FormField>
              <FormField label="Port"><Input type="number" value={form.port} onChange={(e) => setForm({ ...form, port: Number(e.target.value) })} /></FormField>
            </div>
            <FormField label={editing ? 'Password (leave blank to keep)' : 'Password'}><Input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></FormField>
            <FormField label="Enable / secret (optional)"><Input type="password" value={form.enable_password} onChange={(e) => setForm({ ...form, enable_password: e.target.value })} /></FormField>
            <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
              <span className="text-xs font-medium uppercase tracking-wider text-muted">Default profile</span>
              <Switch checked={form.is_default} onCheckedChange={(v) => setForm({ ...form, is_default: v })} />
            </div>
            <div className="flex justify-end gap-2">
              {editing && <Button variant="outline" onClick={() => { setEditing(null); setForm(empty) }}>New</Button>}
              <Button disabled={!form.name || !form.username || save.isPending} onClick={() => save.mutate()}>
                <Plus className="h-3.5 w-3.5" /> {editing ? 'Update' : 'Create'}
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
