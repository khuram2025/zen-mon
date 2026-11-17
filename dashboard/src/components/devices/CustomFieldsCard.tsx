import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil, Save, Tags } from 'lucide-react'
import { api } from '@/lib/api'
import { useCan } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'

type Field = {
  key: string
  label: string
  field_type: 'text' | 'number' | 'date' | 'choice' | 'boolean'
  choices: string[] | null
  asset_kinds: string[]
  description: string | null
}

/** Custom field values of one device or server (migrate-192), editable with devices.manage / servers.manage. */
export function CustomFieldsCard({ kind, id }: { kind: 'device' | 'server'; id: string }) {
  const qc = useQueryClient()
  const can = useCan()
  const canEdit = can(kind === 'device' ? 'devices.manage' : 'servers.manage')
  const fields = useQuery<Field[]>({ queryKey: ['custom-fields'], queryFn: async () => (await api.get('/custom-fields')).data.data, retry: false })
  const values = useQuery<Record<string, any>>({
    queryKey: ['custom-field-values', kind, id],
    queryFn: async () => (await api.get('/custom-fields/values', { params: { asset_kind: kind, asset_id: id } })).data.values,
    retry: false,
  })
  const [draft, setDraft] = useState<Record<string, any> | null>(null)
  const save = useMutation({
    mutationFn: async () => (await api.put('/custom-fields/values', { asset_kind: kind, asset_id: id, values: draft })).data.values,
    onSuccess: () => { toast.success('Custom fields saved'); setDraft(null); qc.invalidateQueries({ queryKey: ['custom-field-values', kind, id] }) },
    onError: (e: any) => toast.error('Not saved', e?.response?.data?.detail || e?.message),
  })
  const mine = (fields.data || []).filter((f) => f.asset_kinds.includes(kind))
  if (fields.isError) return null
  if (!mine.length) {
    return (
      <Card><CardContent className="p-4 text-sm text-muted">
        No custom fields are defined for {kind}s. Add them under <Link className="text-primary" to="/device-groups?tab=fields">Groups &amp; Fields</Link>.
      </CardContent></Card>
    )
  }
  const vals = values.data || {}
  return (
    <Card><CardContent className="space-y-2 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold"><Tags className="h-4 w-4 text-muted" /> Custom fields</div>
        {canEdit && !draft && <Button size="sm" variant="ghost" onClick={() => setDraft({ ...vals })}><Pencil className="h-3.5 w-3.5" /> Edit</Button>}
      </div>
      <div className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
        {mine.map((f) => (
          <div key={f.key} className="text-sm">
            <div className="text-xs text-muted" title={f.description || undefined}>{f.label}</div>
            {draft ? <ValueEditor field={f} value={draft[f.key]} onChange={(v) => setDraft({ ...draft, [f.key]: v })} />
              : <div>{display(f, vals[f.key])}</div>}
          </div>
        ))}
      </div>
      {draft && (
        <div className="flex gap-2">
          <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}><Save className="h-3.5 w-3.5" /> Save</Button>
          <Button size="sm" variant="outline" onClick={() => setDraft(null)}>Cancel</Button>
        </div>
      )}
      <div className="text-xs text-muted">Fields are defined under <Link className="text-primary" to="/device-groups?tab=fields">Groups &amp; Fields</Link>.</div>
    </CardContent></Card>
  )
}

function display(f: Field, v: any): string {
  if (v === null || v === undefined || v === '') return '—'
  if (f.field_type === 'boolean') return v ? 'Yes' : 'No'
  return String(v)
}

function ValueEditor({ field, value, onChange }: { field: Field; value: any; onChange: (v: any) => void }) {
  if (field.field_type === 'choice' || field.field_type === 'boolean') {
    const opts = field.field_type === 'boolean' ? [['true', 'Yes'], ['false', 'No']] : (field.choices || []).map((c) => [c, c])
    const current = value === null || value === undefined || value === '' ? '__none__' : String(value)
    return (
      <Select value={current} onValueChange={(v) => onChange(v === '__none__' ? null : field.field_type === 'boolean' ? v === 'true' : v)}>
        <SelectTrigger className="h-8"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="__none__">Not set</SelectItem>
          {opts.map(([k, l]) => <SelectItem key={k} value={k}>{l}</SelectItem>)}
        </SelectContent>
      </Select>
    )
  }
  return (
    <Input className="h-8" type={field.field_type === 'number' ? 'number' : field.field_type === 'date' ? 'date' : 'text'}
           value={value ?? ''} onChange={(e) => onChange(e.target.value === '' ? null : e.target.value)} />
  )
}
