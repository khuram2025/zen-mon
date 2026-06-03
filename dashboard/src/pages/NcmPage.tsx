import { useMemo, useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  FileCode, RefreshCw, Save, Eye, X, KeyRound, Settings2, DownloadCloud, Download,
  Plus, Trash2, Pencil, Search, ChevronLeft, ChevronRight, Clock, GitCompare,
} from 'lucide-react'
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

const PAGE_SIZE = 10
type Filter = { key: 'status' | 'type' | 'location' | 'vendor'; value: string } | null

function statusKey(d: any): 'backed_up' | 'failed' | 'pending' | 'unconfigured' {
  if (!d.enrolled) return 'unconfigured'
  if (d.last_status === 'failed') return 'failed'
  if (d.versions) return 'backed_up'
  return 'pending'
}
const STATUS_META: Record<string, { label: string; cls: string; badge: any }> = {
  backed_up: { label: 'Backed up', cls: 'bg-success', badge: 'success' },
  failed: { label: 'Failed', cls: 'bg-danger', badge: 'danger' },
  pending: { label: 'Pending', cls: 'bg-warning', badge: 'warning' },
  unconfigured: { label: 'Not configured', cls: 'bg-border', badge: undefined },
}

function StatusBadge({ d }: { d: any }) {
  const m = STATUS_META[statusKey(d)]
  return m.badge ? <Badge variant={m.badge}>{m.label}{d.versions ? ` · ${d.versions}v` : ''}</Badge> : <span className="text-xs text-muted">{m.label}</span>
}

function Facet({ title, items, active, onPick }: { title: string; items: [string, number][]; active?: string; onPick: (v: string) => void }) {
  if (!items.length) return null
  const max = Math.max(...items.map((i) => i[1]), 1)
  return (
    <div className="space-y-1.5">
      <div className="text-xs font-semibold uppercase tracking-wider text-muted">{title}</div>
      {items.map(([label, n]) => (
        <button key={label} onClick={() => onPick(label)}
          className={`group w-full rounded px-2 py-1 text-left text-xs transition-colors ${active === label ? 'bg-primary/10 ring-1 ring-primary/40' : 'hover:bg-surface2'}`}>
          <div className="flex items-center justify-between">
            <span className="truncate">{label}</span>
            <span className="ml-2 tabular-nums text-muted">{n}</span>
          </div>
          <div className="mt-1 h-1 overflow-hidden rounded-full bg-surface2">
            <div className="h-full rounded-full bg-primary/60" style={{ width: `${(n / max) * 100}%` }} />
          </div>
        </button>
      ))}
    </div>
  )
}

