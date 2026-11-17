import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Loader2, Pencil, Plus, Save, Sparkles, Trash2, UserMinus } from 'lucide-react'
import { api } from '@/lib/api'
import { useCan } from '@/stores/auth'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { QueryFailure } from '@/components/ui/QueryState'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { SkeletonCard } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { GroupEditor as ConditionGroupEditor } from '@/pages/discovery/RuleEditorPage'
import { conditionProblems, summarizeCondition } from '@/pages/discovery/ruleModel'
import type { ConditionGroup } from '@/pages/discovery/types'
import {
  type AssetKind, type DeviceGroup, type Preview, apiDetail, groupsApi, treeOrder, useGroupCatalog, useGroupLookups,
} from './api'
import { MANAGE_GROUPS } from './DeviceGroupsPage'

type Draft = {
  name: string
  description: string
  parent_id: string | null
  kind: 'static' | 'dynamic'
  asset_kinds: AssetKind[]
  condition: ConditionGroup
}

const EMPTY: ConditionGroup = { op: 'all', conditions: [] }

export function GroupDetailPage() {
  const { id = '' } = useParams()
  const isNew = id === 'new'
  const can = useCan()
  const canManage = can(MANAGE_GROUPS)
  const q = useQuery({ queryKey: ['device-group', id], queryFn: () => groupsApi.get(id), enabled: !isNew })
  const [editing, setEditing] = useState(isNew)

  if (!isNew && q.isLoading) return <SkeletonCard />
  if (!isNew && (q.isError || !q.data)) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="this group" />
  const g = q.data

  return (
    <div className="space-y-4">
      <Link to="/device-groups" className="inline-flex items-center gap-1 text-xs text-muted hover:text-text"><ArrowLeft className="h-3 w-3" /> Groups &amp; Fields</Link>
      {editing || isNew
        ? <GroupForm group={g} onDone={() => setEditing(false)} />
        : g && <GroupView group={g} canManage={canManage} onEdit={() => setEditing(true)} />}
    </div>
  )
}

