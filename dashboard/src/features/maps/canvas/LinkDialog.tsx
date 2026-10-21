import { useEffect, useState } from 'react'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { NetworkIcon } from '@/components/network-icons'
import { iconForNode, nodeKindOf, type LinkKind, type LinkShape, type ManualMapNode } from '../core'
import { Field, IfaceSelect, PortText, SPEEDS, findIface, inputCls, speedLabel, useInterfaces } from './IfacePicker'

const KINDS: LinkKind[] = ['ethernet', 'fiber', 'trunk', 'wireless', 'vpn', 'serial']
const SHAPES: { value: LinkShape; label: string }[] = [
  { value: 'curve', label: 'Curved' },
  { value: 'straight', label: 'Straight' },
  { value: 'orthogonal', label: 'Orthogonal' },
]

export type NewLink = {
  source_node_id: string
  target_node_id: string
  label?: string | null
  link_type: string
  metadata: { kind: LinkKind; shape: LinkShape; src_interface?: string | null; dst_interface?: string | null; speed?: string | null; notes?: string | null }
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
  // Straight by default: a curve between distant devices can sweep behind
  // other tiles and read as a connection to them.
  const [shape, setShape] = useState<LinkShape>('straight')
  const [label, setLabel] = useState('')
  const [speed, setSpeed] = useState('')
  const [notes, setNotes] = useState('')

  const srcIfaces = useInterfaces(nodeKindOf(source) === 'device' ? source.device_id : null)
  const dstIfaces = useInterfaces(nodeKindOf(target) === 'device' ? target.device_id : null)

  // Suggest speed and type from the chosen ports (the operator can override).
  const srcMatch = findIface(srcIfaces.data, srcIf)
  const dstMatch = findIface(dstIfaces.data, dstIf)
  useEffect(() => {
    const bps = Math.max(srcMatch?.if_speed || 0, dstMatch?.if_speed || 0)
    if (bps) setSpeed((cur) => cur || speedLabel(bps))
  }, [srcMatch?.if_speed, dstMatch?.if_speed])

  const submit = () => onCreate({
    source_node_id: source.id,
    target_node_id: target.id,
    label: label || null,
    link_type: kind,
    metadata: {
      kind, shape,
      src_interface: (srcMatch?.if_name || srcIf) || null,
      dst_interface: (dstMatch?.if_name || dstIf) || null,
      speed: speed || null,
      notes: notes.trim() || null,
    },
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
          {nodeKindOf(source) === 'device'
            ? <IfaceSelect label={`${source.hostname} interface`} loading={srcIfaces.isLoading} ifaces={srcIfaces.data} value={srcIf} onChange={setSrcIf} />
            : <PortText label={`${source.hostname} port`} value={srcIf} onChange={setSrcIf} />}
          {nodeKindOf(target) === 'device'
            ? <IfaceSelect label={`${target.hostname} interface`} loading={dstIfaces.isLoading} ifaces={dstIfaces.data} value={dstIf} onChange={setDstIf} />
            : <PortText label={`${target.hostname} port`} value={dstIf} onChange={setDstIf} />}
        </div>

        <div className="grid grid-cols-3 gap-3">
          <Field label="Type">
            <select className={inputCls} value={kind} onChange={(e) => setKind(e.target.value as LinkKind)}>
              {KINDS.map((k) => <option key={k} value={k}>{k[0].toUpperCase() + k.slice(1)}</option>)}
            </select>
          </Field>
          <Field label="Speed">
            <select className={inputCls} value={speed} onChange={(e) => setSpeed(e.target.value)}>
              {SPEEDS.map((v) => <option key={v} value={v}>{v || '—'}</option>)}
            </select>
          </Field>
          <Field label="Shape">
            <select className={inputCls} value={shape} onChange={(e) => setShape(e.target.value as LinkShape)}>
              {SHAPES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
            </select>
          </Field>
        </div>

        <Field label="Label (optional)">
          <input className={inputCls} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. uplink" />
        </Field>
        <Field label="Notes (optional)">
          <input className={inputCls} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="e.g. circuit ID, patch panel, how this link was verified" />
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
