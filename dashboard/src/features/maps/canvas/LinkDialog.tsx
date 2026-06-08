import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { NetworkIcon } from '@/components/network-icons'
import { iconForNode, type LinkKind, type LinkShape, type ManualMapNode } from '../core'

type DeviceInterface = { if_index: number; if_name: string | null; if_descr: string | null; if_alias: string | null }

const KINDS: LinkKind[] = ['ethernet', 'fiber', 'trunk', 'wireless', 'vpn', 'serial']
const SHAPES: { value: LinkShape; label: string }[] = [
  { value: 'curve', label: 'Curved' },
  { value: 'straight', label: 'Straight' },
  { value: 'orthogonal', label: 'Orthogonal' },
]

function useInterfaces(deviceId: string | undefined) {
  return useQuery<DeviceInterface[]>({
    queryKey: ['device-interfaces', deviceId],
    enabled: !!deviceId,
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces`)).data,
  })
}

export type NewLink = {
  source_node_id: string
  target_node_id: string
  label?: string | null
  link_type: string
  metadata: { kind: LinkKind; shape: LinkShape; src_interface?: string | null; dst_interface?: string | null }
}

export function LinkDialog({ source, target, onCancel, onCreate, saving }: {
  source: ManualMapNode
  target: ManualMapNode
  onCancel: () => void
  onCreate: (link: NewLink) => void
  saving: boolean
}) {
  const [srcIf, setSrcIf] = useState('')
  const [dstIf, setDstIf] = useState('')
  const [kind, setKind] = useState<LinkKind>('ethernet')
  const [shape, setShape] = useState<LinkShape>('curve')
  const [label, setLabel] = useState('')

  const srcIfaces = useInterfaces(source.device_id)
  const dstIfaces = useInterfaces(target.device_id)

  const submit = () => onCreate({
    source_node_id: source.id,
    target_node_id: target.id,
    label: label || null,
    link_type: kind,
    metadata: { kind, shape, src_interface: srcIf || null, dst_interface: dstIf || null },
  })

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onCancel() }}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>New link</DialogTitle></DialogHeader>

        <div className="flex items-center justify-between gap-2 rounded-lg border border-border bg-surface2/40 p-2 text-xs">
          <Endpoint node={source} />
          <span className="text-muted">—</span>
          <Endpoint node={target} />
        </div>

        <div className="grid grid-cols-2 gap-3">
          <IfaceSelect label={`${source.hostname} interface`} loading={srcIfaces.isLoading} ifaces={srcIfaces.data} value={srcIf} onChange={setSrcIf} />
          <IfaceSelect label={`${target.hostname} interface`} loading={dstIfaces.isLoading} ifaces={dstIfaces.data} value={dstIf} onChange={setDstIf} />
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Type">
            <select className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60" value={kind} onChange={(e) => setKind(e.target.value as LinkKind)}>
              {KINDS.map((k) => <option key={k} value={k}>{k[0].toUpperCase() + k.slice(1)}</option>)}
            </select>
          </Field>
          <Field label="Shape">
            <select className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60" value={shape} onChange={(e) => setShape(e.target.value as LinkShape)}>
              {SHAPES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
            </select>
          </Field>
        </div>

        <Field label="Label (optional)">
          <input className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. uplink" />
        </Field>

        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={saving}>Cancel</Button>
          <Button onClick={submit} disabled={saving}>{saving ? 'Creating…' : 'Create link'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Endpoint({ node }: { node: ManualMapNode }) {
  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <NetworkIcon name={iconForNode(node)} className="h-5 w-5 shrink-0" />
      <span className="truncate font-semibold text-text">{node.label || node.hostname}</span>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs">
      <span className="font-medium text-muted">{label}</span>
      {children}
    </label>
  )
}

function IfaceSelect({ label, loading, ifaces, value, onChange }: {
  label: string; loading: boolean; ifaces?: DeviceInterface[]; value: string; onChange: (v: string) => void
}) {
  return (
    <Field label={label}>
      <select className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60" value={value} onChange={(e) => onChange(e.target.value)} disabled={loading}>
        <option value="">{loading ? 'Loading…' : '— none —'}</option>
        {(ifaces || []).map((i) => {
          const name = i.if_name || i.if_descr || `if${i.if_index}`
          return <option key={i.if_index} value={name}>{name}{i.if_alias ? ` · ${i.if_alias}` : ''}</option>
        })}
      </select>
    </Field>
  )
}