function GroupView({ group: g, canManage, onEdit }: { group: DeviceGroup & { used_by: any }; canManage: boolean; onEdit: () => void }) {
  const nav = useNavigate()
  const qc = useQueryClient()
  const { data: catalog } = useGroupCatalog()
  const { lookups } = useGroupLookups()
  const groups = useQuery({ queryKey: ['groups-tree'], queryFn: groupsApi.list })
  const members = useQuery({ queryKey: ['device-group-members', g.id], queryFn: () => groupsApi.members(g.id) })
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [adding, setAdding] = useState(false)
  const parent = (groups.data || []).find((x) => x.id === g.parent_id)
  const del = useMutation({
    mutationFn: async () => api.delete(`/device-groups/${g.id}`),
    onSuccess: () => { toast.success('Group deleted'); qc.invalidateQueries({ queryKey: ['groups-tree'] }); nav('/device-groups') },
    onError: (e) => { toast.error('Delete refused', apiDetail(e)); setConfirmDelete(false) },
  })
  const remove = useMutation({
    mutationFn: async ({ kind, ids }: { kind: AssetKind; ids: string[] }) =>
      (await api.post(`/device-groups/${g.id}/members/remove`, { asset_kind: kind, asset_ids: ids })).data,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['device-group-members', g.id] }); qc.invalidateQueries({ queryKey: ['device-group', g.id] }) },
    onError: (e) => toast.error('Failed', apiDetail(e)),
  })
  const u = g.used_by
  const usedLists: [string, { id: string; name: string | null }[], (x: any) => string][] = [
    ['Alert rules', u.alert_rules, () => '/alert-rules'],
    ['Discovery rules', u.discovery_rules, (x) => `/discovery/rules/${x.id}`],
    ['Discovery profiles', u.discovery_profiles, (x) => `/discovery/profiles/${x.id}`],
    ['Subgroups', u.subgroups, (x) => `/device-groups/${x.id}`],
  ]
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            {g.name} {g.kind === 'dynamic' ? <Badge variant="info"><Sparkles className="h-3 w-3" />Dynamic</Badge> : <Badge variant="outline">Static</Badge>}
          </h1>
          <div className="mt-1 text-sm text-muted">
            {g.member_count} member(s) · {g.asset_kinds.join(' and ')}s{parent && <> · in <Link className="text-primary" to={`/device-groups/${parent.id}`}>{parent.name}</Link></>}
            {g.evaluated_at && g.kind === 'dynamic' && <> · evaluated {new Date(g.evaluated_at).toLocaleString()}</>}
          </div>
          {g.description && <p className="mt-1 max-w-3xl text-sm">{g.description}</p>}
        </div>
        {canManage && (
          <div className="flex gap-2">
            {g.kind === 'static' && <Button variant="outline" onClick={() => setAdding(true)}><Plus className="h-4 w-4" /> Add members</Button>}
            <Button variant="outline" onClick={onEdit}><Pencil className="h-4 w-4" /> Edit</Button>
            <Button variant="outline" onClick={() => setConfirmDelete(true)}><Trash2 className="h-4 w-4" /> Delete</Button>
          </div>
        )}
      </div>
      {g.eval_error && <Card><CardContent className="p-3 text-sm text-danger">The condition could not be evaluated; the previous members are kept: {g.eval_error}</CardContent></Card>}
      {g.kind === 'dynamic' && g.condition && (
        <Card><CardContent className="p-4 text-sm">
          <div className="mb-1 text-xs font-medium text-muted">Members are</div>
          {catalog ? summarizeCondition(g.condition, catalog, lookups) : '…'}
        </CardContent></Card>
      )}
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/60"><Tr><Th>Member</Th><Th>Kind</Th><Th>How</Th><Th>Status</Th><Th /></Tr></THead>
              <TBody>
                {(members.data || []).map((m) => (
                  <Tr key={`${m.asset_kind}-${m.id}`}>
                    <Td><Link className="font-medium hover:text-primary" to={m.asset_kind === 'device' ? `/devices/${m.id}` : `/servers/${m.id}`}>{m.name || m.id}</Link>
                      <div className="text-xs text-muted">{m.ip_address}</div></Td>
                    <Td className="text-sm">{m.asset_kind}</Td>
                    <Td className="text-xs text-muted">
                      {m.via_subgroup ? 'through a subgroup' : m.sources.map((s) => ({ primary: 'its group', static: 'added', rule: 'condition' } as Record<string, string>)[s] || s).join(', ')}
                    </Td>
                    <Td className="text-xs">{m.status || '—'}</Td>
                    <Td className="text-right">
                      {canManage && g.kind === 'static' && !m.via_subgroup && m.sources.includes('static') && (
                        <Button size="icon" variant="ghost" title="Remove from the group" onClick={() => remove.mutate({ kind: m.asset_kind, ids: [m.id] })}>
                          <UserMinus className="h-4 w-4" /></Button>
                      )}
                    </Td>
                  </Tr>
                ))}
                {!members.isLoading && (members.data || []).length === 0 && <Tr><Td colSpan={5} className="py-8 text-center text-sm text-muted">No members.</Td></Tr>}
              </TBody>
            </Table>
          </div>
          {g.kind === 'static' && <p className="mt-2 text-xs text-muted">A device whose own group this is joins and leaves with its group setting (Devices → Edit); members "added" here can be removed here.</p>}
        </div>
        <Card><CardContent className="space-y-3 p-4 text-sm">
          <div className="font-medium">Used by</div>
          {usedLists.map(([label, list, href]) => list.length > 0 && (
            <div key={label}><div className="text-xs text-muted">{label}</div>
              {list.map((x) => <div key={x.id}><Link className="text-primary hover:underline" to={href(x)}>{x.name || x.id}</Link></div>)}</div>
          ))}
          {u.maintenance.length > 0 && <div><div className="text-xs text-muted">Maintenance windows</div>{u.maintenance.length} window(s)</div>}
          {u.sensor_assignments.length > 0 && <div><div className="text-xs text-muted">Sensor assignments</div>{u.sensor_assignments.map((s: any) => s.name).join(', ')}</div>}
          {u.primary_devices > 0 && <div><div className="text-xs text-muted">Devices whose group it is</div>{u.primary_devices}</div>}
          {usedLists.every(([, l]) => !l.length) && !u.maintenance.length && !u.sensor_assignments.length && !u.primary_devices &&
            <div className="text-xs text-muted">Nothing uses this group yet.</div>}
        </CardContent></Card>
      </div>
      {adding && <AddMembersDialog group={g} onClose={() => setAdding(false)} />}
      <ConfirmDialog open={confirmDelete} onOpenChange={setConfirmDelete} destructive confirmText="Delete" title={`Delete ${g.name}?`}
                     loading={del.isPending} onConfirm={() => del.mutate()}
                     description="A group that alert rules, maintenance windows, sensors, discovery or subgroups use, or that is some device's own group, cannot be deleted." />
    </>
  )
}

