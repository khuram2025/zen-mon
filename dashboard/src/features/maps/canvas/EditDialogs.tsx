import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { NetworkIcon, iconLabel } from '@/components/network-icons'
import { cn } from '@/lib/utils'
import {
  PALETTE_ICONS,
  DEFAULT_ICON_FILL,
  iconForNode,
  linkKindOf,
  linkShapeOf,
  type LinkKind,
  type LinkShape,
  type ManualMapLink,
  type ManualMapNode,
} from '../core'
import { useDevices } from '../useMapData'

type DeviceInterface = { if_index: number; if_name: string | null; if_descr: string | null; if_alias: string | null }

const KINDS: LinkKind[] = ['ethernet', 'fiber', 'trunk', 'wireless', 'vpn', 'serial']
const SHAPES: { value: LinkShape; label: string }[] = [
  { value: 'curve', label: 'Curved' },
  { value: 'straight', label: 'Straight' },
  { value: 'orthogonal', label: 'Orthogonal' },
]

const inputCls = 'w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60'

function useInterfaces(deviceId: string | undefined) {
  return useQuery<DeviceInterface[]>({
    queryKey: ['device-interfaces', deviceId],
    enabled: !!deviceId,
    queryFn: async () => (await api.get(`/devices/${deviceId}/interfaces`)).data,
  })
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs">
      <span className="font-medium text-muted">{label}</span>
      {children}
    </label>
  )
}

/* ── Edit a placed device (label text + styling + icon + size) ───── */
const NODE_FONTS = ['Inter, system-ui, sans-serif', 'Georgia, serif', 'monospace']
const NODE_FONT_LABEL: Record<string, string> = { 'Inter, system-ui, sans-serif': 'Sans', 'Georgia, serif': 'Serif', 'monospace': 'Mono' }

