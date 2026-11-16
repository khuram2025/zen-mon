import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Braces, ChevronDown, ChevronRight, Lock, Plus, Search, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Textarea } from '@/components/ui/Textarea'
import type { TemplateGroup, TemplateMetric } from './api'

const KEY_RE = /^[a-z0-9][a-z0-9_]{0,62}$/
const OID_RE = /^\.?\d+(\.\d+)+$/

/** Edits the effective metric groups of a template. In a customised template the
 *  groups and metrics inherited from the parent keep their OIDs and structure (the
 *  server stores only the differences); they can be renamed, re-timed, given other
 *  thresholds or turned off, and new groups and metrics can be added. */
export function GroupEditor({
  groups, onChange, readOnly, inherited,
}: {
  groups: TemplateGroup[]
  onChange: (g: TemplateGroup[]) => void
  readOnly: boolean
  inherited?: TemplateGroup[] | null
}) {
  const [open, setOpen] = useState<Set<string>>(new Set())
  const parent = new Map((inherited || []).map((g) => [g.key, g]))
  const disabled = (inherited || []).filter((g) => !groups.some((x) => x.key === g.key))

  const update = (i: number, g: TemplateGroup) => onChange(groups.map((x, j) => (j === i ? g : x)))
  const remove = (i: number) => onChange(groups.filter((_, j) => j !== i))
  const toggle = (k: string) => {
    const n = new Set(open); n.has(k) ? n.delete(k) : n.add(k); setOpen(n)
  }
  const addGroup = () => {
    let n = 1
    while (groups.some((g) => g.key === `group_${n}`)) n++
    const key = `group_${n}`
    onChange([...groups, { key, name: `New group ${n}`, kind: 'scalar', interval_seconds: 0, metrics: [] }])
    setOpen(new Set([...open, key]))
  }

  return (
    <div className="space-y-2">
      {groups.map((g, i) => {
        const base = parent.get(g.key)
        const isOpen = open.has(g.key)
        const hidden = g.display === 'children'
        return (
          <div key={`${g.key}-${i}`} className="rounded-md border border-border">
            <div className="flex items-center gap-2 px-3 py-2">
              <button type="button" onClick={() => toggle(g.key)} className="flex flex-1 items-center gap-2 text-left">
                {isOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                <span className="font-medium">{g.name}</span>
                <span className="font-mono text-xs text-muted">{g.key}</span>
                <Badge variant="outline">{g.kind || 'scalar'}</Badge>
                <span className="text-xs text-muted">{g.metrics.length} metric(s)</span>
                {g.interval_seconds ? <span className="text-xs text-muted">every {g.interval_seconds}s</span> : null}
                {base && <Badge variant="outline"><Lock className="h-3 w-3" />inherited</Badge>}
                {hidden && <Badge variant="info">shown on managed children</Badge>}
              </button>
              {!readOnly && (
                <Button size="icon" variant="ghost" title={base ? 'Turn this group off' : 'Remove group'}
                        onClick={() => remove(i)}><Trash2 className="h-4 w-4" /></Button>
              )}
            </div>
            {isOpen && <GroupBody g={g} base={base} readOnly={readOnly} onChange={(ng) => update(i, ng)}
                                  takenKeys={groups.filter((_, j) => j !== i).map((x) => x.key)} />}
          </div>
        )
      })}
      {disabled.length > 0 && (
        <div className="rounded-md border border-dashed border-border p-3 text-sm">
          <div className="mb-1 text-xs font-medium text-muted">Turned off (inherited from the parent template)</div>
          <div className="flex flex-wrap gap-2">
            {disabled.map((g) => (
              <span key={g.key} className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs">
                {g.name}
                {!readOnly && (
                  <button type="button" className="text-primary hover:underline"
                          onClick={() => onChange([...groups, JSON.parse(JSON.stringify(g))])}>turn on</button>
                )}
              </span>
            ))}
          </div>
        </div>
      )}
      {!readOnly && <Button variant="outline" size="sm" onClick={addGroup}><Plus className="h-4 w-4" /> Add group</Button>}
    </div>
  )
}

function GroupBody({ g, base, readOnly, onChange, takenKeys }: {
  g: TemplateGroup; base?: TemplateGroup; readOnly: boolean; onChange: (g: TemplateGroup) => void; takenKeys: string[]
}) {
  const [json, setJson] = useState<string | null>(null)
  const [jsonError, setJsonError] = useState<string | null>(null)
  const locked = !!base || readOnly
  const keyError = !KEY_RE.test(g.key) ? 'lowercase letters, digits and _' : takenKeys.includes(g.key) ? 'already used' : null
  const baseMetrics = new Map((base?.metrics || []).map((m) => [m.key, m]))
  const offMetrics = (base?.metrics || []).filter((m) => !g.metrics.some((x) => x.key === m.key))
  const setMetric = (i: number, m: TemplateMetric) => onChange({ ...g, metrics: g.metrics.map((x, j) => (j === i ? m : x)) })

  if (json !== null) {
    return (
      <div className="space-y-2 border-t border-border p-3">
        <p className="text-xs text-muted">
          The whole group as JSON, for fields without a form (value maps, state labels, applicability, children
          mapping). In a customised template, inherited OIDs and table settings cannot change.
        </p>
        <Textarea className="font-mono text-xs" rows={18} value={json} onChange={(e) => setJson(e.target.value)} />
        {jsonError && <p className="text-xs text-danger">{jsonError}</p>}
        <div className="flex gap-2">
          <Button size="sm" onClick={() => {
            try {
              const parsed = JSON.parse(json)
              if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.metrics)) throw new Error('a group needs a metrics array')
              onChange(parsed); setJson(null); setJsonError(null)
            } catch (e: any) { setJsonError(e.message) }
          }}>Apply</Button>
          <Button size="sm" variant="outline" onClick={() => { setJson(null); setJsonError(null) }}>Cancel</Button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-3 border-t border-border p-3">
      <div className="grid gap-3 md:grid-cols-6">
        <FormField label="Name" className="md:col-span-2">
          <Input value={g.name} disabled={readOnly} onChange={(e) => onChange({ ...g, name: e.target.value })} />
        </FormField>
        <FormField label="Key" error={!locked ? keyError || undefined : undefined}>
          <Input className="font-mono" value={g.key} disabled={locked} onChange={(e) => onChange({ ...g, key: e.target.value })} />
        </FormField>
        <FormField label="Kind">
          <Select value={g.kind || 'scalar'} disabled={locked} onValueChange={(v) => onChange({ ...g, kind: v as 'scalar' | 'table' })}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="scalar">Scalar (GET)</SelectItem><SelectItem value="table">Table (walk)</SelectItem></SelectContent>
          </Select>
        </FormField>
        <FormField label="Interval (s)" hint="0 = every device poll">
          <Input type="number" min={0} value={g.interval_seconds || 0} disabled={readOnly}
                 onChange={(e) => onChange({ ...g, interval_seconds: Math.max(0, Number(e.target.value) || 0) })} />
        </FormField>
        <FormField label="Device page tab">
          <Input value={g.tab || ''} disabled={readOnly} placeholder="automatic"
                 onChange={(e) => onChange({ ...g, tab: e.target.value || null })} />
        </FormField>
      </div>
      {(g.kind || 'scalar') === 'table' && (
        <div className="grid gap-3 md:grid-cols-6">
          <FormField label="Row label OID" className="md:col-span-3">
            <OidInput value={g.table?.label_oid || ''} disabled={locked}
                      onChange={(v) => onChange({ ...g, table: { ...(g.table || {}), label_oid: v || null } })} />
          </FormField>
          <FormField label="Max rows per poll">
            <Input type="number" min={1} max={5000} value={g.table?.max_rows || ''} disabled={locked} placeholder="500"
                   onChange={(e) => onChange({ ...g, table: { ...(g.table || {}), max_rows: e.target.value ? Number(e.target.value) : null } })} />
          </FormField>
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead><tr className="text-left text-xs text-muted">
            <th className="py-1 pr-2">Key</th><th className="pr-2">Name</th><th className="pr-2">OID</th><th className="pr-2">Type</th>
            <th className="pr-2">Unit</th><th className="pr-2">Warn</th><th className="pr-2">Crit</th><th className="pr-2">Op</th><th />
          </tr></thead>
          <tbody>
            {g.metrics.map((m, i) => {
              const inheritedMetric = baseMetrics.has(m.key)
              const lockM = readOnly || inheritedMetric
              return (
                <tr key={`${m.key}-${i}`} className="align-top">
                  <td className="py-1 pr-2"><Input className={cn('h-8 font-mono text-xs', !KEY_RE.test(m.key) && 'border-danger')}
                    value={m.key} disabled={lockM} onChange={(e) => setMetric(i, { ...m, key: e.target.value })} /></td>
                  <td className="pr-2"><Input className="h-8" value={m.name} disabled={readOnly}
                    onChange={(e) => setMetric(i, { ...m, name: e.target.value })} /></td>
                  <td className="min-w-[14rem] pr-2"><OidInput small value={m.oid} disabled={lockM}
                    onChange={(v) => setMetric(i, { ...m, oid: v })} /></td>
                  <td className="pr-2">
                    <Select value={m.type || 'gauge'} disabled={lockM} onValueChange={(v) => setMetric(i, { ...m, type: v })}>
                      <SelectTrigger className="h-8 w-28"><SelectValue /></SelectTrigger>
                      <SelectContent>{['gauge', 'counter', 'enum', 'string'].map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}</SelectContent>
                    </Select>
                  </td>
                  <td className="pr-2"><Input className="h-8 w-20" value={m.unit || ''} disabled={readOnly}
                    onChange={(e) => setMetric(i, { ...m, unit: e.target.value || null })} /></td>
                  <td className="pr-2"><Input className="h-8 w-20" type="number" value={m.thresholds?.warn ?? ''} disabled={readOnly}
                    onChange={(e) => setMetric(i, withThreshold(m, 'warn', e.target.value))} /></td>
                  <td className="pr-2"><Input className="h-8 w-20" type="number" value={m.thresholds?.crit ?? ''} disabled={readOnly}
                    onChange={(e) => setMetric(i, withThreshold(m, 'crit', e.target.value))} /></td>
                  <td className="pr-2">
                    <Select value={m.thresholds?.op || '>='} disabled={readOnly || !m.thresholds}
                            onValueChange={(v) => setMetric(i, { ...m, thresholds: { ...(m.thresholds || {}), op: v } })}>
                      <SelectTrigger className="h-8 w-16"><SelectValue /></SelectTrigger>
                      <SelectContent><SelectItem value=">=">≥</SelectItem><SelectItem value="<=">≤</SelectItem></SelectContent>
                    </Select>
                  </td>
                  <td>{!readOnly && (
                    <Button size="icon" variant="ghost" title={inheritedMetric ? 'Turn this metric off' : 'Remove metric'}
                            onClick={() => onChange({ ...g, metrics: g.metrics.filter((_, j) => j !== i) })}>
                      <Trash2 className="h-4 w-4" /></Button>
                  )}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <p className="mt-1 text-xs text-muted">
          Warn / crit colour the reading on the device page. Alerts come from alert rules (metric <code>tpl_&lt;key&gt;</code>).
        </p>
      </div>
      {offMetrics.length > 0 && (
        <div className="text-xs text-muted">Turned off: {offMetrics.map((m) => (
          <span key={m.key} className="mr-2">{m.name}{!readOnly && (
            <button type="button" className="ml-1 text-primary hover:underline"
                    onClick={() => onChange({ ...g, metrics: [...g.metrics, JSON.parse(JSON.stringify(m))] })}>turn on</button>)}</span>
        ))}</div>
      )}
      <div className="flex gap-2">
        {!readOnly && (
          <Button size="sm" variant="outline" onClick={() => {
            let n = 1
            while (g.metrics.some((m) => m.key === `${g.key}_m${n}`)) n++
            onChange({ ...g, metrics: [...g.metrics, { key: `${g.key}_m${n}`.slice(0, 63), name: `Metric ${n}`, oid: '', type: 'gauge' }] })
          }}><Plus className="h-4 w-4" /> Add metric</Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => setJson(JSON.stringify(g, null, 2))}>
          <Braces className="h-4 w-4" /> {readOnly ? 'View JSON' : 'Edit as JSON'}
        </Button>
      </div>
    </div>
  )
}

function withThreshold(m: TemplateMetric, f: 'warn' | 'crit', raw: string): TemplateMetric {
  const t = { op: '>=', ...(m.thresholds || {}) } as Record<string, any>
  if (raw === '') delete t[f]; else t[f] = Number(raw)
  const has = t.warn !== undefined && t.warn !== null || t.crit !== undefined && t.crit !== null
  return { ...m, thresholds: has ? t : null }
}

function OidInput({ value, onChange, disabled, small }: {
  value: string; onChange: (v: string) => void; disabled?: boolean; small?: boolean
}) {
  const [pick, setPick] = useState(false)
  const bad = value !== '' && !OID_RE.test(value)
  return (
    <div className="flex gap-1">
      <Input className={cn('font-mono text-xs', small && 'h-8', bad && 'border-danger')} value={value} disabled={disabled}
             placeholder="1.3.6.1.4.1…" onChange={(e) => onChange(e.target.value.trim())} />
      {!disabled && (
        <Button type="button" size="icon" variant="ghost" className={small ? 'h-8 w-8' : ''} title="Find in the MIB Library"
                onClick={() => setPick(true)}><Search className="h-4 w-4" /></Button>
      )}
      {pick && <MibPicker onPick={(oid) => { onChange(oid); setPick(false) }} onClose={() => setPick(false)} />}
    </div>
  )
}

function MibPicker({ onPick, onClose }: { onPick: (oid: string) => void; onClose: () => void }) {
  const [q, setQ] = useState('')
  const res = useQuery({
    queryKey: ['mib-objects', q],
    queryFn: async () => (await api.get('/snmp/mibs/objects', { params: { search: q } })).data,
    enabled: q.trim().length >= 2,
    retry: false,
  })
  const err = (res.error as any)?.response?.data?.detail
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader><DialogTitle>Find an OID in the MIB Library</DialogTitle></DialogHeader>
        <Input autoFocus placeholder="Symbol, module or OID (at least 2 characters)" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="max-h-96 overflow-y-auto rounded-md border border-border">
          {err && <p className="p-3 text-sm text-muted">{String(err)} (Administration → Settings → MIB Library)</p>}
          {(res.data?.data || []).map((o: any) => (
            <button key={`${o.module}::${o.symbol}`} type="button" onClick={() => onPick(String(o.oid).replace(/^\./, ''))}
                    className="flex w-full items-baseline justify-between gap-3 border-b border-border px-3 py-1.5 text-left text-sm hover:bg-surface2">
              <span><span className="font-medium">{o.symbol}</span> <span className="text-xs text-muted">{o.module}</span></span>
              <span className="font-mono text-xs text-muted">{o.oid}</span>
            </button>
          ))}
          {res.data && (res.data.data || []).length === 0 && <p className="p-3 text-sm text-muted">No objects match.</p>}
        </div>
        <DialogFooter><Button variant="outline" onClick={onClose}>Close</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