function GroupForm({ group, onDone }: { group?: DeviceGroup; onDone: () => void }) {
  const nav = useNavigate()
  const qc = useQueryClient()
  const { data: catalog } = useGroupCatalog()
  const { options, lookups } = useGroupLookups()
  const groups = useQuery({ queryKey: ['groups-tree'], queryFn: groupsApi.list })
  const [d, setD] = useState<Draft>(() => ({
    name: group?.name || '', description: group?.description || '', parent_id: group?.parent_id || null,
    kind: group?.kind || 'static', asset_kinds: group?.asset_kinds || ['device'], condition: group?.condition || EMPTY,
  }))
  const [preview, setPreview] = useState<Preview | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const problems = useMemo(() => (d.kind === 'dynamic' && catalog ? conditionProblems(d.condition, catalog) : []), [d, catalog])
  useEffect(() => {
    if (d.kind !== 'dynamic' || problems.length) { setPreview(null); return }
    const t = setTimeout(() => {
      groupsApi.preview(d.condition, d.asset_kinds).then((p) => { setPreview(p); setPreviewError(null) })
        .catch((e) => { setPreview(null); setPreviewError(apiDetail(e)) })
    }, 400)
    return () => clearTimeout(t)
  }, [d.condition, d.asset_kinds, d.kind, problems.length])
  const save = useMutation({
    mutationFn: async () => {
      const body: Record<string, unknown> = {
        name: d.name.trim(), description: d.description.trim() || null, parent_id: d.parent_id, asset_kinds: d.asset_kinds,
      }
      if (d.kind === 'dynamic') body.condition = d.condition
      if (group) return (await api.put(`/device-groups/${group.id}`, body)).data
      return (await api.post('/device-groups', { ...body, kind: d.kind })).data
    },
    onSuccess: (r: DeviceGroup) => {
      toast.success('Group saved', `${r.member_count} member(s)`)
      qc.invalidateQueries({ queryKey: ['groups-tree'] })
      qc.invalidateQueries({ queryKey: ['device-group', r.id] })
      qc.invalidateQueries({ queryKey: ['device-group-members', r.id] })
      if (group) onDone(); else nav(`/device-groups/${r.id}`, { replace: true })
    },
    onError: (e) => toast.error('Not saved', apiDetail(e)),
  })
  const parents = treeOrder((groups.data || []).filter((x) => x.id !== group?.id))
  const toggleKind = (k: AssetKind) => setD({ ...d, asset_kinds: d.asset_kinds.includes(k) ? d.asset_kinds.filter((x) => x !== k) : [...d.asset_kinds, k] })
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">{group ? `Edit ${group.name}` : 'New group'}</h1>
      <Card><CardContent className="grid gap-3 p-4 md:grid-cols-2">
        <FormField label="Name" required><Input value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} /></FormField>
        <FormField label="Inside group" hint="A group covers the members of its subgroups">
          <Select value={d.parent_id || '__none__'} onValueChange={(v) => setD({ ...d, parent_id: v === '__none__' ? null : v })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">Top level</SelectItem>
              {parents.map(({ group: p, depth }) => <SelectItem key={p.id} value={p.id}>{'  '.repeat(depth)}{p.name}</SelectItem>)}
            </SelectContent>
          </Select>
        </FormField>
        <FormField label="Kind" hint={group ? 'Cannot change after creation' : undefined}>
          <Select value={d.kind} disabled={!!group} onValueChange={(v) => setD({ ...d, kind: v as Draft['kind'] })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="static">Static: devices whose group it is, plus members added by hand</SelectItem>
              <SelectItem value="dynamic">Dynamic: everything that matches a condition</SelectItem>
            </SelectContent>
          </Select>
        </FormField>
        <FormField label="Holds">
          <div className="flex h-9 items-center gap-4 text-sm">
            {(['device', 'server'] as AssetKind[]).map((k) => (
              <label key={k} className="flex items-center gap-2"><input type="checkbox" checked={d.asset_kinds.includes(k)} onChange={() => toggleKind(k)} />{k}s</label>
            ))}
          </div>
        </FormField>
        <FormField label="Description" className="md:col-span-2"><Textarea rows={2} value={d.description} onChange={(e) => setD({ ...d, description: e.target.value })} /></FormField>
      </CardContent></Card>

      {d.kind === 'dynamic' && (
        <div className="grid gap-4 lg:grid-cols-3">
          <Card className="lg:col-span-2"><CardContent className="space-y-2 p-4">
            <div className="text-sm font-medium">Members are devices and servers that match</div>
            {catalog ? (
              <ConditionGroupEditor root={d.condition} node={d.condition} path={[]} catalog={catalog} options={options}
                                    onChange={(c) => setD({ ...d, condition: c })}
                                    emptyAllText="No conditions: the group holds every device (and server) it applies to." />
            ) : <Loader2 className="h-4 w-4 animate-spin" />}
            {problems.map((p) => <div key={p} className="text-xs text-danger">{p}</div>)}
            {catalog && <div className="text-xs text-muted">{summarizeCondition(d.condition, catalog, lookups)}</div>}
          </CardContent></Card>
          <Card><CardContent className="space-y-2 p-4 text-sm">
            <div className="font-medium">Matches now</div>
            {previewError && <div className="text-xs text-danger">{previewError}</div>}
            {!preview && !previewError && <div className="text-xs text-muted">Complete the condition to see who matches.</div>}
            {preview && (
              <>
                <div className="text-2xl font-semibold tabular-nums">{preview.count}<span className="text-sm font-normal text-muted"> of {preview.total_considered}</span></div>
                <div className="space-y-0.5">
                  {preview.sample.map((s) => <div key={s.id} className="truncate text-xs">{s.name} <span className="text-muted">{s.ip_address} {s.vendor || ''}</span></div>)}
                  {preview.count > preview.sample.length && <div className="text-xs text-muted">… and {preview.count - preview.sample.length} more</div>}
                </div>
              </>
            )}
          </CardContent></Card>
        </div>
      )}
      <div className="flex gap-2">
        <Button onClick={() => save.mutate()} disabled={!d.name.trim() || !d.asset_kinds.length || problems.length > 0 || save.isPending}>
          <Save className="h-4 w-4" /> Save</Button>
        <Button variant="outline" onClick={() => (group ? onDone() : nav('/device-groups'))}>Cancel</Button>
      </div>
    </div>
  )
}

