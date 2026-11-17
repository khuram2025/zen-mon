import { useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Boxes, FileUp, Pencil, Plus, Sparkles, Trash2 } from 'lucide-react'
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
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { summarizeCondition } from '@/pages/discovery/ruleModel'
import { type AssetKind, type CustomField, apiDetail, groupsApi, treeOrder, useGroupCatalog, useGroupLookups } from './api'

export const MANAGE_GROUPS = 'devices.manage_policies'

export function DeviceGroupsPage() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') || 'groups'
  const can = useCan()
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Boxes className="h-6 w-6 text-primary" /> Groups &amp; Fields
          </h1>
          <p className="mt-1 max-w-3xl text-sm text-muted">
            Groups scope alert rules and maintenance windows. A static group holds the devices whose group it is and
            anything added by hand; a dynamic group holds whatever matches its condition and updates itself. A group
            covers its subgroups. Custom fields add your own typed data to devices and servers.
          </p>
        </div>
        {can(MANAGE_GROUPS) && tab === 'groups' && (
          <Button asChild><Link to="/device-groups/new"><Plus className="h-4 w-4" /> New group</Link></Button>
        )}
      </div>
      <Tabs value={tab} onValueChange={(v) => setParams({ tab: v }, { replace: true })}>
        <TabsList>
          <TabsTrigger value="groups">Groups</TabsTrigger>
          <TabsTrigger value="fields">Custom fields</TabsTrigger>
        </TabsList>
        <TabsContent value="groups"><GroupList /></TabsContent>
        <TabsContent value="fields"><CustomFieldsTab canManage={can(MANAGE_GROUPS)} /></TabsContent>
      </Tabs>
    </div>
  )
}

function GroupList() {
  const q = useQuery({ queryKey: ['groups-tree'], queryFn: groupsApi.list })
  const { data: catalog } = useGroupCatalog()
  const { lookups } = useGroupLookups()
  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="groups" />
  const rows = treeOrder(q.data || [])
  return (
    <div className="overflow-hidden rounded-md border border-border">
      <Table>
        <THead className="bg-surface2/60">
          <Tr><Th>Group</Th><Th>Kind</Th><Th>Members</Th><Th className="text-right">Count</Th></Tr>
        </THead>
        <TBody>
          {rows.map(({ group: g, depth }) => (
            <Tr key={g.id}>
              <Td>
                <div style={{ paddingLeft: `${depth * 1.25}rem` }}>
                  <Link to={`/device-groups/${g.id}`} className="font-medium hover:text-primary">{g.name}</Link>
                  {g.description && <div className="text-xs text-muted">{g.description}</div>}
                </div>
              </Td>
              <Td>
                {g.kind === 'dynamic' ? <Badge variant="info"><Sparkles className="h-3 w-3" />Dynamic</Badge> : <Badge variant="outline">Static</Badge>}
                {g.asset_kinds.includes('server') && <Badge variant="outline" className="ml-1">{g.asset_kinds.length > 1 ? 'devices + servers' : 'servers'}</Badge>}
                {g.eval_error && <Badge variant="danger" className="ml-1" title={g.eval_error}>condition error</Badge>}
              </Td>
              <Td className="max-w-md text-xs text-muted">
                {g.kind === 'dynamic' && g.condition
                  ? (catalog ? summarizeCondition(g.condition, catalog, lookups) : '…')
                  : `${g.primary_count} by their group${g.static_count ? `, ${g.static_count} added` : ''}${g.child_count ? `, ${g.child_count} subgroup(s)` : ''}`}
              </Td>
              <Td className="text-right tabular-nums">{g.member_count}</Td>
            </Tr>
          ))}
          {!q.isLoading && rows.length === 0 && <Tr><Td colSpan={4} className="py-8 text-center text-sm text-muted">No groups yet.</Td></Tr>}
        </TBody>
      </Table>
    </div>
  )
}

const TYPE_LABEL: Record<string, string> = { text: 'Text', number: 'Number', date: 'Date', choice: 'Choice', boolean: 'Yes / no' }

