import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, CheckCircle2, ChevronDown, ChevronRight, Copy, Layers,
  LayoutTemplate, Loader2, Lock, Pencil, Plus, Router, Search,
  Trash2, Unplug,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { SkeletonCard } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type TemplateMetric = {
  key: string; name: string; oid: string; type: string
  unit?: string; scale?: number
  labels?: Record<string, { text: string; sev: string }>
  thresholds?: { warn?: number; crit?: number; op?: string }
}
type TemplateGroup = {
  key: string; name: string; kind: 'scalar' | 'table'
  description?: string
  table?: { label_oid?: string }
  metrics: TemplateMetric[]
}
type Template = {
  id: string; name: string; vendor: string | null; version: number
  builtin: boolean; description: string | null
  match_rules: any; oid_groups: TemplateGroup[]
  device_count: number; created_at: string; updated_at: string
}

/* ------------------------------------------------------------------ */
/*  Vendor visual identity (brand-inspired accents, initials glyph)    */
/* ------------------------------------------------------------------ */

function vendorTheme(vendor?: string | null) {
  const v = (vendor || '').toLowerCase()
  if (v.includes('fortinet')) return { glyph: 'FG', accent: '#ee3124' }
  if (v.includes('cisco')) return { glyph: 'CS', accent: '#049fd9' }
  if (v.includes('palo')) return { glyph: 'PA', accent: '#fa582d' }
  if (v.includes('f5')) return { glyph: 'F5', accent: '#a41e35' }
  if (v.includes('juniper')) return { glyph: 'JN', accent: '#84b135' }
  if (v.includes('aruba') || v.includes('hpe')) return { glyph: 'AR', accent: '#ff8300' }
  return { glyph: 'TPL', accent: 'rgb(var(--primary))' }
}

function VendorGlyph({ vendor, size = 'lg' }: { vendor?: string | null; size?: 'sm' | 'lg' }) {
  const t = vendorTheme(vendor)
  return (
    <div
      className={cn(
        'flex shrink-0 items-center justify-center rounded-lg font-bold tracking-tight text-white',
        size === 'lg' ? 'h-11 w-11 text-sm' : 'h-8 w-8 text-[10px]',
      )}
      style={{ background: `linear-gradient(135deg, ${t.accent}, ${t.accent}bb)` }}
    >
      {t.glyph}
    </div>
  )
}

const typeBadge: Record<string, string> = {
  gauge: 'bg-info/10 text-info',
  counter: 'bg-accent/10 text-accent',
  enum: 'bg-warning/10 text-warning',
  string: 'bg-surface3 text-muted',
}

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */

export default function MonitoringTemplatesPage() {
  const qc = useQueryClient()
  const [search, setSearch] = useState('')
  const [detail, setDetail] = useState<Template | null>(null)
  const [editor, setEditor] = useState<{ open: boolean; template?: Template }>({ open: false })
  const [toDelete, setToDelete] = useState<Template | null>(null)

  const { data: templates, isLoading } = useQuery<Template[]>({
    queryKey: ['snmp-profiles'],
    queryFn: async () => (await api.get('/snmp/profiles')).data,
    refetchInterval: 30_000,
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/snmp/profiles/${id}`),
    onSuccess: () => {
      toast.success('Template deleted')
      qc.invalidateQueries({ queryKey: ['snmp-profiles'] })
      setToDelete(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const clone = useMutation({
    mutationFn: async (id: string) => (await api.post(`/snmp/profiles/${id}/clone`)).data,
    onSuccess: (t: Template) => {
      toast.success('Template cloned', `"${t.name}" is ready to customize`)
      qc.invalidateQueries({ queryKey: ['snmp-profiles'] })
      setEditor({ open: true, template: t })
      setDetail(null)
    },
    onError: (e: any) => toast.error('Clone failed', apiErrorMessage(e)),
  })

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return templates || []
    return (templates || []).filter((t) =>
      [t.name, t.vendor, t.description].filter(Boolean).some((s) => s!.toLowerCase().includes(q)),
    )
  }, [templates, search])

  const totals = useMemo(() => {
    const list = templates || []
    return {
      templates: list.length,
      builtin: list.filter((t) => t.builtin).length,
      devices: list.reduce((a, t) => a + (t.device_count || 0), 0),
      metrics: list.reduce(
        (a, t) => a + (t.oid_groups || []).reduce((b, g) => b + (g.metrics?.length || 0), 0), 0),
    }
  }, [templates])

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-bold">
            <LayoutTemplate className="h-5 w-5 text-primary" /> Monitoring Templates
          </h1>
          <p className="mt-1 text-sm text-muted">
            Vendor-specific SNMP insight packs. Attach a template to a device — or let
            auto-detection do it — to unlock deep monitoring beyond standard ping/CPU/interfaces:
            HA clusters, VPN tunnels, SD-WAN health, managed APs &amp; switches, hardware sensors.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
            <Input className="w-56 pl-8" placeholder="Search templates…" value={search}
              onChange={(e) => setSearch(e.target.value)} />
          </div>
          <Button onClick={() => setEditor({ open: true })}>
            <Plus className="h-4 w-4" /> New Template
          </Button>
        </div>
      </div>

      {/* Stat strip */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          { label: 'Templates', value: totals.templates, icon: LayoutTemplate },
          { label: 'Built-in packs', value: totals.builtin, icon: Lock },
          { label: 'Devices covered', value: totals.devices, icon: Router },
          { label: 'Metrics defined', value: totals.metrics, icon: Activity },
        ].map(({ label, value, icon: Icon }) => (
          <Card key={label}>
            <CardContent className="flex items-center gap-3 p-4">
              <div className="rounded-md bg-primary/10 p-2"><Icon className="h-4 w-4 text-primary" /></div>
              <div>
                <div className="text-lg font-bold tabular-nums">{value}</div>
                <div className="text-[11px] text-muted">{label}</div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Template cards */}
      {isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {[...Array(4)].map((_, i) => <SkeletonCard key={i} lines={4} />)}
        </div>
      ) : filtered.length === 0 ? (
        <Card><CardContent className="p-10 text-center text-sm text-muted">
          No templates match “{search}”.
        </CardContent></Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((t) => (
            <TemplateCard key={t.id} t={t}
              onOpen={() => setDetail(t)}
              onClone={() => clone.mutate(t.id)}
              onEdit={() => setEditor({ open: true, template: t })}
              onDelete={() => setToDelete(t)} />
          ))}
        </div>
      )}

      {detail && (
        <TemplateDetailDialog template={detail} onOpenChange={(o) => !o && setDetail(null)}
          onClone={() => clone.mutate(detail.id)} />
      )}
      <TemplateEditorDialog key={editor.template?.id || 'new'} open={editor.open}
        template={editor.template} onOpenChange={(o) => setEditor((s) => ({ ...s, open: o }))} />
      <ConfirmDialog open={!!toDelete} onOpenChange={(o) => !o && setToDelete(null)}
        title="Delete template?"
        description={<>“{toDelete?.name}” will be removed and {toDelete?.device_count || 0} attached device(s) fall back to Default monitoring.</>}
        confirmText="Delete" destructive loading={del.isPending}
        onConfirm={() => toDelete && del.mutate(toDelete.id)} />
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Card                                                               */
/* ------------------------------------------------------------------ */

function TemplateCard({ t, onOpen, onClone, onEdit, onDelete }: {
  t: Template; onOpen: () => void; onClone: () => void; onEdit: () => void; onDelete: () => void
}) {
  const groups = t.oid_groups || []
  const metricCount = groups.reduce((a, g) => a + (g.metrics?.length || 0), 0)
  const accent = vendorTheme(t.vendor).accent
  return (
    <Card className="group relative overflow-hidden transition-shadow hover:shadow-elevated">
      <div className="absolute inset-x-0 top-0 h-0.5" style={{ background: accent }} />
      <CardContent className="flex h-full flex-col gap-3 p-5">
        <div className="flex items-start gap-3">
          <VendorGlyph vendor={t.vendor} />
          <div className="min-w-0 flex-1">
            <button onClick={onOpen} className="block truncate text-left text-sm font-semibold hover:text-primary">
              {t.name}
            </button>
            <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted">
              {t.vendor || 'Custom'}
              {t.builtin
                ? <Badge variant="info" className="gap-1"><Lock className="h-2.5 w-2.5" />Built-in</Badge>
                : <Badge variant="outline">Custom</Badge>}
            </div>
          </div>
        </div>

        {t.description && (
          <p className="line-clamp-2 text-xs leading-relaxed text-muted">{t.description}</p>
        )}

        <div className="flex flex-wrap gap-1.5">
          {groups.slice(0, 5).map((g) => (
            <span key={g.key} className="rounded-full border border-border bg-surface2 px-2 py-0.5 text-[10px] text-text2">
              {g.name}
            </span>
          ))}
          {groups.length > 5 && (
            <span className="rounded-full px-2 py-0.5 text-[10px] text-muted">+{groups.length - 5} more</span>
          )}
        </div>

        <div className="mt-auto flex items-center justify-between border-t border-border pt-3">
          <div className="flex items-center gap-3 text-[11px] text-muted">
            <span className="flex items-center gap-1"><Layers className="h-3 w-3" />{groups.length} groups</span>
            <span className="flex items-center gap-1"><Activity className="h-3 w-3" />{metricCount} metrics</span>
            <span className={cn('flex items-center gap-1', t.device_count > 0 && 'text-success')}>
              <Router className="h-3 w-3" />{t.device_count} device{t.device_count === 1 ? '' : 's'}
            </span>
          </div>
          <div className="flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
            <Button variant="ghost" size="icon" className="h-7 w-7" title="Clone" onClick={onClone}>
              <Copy className="h-3.5 w-3.5" />
            </Button>
            {!t.builtin && (
              <>
                <Button variant="ghost" size="icon" className="h-7 w-7" title="Edit" onClick={onEdit}>
                  <Pencil className="h-3.5 w-3.5" />
                </Button>
                <Button variant="ghost" size="icon" className="h-7 w-7 text-danger" title="Delete" onClick={onDelete}>
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

/* ------------------------------------------------------------------ */
/*  Detail dialog — group/metric browser + device attachment           */
/* ------------------------------------------------------------------ */

function TemplateDetailDialog({ template: t, onOpenChange, onClone }: {
  template: Template; onOpenChange: (o: boolean) => void; onClone: () => void
}) {
  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[88vh] overflow-y-auto">
        <DialogHeader>
          <div className="flex items-center gap-3">
            <VendorGlyph vendor={t.vendor} />
            <div>
              <DialogTitle className="flex items-center gap-2">
                {t.name}
                {t.builtin && <Badge variant="info" className="gap-1"><Lock className="h-2.5 w-2.5" />Built-in</Badge>}
              </DialogTitle>
              <DialogDescription>{t.vendor || 'Custom template'} · v{t.version}</DialogDescription>
            </div>
            <Button variant="outline" size="sm" className="ml-auto" onClick={onClone}>
              <Copy className="h-3.5 w-3.5" /> Clone
            </Button>
          </div>
        </DialogHeader>

        {t.description && <p className="text-xs leading-relaxed text-muted">{t.description}</p>}

        <Tabs defaultValue="groups">
          <TabsList>
            <TabsTrigger value="groups">Metric Groups ({(t.oid_groups || []).length})</TabsTrigger>
            <TabsTrigger value="devices">Devices ({t.device_count})</TabsTrigger>
            <TabsTrigger value="match">Auto-Match</TabsTrigger>
          </TabsList>

          <TabsContent value="groups" className="space-y-2">
            {(t.oid_groups || []).map((g) => <GroupAccordion key={g.key} group={g} />)}
          </TabsContent>

          <TabsContent value="devices">
            <TemplateDevices template={t} />
          </TabsContent>

          <TabsContent value="match" className="space-y-3">
            <MatchRulesView rules={t.match_rules} />
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}

function GroupAccordion({ group: g }: { group: TemplateGroup }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-lg border border-border">
      <button type="button" onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left hover:bg-surface2">
        {open ? <ChevronDown className="h-3.5 w-3.5 text-muted" /> : <ChevronRight className="h-3.5 w-3.5 text-muted" />}
        <span className="text-sm font-medium">{g.name}</span>
        <Badge variant={g.kind === 'table' ? 'warning' : 'default'} className="text-[10px]">
          {g.kind === 'table' ? 'table' : 'scalars'}
        </Badge>
        <span className="ml-auto text-[11px] text-muted">{g.metrics.length} metrics</span>
      </button>
      {open && (
        <div className="border-t border-border px-3 pb-3">
          {g.description && <p className="py-2 text-[11px] leading-relaxed text-muted">{g.description}</p>}
          <Table>
            <THead>
              <Tr>
                <Th>Metric</Th><Th>OID</Th><Th>Type</Th><Th>Unit</Th><Th>Thresholds / States</Th>
              </Tr>
            </THead>
            <TBody>
              {g.metrics.map((m) => (
                <Tr key={m.key}>
                  <Td className="whitespace-nowrap text-xs font-medium">{m.name}</Td>
                  <Td className="max-w-[220px] truncate font-mono text-[10px] text-muted" title={m.oid}>{m.oid}</Td>
                  <Td><span className={cn('rounded px-1.5 py-0.5 text-[10px] font-medium', typeBadge[m.type] || typeBadge.gauge)}>{m.type}</span></Td>
                  <Td className="text-xs text-muted">{m.unit || '—'}</Td>
                  <Td className="text-[11px] text-muted">
                    {m.thresholds && (m.thresholds.warn != null || m.thresholds.crit != null) ? (
                      <span>
                        {m.thresholds.warn != null && <span className="text-warning">warn {m.thresholds.op || '≥'} {m.thresholds.warn}</span>}
                        {m.thresholds.warn != null && m.thresholds.crit != null && ' · '}
                        {m.thresholds.crit != null && <span className="text-danger">crit {m.thresholds.op || '≥'} {m.thresholds.crit}</span>}
                      </span>
                    ) : m.labels ? (
                      <span className="line-clamp-1">
                        {Object.entries(m.labels).map(([c, l]) => `${c}=${l.text}`).join(', ')}
                      </span>
                    ) : '—'}
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </div>
      )}
    </div>
  )
}

function MatchRulesView({ rules }: { rules: any }) {
  const prefixes: string[] = rules?.sys_object_id_prefixes || []
  return (
    <div className="space-y-3 text-xs">
      <p className="text-muted">
        Devices whose SNMP identity matches these rules get this template attached automatically
        (unless an operator picked one explicitly).
      </p>
      <div className="rounded-lg border border-border p-3">
        <div className="mb-1.5 font-semibold">sysObjectID prefixes</div>
        {prefixes.length ? prefixes.map((p) => (
          <div key={p} className="font-mono text-[11px] text-muted">.{p}</div>
        )) : <div className="text-muted">—</div>}
      </div>
      <div className="rounded-lg border border-border p-3">
        <div className="mb-1.5 font-semibold">sysDescr pattern</div>
        <div className="font-mono text-[11px] text-muted">{rules?.sys_descr_regex || '—'}</div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Devices tab — attached list + assign                               */
/* ------------------------------------------------------------------ */

function TemplateDevices({ template: t }: { template: Template }) {
  const qc = useQueryClient()
  const [assignOpen, setAssignOpen] = useState(false)

  const { data: attached } = useQuery<{ data: any[] }>({
    queryKey: ['snmp-profiles', t.id, 'devices'],
    queryFn: async () => (await api.get(`/snmp/profiles/${t.id}/devices`)).data,
  })

  const unassign = useMutation({
    mutationFn: async (deviceId: string) =>
      api.post('/snmp/profiles/unassign', { device_ids: [deviceId] }),
    onSuccess: () => {
      toast.success('Template detached')
      qc.invalidateQueries({ queryKey: ['snmp-profiles'] })
      qc.invalidateQueries({ queryKey: ['devices'] })
    },
    onError: (e: any) => toast.error('Detach failed', apiErrorMessage(e)),
  })

  const rows = attached?.data || []
  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button size="sm" onClick={() => setAssignOpen(true)}><Plus className="h-3.5 w-3.5" /> Assign devices</Button>
      </div>
      {rows.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border p-6 text-center text-xs text-muted">
          No devices attached yet. Assign devices manually, or let auto-detection pick this
          template up on the next poll of a matching device.
        </div>
      ) : (
        <Table>
          <THead><Tr><Th>Device</Th><Th>IP</Th><Th>Type</Th><Th>Status</Th><Th /></Tr></THead>
          <TBody>
            {rows.map((d) => (
              <Tr key={d.id}>
                <Td className="text-xs font-medium">{d.hostname}</Td>
                <Td className="font-mono text-[11px] text-muted">{d.ip_address}</Td>
                <Td className="text-xs text-muted">{d.device_type}</Td>
                <Td>
                  <Badge variant={d.status === 'up' || d.status === 'healthy' ? 'success' : d.status === 'unknown' ? 'outline' : 'danger'}>
                    {d.status}
                  </Badge>
                </Td>
                <Td className="text-right">
                  <Button variant="ghost" size="icon" className="h-7 w-7" title="Detach template"
                    onClick={() => unassign.mutate(d.id)}>
                    <Unplug className="h-3.5 w-3.5" />
                  </Button>
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      )}
      {assignOpen && (
        <AssignDevicesDialog template={t} onOpenChange={(o) => !o && setAssignOpen(false)} />
      )}
    </div>
  )
}

function AssignDevicesDialog({ template: t, onOpenChange }: {
  template: Template; onOpenChange: (o: boolean) => void
}) {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [filter, setFilter] = useState('')

  const { data } = useQuery<{ data: any[] }>({
    queryKey: ['devices', 'for-assign'],
    queryFn: async () => (await api.get('/devices?limit=200')).data,
  })

  const assign = useMutation({
    mutationFn: async () =>
      api.post(`/snmp/profiles/${t.id}/assign`, { device_ids: Array.from(selected) }),
    onSuccess: () => {
      toast.success(`Template assigned to ${selected.size} device(s)`)
      qc.invalidateQueries({ queryKey: ['snmp-profiles'] })
      qc.invalidateQueries({ queryKey: ['devices'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Assign failed', apiErrorMessage(e)),
  })

  const devices = (data?.data || [])
    .filter((d) => d.snmp_enabled)
    .filter((d) => !filter || `${d.hostname} ${d.ip_address}`.toLowerCase().includes(filter.toLowerCase()))

  const toggle = (id: string) => setSelected((s) => {
    const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n
  })

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Assign “{t.name}”</DialogTitle>
          <DialogDescription>SNMP-enabled devices only. Assigning overrides auto-detection.</DialogDescription>
        </DialogHeader>
        <Input placeholder="Filter devices…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <div className="max-h-72 space-y-1 overflow-y-auto rounded-md border border-border p-2">
          {devices.length === 0 && (
            <div className="p-4 text-center text-xs text-muted">No SNMP-enabled devices found.</div>
          )}
          {devices.map((d) => (
            <label key={d.id} className="flex cursor-pointer items-center gap-2.5 rounded-md px-2 py-1.5 hover:bg-surface2">
              <input type="checkbox" className="accent-[rgb(var(--primary))]"
                checked={selected.has(d.id)} onChange={() => toggle(d.id)} />
              <span className="text-xs font-medium">{d.hostname}</span>
              <span className="font-mono text-[10px] text-muted">{d.ip_address}</span>
              {d.profile_name && d.profile_id !== t.id && (
                <Badge variant="outline" className="ml-auto text-[9px]">{d.profile_name}</Badge>
              )}
              {d.profile_id === t.id && (
                <CheckCircle2 className="ml-auto h-3.5 w-3.5 text-success" />
              )}
            </label>
          ))}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={selected.size === 0 || assign.isPending} onClick={() => assign.mutate()}>
            {assign.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            Assign {selected.size > 0 && `(${selected.size})`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ------------------------------------------------------------------ */
/*  Editor dialog — create / edit custom templates                     */
/* ------------------------------------------------------------------ */

function TemplateEditorDialog({ open, template, onOpenChange }: {
  open: boolean; template?: Template; onOpenChange: (o: boolean) => void
}) {
  const qc = useQueryClient()
  const isEdit = !!template
  const [name, setName] = useState(template?.name || '')
  const [vendor, setVendor] = useState(template?.vendor || '')
  const [description, setDescription] = useState(template?.description || '')
  const [prefixes, setPrefixes] = useState((template?.match_rules?.sys_object_id_prefixes || []).join('\n'))
  const [descrRegex, setDescrRegex] = useState(template?.match_rules?.sys_descr_regex || '')
  const [groupsJson, setGroupsJson] = useState(
    JSON.stringify(template?.oid_groups || [], null, 2),
  )
  const [jsonError, setJsonError] = useState<string | null>(null)

  const save = useMutation({
    mutationFn: async () => {
      let groups: any
      try {
        groups = JSON.parse(groupsJson || '[]')
        if (!Array.isArray(groups)) throw new Error('must be a JSON array of groups')
      } catch (e: any) {
        setJsonError(e.message)
        throw new Error('invalid-json')
      }
      const payload = {
        name, vendor: vendor || null, description: description || null,
        match_rules: {
          sys_object_id_prefixes: prefixes.split('\n').map((s: string) => s.trim().replace(/^\./, '')).filter(Boolean),
          sys_descr_regex: descrRegex || null,
        },
        oid_groups: groups,
      }
      if (isEdit) return (await api.put(`/snmp/profiles/${template!.id}`, payload)).data
      return (await api.post('/snmp/profiles', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Template updated' : 'Template created', 'The poller applies changes within a minute')
      qc.invalidateQueries({ queryKey: ['snmp-profiles'] })
      onOpenChange(false)
    },
    onError: (e: any) => {
      if (e?.message !== 'invalid-json') toast.error('Save failed', apiErrorMessage(e))
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[88vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit "${template!.name}"` : 'New monitoring template'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'Changes go live on attached devices within one polling sync.'
              : 'Tip: the fastest path to a custom template is cloning a built-in one and trimming it.'}
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-3">
          <FormField label="Name" required>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="My FortiGate (branch)" />
          </FormField>
          <FormField label="Vendor">
            <Input value={vendor} onChange={(e) => setVendor(e.target.value)} placeholder="Fortinet" />
          </FormField>
          <FormField label="Description" className="col-span-2">
            <Textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
          </FormField>
          <FormField label="Auto-match sysObjectID prefixes" hint="One per line, e.g. 1.3.6.1.4.1.12356.101.1">
            <Textarea rows={3} className="font-mono text-xs" value={prefixes} onChange={(e) => setPrefixes(e.target.value)} />
          </FormField>
          <FormField label="Auto-match sysDescr regex" hint="Fallback when no prefix matches">
            <Textarea rows={3} className="font-mono text-xs" value={descrRegex} onChange={(e) => setDescrRegex(e.target.value)} />
          </FormField>
          <FormField label="OID groups (JSON)" className="col-span-2"
            hint='[{ "key", "name", "kind": "scalar|table", "table": {"label_oid"}, "metrics": [{ "key", "name", "oid", "type": "gauge|counter|enum|string", "unit", "scale", "value_map", "labels", "thresholds" }] }]'>
            <Textarea rows={14} className="font-mono text-[11px] leading-relaxed"
              value={groupsJson}
              onChange={(e) => { setGroupsJson(e.target.value); setJsonError(null) }} />
          </FormField>
        </div>
        {jsonError && <p className="text-xs text-danger">JSON error: {jsonError}</p>}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={!name.trim() || save.isPending} onClick={() => save.mutate()}>
            {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
            {isEdit ? 'Save changes' : 'Create template'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
