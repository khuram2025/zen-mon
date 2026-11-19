import { useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileJson, GitBranch, LayoutTemplate, Lock, Plus, Search, Trash2, Pencil } from 'lucide-react'
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { toast } from '@/components/ui/Toast'
import {
  DEVICE_TYPE_CATEGORIES, type DeviceTypeInfo, deviceTypeLabel, useDeviceTypes,
} from '@/hooks/useDeviceTypes'
import { iconKeys } from '@/components/network-icons'
import { P, type Template, templateError, templatesApi } from './api'

const MANAGE = 'devices.manage_templates'

function metricCount(t: Template) {
  return (t.oid_groups || []).reduce((n, g) => n + (g.metrics?.length || 0), 0)
}

export function TemplateStateBadges({ t }: { t: Pick<Template, 'builtin' | 'parent_name' | 'has_draft' | 'pilot_device_count'> }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {t.builtin && <Badge variant="info"><Lock className="h-3 w-3" />Built-in</Badge>}
      {t.parent_name && <Badge variant="outline"><GitBranch className="h-3 w-3" />Customised from {t.parent_name}</Badge>}
      {t.has_draft && <Badge variant="warning">Draft</Badge>}
      {t.pilot_device_count > 0 && <Badge variant="warning">Pilot on {t.pilot_device_count}</Badge>}
    </span>
  )
}

export function DeviceTemplatesPage() {
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') || 'templates'
  const can = useCan()
  const canManage = can(MANAGE)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <LayoutTemplate className="h-6 w-6 text-primary" /> Device Templates
          </h1>
          <p className="mt-1 max-w-3xl text-sm text-muted">
            What each kind of device reports and how it is read. A device gets a template when it is classified
            (sysObjectID / sysDescr), by a discovery rule or by hand. Changes are saved as a draft, tested on a
            device and published to a pilot or to every device; every published revision can be rolled back.
          </p>
        </div>
        {canManage && tab === 'templates' && <TemplateCreateActions />}
      </div>

      <Tabs value={tab} onValueChange={(v) => setParams({ tab: v }, { replace: true })}>
        <TabsList>
          <TabsTrigger value="templates">Templates</TabsTrigger>
          <TabsTrigger value="unmatched">Unmatched devices</TabsTrigger>
          <TabsTrigger value="suggestions">Suggestions</TabsTrigger>
          <TabsTrigger value="types">Device types</TabsTrigger>
        </TabsList>
        <TabsContent value="templates"><TemplateList /></TabsContent>
        <TabsContent value="unmatched"><UnmatchedList /></TabsContent>
        <TabsContent value="suggestions"><SuggestionList /></TabsContent>
        <TabsContent value="types"><DeviceTypeList canManage={canManage} /></TabsContent>
      </Tabs>
    </div>
  )
}