function AddMembersDialog({ group, onClose }: { group: DeviceGroup; onClose: () => void }) {
  const qc = useQueryClient()
  const [kind, setKind] = useState<AssetKind>(group.asset_kinds[0])
  const [search, setSearch] = useState('')
  const [sel, setSel] = useState<Set<string>>(new Set())
  const list = useQuery({
    queryKey: ['group-add-search', kind, search],
    queryFn: async () => kind === 'device'
      ? ((await api.get('/devices', { params: { limit: 50, ...(search ? { search } : {}) } })).data.data as any[]).map((d) => ({ id: d.id, name: d.hostname, sub: d.ip_address }))
      : (((d: any) => (Array.isArray(d) ? d : d?.items ?? d?.data ?? []))((await api.get('/servers', { params: { page_size: 50, ...(search ? { q: search } : {}) } })).data))
          .map((s: any) => ({ id: s.id, name: s.display_name || s.hostname, sub: s.primary_ip })),
  })
  const add = useMutation({
    mutationFn: async () => (await api.post(`/device-groups/${group.id}/members`, { asset_kind: kind, asset_ids: [...sel] })).data,
    onSuccess: (r) => {
      toast.success(`${r.added} member(s) added`)
      qc.invalidateQueries({ queryKey: ['device-group-members', group.id] }); qc.invalidateQueries({ queryKey: ['device-group', group.id] }); onClose()
    },
    onError: (e) => toast.error('Failed', apiDetail(e)),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>Add members to {group.name}</DialogTitle></DialogHeader>
        {group.asset_kinds.length > 1 && (
          <Select value={kind} onValueChange={(v) => { setKind(v as AssetKind); setSel(new Set()) }}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="device">Devices</SelectItem><SelectItem value="server">Servers</SelectItem></SelectContent>
          </Select>
        )}
        <Input placeholder="Search" value={search} onChange={(e) => setSearch(e.target.value)} />
        <div className="max-h-64 overflow-y-auto rounded-md border border-border p-2 text-sm">
          {(list.data || []).map((x: any) => (
            <label key={x.id} className="flex items-center gap-2 py-0.5">
              <input type="checkbox" checked={sel.has(x.id)} onChange={(e) => { const n = new Set(sel); e.target.checked ? n.add(x.id) : n.delete(x.id); setSel(n) }} />
              {x.name} <span className="text-xs text-muted">{x.sub}</span>
            </label>
          ))}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button disabled={!sel.size || add.isPending} onClick={() => add.mutate()}>Add {sel.size || ''}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
