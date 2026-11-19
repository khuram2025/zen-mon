import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, GitBranch, Plus, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { useCan } from '@/stores/auth'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { QueryFailure } from '@/components/ui/QueryState'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { StatusDot, deviceStatusKind } from '@/components/ui/StatusDot'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { toast } from '@/components/ui/Toast'
import { apiDetail } from '@/pages/groups/api'
import {
  type DepEnd, type Dependency, type SuggestionResult, DEPENDENCY_TYPES, SOURCE_BADGE, acceptBody, byParent,
  endLabel, filterDependencies, holdsAlerts, ownerNote, rootReasonText, viaText,
} from './dependenciesModel'

export const MANAGE_DEPENDENCIES = 'devices.manage'

const depsApi = {
  list: async (): Promise<Dependency[]> => (await api.get('/topology-dependencies')).data.data,
  suggestions: async (root?: string): Promise<SuggestionResult> =>
    (await api.get('/topology-dependencies/suggestions', { params: root ? { root_device_id: root } : {} })).data,
}

function endStatus(e: DepEnd) {
  const s = e.status || 'unknown'
  if (e.kind === 'server') return s === 'healthy' ? 'up' : s === 'critical' ? 'down' : s === 'warning' ? 'warn' : 'idle'
  return deviceStatusKind(s)
}

function EndCell({ end }: { end: DepEnd }) {
  const to = end.kind === 'server' ? `/servers/${end.id}` : `/devices/${end.id}`
  return (
    <span className="inline-flex items-center gap-2">
      <StatusDot status={endStatus(end)} />
      <Link to={to} className="font-medium hover:text-primary">{endLabel(end)}</Link>
      {end.kind === 'server' && <Badge variant="outline">server</Badge>}
    </span>
  )
}

export function DependenciesPage() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') || 'list'
  const can = useCan()
  const canManage = can(MANAGE_DEPENDENCIES)
  const [adding, setAdding] = useState(false)
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <GitBranch className="h-6 w-6 text-primary" /> Dependencies
          </h1>
          <p className="mt-1 max-w-3xl text-sm text-muted">
            A dependency says a device or server sits behind a parent device. While the parent is down, degraded or
            unknown, the child's down alerts are recorded as held instead of paging, so one outage sends one alert.
            Rows written by a discovery rule or the controller sync are shown here but stay theirs.
          </p>
        </div>
        {canManage && tab === 'list' && (
          <Button onClick={() => setAdding(true)}><Plus className="h-4 w-4" /> Add dependency</Button>
        )}
      </div>
      <Tabs value={tab} onValueChange={(v) => setParams({ tab: v }, { replace: true })}>
        <TabsList>
          <TabsTrigger value="list">Dependencies</TabsTrigger>
          <TabsTrigger value="suggestions">Suggested from topology</TabsTrigger>
        </TabsList>
        <TabsContent value="list"><DependencyList canManage={canManage} /></TabsContent>
        <TabsContent value="suggestions"><Suggestions canManage={canManage} /></TabsContent>
      </Tabs>
      {adding && <AddDependencyDialog onClose={() => setAdding(false)} />}
    </div>
  )
}