function TemplateList() {
  const [search, setSearch] = useState('')
  const { types } = useDeviceTypes()
  const q = useQuery({ queryKey: ['device-templates'], queryFn: templatesApi.list })
  const rows = useMemo(() => {
    const s = search.trim().toLowerCase()
    return (q.data || []).filter((t) => !s || `${t.name} ${t.vendor || ''} ${t.device_type || ''}
      ${(t.match_rules?.sys_object_id_prefixes || []).join(' ')}`.toLowerCase().includes(s))
  }, [q.data, search])

  if (q.isLoading) return <div className="grid gap-3 md:grid-cols-3"><SkeletonCard /><SkeletonCard /><SkeletonCard /></div>
  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="templates" />
  const total = q.data || []
  const devices = total.reduce((n, t) => n + t.device_count, 0)

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-4">
        <Kpi label="Templates" value={total.length} />
        <Kpi label="Built-in" value={total.filter((t) => t.builtin).length} />
        <Kpi label="Devices on a template" value={devices} />
        <Kpi label="Pending drafts" value={total.filter((t) => t.has_draft).length} />
      </div>
      <div className="relative max-w-sm">
        <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted" />
        <Input className="pl-8" placeholder="Search name, vendor, type, OID…" value={search}
               onChange={(e) => setSearch(e.target.value)} />
      </div>
      <div className="overflow-hidden rounded-md border border-border">
        <Table>
          <THead className="bg-surface2/60">
            <Tr><Th>Template</Th><Th>Device type</Th><Th className="text-right">Devices</Th>
              <Th className="text-right">Groups / metrics</Th><Th className="text-right">Priority</Th>
              <Th className="text-right">Revision</Th></Tr>
          </THead>
          <TBody>
            {rows.map((t) => (
              <Tr key={t.id}>
                <Td>
                  <Link to={`/device-templates/${t.id}`} className="font-medium text-text hover:text-primary">{t.name}</Link>
                  <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted">
                    {t.vendor && <span>{t.vendor}</span>}
                    <TemplateStateBadges t={t} />
                    {t.suggestion_count > 0 && <Badge variant="outline">{t.suggestion_count} suggestion(s)</Badge>}
                  </div>
                </Td>
                <Td className="text-sm">{deviceTypeLabel(types, t.device_type)}</Td>
                <Td className="text-right tabular-nums">{t.device_count}</Td>
                <Td className="text-right tabular-nums">{t.oid_groups.length} / {metricCount(t)}</Td>
                <Td className="text-right tabular-nums">{t.match_priority}</Td>
                <Td className="text-right tabular-nums">{t.revision}</Td>
              </Tr>
            ))}
            {rows.length === 0 && (
              <Tr><Td colSpan={6} className="py-8 text-center text-sm text-muted">No templates match.</Td></Tr>
            )}
          </TBody>
        </Table>
      </div>
    </div>
  )
}

function Kpi({ label, value }: { label: string; value: number }) {
  return (
    <Card><CardContent className="p-4">
      <div className="text-2xl font-semibold tabular-nums">{value}</div>
      <div className="text-xs text-muted">{label}</div>
    </CardContent></Card>
  )
}

function TemplateCreateActions() {
  const nav = useNavigate()
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const importMut = useMutation({
    mutationFn: async (bundle: unknown) => (await api.post(`${P}/import`, bundle)).data as Template,
    onSuccess: (t) => {
      toast.success('Template imported', t.name)
      qc.invalidateQueries({ queryKey: ['device-templates'] })
      nav(`/device-templates/${t.id}`)
    },
    onError: (e) => toast.error('Import failed', templateError(e)),
  })
  return (
    <div className="flex gap-2">
      <input ref={fileRef} type="file" accept="application/json,.json" className="hidden"
             onChange={async (e) => {
               const f = e.target.files?.[0]
               e.target.value = ''
               if (!f) return
               try { importMut.mutate(JSON.parse(await f.text())) } catch { toast.error('Import failed', 'Not a JSON file') }
             }} />
      <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={importMut.isPending}>
        <FileJson className="h-4 w-4" /> Import JSON
      </Button>
      <Button onClick={() => setOpen(true)}><Plus className="h-4 w-4" /> New template</Button>
      <NewTemplateDialog open={open} onOpenChange={setOpen} />
    </div>
  )
}

