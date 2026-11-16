import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowLeft, Copy, Download, FlaskConical, GitBranch, History, Loader2, Play, RotateCcw, Rocket,
  Save, Trash2, Undo2, UserPlus, X,
} from 'lucide-react'
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
import { Switch } from '@/components/ui/Switch'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { useDeviceTypes } from '@/hooks/useDeviceTypes'
import {
  P, contentOf, type Impact, type TemplateContent, type TemplateDetail, type TestResult, templateError, templatesApi,
} from './api'
import { GroupEditor } from './GroupEditor'
import { TemplateStateBadges, sourceLabel } from './DeviceTemplatesPage'

const MANAGE = 'devices.manage_templates'

export function TemplateDetailPage() {
  const { id = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') || 'overview'
  const can = useCan()
  const qc = useQueryClient()
  const nav = useNavigate()
  const q = useQuery({ queryKey: ['device-template', id], queryFn: () => templatesApi.get(id) })
  const t = q.data
  const editable = !!t && !t.builtin && can(MANAGE)

  const [work, setWork] = useState<TemplateContent | null>(null)
  const [dirty, setDirty] = useState(false)
  useEffect(() => {
    if (t && !dirty) setWork(contentOf(t.draft || t))
  }, [t, dirty])
  const edit = (patch: Partial<TemplateContent>) => {
    setWork((w) => (w ? { ...w, ...patch } : w)); setDirty(true)
  }
  const refresh = () => {
    setDirty(false)
    qc.invalidateQueries({ queryKey: ['device-template', id] })
    qc.invalidateQueries({ queryKey: ['device-templates'] })
  }

  const saveDraft = useMutation({
    mutationFn: async () => (await api.put(`${P}/${id}/draft`, work)).data,
    onSuccess: (r) => {
      toast.success('Draft saved', r.draft_warnings?.length ? r.draft_warnings.join('; ') : 'Devices still poll the published revision')
      refresh()
    },
    onError: (e) => toast.error('Draft not saved', templateError(e)),
  })
  const discard = useMutation({
    mutationFn: async () => api.delete(`${P}/${id}/draft`),
    onSuccess: () => { toast.success('Draft discarded'); refresh() },
    onError: (e) => toast.error('Failed', templateError(e)),
  })
  const action = useMutation({
    mutationFn: async ({ path, body }: { path: string; body?: unknown }) => (await api.post(`${P}/${id}/${path}`, body ?? {})).data,
    onSuccess: (r, v) => {
      if (v.path === 'customize' || v.path === 'clone') {
        toast.success(v.path === 'customize' ? 'Customised template created' : 'Copy created', r.name)
        qc.invalidateQueries({ queryKey: ['device-templates'] })
        nav(`/device-templates/${r.id}`)
        return
      }
      toast.success('Done'); refresh()
    },
    onError: (e) => toast.error('Failed', templateError(e)),
  })
  const del = useMutation({
    mutationFn: async () => api.delete(`${P}/${id}`),
    onSuccess: () => { toast.success('Template deleted'); qc.invalidateQueries({ queryKey: ['device-templates'] }); nav('/device-templates') },
    onError: (e) => toast.error('Delete failed', templateError(e)),
  })
  const [publishOpen, setPublishOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [customizeOpen, setCustomizeOpen] = useState(false)

  if (q.isLoading || (t && !work)) return <SkeletonCard />
  if (q.isError || !t || !work) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="this template" />

  const exportJson = async () => {
    const data = (await api.get(`${P}/${id}/export`)).data
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${t.name.replace(/[^\w.-]+/g, '_')}.template.json`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  return (
    <div className="space-y-4">
      <div>
        <Link to="/device-templates" className="mb-1 inline-flex items-center gap-1 text-xs text-muted hover:text-text">
          <ArrowLeft className="h-3 w-3" /> Device Templates
        </Link>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">{t.name}</h1>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted">
              {t.vendor && <span>{t.vendor}</span>}
              <span>revision {t.revision}</span>
              <span>{t.device_count} device(s)</span>
              <TemplateStateBadges t={t} />
              {t.parent_id && <Link className="text-primary hover:underline" to={`/device-templates/${t.parent_id}`}>open parent</Link>}
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            {can(MANAGE) && <Button variant="outline" onClick={() => setCustomizeOpen(true)}><GitBranch className="h-4 w-4" /> Customise</Button>}
            {can(MANAGE) && <Button variant="outline" onClick={() => action.mutate({ path: 'clone' })}><Copy className="h-4 w-4" /> Clone</Button>}
            <Button variant="outline" onClick={exportJson}><Download className="h-4 w-4" /> Export</Button>
            {editable && <Button variant="outline" onClick={() => setConfirmDelete(true)}><Trash2 className="h-4 w-4" /> Delete</Button>}
          </div>
        </div>
      </div>

      {t.builtin && (
        <Card><CardContent className="p-3 text-sm text-muted">
          Built-in templates are maintained by ZenPlus and updated with releases. <b>Customise</b> creates a template that
          inherits this one: you change, turn off or add metrics there, and the vendor updates still arrive.
        </CardContent></Card>
      )}
      {(t.overlay_warnings || []).length > 0 && (
        <Card><CardContent className="space-y-1 p-3 text-sm">
          <div className="flex items-center gap-2 font-medium text-warning-fg"><AlertTriangle className="h-4 w-4" /> Parts of your customisation no longer apply</div>
          {(t.overlay_warnings || []).map((w) => <div key={w} className="text-xs text-muted">{w}</div>)}
        </CardContent></Card>
      )}

      {t.pilot_device_count > 0 && (
        <Card><CardContent className="flex flex-wrap items-center justify-between gap-3 p-3">
          <div className="text-sm">
            <b>Pilot running</b> on {t.pilot_device_count} device(s) since {t.pilot_started_at ? new Date(t.pilot_started_at).toLocaleString() : '—'}:
            they poll the draft, every other device polls revision {t.revision}.
          </div>
          {can(MANAGE) && (
            <div className="flex gap-2">
              <Button size="sm" onClick={() => setPublishOpen(true)}><Rocket className="h-4 w-4" /> Publish to all</Button>
              <Button size="sm" variant="outline" onClick={() => action.mutate({ path: 'pilot/cancel' })}><X className="h-4 w-4" /> Cancel pilot</Button>
            </div>
          )}
        </CardContent></Card>
      )}

      {editable && (dirty || t.has_draft) && (
        <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-3 rounded-md border border-warning/40 bg-warning/10 p-3">
          <div className="text-sm">
            {dirty ? 'Unsaved changes.' : `Draft saved ${t.draft_updated_at ? new Date(t.draft_updated_at).toLocaleString() : ''}.`}
            {' '}Devices keep polling revision {t.revision} until you publish.
          </div>
          <div className="flex gap-2">
            {dirty && <Button size="sm" variant="outline" onClick={() => { setDirty(false); setWork(contentOf(t.draft || t)) }}><Undo2 className="h-4 w-4" /> Undo changes</Button>}
            {dirty && <Button size="sm" onClick={() => saveDraft.mutate()} disabled={saveDraft.isPending}><Save className="h-4 w-4" /> Save draft</Button>}
            {!dirty && t.has_draft && <Button size="sm" onClick={() => setPublishOpen(true)}><Rocket className="h-4 w-4" /> Review and publish</Button>}
            {!dirty && t.has_draft && !t.pilot_device_count && <Button size="sm" variant="outline" onClick={() => discard.mutate()}>Discard draft</Button>}
          </div>
        </div>
      )}

      <Tabs value={tab} onValueChange={(v) => setParams({ tab: v }, { replace: true })}>
        <TabsList>
          <TabsTrigger value="overview">Identity &amp; matching</TabsTrigger>
          <TabsTrigger value="metrics">Metrics ({work.oid_groups.length})</TabsTrigger>
          <TabsTrigger value="devices">Devices ({t.device_count})</TabsTrigger>
          <TabsTrigger value="test">Test on a device</TabsTrigger>
          <TabsTrigger value="history">History</TabsTrigger>
        </TabsList>
        <TabsContent value="overview"><IdentityForm work={work} edit={edit} readOnly={!editable} /></TabsContent>
        <TabsContent value="metrics">
          <GroupEditor groups={work.oid_groups} onChange={(g) => edit({ oid_groups: g })} readOnly={!editable}
                       inherited={t.parent_id ? t.parent_groups : null} />
        </TabsContent>
        <TabsContent value="devices"><DevicesTab t={t} /></TabsContent>
        <TabsContent value="test"><TestTab t={t} /></TabsContent>
        <TabsContent value="history"><HistoryTab t={t} canRollback={editable} onDone={refresh} /></TabsContent>
      </Tabs>

      {publishOpen && <PublishDialog t={t} onClose={() => setPublishOpen(false)} onDone={() => { setPublishOpen(false); refresh() }} />}
      {customizeOpen && (
        <CustomizeDialog t={t} onClose={() => setCustomizeOpen(false)}
                         onSubmit={(body) => { setCustomizeOpen(false); action.mutate({ path: 'customize', body }) }} />
      )}
      <ConfirmDialog open={confirmDelete} onOpenChange={setConfirmDelete} destructive title={`Delete ${t.name}?`}
                     confirmText="Delete" loading={del.isPending} onConfirm={() => del.mutate()}
                     description={`${t.device_count} device(s) go back to standard monitoring and lose this template's current readings. History in charts is kept until normal retention.`} />
    </div>
  )
}

function IdentityForm({ work, edit, readOnly }: {
  work: TemplateContent; edit: (p: Partial<TemplateContent>) => void; readOnly: boolean
}) {
  const { types } = useDeviceTypes()
  const mr = work.match_rules || {}
  const setRule = (patch: Record<string, unknown>) => edit({ match_rules: { ...mr, ...patch } })
  const [prefixText, setPrefixText] = useState((mr.sys_object_id_prefixes || []).join('\n'))
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card><CardContent className="space-y-3 p-4">
        <h2 className="text-sm font-semibold">Identity</h2>
        <FormField label="Name" required><Input value={work.name} disabled={readOnly} onChange={(e) => edit({ name: e.target.value })} /></FormField>
        <FormField label="Vendor"><Input value={work.vendor || ''} disabled={readOnly} onChange={(e) => edit({ vendor: e.target.value || null })} /></FormField>
        <FormField label="Device type" hint="Given to devices classified to this template while their type is still Other, and offered when you assign devices.">
          <Select value={work.device_type || '__none__'} disabled={readOnly}
                  onValueChange={(v) => edit({ device_type: v === '__none__' ? null : v })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">Not set</SelectItem>
              {types.map((x) => <SelectItem key={x.key} value={x.key}>{x.label}</SelectItem>)}
            </SelectContent>
          </Select>
        </FormField>
        <FormField label="Description">
          <Textarea rows={4} value={work.description || ''} disabled={readOnly} onChange={(e) => edit({ description: e.target.value || null })} />
        </FormField>
      </CardContent></Card>
      <Card><CardContent className="space-y-3 p-4">
        <h2 className="text-sm font-semibold">Matching</h2>
        <p className="text-xs text-muted">
          The template with the highest priority wins; at equal priority a sysObjectID prefix beats a sysDescr match and
          the longest prefix wins. A device assigned by hand or by a discovery rule is never re-classified; when a
          better template matches later it is listed under Suggestions.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label="Match priority" hint="0–1000, higher wins">
            <Input type="number" min={0} max={1000} value={work.match_priority} disabled={readOnly}
                   onChange={(e) => edit({ match_priority: Math.min(1000, Math.max(0, Number(e.target.value) || 0)) })} />
          </FormField>
          <FormField label="Require sysDescr match">
            <div className="flex h-9 items-center gap-2">
              <Switch checked={!!mr.require_sys_descr} disabled={readOnly} onCheckedChange={(v) => setRule({ require_sys_descr: v })} />
              <span className="text-xs text-muted">for models that share a sysObjectID</span>
            </div>
          </FormField>
        </div>
        <FormField label="sysObjectID prefixes" hint="One per line; a prefix matches on a dot boundary.">
          <Textarea rows={3} className="font-mono text-xs" value={prefixText} disabled={readOnly}
                    onChange={(e) => {
                      setPrefixText(e.target.value)
                      setRule({ sys_object_id_prefixes: e.target.value.split('\n').map((s) => s.trim().replace(/^\./, '')).filter(Boolean) })
                    }} />
        </FormField>
        <FormField label="sysDescr pattern" hint="Regular expression; lookarounds and backreferences are not supported.">
          <Input className="font-mono text-xs" value={mr.sys_descr_regex || ''} disabled={readOnly}
                 onChange={(e) => setRule({ sys_descr_regex: e.target.value || null })} />
        </FormField>
        <details className="text-sm">
          <summary className="cursor-pointer text-xs text-muted">Vendor, model and OS version read from sysDescr</summary>
          <div className="mt-2 grid gap-3 sm:grid-cols-2">
            {([['extract_vendor', 'Vendor pattern'], ['default_vendor', 'Default vendor'], ['extract_model', 'Model pattern'],
               ['default_model', 'Default model'], ['extract_os_version', 'OS version pattern']] as const).map(([k, label]) => (
              <FormField key={k} label={label}>
                <Input className="font-mono text-xs" value={(mr as any)[k] || ''} disabled={readOnly}
                       onChange={(e) => setRule({ [k]: e.target.value || null })} />
              </FormField>
            ))}
          </div>
        </details>
      </CardContent></Card>
    </div>
  )
}

function PublishDialog({ t, onClose, onDone }: { t: TemplateDetail; onClose: () => void; onDone: () => void }) {
  const impact = useQuery<Impact>({
    queryKey: ['device-template-impact', t.id, t.draft_updated_at],
    queryFn: async () => (await api.post(`${P}/${t.id}/impact`, {})).data,
  })
  const devices = useQuery({ queryKey: ['device-template-devices', t.id], queryFn: () => templatesApi.devices(t.id) })
  const [mode, setMode] = useState<'all' | 'pilot'>(t.pilot_device_count ? 'all' : 'all')
  const [pilot, setPilot] = useState<Set<string>>(new Set(t.pilot_device_ids))
  const [reason, setReason] = useState('')
  const [confirmLoad, setConfirmLoad] = useState(false)
  const publish = useMutation({
    mutationFn: async () => (await api.post(`${P}/${t.id}/publish`, {
      reason: reason.trim() || null, pilot_device_ids: mode === 'pilot' ? [...pilot] : null,
    })).data,
    onSuccess: () => { toast.success(mode === 'pilot' ? 'Pilot started' : 'Published'); onDone() },
    onError: (e) => toast.error('Publish failed', templateError(e)),
  })
  const i = impact.data
  const blocked = !!i?.needs_confirmation && !confirmLoad
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader><DialogTitle>Publish draft of {t.name}</DialogTitle></DialogHeader>
        {impact.isLoading && <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Working out the impact…</div>}
        {impact.isError && <p className="text-sm text-danger">{templateError(impact.error)}</p>}
        {i && (
          <div className="space-y-2 text-sm">
            <div className="grid grid-cols-3 gap-2">
              <Stat label="Devices on this template" value={i.devices} />
              <Stat label="OIDs per poll" value={`${i.oids_before} → ${i.oids_after}`} />
              <Stat label="Customised from it" value={i.customized_templates.length} />
            </div>
            <ChangeList label="Groups added" items={i.groups_added} />
            <ChangeList label="Groups removed (their current readings are cleared)" items={i.groups_removed} tone="danger" />
            <ChangeList label="Groups changed" items={i.groups_changed} />
            <ChangeList label="Metrics added" items={i.metrics_added} />
            <ChangeList label="Metrics removed" items={i.metrics_removed} tone="danger" />
            {i.match_rules_changed && <p className="text-warning-fg">Matching changes: devices discovered from now on may classify differently. Devices already on a template are not moved.</p>}
            {i.alert_rules_on_removed_metrics.length > 0 && (
              <p className="text-danger">Alert rules that use a removed metric will stop seeing data: {i.alert_rules_on_removed_metrics.map((r) => r.name).join(', ')}</p>
            )}
            {i.warnings.map((w) => <p key={w} className="text-xs text-warning-fg">{w}</p>)}
            {i.needs_confirmation && (
              <label className="flex items-center gap-2 text-warning-fg">
                <input type="checkbox" checked={confirmLoad} onChange={(e) => setConfirmLoad(e.target.checked)} />
                This more than doubles the OIDs polled per device; I have tested it on a device.
              </label>
            )}
          </div>
        )}
        <div className="space-y-2">
          <div className="flex gap-4 text-sm">
            <label className="flex items-center gap-2"><input type="radio" checked={mode === 'all'} onChange={() => setMode('all')} /> Every device</label>
            <label className="flex items-center gap-2"><input type="radio" checked={mode === 'pilot'} onChange={() => setMode('pilot')} /> Pilot on selected devices first</label>
          </div>
          {mode === 'pilot' && (
            <div className="max-h-48 overflow-y-auto rounded-md border border-border p-2 text-sm">
              {(devices.data?.data || []).map((d) => (
                <label key={d.id} className="flex items-center gap-2 py-0.5">
                  <input type="checkbox" checked={pilot.has(d.id)} onChange={(e) => {
                    const n = new Set(pilot); e.target.checked ? n.add(d.id) : n.delete(d.id); setPilot(n)
                  }} />
                  {d.hostname} <span className="text-xs text-muted">{d.ip_address}</span>
                </label>
              ))}
              {(devices.data?.data || []).length === 0 && <p className="text-xs text-muted">No device uses this template yet.</p>}
            </div>
          )}
          <FormField label="Reason (kept in the history)"><Input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} /></FormField>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button disabled={publish.isPending || blocked || !i || (mode === 'pilot' && pilot.size === 0)} onClick={() => publish.mutate()}>
            <Rocket className="h-4 w-4" /> {mode === 'pilot' ? `Start pilot on ${pilot.size}` : 'Publish to every device'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return <div className="rounded-md border border-border p-2"><div className="text-lg font-semibold tabular-nums">{value}</div><div className="text-xs text-muted">{label}</div></div>
}

function ChangeList({ label, items, tone }: { label: string; items: string[]; tone?: 'danger' }) {
  if (!items.length) return null
  return (
    <div>
      <span className={tone === 'danger' ? 'text-danger' : 'text-muted'}>{label}: </span>
      <span className="font-mono text-xs">{items.slice(0, 20).join(', ')}{items.length > 20 ? ` +${items.length - 20} more` : ''}</span>
    </div>
  )
}

function CustomizeDialog({ t, onClose, onSubmit }: {
  t: TemplateDetail; onClose: () => void; onSubmit: (b: { name: string | null; move_devices: boolean }) => void
}) {
  const [name, setName] = useState(`${t.name} (customised)`)
  const [move, setMove] = useState(t.device_count > 0)
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>Customise {t.name}</DialogTitle></DialogHeader>
        <p className="text-sm text-muted">
          The new template inherits every group of {t.name} and keeps receiving its updates. It stores only what you
          change, and matches the same devices at priority {Math.min(t.match_priority + 10, 1000)} (one step above).
        </p>
        <FormField label="Name"><Input value={name} onChange={(e) => setName(e.target.value)} /></FormField>
        {t.device_count > 0 && (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={move} onChange={(e) => setMove(e.target.checked)} />
            Move its {t.device_count} device(s) to the customised template now
          </label>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={() => onSubmit({ name: name.trim() || null, move_devices: move })}>Customise</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DevicesTab({ t }: { t: TemplateDetail }) {
  const qc = useQueryClient()
  const can = useCan()
  const q = useQuery({ queryKey: ['device-template-devices', t.id], queryFn: () => templatesApi.devices(t.id) })
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [assignOpen, setAssignOpen] = useState(false)
  const unassign = useMutation({
    mutationFn: async () => (await api.post(`${P}/unassign`, { device_ids: [...sel] })).data,
    onSuccess: () => {
      toast.success('Removed from the template')
      setSel(new Set())
      qc.invalidateQueries({ queryKey: ['device-template-devices', t.id] })
      qc.invalidateQueries({ queryKey: ['device-template', t.id] })
    },
    onError: (e) => toast.error('Failed', templateError(e)),
  })
  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="devices" />
  const rows = q.data?.data || []
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted">
          Coverage: metrics that answered in the last 24 h out of {q.data?.defined ?? 0} defined
          {q.data?.average_pct != null ? ` (average ${q.data.average_pct}%)` : ''}. Low coverage usually means a metric
          the model does not implement, or the wrong template.
        </p>
        {can('devices.manage') && (
          <div className="flex gap-2">
            <Button size="sm" variant="outline" disabled={!sel.size || unassign.isPending} onClick={() => unassign.mutate()}>Remove {sel.size || ''} from template</Button>
            <Button size="sm" onClick={() => setAssignOpen(true)}><UserPlus className="h-4 w-4" /> Assign devices</Button>
          </div>
        )}
      </div>
      <div className="overflow-hidden rounded-md border border-border">
        <Table>
          <THead className="bg-surface2/60"><Tr><Th className="w-8" /><Th>Device</Th><Th>Assigned by</Th><Th className="text-right">Coverage</Th><Th>Not answering</Th><Th>Note</Th></Tr></THead>
          <TBody>
            {rows.map((d) => (
              <Tr key={d.id}>
                <Td><input type="checkbox" checked={sel.has(d.id)} onChange={(e) => {
                  const n = new Set(sel); e.target.checked ? n.add(d.id) : n.delete(d.id); setSel(n)
                }} /></Td>
                <Td><Link className="font-medium hover:text-primary" to={`/devices/${d.id}`}>{d.hostname}</Link>
                  <div className="text-xs text-muted">{d.ip_address} {d.model ? `· ${d.model}` : ''}</div></Td>
                <Td className="text-xs text-muted">{sourceLabel(d.profile_source)}</Td>
                <Td className="text-right tabular-nums">{d.coverage_pct == null ? '—' : `${d.coverage_pct}%`}</Td>
                <Td className="max-w-xs truncate font-mono text-xs text-muted" title={d.missing.join(', ')}>{d.missing.slice(0, 4).join(', ')}{d.missing.length > 4 ? '…' : ''}</Td>
                <Td className="text-xs">
                  {d.in_pilot && <Badge variant="warning">pilot</Badge>}
                  {d.suggested_name && <span className="text-muted"> better match: <Link className="text-primary" to={`/device-templates/${d.suggested_id}`}>{d.suggested_name}</Link></span>}
                </Td>
              </Tr>
            ))}
            {!q.isLoading && rows.length === 0 && <Tr><Td colSpan={6} className="py-8 text-center text-sm text-muted">No device uses this template.</Td></Tr>}
          </TBody>
        </Table>
      </div>
      {assignOpen && <AssignDialog templateId={t.id} templateName={t.name} deviceType={t.device_type}
                                   onClose={() => setAssignOpen(false)}
                                   onDone={() => { qc.invalidateQueries({ queryKey: ['device-template-devices', t.id] }); qc.invalidateQueries({ queryKey: ['device-template', t.id] }) }} />}
    </div>
  )
}

export function AssignDialog({ templateId, templateName, deviceType, presetIds, onClose, onDone }: {
  templateId?: string; templateName?: string; deviceType?: string | null; presetIds?: string[]
  onClose: () => void; onDone: () => void
}) {
  const [search, setSearch] = useState('')
  const [sel, setSel] = useState<Set<string>>(new Set(presetIds || []))
  const [tpl, setTpl] = useState(templateId || '')
  const [setType, setSetType] = useState(false)
  const templates = useQuery({ queryKey: ['device-templates'], queryFn: templatesApi.list, enabled: !templateId })
  const devices = useQuery({
    queryKey: ['assign-device-search', search],
    queryFn: async () => (await api.get('/devices', { params: { limit: 50, ...(search ? { search } : {}) } })).data.data as any[],
    enabled: !presetIds,
  })
  const chosen = useMemo(() => (templates.data || []).find((x) => x.id === tpl), [templates.data, tpl])
  const typeOfTemplate = templateId ? deviceType : chosen?.device_type
  const assign = useMutation({
    mutationFn: async () => (await api.post(`${P}/${tpl}/assign`, { device_ids: [...sel], set_device_type: setType })).data,
    onSuccess: (r) => {
      const skipped = (r.skipped || []).length
      toast.success(`${r.assigned} device(s) assigned`, skipped ? `${skipped} skipped (controller-managed or not found)` : undefined)
      onDone(); onClose()
    },
    onError: (e) => toast.error('Assign failed', templateError(e)),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>Assign {templateName ? `to ${templateName}` : 'a template'}</DialogTitle></DialogHeader>
        {!templateId && (
          <FormField label="Template">
            <Select value={tpl} onValueChange={setTpl}>
              <SelectTrigger><SelectValue placeholder="Choose a template" /></SelectTrigger>
              <SelectContent>{(templates.data || []).map((x) => <SelectItem key={x.id} value={x.id}>{x.name}</SelectItem>)}</SelectContent>
            </Select>
          </FormField>
        )}
        {presetIds ? <p className="text-sm text-muted">{presetIds.length} selected device(s).</p> : (
          <>
            <Input placeholder="Search devices" value={search} onChange={(e) => setSearch(e.target.value)} />
            <div className="max-h-64 overflow-y-auto rounded-md border border-border p-2 text-sm">
              {(devices.data || []).map((d) => (
                <label key={d.id} className="flex items-center gap-2 py-0.5">
                  <input type="checkbox" checked={sel.has(d.id)} onChange={(e) => {
                    const n = new Set(sel); e.target.checked ? n.add(d.id) : n.delete(d.id); setSel(n)
                  }} />
                  {d.hostname} <span className="text-xs text-muted">{d.ip_address} {d.vendor || ''}</span>
                </label>
              ))}
            </div>
          </>
        )}
        {typeOfTemplate && (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={setType} onChange={(e) => setSetType(e.target.checked)} />
            Also set the device type to the template's type ({typeOfTemplate})
          </label>
        )}
        <p className="text-xs text-muted">Readings of each device's previous template are cleared. Controller-managed devices are skipped.</p>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button disabled={!tpl || !sel.size || assign.isPending} onClick={() => assign.mutate()}>Assign {sel.size || ''}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function TestTab({ t }: { t: TemplateDetail }) {
  const can = useCan()
  const devices = useQuery({ queryKey: ['device-template-devices', t.id], queryFn: () => templatesApi.devices(t.id) })
  const [deviceId, setDeviceId] = useState('')
  const [other, setOther] = useState('')
  const [source, setSource] = useState<'draft' | 'published'>(t.has_draft ? 'draft' : 'published')
  const search = useQuery({
    queryKey: ['test-device-search', other],
    queryFn: async () => (await api.get('/devices', { params: { limit: 20, search: other } })).data.data as any[],
    enabled: other.trim().length >= 2,
  })
  const run = useMutation({
    mutationFn: async () => (await api.post(`${P}/${t.id}/test`, { device_id: deviceId, source })).data as TestResult,
    onError: (e) => toast.error('Test failed', templateError(e)),
  })
  if (!can(MANAGE) && !can('devices.manage')) return <p className="text-sm text-muted">Testing needs permission to manage devices or templates.</p>
  const r = run.data
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">
        Runs the template's requests against one device from this appliance (GET per scalar metric, a walk per table
        column, at most 200 rows) and shows what came back. Nothing is stored. Devices polled through a remote sensor
        cannot be tested from here.
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <FormField label="Device on this template" className="w-64">
          <Select value={deviceId} onValueChange={setDeviceId}>
            <SelectTrigger><SelectValue placeholder="Choose" /></SelectTrigger>
            <SelectContent>{(devices.data?.data || []).map((d) => <SelectItem key={d.id} value={d.id}>{d.hostname}</SelectItem>)}</SelectContent>
          </Select>
        </FormField>
        <FormField label="…or any device" className="w-64">
          <Input placeholder="Search" value={other} onChange={(e) => setOther(e.target.value)} />
        </FormField>
        <FormField label="Run">
          <Select value={source} onValueChange={(v) => setSource(v as 'draft' | 'published')}>
            <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
            <SelectContent>
              {t.has_draft && <SelectItem value="draft">Saved draft</SelectItem>}
              <SelectItem value="published">Published revision</SelectItem>
            </SelectContent>
          </Select>
        </FormField>
        <Button disabled={!deviceId || run.isPending} onClick={() => run.mutate()}>
          {run.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />} Run test
        </Button>
      </div>
      {other.trim().length >= 2 && (
        <div className="flex flex-wrap gap-2">
          {(search.data || []).map((d) => (
            <Button key={d.id} size="sm" variant={deviceId === d.id ? 'default' : 'outline'} onClick={() => setDeviceId(d.id)}>{d.hostname}</Button>
          ))}
        </div>
      )}
      {r && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 text-sm">
            <FlaskConical className="h-4 w-4 text-primary" />
            <b>{r.device.hostname}</b> answered {r.answered} of {r.defined} metrics
            {r.coverage_pct != null && ` (${r.coverage_pct}%)`} in {r.duration_ms} ms, testing the {r.source} template.
          </div>
          {r.groups.map((g) => (
            <Card key={g.key}><CardContent className="space-y-2 p-3">
              <div className="flex items-center gap-2 text-sm font-medium">
                {g.name} <span className="font-mono text-xs text-muted">{g.key}</span>
                {g.kind === 'table' && <Badge variant="outline">{g.rows ?? 0} row(s){g.truncated ? ', truncated' : ''}</Badge>}
                <span className="text-xs text-muted">{g.duration_ms} ms</span>
              </div>
              {g.error && <p className="text-xs text-danger">{g.error}</p>}
              <table className="w-full text-sm">
                <tbody>
                  {g.metrics.map((m) => (
                    <tr key={m.key} className="border-t border-border align-top">
                      <td className="py-1 pr-2">{m.ok ? <Badge variant="success">ok</Badge> : <Badge variant="danger">missing</Badge>}</td>
                      <td className="pr-2">{m.name}<div className="font-mono text-xs text-muted">{m.oid}</div></td>
                      <td className="text-xs">
                        {g.kind === 'table'
                          ? (m.sample || []).slice(0, 5).map((s) => `${s.label || s.index}: ${String(s.value)}`).join(' · ') + ((m.rows || 0) > 5 ? ` … (${m.rows} rows)` : '')
                          : m.ok ? `${String(m.value)}${m.unit ? ` ${m.unit}` : ''}${m.label ? ` (${m.label})` : ''}` : (m.error || '')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardContent></Card>
          ))}
        </div>
      )}
    </div>
  )
}

function HistoryTab({ t, canRollback, onDone }: { t: TemplateDetail; canRollback: boolean; onDone: () => void }) {
  const q = useQuery({ queryKey: ['device-template-versions', t.id, t.revision], queryFn: () => templatesApi.versions(t.id) })
  const [view, setView] = useState<number | null>(null)
  const detail = useQuery({ queryKey: ['device-template-version', t.id, view], queryFn: () => templatesApi.version(t.id, view as number), enabled: view !== null })
  const [rollTo, setRollTo] = useState<number | null>(null)
  const rollback = useMutation({
    mutationFn: async () => (await api.post(`${P}/${t.id}/rollback`, { revision: rollTo })).data,
    onSuccess: () => { toast.success(`Revision ${rollTo} published again`); setRollTo(null); onDone() },
    onError: (e) => toast.error('Rollback failed', templateError(e)),
  })
  const ACTION: Record<string, string> = {
    baseline: 'State before history began', created: 'Created', published: 'Published', rollback: 'Rolled back',
    imported: 'Imported', cloned: 'Copied', customized: 'Customised', external_update: 'Release update',
    parent_update: 'Parent template updated',
  }
  if (q.isError) return <QueryFailure error={q.error} onRetry={() => q.refetch()} what="history" />
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="overflow-hidden rounded-md border border-border">
        <Table>
          <THead className="bg-surface2/60"><Tr><Th>Revision</Th><Th>What</Th><Th>Who / when</Th><Th /></Tr></THead>
          <TBody>
            {(q.data || []).map((v) => (
              <Tr key={v.revision} className={view === v.revision ? 'bg-surface2' : ''}>
                <Td className="tabular-nums">{v.revision}{v.revision === t.revision && <Badge variant="success" className="ml-2">current</Badge>}</Td>
                <Td className="text-sm">{ACTION[v.action] || v.action}<div className="text-xs text-muted">{v.reason}</div></Td>
                <Td className="text-xs text-muted">{v.changed_by_name || 'system'}<div>{new Date(v.changed_at).toLocaleString()}</div></Td>
                <Td className="text-right">
                  <span className="inline-flex gap-1">
                    <Button size="sm" variant="ghost" onClick={() => setView(v.revision)}><History className="h-4 w-4" /></Button>
                    {canRollback && v.revision !== t.revision && (
                      <Button size="sm" variant="ghost" title="Publish this revision again" onClick={() => setRollTo(v.revision)}><RotateCcw className="h-4 w-4" /></Button>
                    )}
                  </span>
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
      <Card><CardContent className="space-y-2 p-4 text-sm">
        {view === null && <p className="text-muted">Choose a revision to compare it with the current one.</p>}
        {detail.data && (
          <>
            <div className="font-medium">Revision {view} compared with current (revision {t.revision})</div>
            <ChangeList label="Groups only in the current revision" items={detail.data.compare_to_current.groups_added} />
            <ChangeList label="Groups only in revision" items={detail.data.compare_to_current.groups_removed} />
            <ChangeList label="Groups that differ" items={detail.data.compare_to_current.groups_changed} />
            <ChangeList label="Metrics only in the current revision" items={detail.data.compare_to_current.metrics_added} />
            <ChangeList label="Metrics only in revision" items={detail.data.compare_to_current.metrics_removed} />
            <div className="text-xs text-muted">OIDs per poll: {detail.data.compare_to_current.oids_before} → {detail.data.compare_to_current.oids_after}</div>
          </>
        )}
      </CardContent></Card>
      <ConfirmDialog open={rollTo !== null} onOpenChange={(o) => !o && setRollTo(null)} title={`Publish revision ${rollTo} again?`}
                     confirmText="Roll back" loading={rollback.isPending} onConfirm={() => rollback.mutate()}
                     description="It becomes a new revision; the history keeps every earlier one. Readings of groups that disappear are cleared." />
    </div>
  )
}