export function NcmPage() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<Filter>(null)
  const [page, setPage] = useState(1)
  const [profilesOpen, setProfilesOpen] = useState(false)
  const openDevice = (d: any) => navigate(`/ncm/${d.device_id}`)

  const { data: overview, isFetching, refetch } = useQuery<any>({
    queryKey: ['ncm', 'overview'], queryFn: async () => (await api.get('/ncm/overview')).data, refetchInterval: 30000,
  })
  const { data: creds } = useQuery<any>({ queryKey: ['ncm', 'credentials'], queryFn: async () => (await api.get('/ncm/credentials')).data })
  const { data: platforms } = useQuery<any>({ queryKey: ['ncm', 'platforms'], queryFn: async () => (await api.get('/ncm/platforms')).data })
  const devices: any[] = overview?.data || []
  const credentials: any[] = creds?.data || []
  const platformList: any[] = platforms?.data || [{ value: 'autodetect', label: 'Auto-detect' }]

  const runScheduled = useMutation({
    mutationFn: async () => (await api.post('/ncm/run-scheduled', null, { timeout: 600000 })).data,
    onSuccess: (d: any) => { toast.success(`Scheduled run: ${d.backed_up} backed up, ${d.failed} failed (${d.due} due)`); qc.invalidateQueries({ queryKey: ['ncm'] }) },
    onError: (e: any) => toast.error('Run failed', apiErrorMessage(e)),
  })

  const facets = useMemo(() => {
    const by = (fn: (d: any) => string | null | undefined) => {
      const m = new Map<string, number>()
      for (const d of devices) { const k = fn(d); if (k) m.set(k, (m.get(k) || 0) + 1) }
      return [...m.entries()].sort((a, b) => b[1] - a[1])
    }
    return {
      status: by((d) => STATUS_META[statusKey(d)].label),
      type: by((d) => d.device_type || 'unknown'),
      location: by((d) => d.location || 'unassigned'),
      vendor: by((d) => d.vendor || 'unknown'),
    }
  }, [devices])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return devices.filter((d) => {
      if (q && ![d.hostname, d.ip, d.vendor, d.device_type, d.location, d.credential_name, d.platform]
        .some((v) => (v || '').toString().toLowerCase().includes(q))) return false
      if (filter) {
        if (filter.key === 'status' && STATUS_META[statusKey(d)].label !== filter.value) return false
        if (filter.key === 'type' && (d.device_type || 'unknown') !== filter.value) return false
        if (filter.key === 'location' && (d.location || 'unassigned') !== filter.value) return false
        if (filter.key === 'vendor' && (d.vendor || 'unknown') !== filter.value) return false
      }
      return true
    })
  }, [devices, search, filter])

  useEffect(() => { setPage(1) }, [search, filter])
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)
  const coverage = overview ? Math.round((overview.backed_up / Math.max(1, overview.total_devices)) * 100) : 0
  const pick = (key: Filter extends null ? never : NonNullable<Filter>['key'], value: string) =>
    setFilter((f) => (f && f.key === key && f.value === value ? null : { key, value }))

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <FileCode className="h-5 w-5 text-primary" /> Config Backup (NCM)
          </h1>
          <p className="text-xs text-muted">Versioned device configuration archive with change diffs, over SSH</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => runScheduled.mutate()} disabled={runScheduled.isPending}><Clock className="h-3.5 w-3.5" /> Run scheduled</Button>
          <Button variant="outline" size="sm" onClick={() => setProfilesOpen(true)}><KeyRound className="h-3.5 w-3.5" /> Connection Profiles</Button>
          <Button variant="outline" size="sm" onClick={() => refetch()}><RefreshCw className={`h-3.5 w-3.5 ${isFetching ? 'animate-spin' : ''}`} /> Refresh</Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          { label: 'Devices', value: overview?.total_devices ?? 0, cls: 'text-text' },
          { label: 'Enrolled', value: overview?.enrolled ?? 0, cls: 'text-primary' },
          { label: 'Backed up', value: overview?.backed_up ?? 0, cls: 'text-success' },
          { label: 'Coverage', value: `${coverage}%`, cls: 'text-text' },
        ].map((c) => (
          <Card key={c.label}><CardContent className="py-3"><div className="text-xs text-muted">{c.label}</div><div className={`text-2xl font-semibold ${c.cls}`}>{c.value}</div></CardContent></Card>
        ))}
      </div>

      {!credentials.length && (
        <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-xs text-warning">
          No connection profiles yet. Create one (<span className="font-medium">Connection Profiles</span>) with the device CLI/SSH username &amp; password, then add devices to backup.
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[1fr_280px]">
        {/* main */}
        <Card>
          <CardContent className="space-y-3 pt-4">
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-[220px] flex-1">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                <Input className="pl-8" placeholder="Search hostname, IP, vendor, location, profile…" value={search} onChange={(e) => setSearch(e.target.value)} />
              </div>
              {filter && (
                <button onClick={() => setFilter(null)} className="flex items-center gap-1 rounded-full bg-primary/10 px-2.5 py-1 text-xs text-primary">
                  {filter.key}: {filter.value} <X className="h-3 w-3" />
                </button>
              )}
              <span className="text-xs text-muted">{filtered.length} device{filtered.length === 1 ? '' : 's'}</span>
            </div>

            <div className="overflow-hidden rounded-md border border-border">
              <Table>
                <THead className="bg-surface2/50">
                  <Tr>
                    <Th>Device</Th><Th>Type / Vendor</Th><Th>Location</Th><Th>Backup profile</Th><Th>Status</Th><Th className="text-right">Actions</Th>
                  </Tr>
                </THead>
                <TBody>
                  {pageRows.map((d) => (
                    <Tr key={d.device_id} className="cursor-pointer" onClick={() => openDevice(d)}>
                      <Td><div className="font-medium">{d.hostname}</div><div className="font-mono text-xs text-muted">{d.ip}</div></Td>
                      <Td className="text-xs text-muted">{d.device_type || '—'}{d.vendor ? ` · ${d.vendor}` : ''}</Td>
                      <Td className="text-xs text-muted">{d.location || '—'}</Td>
                      <Td className="text-xs">{d.enrolled ? <div><div>{d.credential_name || <span className="text-warning">no profile</span>}</div><div className="text-muted">{d.platform}{d.schedule_enabled ? ` · ${d.schedule_interval_hours}h` : ''}</div></div> : <span className="text-muted">not configured</span>}</Td>
                      <Td><StatusBadge d={d} /></Td>
                      <Td onClick={(e) => e.stopPropagation()}>
                        <div className="flex justify-end">
                          <Button variant="ghost" size="sm" onClick={() => openDevice(d)}><Settings2 className="h-3.5 w-3.5" /> Details</Button>
                        </div>
                      </Td>
                    </Tr>
                  ))}
                  {!pageRows.length && <Tr><Td colSpan={6} className="py-10 text-center text-muted">No matching devices.</Td></Tr>}
                </TBody>
              </Table>
            </div>

            {pageCount > 1 && (
              <div className="flex items-center justify-between text-xs text-muted">
                <span>Page {page} of {pageCount}</span>
                <div className="flex items-center gap-1">
                  <Button variant="outline" size="icon" className="h-7 w-7" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}><ChevronLeft className="h-4 w-4" /></Button>
                  {Array.from({ length: pageCount }).slice(0, 7).map((_, i) => (
                    <Button key={i} variant={page === i + 1 ? 'default' : 'outline'} size="icon" className="h-7 w-7" onClick={() => setPage(i + 1)}>{i + 1}</Button>
                  ))}
                  <Button variant="outline" size="icon" className="h-7 w-7" disabled={page >= pageCount} onClick={() => setPage((p) => p + 1)}><ChevronRight className="h-4 w-4" /></Button>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        {/* right breakdown */}
        <Card>
          <CardContent className="space-y-4 pt-4">
            <div className="text-sm font-semibold">Breakdown</div>
            <Facet title="By status" items={facets.status} active={filter?.key === 'status' ? filter.value : undefined} onPick={(v) => pick('status', v)} />
            <Facet title="By category" items={facets.type} active={filter?.key === 'type' ? filter.value : undefined} onPick={(v) => pick('type', v)} />
            <Facet title="By location" items={facets.location} active={filter?.key === 'location' ? filter.value : undefined} onPick={(v) => pick('location', v)} />
            <Facet title="By vendor" items={facets.vendor} active={filter?.key === 'vendor' ? filter.value : undefined} onPick={(v) => pick('vendor', v)} />
          </CardContent>
        </Card>
      </div>

      <ProfilesDialog open={profilesOpen} onOpenChange={setProfilesOpen} credentials={credentials} />
    </div>
  )
}

function ProfilesDialog({ open, onOpenChange, credentials }: { open: boolean; onOpenChange: (o: boolean) => void; credentials: any[] }) {
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
  const del = useMutation({ mutationFn: async (id: string) => api.delete(`/ncm/credentials/${id}`), onSuccess: () => { toast.success('Profile deleted'); qc.invalidateQueries({ queryKey: ['ncm'] }) }, onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)) })

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) { setForm(empty); setEditing(null) } onOpenChange(o) }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader><DialogTitle>Connection profiles (CLI credentials)</DialogTitle></DialogHeader>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-1">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted">Profiles</div>
            {credentials.map((c) => (
              <div key={c.id} className="flex items-center justify-between rounded border border-border px-2 py-1.5 text-xs">
                <div><div className="font-medium">{c.name}{c.is_default && <Badge variant="info" className="ml-1">default</Badge>}</div><div className="text-muted">{c.protocol}:{c.port} · {c.username} · used by {c.used_by}</div></div>
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
            <FormField label={editing ? 'Password (blank = keep)' : 'Password'}><Input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></FormField>
            <FormField label="Enable / secret (optional)"><Input type="password" value={form.enable_password} onChange={(e) => setForm({ ...form, enable_password: e.target.value })} /></FormField>
            <div className="flex items-center justify-between rounded-md border border-border px-3 py-2"><span className="text-xs font-medium uppercase tracking-wider text-muted">Default profile</span><Switch checked={form.is_default} onCheckedChange={(v) => setForm({ ...form, is_default: v })} /></div>
            <div className="flex justify-end gap-2">
              {editing && <Button variant="outline" onClick={() => { setEditing(null); setForm(empty) }}>New</Button>}
              <Button disabled={!form.name || !form.username || save.isPending} onClick={() => save.mutate()}><Plus className="h-3.5 w-3.5" /> {editing ? 'Update' : 'Create'}</Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