export function NewTemplateDialog({ open, onOpenChange, prefill }: {
  open: boolean; onOpenChange: (o: boolean) => void
  prefill?: { name?: string; vendor?: string; prefix?: string; descr?: string }
}) {
  const nav = useNavigate()
  const qc = useQueryClient()
  const { types } = useDeviceTypes()
  const [name, setName] = useState(prefill?.name || '')
  const [vendor, setVendor] = useState(prefill?.vendor || '')
  const [type, setType] = useState('__none__')
  const [prefix, setPrefix] = useState(prefill?.prefix || '')
  const create = useMutation({
    mutationFn: async () => (await api.post(P, {
      name: name.trim(), vendor: vendor.trim() || null, device_type: type === '__none__' ? null : type,
      match_rules: { sys_object_id_prefixes: prefix.split(/[\s,]+/).filter(Boolean),
                     sys_descr_regex: prefill?.descr || null },
      oid_groups: [],
    })).data as Template,
    onSuccess: (t) => {
      qc.invalidateQueries({ queryKey: ['device-templates'] })
      onOpenChange(false)
      nav(`/device-templates/${t.id}?tab=metrics`)
    },
    onError: (e) => toast.error('Create failed', templateError(e)),
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>New device template</DialogTitle></DialogHeader>
        <p className="text-sm text-muted">
          For a vendor ZenPlus already covers, open its built-in template and use <b>Customise</b> instead: your
          changes then keep receiving the vendor updates that ship with releases.
        </p>
        <div className="space-y-3">
          <FormField label="Name" required><Input value={name} onChange={(e) => setName(e.target.value)} /></FormField>
          <FormField label="Vendor"><Input value={vendor} onChange={(e) => setVendor(e.target.value)} /></FormField>
          <FormField label="Device type" hint="Set on devices that are classified to this template while their type is still Other.">
            <Select value={type} onValueChange={setType}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">Not set</SelectItem>
                {types.map((t) => <SelectItem key={t.key} value={t.key}>{t.label}</SelectItem>)}
              </SelectContent>
            </Select>
          </FormField>
          <FormField label="sysObjectID prefixes" hint="One or more, separated by spaces. A prefix matches on a dot boundary.">
            <Input value={prefix} onChange={(e) => setPrefix(e.target.value)} placeholder="1.3.6.1.4.1.12356.101" />
          </FormField>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => create.mutate()} disabled={!name.trim() || create.isPending}>Create and edit</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function enterprisePrefix(oid: string | null | undefined): string {
  const parts = (oid || '').replace(/^\./, '').split('.')
  const i = parts.indexOf('1') === 0 ? 7 : parts.length
  return parts.slice(0, Math.min(parts.length, i)).join('.')
}

function UnmatchedList() {
  const qc = useQueryClient()
  const can = useCan()
  const q = useQuery({ queryKey: ['device-templates', 'unmatched'], queryFn: templatesApi.unmatched })
  const [prefill, setPrefill] = useState<{ name: string; vendor: string; prefix: string } | null>(null)
  const assign = useMutation({
    mutationFn: async ({ tpl, id }: { tpl: string; id: string }) =>
      (await api.post(`${P}/${tpl}/assign`, { device_ids: [id] })).data,
    onSuccess: () => {
      toast.success('Template assigned')
      qc.invalidateQueries({ queryKey: ['device-templates'] })
    },
    onError: (e) => toast.error('Assign failed', templateError(e)),
  })
  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="unmatched devices" />
  const rows = q.data || []
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">
        SNMP devices no template matched. They still get standard monitoring (availability, interfaces, system
        and ENTITY data); a template adds the vendor-specific readings.
      </p>
      <div className="overflow-hidden rounded-md border border-border">
        <Table>
          <THead className="bg-surface2/60"><Tr><Th>Device</Th><Th>sysObjectID</Th><Th>sysDescr</Th><Th>Would match now</Th><Th /></Tr></THead>
          <TBody>
            {rows.map((d) => (
              <Tr key={d.id}>
                <Td><Link className="font-medium hover:text-primary" to={`/devices/${d.id}`}>{d.hostname}</Link>
                  <div className="text-xs text-muted">{d.ip}</div></Td>
                <Td className="font-mono text-xs">{d.sys_object_id || '—'}</Td>
                <Td className="max-w-md truncate text-xs text-muted" title={d.sys_descr || ''}>{d.sys_descr || '—'}</Td>
                <Td className="text-sm">{d.would_match?.name || <span className="text-muted">nothing</span>}</Td>
                <Td className="text-right">
                  {d.would_match && can('devices.manage') && (
                    <Button size="sm" variant="outline" disabled={assign.isPending}
                            onClick={() => assign.mutate({ tpl: d.would_match.id, id: d.id })}>Assign</Button>
                  )}
                  {!d.would_match && can(MANAGE) && d.sys_object_id && (
                    <Button size="sm" variant="outline" onClick={() => setPrefill({
                      name: `${d.vendor || 'New'} ${d.model || 'device'}`.trim(), vendor: d.vendor || '',
                      prefix: enterprisePrefix(d.sys_object_id),
                    })}>New template from this</Button>
                  )}
                </Td>
              </Tr>
            ))}
            {!q.isLoading && rows.length === 0 && (
              <Tr><Td colSpan={5} className="py-8 text-center text-sm text-muted">Every SNMP device has a template.</Td></Tr>
            )}
          </TBody>
        </Table>
      </div>
      {prefill && <NewTemplateDialog open onOpenChange={(o) => !o && setPrefill(null)} prefill={prefill} />}
    </div>
  )
}

function SuggestionList() {
  const qc = useQueryClient()
  const can = useCan()
  const q = useQuery({ queryKey: ['device-templates', 'suggestions'], queryFn: templatesApi.suggestions })
  const [sel, setSel] = useState<Set<string>>(new Set())
  const accept = useMutation({
    mutationFn: async () => (await api.post(`${P}/suggestions/accept`, { device_ids: [...sel] })).data,
    onSuccess: (r) => {
      toast.success(`${r.moved} device(s) moved`)
      setSel(new Set())
      qc.invalidateQueries({ queryKey: ['device-templates'] })
    },
    onError: (e) => toast.error('Failed', templateError(e)),
  })
  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="suggestions" />
  const rows = q.data || []
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted">
          A different template now matches these devices (after a firmware or hardware change, or a new template).
          Nothing changes until you accept.
        </p>
        {can('devices.manage') && (
          <Button size="sm" disabled={!sel.size || accept.isPending} onClick={() => accept.mutate()}>
            Move {sel.size || ''} to suggested
          </Button>
        )}
      </div>
      <div className="overflow-hidden rounded-md border border-border">
        <Table>
          <THead className="bg-surface2/60"><Tr><Th className="w-8" /><Th>Device</Th><Th>Current template</Th><Th>Suggested</Th><Th>Assigned by</Th><Th>Since</Th></Tr></THead>
          <TBody>
            {rows.map((d) => (
              <Tr key={d.id}>
                <Td><input type="checkbox" checked={sel.has(d.id)} onChange={(e) => {
                  const n = new Set(sel); e.target.checked ? n.add(d.id) : n.delete(d.id); setSel(n)
                }} /></Td>
                <Td><Link className="font-medium hover:text-primary" to={`/devices/${d.id}`}>{d.hostname}</Link>
                  <div className="text-xs text-muted">{d.ip}</div></Td>
                <Td className="text-sm">{d.current_name || '—'}</Td>
                <Td className="text-sm"><Link className="hover:text-primary" to={`/device-templates/${d.suggested_id}`}>{d.suggested_name}</Link></Td>
                <Td className="text-xs text-muted">{sourceLabel(d.profile_source)}</Td>
                <Td className="text-xs text-muted">{d.profile_suggested_at ? new Date(d.profile_suggested_at).toLocaleString() : '—'}</Td>
              </Tr>
            ))}
            {!q.isLoading && rows.length === 0 && (
              <Tr><Td colSpan={6} className="py-8 text-center text-sm text-muted">No suggestions.</Td></Tr>
            )}
          </TBody>
        </Table>
      </div>
    </div>
  )
}