export function NodeEditDialog({ node, onCancel, onSave, saving }: {
  node: ManualMapNode
  onCancel: () => void
  onSave: (patch: { label: string | null; icon: string; metadata: Record<string, unknown>; device_id?: string }) => void
  saving: boolean
}) {
  const md = node.metadata || {}
  const ls0 = md.label_style || {}
  const [label, setLabel] = useState(node.label || '')
  const [icon, setIcon] = useState(node.icon || 'auto')
  const [color, setColor] = useState(ls0.color || '#e5e7eb')
  const [font, setFont] = useState(ls0.fontFamily || NODE_FONTS[0])
  const [fontSize, setFontSize] = useState(ls0.fontSize || 11)
  const [bold, setBold] = useState(ls0.bold !== false)
  const [scale, setScale] = useState(md.size_scale || 1)
  const [iconFill, setIconFill] = useState(md.icon_fill ?? DEFAULT_ICON_FILL)
  const [frame, setFrame] = useState<'circle' | 'rounded'>(md.frame === 'rounded' ? 'rounded' : 'circle')
  const [deviceId, setDeviceId] = useState(node.device_id)
  const devices = useDevices()
  const deviceList = (devices.data?.data || []).slice().sort((a, b) => a.hostname.localeCompare(b.hostname))
  const selectedDev = deviceList.find((d) => d.id === deviceId)
  const profileChanged = deviceId !== node.device_id

  // On a profile swap, a "label" that merely mirrors the old hostname is not a
  // real override — clear it so the node shows the new device's hostname.
  const effectiveLabel = () => {
    const t = label.trim()
    if (!t) return null
    if (profileChanged && t === node.hostname) return null
    return t
  }

  const save = () => onSave({
    label: effectiveLabel(),
    icon,
    metadata: { ...md, size_scale: scale, icon_fill: iconFill, frame, label_style: { color, fontFamily: font, fontSize, bold } },
    ...(profileChanged ? { device_id: deviceId } : {}),
  })

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onCancel() }}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>Edit device</DialogTitle></DialogHeader>

        <div className="flex items-center gap-2 rounded-lg border border-border bg-surface2/40 p-2 text-xs">
          <NetworkIcon name={iconForNode({ ...node, icon })} className="h-6 w-6" />
          <div className="min-w-0">
            <div className="truncate font-semibold text-text">{selectedDev?.hostname || node.hostname}</div>
            <div className="truncate text-[10px] text-muted">{selectedDev?.ip_address || node.ip_address} · {selectedDev?.device_type || node.device_type}</div>
          </div>
        </div>

        <Field label="Device profile — swap which device this node represents">
          <select className={inputCls} value={deviceId} onChange={(e) => setDeviceId(e.target.value)} disabled={devices.isLoading}>
            {!deviceList.some((d) => d.id === node.device_id) && (
              <option value={node.device_id}>{node.hostname}</option>
            )}
            {deviceList.map((d) => (
              <option key={d.id} value={d.id}>{d.hostname} · {d.ip_address}</option>
            ))}
          </select>
        </Field>
        {profileChanged && (
          <div className="rounded-md border border-warning/40 bg-warning/10 px-2 py-1.5 text-[11px] text-warning">
            Links to this node are kept. Their interface labels still reference the old device —
            edit each link to re-map its interfaces to {selectedDev?.hostname || 'the new device'}.
          </div>
        )}

        <Field label="Display label (blank = hostname)">
          <input className={inputCls} value={label} onChange={(e) => setLabel(e.target.value)} placeholder={selectedDev?.hostname || node.hostname} autoFocus />
        </Field>

        <div className="grid grid-cols-3 gap-3">
          <Field label="Label colour">
            <input type="color" value={color.slice(0, 7)} onChange={(e) => setColor(e.target.value)} className="h-8 w-full cursor-pointer rounded border border-border bg-surface" />
          </Field>
          <Field label="Font">
            <select className={inputCls} value={font} onChange={(e) => setFont(e.target.value)}>
              {NODE_FONTS.map((f) => <option key={f} value={f}>{NODE_FONT_LABEL[f]}</option>)}
            </select>
          </Field>
          <Field label="Size">
            <input type="number" min={8} max={40} value={fontSize} onChange={(e) => setFontSize(Number(e.target.value) || 11)} className={inputCls} />
          </Field>
        </div>

        <div className="space-y-2">
          <div className="flex items-center gap-3">
            <button type="button" onClick={() => setBold((v) => !v)}
              className={cn('h-8 rounded border px-3 text-xs font-bold', bold ? 'border-primary bg-primary/15 text-primary' : 'border-border text-muted')}>B</button>
            <div className="flex flex-1 items-center gap-2">
              <span className="text-xs text-muted">Tile size</span>
              <input type="range" min={0.6} max={2.2} step={0.1} value={scale} onChange={(e) => setScale(Number(e.target.value))} className="flex-1 accent-primary" />
              <span className="w-10 text-right text-xs tabular-nums text-muted">{Math.round(scale * 100)}%</span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className="w-8 text-xs text-muted" />
            <div className="flex flex-1 items-center gap-2">
              <span className="text-xs text-muted">Icon fill</span>
              <input type="range" min={0.45} max={0.95} step={0.05} value={iconFill} onChange={(e) => setIconFill(Number(e.target.value))} className="flex-1 accent-primary" />
              <span className="w-10 text-right text-xs tabular-nums text-muted">{Math.round(iconFill * 100)}%</span>
            </div>
          </div>
          <div className="flex justify-center">
            <div
              className={cn(
                'relative flex items-center justify-center border-2 border-primary/40 bg-surface shadow-sm',
                frame === 'rounded' ? 'rounded-lg' : 'rounded-full',
              )}
              style={{ width: 64 * scale, height: 64 * scale }}
            >
              <NetworkIcon
                name={iconForNode({ ...node, icon })}
                style={{ width: 64 * scale * iconFill, height: 64 * scale * iconFill }}
              />
            </div>
          </div>
        </div>

        <Field label="Frame">
          <div className="flex gap-1">
            <button type="button" onClick={() => setFrame('circle')}
              className={cn('flex items-center gap-1.5 rounded border px-2 py-1 text-xs', frame === 'circle' ? 'border-primary bg-primary/15 text-primary' : 'border-border text-muted hover:border-primary/45')}>
              <span className="h-3.5 w-3.5 rounded-full border-2 border-current" /> Circle
            </button>
            <button type="button" onClick={() => setFrame('rounded')}
              className={cn('flex items-center gap-1.5 rounded border px-2 py-1 text-xs', frame === 'rounded' ? 'border-primary bg-primary/15 text-primary' : 'border-border text-muted hover:border-primary/45')}>
              <span className="h-3.5 w-3.5 rounded-[4px] border-2 border-current" /> Rounded
            </button>
          </div>
        </Field>

        <Field label="Icon">
          <div className="grid grid-cols-8 gap-1">
            <button
              type="button"
              title="Auto (by device type)"
              onClick={() => setIcon('auto')}
              className={cn('flex aspect-square items-center justify-center rounded border text-[9px] font-semibold uppercase',
                icon === 'auto' ? 'border-primary bg-primary/15 text-primary' : 'border-border bg-surface2/40 text-muted hover:border-primary/45')}
            >
              Auto
            </button>
            {PALETTE_ICONS.map((key) => (
              <button
                key={key}
                type="button"
                title={iconLabel[key]}
                onClick={() => setIcon(key)}
                className={cn('flex aspect-square items-center justify-center rounded border p-1',
                  icon === key ? 'border-primary bg-primary/15 text-primary' : 'border-border bg-surface2/40 text-text2 hover:border-primary/45')}
              >
                <NetworkIcon name={key} className="h-4 w-4" />
              </button>
            ))}
          </div>
        </Field>

        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={saving}>Cancel</Button>
          <Button onClick={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ── Edit an existing link (label, type, shape, interfaces) ──────── */
export function LinkEditDialog({ link, source, target, sourceLabel, targetLabel, onCancel, onSave, saving }: {
  link: ManualMapLink
  source: ManualMapNode | undefined
  target: ManualMapNode | undefined
  sourceLabel?: string
  targetLabel?: string
  onCancel: () => void
  onSave: (patch: { label: string | null; link_type: string; metadata: Record<string, unknown> }) => void
  saving: boolean
}) {
  const meta = link.metadata || {}
  const [label, setLabel] = useState(link.label || '')
  const [kind, setKind] = useState<LinkKind>(linkKindOf(link))
  const [shape, setShape] = useState<LinkShape>(linkShapeOf(link))
  const [srcIf, setSrcIf] = useState((meta as any).src_interface || '')
  const [dstIf, setDstIf] = useState((meta as any).dst_interface || '')
  const [widthScale, setWidthScale] = useState(Number((meta as any).width_scale) || 1)

  const srcIfaces = useInterfaces(source?.device_id)
  const dstIfaces = useInterfaces(target?.device_id)

  // Backend REPLACES link metadata, so send the full object (preserving any
  // waypoints / extra keys we don't edit here).
  const submit = () => onSave({
    label: label.trim() || null,
    link_type: kind,
    metadata: { ...meta, kind, shape, width_scale: widthScale, src_interface: srcIf || null, dst_interface: dstIf || null },
  })

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onCancel() }}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>Edit link</DialogTitle></DialogHeader>

        <div className="flex items-center justify-between gap-2 rounded-lg border border-border bg-surface2/40 p-2 text-xs">
          <span className="truncate font-semibold text-text">{source?.label || source?.hostname || sourceLabel || 'source'}</span>
          <span className="text-muted">—</span>
          <span className="truncate font-semibold text-text">{target?.label || target?.hostname || targetLabel || 'target'}</span>
        </div>

        <div className="grid grid-cols-2 gap-3">
          {source ? (
            <IfaceSelect label={`${source.hostname} interface`} loading={srcIfaces.isLoading} ifaces={srcIfaces.data} value={srcIf} onChange={setSrcIf} />
          ) : (
            <Field label="Source interface"><div className="rounded-md border border-border bg-surface2/40 px-2 py-1.5 text-xs text-muted">Annotation end</div></Field>
          )}
          {target ? (
            <IfaceSelect label={`${target.hostname} interface`} loading={dstIfaces.isLoading} ifaces={dstIfaces.data} value={dstIf} onChange={setDstIf} />
          ) : (
            <Field label="Target interface"><div className="rounded-md border border-border bg-surface2/40 px-2 py-1.5 text-xs text-muted">Annotation end</div></Field>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Type">
            <select className={inputCls} value={kind} onChange={(e) => setKind(e.target.value as LinkKind)}>
              {KINDS.map((k) => <option key={k} value={k}>{k[0].toUpperCase() + k.slice(1)}</option>)}
            </select>
          </Field>
          <Field label="Shape">
            <select className={inputCls} value={shape} onChange={(e) => setShape(e.target.value as LinkShape)}>
              {SHAPES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
            </select>
          </Field>
        </div>

        <Field label="Line width">
          <div className="flex items-center gap-2">
            <input
              type="range" min={0.4} max={4} step={0.2} value={widthScale}
              onChange={(e) => setWidthScale(Number(e.target.value))}
              className="flex-1 accent-primary"
            />
            <span className="w-12 text-right text-xs tabular-nums text-muted">{Math.round(widthScale * 100)}%</span>
          </div>
        </Field>

        <Field label="Label (optional)">
          <input className={inputCls} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. uplink" />
        </Field>

        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={saving}>Cancel</Button>
          <Button onClick={submit} disabled={saving}>{saving ? 'Saving…' : 'Save'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function IfaceSelect({ label, loading, ifaces, value, onChange }: {
  label: string; loading: boolean; ifaces?: DeviceInterface[]; value: string; onChange: (v: string) => void
}) {
  return (
    <Field label={label}>
      <select className={inputCls} value={value} onChange={(e) => onChange(e.target.value)} disabled={loading}>
        <option value="">{loading ? 'Loading…' : '— none —'}</option>
        {(ifaces || []).map((i) => {
          const name = i.if_name || i.if_descr || `if${i.if_index}`
          return <option key={i.if_index} value={name}>{name}{i.if_alias ? ` · ${i.if_alias}` : ''}</option>
        })}
      </select>
    </Field>
  )
}