function CustomFieldsTab({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['custom-fields'], queryFn: groupsApi.fields })
  const [editing, setEditing] = useState<Partial<CustomField> | null>(null)
  const [deleting, setDeleting] = useState<CustomField | null>(null)
  const [importing, setImporting] = useState(false)
  const del = useMutation({
    mutationFn: async (key: string) => api.delete(`/custom-fields/${key}`),
    onSuccess: () => { toast.success('Field deleted'); setDeleting(null); qc.invalidateQueries({ queryKey: ['custom-fields'] }) },
    onError: (e) => toast.error('Delete failed', apiDetail(e)),
  })
  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="custom fields" />
  const rows = q.data || []
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-3xl text-sm text-muted">
          Typed data of your own on devices and servers (building, department, contract end ...). A field can be a
          dynamic group condition (<code>custom.&lt;key&gt;</code>), a Devices list column, and an alert message
          placeholder <code>{'{custom.<key>}'}</code>. Values are set on each device's Configuration tab or imported
          from CSV.
        </p>
        {canManage && (
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => setImporting(true)}><FileUp className="h-4 w-4" /> Import CSV</Button>
            <Button size="sm" onClick={() => setEditing({ field_type: 'text', asset_kinds: ['device', 'server'], show_in_list: false })}>
              <Plus className="h-4 w-4" /> Add field</Button>
          </div>
        )}
      </div>
      <div className="overflow-hidden rounded-md border border-border">
        <Table>
          <THead className="bg-surface2/60"><Tr><Th>Field</Th><Th>Key</Th><Th>Type</Th><Th>Applies to</Th><Th>List column</Th><Th className="text-right">Values</Th><Th /></Tr></THead>
          <TBody>
            {rows.map((f) => (
              <Tr key={f.id}>
                <Td className="font-medium">{f.label}{f.description && <div className="text-xs font-normal text-muted">{f.description}</div>}</Td>
                <Td className="font-mono text-xs">{f.key}</Td>
                <Td className="text-sm">{TYPE_LABEL[f.field_type]}{f.choices ? <span className="text-xs text-muted"> ({f.choices.join(', ')})</span> : null}</Td>
                <Td className="text-sm">{f.asset_kinds.join(', ')}</Td>
                <Td className="text-sm">{f.show_in_list ? 'Yes' : '—'}</Td>
                <Td className="text-right tabular-nums">{f.value_count}</Td>
                <Td className="text-right">
                  {canManage && (
                    <span className="inline-flex gap-1">
                      <Button size="icon" variant="ghost" title="Edit" onClick={() => setEditing(f)}><Pencil className="h-4 w-4" /></Button>
                      <Button size="icon" variant="ghost" title="Delete" onClick={() => setDeleting(f)}><Trash2 className="h-4 w-4" /></Button>
                    </span>
                  )}
                </Td>
              </Tr>
            ))}
            {!q.isLoading && rows.length === 0 && <Tr><Td colSpan={7} className="py-8 text-center text-sm text-muted">No custom fields yet.</Td></Tr>}
          </TBody>
        </Table>
      </div>
      {editing && <FieldDialog initial={editing} onClose={() => setEditing(null)} />}
      {importing && <ImportDialog onClose={() => setImporting(false)} />}
      <ConfirmDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)} destructive confirmText="Delete"
                     title={`Delete ${deleting?.label}?`} loading={del.isPending}
                     description={`Its ${deleting?.value_count ?? 0} value(s) are deleted too. A field used by a dynamic group cannot be deleted.`}
                     onConfirm={() => { if (deleting) del.mutate(deleting.key) }} />
    </div>
  )
}