export function sourceLabel(s: string | null | undefined): string {
  switch (s) {
    case 'auto': return 'Classifier'
    case 'manual': return 'Operator'
    case 'rule': return 'Discovery rule'
    case 'import': return 'Import'
    default: return 'Before 1.25.10'
  }
}

function DeviceTypeList({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const { types, isError, error, refetch } = useDeviceTypes()
  const [editing, setEditing] = useState<Partial<DeviceTypeInfo> | null>(null)
  const [deleting, setDeleting] = useState<DeviceTypeInfo | null>(null)
  const del = useMutation({
    mutationFn: async (key: string) => api.delete(`/device-types/${key}`),
    onSuccess: () => { toast.success('Device type deleted'); qc.invalidateQueries({ queryKey: ['device-types'] }); setDeleting(null) },
    onError: (e) => toast.error('Delete failed', templateError(e)),
  })
  if (isError) return <QueryFailure error={error} onRetry={() => refetch()} what="device types" />
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted">
          The types a device can have. The category groups types in charts; built-in types can be relabelled but
          not deleted, and a type in use cannot be deleted.
        </p>
        {canManage && <Button size="sm" onClick={() => setEditing({ category: 'other', icon: 'other', sort_order: 500 })}>
          <Plus className="h-4 w-4" /> Add type</Button>}
      </div>
      <div className="overflow-hidden rounded-md border border-border">
        <Table>
          <THead className="bg-surface2/60"><Tr><Th>Type</Th><Th>Key</Th><Th>Category</Th><Th>Icon</Th>
            <Th className="text-right">Devices</Th><Th className="text-right">Templates</Th><Th /></Tr></THead>
          <TBody>
            {types.map((t) => (
              <Tr key={t.key}>
                <Td className="font-medium">{t.label} {t.builtin && <Badge variant="outline" className="ml-1">Built-in</Badge>}</Td>
                <Td className="font-mono text-xs">{t.key}</Td>
                <Td className="text-sm capitalize">{t.category}</Td>
                <Td className="text-xs text-muted">{t.icon}</Td>
                <Td className="text-right tabular-nums">{t.device_count}</Td>
                <Td className="text-right tabular-nums">{t.template_count}</Td>
                <Td className="text-right">
                  {canManage && (
                    <span className="inline-flex gap-1">
                      <Button size="icon" variant="ghost" title="Edit" onClick={() => setEditing(t)}><Pencil className="h-4 w-4" /></Button>
                      {!t.builtin && <Button size="icon" variant="ghost" title="Delete" onClick={() => setDeleting(t)}><Trash2 className="h-4 w-4" /></Button>}
                    </span>
                  )}
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
      {editing && <DeviceTypeDialog initial={editing} onClose={() => setEditing(null)}
                                    onSaved={() => qc.invalidateQueries({ queryKey: ['device-types'] })} />}
      <ConfirmDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)} destructive
                     title={`Delete device type ${deleting?.label}?`} confirmText="Delete" loading={del.isPending}
                     description="Only possible while no device or template uses it."
                     onConfirm={() => { if (deleting) del.mutate(deleting.key) }} />
    </div>
  )
}

function DeviceTypeDialog({ initial, onClose, onSaved }: {
  initial: Partial<DeviceTypeInfo>; onClose: () => void; onSaved: () => void
}) {
  const isNew = !initial.key || initial.device_count === undefined
  const [form, setForm] = useState({
    key: initial.key || '', label: initial.label || '', category: initial.category || 'other',
    icon: initial.icon || 'other', sort_order: initial.sort_order ?? 500, description: initial.description || '',
  })
  const save = useMutation({
    mutationFn: async () => {
      const body = { label: form.label.trim(), category: form.category, icon: form.icon,
                     sort_order: Number(form.sort_order), description: form.description.trim() || null }
      return isNew ? api.post('/device-types', { key: form.key.trim(), ...body }) : api.put(`/device-types/${form.key}`, body)
    },
    onSuccess: () => { toast.success('Device type saved'); onSaved(); onClose() },
    onError: (e) => toast.error('Save failed', templateError(e)),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>{isNew ? 'Add device type' : `Edit ${initial.label}`}</DialogTitle></DialogHeader>
        <div className="space-y-3">
          <FormField label="Key" hint="Lowercase letters, digits and _. Cannot be changed later." required>
            <Input value={form.key} disabled={!isNew} onChange={(e) => setForm({ ...form, key: e.target.value })} />
          </FormField>
          <FormField label="Label" required><Input value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} /></FormField>
          <div className="grid grid-cols-2 gap-3">
            <FormField label="Category">
              <Select value={form.category} onValueChange={(v) => setForm({ ...form, category: v })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>{DEVICE_TYPE_CATEGORIES.map((c) => <SelectItem key={c} value={c}><span className="capitalize">{c}</span></SelectItem>)}</SelectContent>
              </Select>
            </FormField>
            <FormField label="Icon">
              <Select value={form.icon} onValueChange={(v) => setForm({ ...form, icon: v })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>{iconKeys.map((k) => <SelectItem key={k} value={k}>{k}</SelectItem>)}</SelectContent>
              </Select>
            </FormField>
          </div>
          <FormField label="Sort order"><Input type="number" value={form.sort_order}
                                                onChange={(e) => setForm({ ...form, sort_order: Number(e.target.value) })} /></FormField>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button disabled={!form.label.trim() || (isNew && !/^[a-z][a-z0-9_]{0,49}$/.test(form.key)) || save.isPending}
                  onClick={() => save.mutate()}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