function DependencyList({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['topology-dependencies'], queryFn: depsApi.list })
  const [search, setSearch] = useState('')
  const [removing, setRemoving] = useState<Dependency | null>(null)
  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/topology-dependencies/${id}`),
    onSuccess: () => {
      toast.success('Dependency removed'); setRemoving(null)
      qc.invalidateQueries({ queryKey: ['topology-dependencies'] }); qc.invalidateQueries({ queryKey: ['dependency-suggestions'] })
    },
    onError: (e) => toast.error('Remove failed', apiDetail(e)),
  })
  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="dependencies" />
  const groups = byParent(filterDependencies(q.data || [], search))
  return (
    <div className="space-y-3">
      <Input className="max-w-sm" placeholder="Filter by name or address" value={search} onChange={(e) => setSearch(e.target.value)} />
      <div className="overflow-hidden rounded-md border border-border">
        <Table>
          <THead className="bg-surface2/60">
            <Tr><Th>Parent</Th><Th>Child</Th><Th>Type</Th><Th>Owner</Th><Th>Holds alerts</Th><Th /></Tr>
          </THead>
          <TBody>
            {groups.flatMap((g) => g.children.map((d, i) => (
              <Tr key={d.id}>
                <Td>{i === 0 ? <EndCell end={g.parent} /> : null}</Td>
                <Td>
                  <EndCell end={d.child} />
                  {d.notes && <div className="text-xs text-muted">{d.notes}</div>}
                </Td>
                <Td className="text-sm">{d.dependency_type}</Td>
                <Td><Badge variant={SOURCE_BADGE[d.source]?.variant ?? 'outline'} title={ownerNote(d) ?? undefined}>{SOURCE_BADGE[d.source]?.label ?? d.source_label}</Badge></Td>
                <Td className="text-sm">{holdsAlerts(d) ? 'Yes' : <span className="text-muted">No (turned off)</span>}</Td>
                <Td className="text-right">
                  {canManage && d.removable && (
                    <Button variant="ghost" size="sm" aria-label="Remove dependency" onClick={() => setRemoving(d)}><Trash2 className="h-4 w-4" /></Button>
                  )}
                </Td>
              </Tr>
            )))}
            {!q.isLoading && groups.length === 0 && (
              <Tr><Td colSpan={6} className="py-8 text-center text-sm text-muted">
                {search ? 'No dependency matches the filter.' : 'No dependencies yet. Add one, or accept the suggestions from the topology.'}
              </Td></Tr>
            )}
          </TBody>
        </Table>
      </div>
      <ConfirmDialog
        open={!!removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        title="Remove dependency"
        description={removing ? `${endLabel(removing.child)} will no longer be held while ${endLabel(removing.parent)} is down: its own down alerts will page.` : ''}
        confirmText="Remove"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (removing) del.mutate(removing.id) }}
      />
    </div>
  )
}

function Suggestions({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const [root, setRoot] = useState<string>('')
  const q = useQuery({ queryKey: ['dependency-suggestions', root], queryFn: () => depsApi.suggestions(root || undefined) })
  const accept = useMutation({
    mutationFn: async (body: ReturnType<typeof acceptBody>) => (await api.post('/topology-dependencies', body)).data,
    onSuccess: (d: Dependency) => {
      toast.success(`${endLabel(d.child)} now depends on ${endLabel(d.parent)}`)
      qc.invalidateQueries({ queryKey: ['topology-dependencies'] }); qc.invalidateQueries({ queryKey: ['dependency-suggestions'] })
    },
    onError: (e) => toast.error('Could not add', apiDetail(e)),
  })
  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="suggestions" />
  const r = q.data
  return (
    <div className="space-y-3">
      <p className="max-w-3xl text-sm text-muted">
        Worked out from current LLDP/CDP neighbours only (seen in the last 24 hours, both ports up). The device
        nearest this appliance is the root; every other device's parent is its neighbour one hop closer. Nothing is
        added until you accept it.
      </p>
      {r && (
        <div className="space-y-1 text-sm">
          {r.roots.map((x) => (
            <div key={x.id}>Root: <span className="font-medium">{endLabel(x)}</span> <span className="text-muted">({rootReasonText(x.reason)})</span></div>
          ))}
          {r.unrooted_components > 0 && (
            <div className="text-muted">
              {r.unrooted_components} part(s) of the topology have no device at this appliance's gateway and no ping round-trip time: pick a starting device to see them.
            </div>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <FormField label="Start from">
          <RootPicker value={root} onChange={setRoot} />
        </FormField>
        {root && <Button variant="outline" size="sm" onClick={() => setRoot('')}>Use the nearest device</Button>}
      </div>
      <div className="overflow-hidden rounded-md border border-border">
        <Table>
          <THead className="bg-surface2/60"><Tr><Th>Child</Th><Th>Depends on</Th><Th>Link</Th><Th className="text-right">Hops from root</Th><Th /></Tr></THead>
          <TBody>
            {(r?.suggestions || []).map((s) => (
              <Tr key={`${s.parent.id}-${s.child.id}`}>
                <Td><EndCell end={s.child} /></Td>
                <Td><EndCell end={s.parent} /></Td>
                <Td className="text-xs text-muted">{viaText(s)}</Td>
                <Td className="text-right tabular-nums">{s.hops}</Td>
                <Td className="text-right">
                  {canManage && (
                    <Button size="sm" variant="outline" disabled={accept.isPending} onClick={() => accept.mutate(acceptBody(s))}>
                      <Check className="h-4 w-4" /> Accept</Button>
                  )}
                </Td>
              </Tr>
            ))}
            {!q.isLoading && !(r?.suggestions || []).length && (
              <Tr><Td colSpan={5} className="py-8 text-center text-sm text-muted">No new suggestions.</Td></Tr>
            )}
          </TBody>
        </Table>
      </div>
      {!!r?.redundant.length && (
        <div className="rounded-md border border-border p-3 text-sm">
          <div className="font-medium">Not suggested: redundant uplinks</div>
          <p className="text-xs text-muted">These devices reach the root through more than one neighbour, so one parent going down does not cut them off. Add a dependency by hand if one path matters.</p>
          <ul className="mt-2 space-y-0.5">
            {r.redundant.map((x) => (
              <li key={x.child.id}>{endLabel(x.child)} <span className="text-muted">via {x.parents.map(endLabel).join(' or ')}</span></li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function useAssetSearch(kind: 'device' | 'server', search: string) {
  return useQuery({
    queryKey: ['dependency-asset-search', kind, search],
    queryFn: async () => kind === 'device'
      ? ((await api.get('/devices', { params: { limit: 50, ...(search ? { search } : {}) } })).data.data as any[])
          .map((d) => ({ id: String(d.id), label: endLabel({ name: d.hostname, ip: d.ip_address }) }))
      : (((d: any) => (Array.isArray(d) ? d : d?.items ?? d?.data ?? []))((await api.get('/servers', { params: { page_size: 50, ...(search ? { q: search } : {}) } })).data) as any[])
          .map((s) => ({ id: String(s.id), label: endLabel({ name: s.display_name || s.hostname, ip: s.primary_ip }) })),
  })
}

function AssetPicker({ kind, value, onChange, placeholder }: {
  kind: 'device' | 'server'; value: string; onChange: (id: string) => void; placeholder: string
}) {
  const [search, setSearch] = useState('')
  const list = useAssetSearch(kind, search)
  return (
    <div className="space-y-1">
      <Input placeholder={`Search ${kind === 'device' ? 'devices' : 'servers'}`} value={search} onChange={(e) => setSearch(e.target.value)} />
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger><SelectValue placeholder={placeholder} /></SelectTrigger>
        <SelectContent>
          {(list.data || []).map((x) => <SelectItem key={x.id} value={x.id}>{x.label}</SelectItem>)}
        </SelectContent>
      </Select>
    </div>
  )
}

function RootPicker({ value, onChange }: { value: string; onChange: (id: string) => void }) {
  return <div className="w-72"><AssetPicker kind="device" value={value} onChange={onChange} placeholder="The device nearest this appliance" /></div>
}

function AddDependencyDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const [parent, setParent] = useState('')
  const [childKind, setChildKind] = useState<'device' | 'server'>('device')
  const [child, setChild] = useState('')
  const [type, setType] = useState<string>('uplink')
  const [notes, setNotes] = useState('')
  const add = useMutation({
    mutationFn: async () => (await api.post('/topology-dependencies', {
      parent_device_id: parent,
      ...(childKind === 'device' ? { child_device_id: child } : { child_server_id: child }),
      dependency_type: type, notes: notes.trim() || null, source: 'manual',
    })).data,
    onSuccess: (d: Dependency) => {
      toast.success(`${endLabel(d.child)} now depends on ${endLabel(d.parent)}`)
      qc.invalidateQueries({ queryKey: ['topology-dependencies'] }); qc.invalidateQueries({ queryKey: ['dependency-suggestions'] }); onClose()
    },
    onError: (e) => toast.error('Could not add', apiDetail(e)),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>Add dependency</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <FormField label="Parent device (the one in front)">
            <AssetPicker kind="device" value={parent} onChange={setParent} placeholder="Choose the parent" />
          </FormField>
          <FormField label="Child (the one behind it)">
            <div className="space-y-1">
              <Select value={childKind} onValueChange={(v) => { setChildKind(v as 'device' | 'server'); setChild('') }}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent><SelectItem value="device">Device</SelectItem><SelectItem value="server">Server</SelectItem></SelectContent>
              </Select>
              <AssetPicker key={childKind} kind={childKind} value={child} onChange={setChild} placeholder="Choose the child" />
            </div>
          </FormField>
          <FormField label="Type">
            <Select value={type} onValueChange={setType}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>{DEPENDENCY_TYPES.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}</SelectContent>
            </Select>
          </FormField>
          <FormField label="Note (optional)">
            <Input value={notes} maxLength={500} onChange={(e) => setNotes(e.target.value)} placeholder="e.g. rack 4, port 12" />
          </FormField>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button disabled={!parent || !child || (childKind === 'device' && parent === child) || add.isPending} onClick={() => add.mutate()}>Add</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