function FieldDialog({ initial, onClose }: { initial: Partial<CustomField>; onClose: () => void }) {
  const qc = useQueryClient()
  const isNew = !initial.id
  const [f, setF] = useState({
    key: initial.key || '', label: initial.label || '', field_type: initial.field_type || 'text',
    choices: (initial.choices || []).join(', '), description: initial.description || '',
    asset_kinds: (initial.asset_kinds || ['device', 'server']) as AssetKind[], show_in_list: !!initial.show_in_list,
    sort_order: initial.sort_order ?? 100,
  })
  const save = useMutation({
    mutationFn: async () => {
      const body: Record<string, unknown> = {
        label: f.label.trim(), description: f.description.trim() || null, asset_kinds: f.asset_kinds,
        show_in_list: f.show_in_list, sort_order: Number(f.sort_order),
      }
      if (f.field_type === 'choice') body.choices = f.choices.split(',').map((s) => s.trim()).filter(Boolean)
      return isNew ? api.post('/custom-fields', { ...body, key: f.key.trim(), field_type: f.field_type }) : api.put(`/custom-fields/${f.key}`, body)
    },
    onSuccess: () => { toast.success('Field saved'); qc.invalidateQueries({ queryKey: ['custom-fields'] }); qc.invalidateQueries({ queryKey: ['groups-tree', 'catalog'] }); onClose() },
    onError: (e) => toast.error('Save failed', apiDetail(e)),
  })
  const toggleKind = (k: AssetKind) => setF({ ...f, asset_kinds: f.asset_kinds.includes(k) ? f.asset_kinds.filter((x) => x !== k) : [...f.asset_kinds, k] })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>{isNew ? 'Add custom field' : `Edit ${initial.label}`}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <FormField label="Label" required><Input value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} /></FormField>
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Key" hint="Cannot change later"><Input className="font-mono text-xs" value={f.key} disabled={!isNew} onChange={(e) => setF({ ...f, key: e.target.value })} /></FormField>
            <FormField label="Type" hint="Cannot change later">
              <Select value={f.field_type} disabled={!isNew} onValueChange={(v) => setF({ ...f, field_type: v as CustomField['field_type'] })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>{Object.entries(TYPE_LABEL).map(([k, l]) => <SelectItem key={k} value={k}>{l}</SelectItem>)}</SelectContent>
              </Select>
            </FormField>
          </div>
          {f.field_type === 'choice' && (
            <FormField label="Choices" hint="Comma separated; choices in use cannot be removed">
              <Input value={f.choices} onChange={(e) => setF({ ...f, choices: e.target.value })} placeholder="Finance, Retail, Operations" />
            </FormField>
          )}
          <FormField label="Applies to">
            <div className="flex gap-4 text-sm">
              {(['device', 'server'] as AssetKind[]).map((k) => (
                <label key={k} className="flex items-center gap-2"><input type="checkbox" checked={f.asset_kinds.includes(k)} onChange={() => toggleKind(k)} />{k}s</label>
              ))}
            </div>
          </FormField>
          <label className="flex items-center gap-2 text-sm"><Switch checked={f.show_in_list} onCheckedChange={(v) => setF({ ...f, show_in_list: v })} /> Show as a Devices list column</label>
          <FormField label="Description"><Textarea rows={2} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></FormField>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button disabled={!f.label.trim() || (isNew && !/^[a-z][a-z0-9_]{0,49}$/.test(f.key)) || !f.asset_kinds.length || save.isPending} onClick={() => save.mutate()}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ImportDialog({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const fileRef = useRef<HTMLInputElement>(null)
  const [csv, setCsv] = useState('')
  const [plan, setPlan] = useState<any>(null)
  const run = useMutation({
    mutationFn: async (apply: boolean) => (await api.post('/custom-fields/import', { csv, apply })).data,
    onSuccess: (r) => {
      setPlan(r)
      if (r.applied) {
        toast.success(`${r.rows.length} asset(s) updated`)
        qc.invalidateQueries({ queryKey: ['custom-fields'] })
      }
    },
    onError: (e) => toast.error('Import failed', apiDetail(e)),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader><DialogTitle>Import custom field values</DialogTitle></DialogHeader>
        <p className="text-sm text-muted">
          First column <code>ip_address</code> or <code>hostname</code> (devices) or <code>server</code> (server name),
          then one column per field key. A blank cell leaves that value as it is. Check first, then apply.
        </p>
        <input ref={fileRef} type="file" accept=".csv,text/csv" className="hidden"
               onChange={async (e) => { const f = e.target.files?.[0]; e.target.value = ''; if (f) { setCsv(await f.text()); setPlan(null) } }} />
        <div className="flex gap-2"><Button variant="outline" size="sm" onClick={() => fileRef.current?.click()}><FileUp className="h-4 w-4" /> Choose file</Button></div>
        <Textarea rows={8} className="font-mono text-xs" value={csv} placeholder={'ip_address,department,rack_u\n10.51.1.1,Finance,12'}
                  onChange={(e) => { setCsv(e.target.value); setPlan(null) }} />
        {plan && (
          <div className="space-y-1 text-sm">
            <div>{plan.applied ? 'Applied' : 'Would update'} {plan.rows.length} asset(s); {plan.problems.length} row(s) skipped.</div>
            {plan.problems.slice(0, 10).map((p: any) => <div key={p.row} className="text-xs text-danger">Row {p.row}: {p.error}</div>)}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Close</Button>
          <Button variant="outline" disabled={!csv.trim() || run.isPending} onClick={() => run.mutate(false)}>Check</Button>
          <Button disabled={!plan || plan.applied || !plan.rows.length || run.isPending} onClick={() => run.mutate(true)}>Apply</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
