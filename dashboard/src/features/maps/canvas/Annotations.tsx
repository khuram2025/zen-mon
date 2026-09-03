import { useEffect, useRef, useState } from 'react'
import {
  Activity, ArrowRight, ChevronDown, Circle, Diamond, Hexagon, Image as ImageIcon, Lock, LockOpen, Minus, Shapes,
  Square, Sticker, Trash2, Type as TypeIcon,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { NetworkIcon, iconLabel, networkIcons, type IconKey } from '@/components/network-icons'
import type { MapShape, ShapeStyle } from '../core'

/** What a new shape needs (position is filled in by the canvas at drop point). */
export type ShapeSpec = {
  kind: MapShape['kind']
  w_pct?: number
  h_pct?: number
  text?: string | null
  fill?: string | null
  stroke?: string | null
  metadata?: ShapeStyle
}

const ICONS = Object.keys(networkIcons) as IconKey[]
const FONTS = ['Inter, system-ui, sans-serif', 'Georgia, serif', 'monospace', 'Comic Sans MS, cursive']
const FONT_LABEL: Record<string, string> = {
  'Inter, system-ui, sans-serif': 'Sans', 'Georgia, serif': 'Serif', 'monospace': 'Mono', 'Comic Sans MS, cursive': 'Casual',
}

/** Shape presets shared by the Insert menu, the palette's Shapes tab and the
 *  canvas right-click "Add here" menu — one source of truth. */
export const SHAPE_PRESETS: { key: string; label: string; icon: React.ReactNode; spec: ShapeSpec }[] = [
  { key: 'text', label: 'Text', icon: <TypeIcon className="h-4 w-4" />, spec: { kind: 'text', text: 'Text', w_pct: 12, h_pct: 5, metadata: { fontSize: 18, align: 'center' } } },
  { key: 'sticky', label: 'Sticky note', icon: <Sticker className="h-4 w-4" />, spec: { kind: 'sticky', text: 'Note', w_pct: 12, h_pct: 9, fill: '#fde68a', metadata: { fontSize: 14, align: 'left' } } },
  { key: 'rectangle', label: 'Rectangle', icon: <Square className="h-4 w-4" />, spec: { kind: 'rectangle', w_pct: 14, h_pct: 9, metadata: { rounded: true } } },
  { key: 'zone', label: 'Zone (group box)', icon: <Square className="h-4 w-4 opacity-60" />, spec: { kind: 'rectangle', w_pct: 24, h_pct: 18, text: 'Zone', fill: 'rgba(59,130,246,0.06)', stroke: '#3b82f6', metadata: { rounded: true, dash: 'dashed', align: 'left', fontSize: 22, bold: true } } },
  { key: 'circle', label: 'Ellipse', icon: <Circle className="h-4 w-4" />, spec: { kind: 'circle', w_pct: 10, h_pct: 10 } },
  { key: 'diamond', label: 'Diamond', icon: <Diamond className="h-4 w-4" />, spec: { kind: 'diamond', w_pct: 10, h_pct: 10 } },
  { key: 'hexagon', label: 'Hexagon', icon: <Hexagon className="h-4 w-4" />, spec: { kind: 'hexagon', w_pct: 10, h_pct: 10 } },
  { key: 'line', label: 'Line', icon: <Minus className="h-4 w-4" />, spec: { kind: 'line', w_pct: 14, h_pct: 2 } },
  { key: 'arrow', label: 'Arrow', icon: <ArrowRight className="h-4 w-4" />, spec: { kind: 'arrow', w_pct: 14, h_pct: 2 } },
  { key: 'conversations', label: 'Top conversations (live)', icon: <Activity className="h-4 w-4" />, spec: { kind: 'rectangle', w_pct: 13, h_pct: 14, fill: null, stroke: null, metadata: { widget: 'conversations', limit: 5, hours: 1 } } },
]

/** Icon annotation preset (network icon as a free-floating symbol). */
export function iconSpec(icon: IconKey): ShapeSpec {
  return { kind: 'image', w_pct: 6, h_pct: 9, metadata: { icon } }
}

/* ── Insert menu (toolbar dropdown) ──────────────────────────────────────── */
export function InsertMenu({ onAdd }: { onAdd: (spec: ShapeSpec) => void }) {
  const [open, setOpen] = useState(false)
  const [iconOpen, setIconOpen] = useState(false)
  const [imgOpen, setImgOpen] = useState(false)

  const pick = (spec: ShapeSpec) => { setOpen(false); onAdd(spec) }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex h-7 items-center gap-1 rounded-md px-2 text-xs font-medium text-muted transition hover:bg-primary/10 hover:text-text"
        title="Insert icon, image, text or shape"
      >
        <Shapes className="h-4 w-4" /> Insert <ChevronDown className="h-3 w-3" />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute left-0 top-9 z-50 w-52 overflow-hidden rounded-lg border border-border bg-surface p-1 shadow-xl">
            <MenuRow icon={<Shapes className="h-4 w-4" />} label="Network icon…" onClick={() => { setOpen(false); setIconOpen(true) }} />
            <MenuRow icon={<ImageIcon className="h-4 w-4" />} label="Image…" onClick={() => { setOpen(false); setImgOpen(true) }} />
            <div className="my-1 h-px bg-border" />
            {SHAPE_PRESETS.map((p, i) => (
              <div key={p.key}>
                {(i === 2 || i === 9) && <div className="my-1 h-px bg-border" />}
                <MenuRow icon={p.icon} label={p.label} onClick={() => pick(p.spec)} />
              </div>
            ))}
          </div>
        </>
      )}

      {iconOpen && <IconPickerDialog onCancel={() => setIconOpen(false)} onPick={(icon) => { setIconOpen(false); onAdd(iconSpec(icon)) }} />}
      {imgOpen && <ImageDialog onCancel={() => setImgOpen(false)} onPick={(src) => { setImgOpen(false); onAdd({ kind: 'image', w_pct: 16, h_pct: 12, metadata: { src } }) }} />}
    </div>
  )
}

function MenuRow({ icon, label, onClick }: { icon: React.ReactNode; label: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-text transition hover:bg-primary/10">
      <span className="text-muted">{icon}</span> {label}
    </button>
  )
}

/* ── Icon picker ─────────────────────────────────────────────────────────── */
export function IconPickerDialog({ onCancel, onPick }: { onCancel: () => void; onPick: (icon: IconKey) => void }) {
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onCancel() }}>
      <DialogContent className="max-w-sm">
        <DialogHeader><DialogTitle>Insert icon</DialogTitle></DialogHeader>
        <div className="grid grid-cols-5 gap-2">
          {ICONS.map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => onPick(key)}
              title={iconLabel[key]}
              className="flex aspect-square flex-col items-center justify-center gap-1 rounded-lg border border-border bg-surface2/40 p-2 transition hover:border-primary/60 hover:bg-primary/5"
            >
              <NetworkIcon name={key} className="h-7 w-7" />
              <span className="truncate text-[8px] text-muted">{iconLabel[key]}</span>
            </button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}

/* ── Image (URL or upload) ───────────────────────────────────────────────── */
const MAX_IMG_BYTES = 1.5 * 1024 * 1024

export function ImageDialog({ onCancel, onPick }: { onCancel: () => void; onPick: (src: string) => void }) {
  const [url, setUrl] = useState('')
  const [err, setErr] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const onFile = (file?: File) => {
    if (!file) return
    if (file.size > MAX_IMG_BYTES) { setErr('Image too large (max 1.5 MB). Use a URL instead.'); return }
    const reader = new FileReader()
    reader.onload = () => onPick(String(reader.result))
    reader.onerror = () => setErr('Could not read that file.')
    reader.readAsDataURL(file)
  }

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onCancel() }}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>Insert image</DialogTitle></DialogHeader>
        <label className="flex flex-col gap-1 text-xs">
          <span className="font-medium text-muted">Image URL</span>
          <input
            className="w-full rounded-md border border-border bg-surface px-2 py-1.5 text-xs text-text outline-none focus:border-primary/60"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://… or paste a link"
            autoFocus
          />
        </label>
        <div className="flex items-center gap-2 text-[10px] text-muted"><div className="h-px flex-1 bg-border" /> or <div className="h-px flex-1 bg-border" /></div>
        <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={(e) => onFile(e.target.files?.[0])} />
        <Button variant="outline" onClick={() => fileRef.current?.click()}>Upload from computer (≤ 1.5 MB)</Button>
        {err && <p className="text-[11px] text-danger">{err}</p>}
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>Cancel</Button>
          <Button onClick={() => url.trim() && onPick(url.trim())} disabled={!url.trim()}>Insert</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* ── Colour helpers ──────────────────────────────────────────────────────── */

/** Parse any CSS colour we store (hex3/6/8, rgb/rgba) into hex6 + alpha. */
export function parseColor(value: string | null | undefined, fallback = '#3b82f6'): { hex: string; alpha: number } {
  const v = (value || '').trim()
  if (!v) return { hex: fallback, alpha: 1 }
  const m = v.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)$/i)
  if (m) {
    const h = [m[1], m[2], m[3]].map((n) => Math.max(0, Math.min(255, Number(n))).toString(16).padStart(2, '0')).join('')
    return { hex: `#${h}`, alpha: m[4] != null ? Math.max(0, Math.min(1, Number(m[4]))) : 1 }
  }
  if (/^#[0-9a-f]{3}$/i.test(v)) return { hex: `#${v[1]}${v[1]}${v[2]}${v[2]}${v[3]}${v[3]}`, alpha: 1 }
  if (/^#[0-9a-f]{6}$/i.test(v)) return { hex: v.toLowerCase(), alpha: 1 }
  if (/^#[0-9a-f]{8}$/i.test(v)) return { hex: v.slice(0, 7).toLowerCase(), alpha: parseInt(v.slice(7, 9), 16) / 255 }
  return { hex: fallback, alpha: 1 }
}

export function composeColor(hex: string, alpha: number): string {
  if (alpha >= 0.995) return hex
  const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r},${g},${b},${Math.round(alpha * 100) / 100})`
}

/* ── Style inspector (shown when a shape is selected) ────────────────────── */
export function ShapeInspector({ shape, devices, onChange, onDelete, onZ, onLock }: {
  shape: MapShape
  /** Devices on this map — used to bind live widgets to one exporter. */
  devices?: Array<{ hostname: string; ip: string }>
  onChange: (patch: Record<string, unknown>, commit: boolean) => void
  onDelete: () => void
  onZ: (dir: 'front' | 'back') => void
  onLock?: (locked: boolean) => void
}) {
  const m = shape.metadata || {}
  const isWidget = m.widget === 'conversations'
  const isText = !isWidget && (shape.kind === 'text' || shape.kind === 'sticky')
  const isImage = !isWidget && shape.kind === 'image'
  const isIcon = isImage && !!m.icon
  const isLine = shape.kind === 'line' || shape.kind === 'arrow'
  const isGeo = !isWidget && !isText && !isImage && !isLine
  const setMeta = (patch: Partial<ShapeStyle>, commit = true) => onChange({ metadata: { ...m, ...patch } }, commit)
  const title = isWidget ? 'Live conversations' : isIcon ? 'Icon' : shape.kind === 'circle' ? 'Ellipse' : shape.kind

  return (
    <div className="flex w-64 flex-col gap-2.5 rounded-lg border border-border bg-surface/95 p-3 text-xs shadow-xl backdrop-blur">
      <div className="flex items-center justify-between">
        <span className="font-semibold capitalize text-text">{title}</span>
        <div className="flex items-center gap-0.5">
          {onLock && (
            <button type="button" onClick={() => onLock(!m.locked)} className={cn('rounded p-1 hover:bg-primary/10', m.locked ? 'text-primary' : 'text-muted hover:text-text')} title={m.locked ? 'Unlock' : 'Lock position & size'}>
              {m.locked ? <Lock className="h-4 w-4" /> : <LockOpen className="h-4 w-4" />}
            </button>
          )}
          <button type="button" onClick={onDelete} className="rounded p-1 text-muted hover:bg-danger/10 hover:text-danger" title="Delete (Del)"><Trash2 className="h-4 w-4" /></button>
        </div>
      </div>

      {isWidget && (
        <>
          <Field label="Show top">
            <select value={m.limit || 5} onChange={(e) => setMeta({ limit: Number(e.target.value) })} className={selCls}>
              {[5, 10].map((n) => <option key={n} value={n}>{n} conversations</option>)}
            </select>
          </Field>
          <Field label="Window">
            <select value={m.hours || 1} onChange={(e) => setMeta({ hours: Number(e.target.value) })} className={selCls}>
              <option value={1}>Last 1h</option>
              <option value={6}>Last 6h</option>
              <option value={24}>Last 24h</option>
            </select>
          </Field>
          <Field label="Device">
            <select value={m.exporter || ''} onChange={(e) => setMeta({ exporter: e.target.value || null })} className={cn(selCls, 'max-w-[8.5rem]')}>
              <option value="">All exporters</option>
              {(devices || []).map((d) => <option key={d.ip} value={d.ip}>{d.hostname}</option>)}
            </select>
          </Field>
          <p className="text-[10px] leading-snug text-muted">
            Live NetFlow data — refreshes every 30s. Bind to a device to show only conversations seen by that exporter.
          </p>
        </>
      )}

      {(isText || isGeo) && (
        <>
          {isGeo && (
            <Field label="Caption">
              <input value={shape.text || ''} onChange={(e) => onChange({ text: e.target.value }, false)} onBlur={(e) => onChange({ text: e.target.value }, true)}
                placeholder="none" className="w-32 rounded border border-border bg-surface px-2 py-1 text-text outline-none focus:border-primary/60" />
            </Field>
          )}
          <Field label="Font size">
            <input type="number" min={8} max={120} value={m.fontSize || (isText ? 16 : 13)}
              onChange={(e) => setMeta({ fontSize: Number(e.target.value) || 16 })}
              className="w-20 rounded border border-border bg-surface px-2 py-1 text-text outline-none focus:border-primary/60" />
          </Field>
          <Field label="Font">
            <select value={m.fontFamily || FONTS[0]} onChange={(e) => setMeta({ fontFamily: e.target.value })} className={selCls}>
              {FONTS.map((f) => <option key={f} value={f}>{FONT_LABEL[f]}</option>)}
            </select>
          </Field>
          <Field label="Text colour">
            <ColorInput value={m.color || '#e5e7eb'} onChange={(c, commit) => setMeta({ color: c }, commit)} />
          </Field>
          <div className="flex gap-1">
            <Toggle on={!!m.bold} onClick={() => setMeta({ bold: !m.bold })}>B</Toggle>
            <Toggle on={!!m.italic} onClick={() => setMeta({ italic: !m.italic })}><span className="italic">I</span></Toggle>
            <div className="mx-1 w-px bg-border" />
            {(['left', 'center', 'right'] as const).map((a) => (
              <Toggle key={a} on={(m.align || 'center') === a} onClick={() => setMeta({ align: a })}>{a[0].toUpperCase()}</Toggle>
            ))}
          </div>
        </>
      )}

      {isText && (
        <>
          <Field label="Background">
            <ColorInput value={shape.fill || ''} onChange={(c, commit) => onChange({ fill: c }, commit)} allowClear onClear={() => onChange({ fill: null }, true)} withAlpha />
          </Field>
          <Field label="Border">
            <ColorInput value={shape.stroke || ''} onChange={(c, commit) => onChange({ stroke: c }, commit)} allowClear onClear={() => onChange({ stroke: null }, true)} />
          </Field>
          {shape.kind === 'text' && (
            <Field label="Corners">
              <div className="flex gap-1">
                <Toggle on={m.rounded !== false} onClick={() => setMeta({ rounded: true })}>◖</Toggle>
                <Toggle on={m.rounded === false} onClick={() => setMeta({ rounded: false })}>◻</Toggle>
              </div>
            </Field>
          )}
        </>
      )}

      {isIcon && (
        <Field label="Icon colour">
          <ColorInput value={m.color || '#e5e7eb'} onChange={(c, commit) => setMeta({ color: c }, commit)} />
        </Field>
      )}

      {isImage && !isIcon && (
        <Field label="Source URL">
          <input value={m.src || ''} onChange={(e) => setMeta({ src: e.target.value }, false)} onBlur={(e) => setMeta({ src: e.target.value })}
            className="w-36 rounded border border-border bg-surface px-2 py-1 text-text outline-none focus:border-primary/60" />
        </Field>
      )}

      {(isGeo || isLine) && (
        <>
          {isGeo && (
            <Field label="Fill">
              <ColorInput value={shape.fill || 'rgba(59,130,246,0.12)'} onChange={(c, commit) => onChange({ fill: c }, commit)} withAlpha allowClear onClear={() => onChange({ fill: 'rgba(0,0,0,0)' }, true)} />
            </Field>
          )}
          <Field label="Border"><ColorInput value={shape.stroke || '#3b82f6'} onChange={(c, commit) => onChange({ stroke: c }, commit)} /></Field>
          <Field label="Border width">
            <input type="range" min={1} max={12} step={1} value={m.strokeWidth ?? 2} onChange={(e) => setMeta({ strokeWidth: Number(e.target.value) }, false)} onMouseUp={(e) => setMeta({ strokeWidth: Number((e.target as HTMLInputElement).value) })} onTouchEnd={(e) => setMeta({ strokeWidth: Number((e.target as HTMLInputElement).value) })} className="w-28 accent-primary" />
          </Field>
          <Field label="Line style">
            <div className="flex gap-1">
              {(['solid', 'dashed', 'dotted'] as const).map((d) => (
                <Toggle key={d} on={(m.dash || 'solid') === d} onClick={() => setMeta({ dash: d })} title={d}>
                  <span className="block w-4 border-t-2" style={{ borderStyle: d, borderColor: 'currentColor' }} />
                </Toggle>
              ))}
            </div>
          </Field>
          {shape.kind === 'rectangle' && (
            <Field label="Corners">
              <div className="flex gap-1">
                <Toggle on={!!m.rounded} onClick={() => setMeta({ rounded: true })}>◖</Toggle>
                <Toggle on={!m.rounded} onClick={() => setMeta({ rounded: false })}>◻</Toggle>
              </div>
            </Field>
          )}
        </>
      )}

      {!isWidget && (
        <>
          <Field label="Rotation">
            <div className="flex items-center gap-1.5">
              <input type="range" min={0} max={359} step={1} value={m.rotation ?? 0} onChange={(e) => setMeta({ rotation: Number(e.target.value) }, false)} onMouseUp={(e) => setMeta({ rotation: Number((e.target as HTMLInputElement).value) })} onTouchEnd={(e) => setMeta({ rotation: Number((e.target as HTMLInputElement).value) })} className="w-20 accent-primary" />
              <span className="w-8 text-right tabular-nums text-muted">{m.rotation ?? 0}°</span>
            </div>
          </Field>
          <Field label="Opacity">
            <div className="flex items-center gap-1.5">
              <input type="range" min={0.1} max={1} step={0.05} value={m.opacity ?? 1} onChange={(e) => setMeta({ opacity: Number(e.target.value) }, false)} onMouseUp={(e) => setMeta({ opacity: Number((e.target as HTMLInputElement).value) })} onTouchEnd={(e) => setMeta({ opacity: Number((e.target as HTMLInputElement).value) })} className="w-20 accent-primary" />
              <span className="w-8 text-right tabular-nums text-muted">{Math.round((m.opacity ?? 1) * 100)}%</span>
            </div>
          </Field>
        </>
      )}

      <div className="flex items-center justify-between border-t border-border pt-2">
        <span className="text-muted">Layer</span>
        <div className="flex gap-1">
          <Button variant="outline" size="sm" onClick={() => onZ('back')}>Back</Button>
          <Button variant="outline" size="sm" onClick={() => onZ('front')}>Front</Button>
        </div>
      </div>
    </div>
  )
}

const selCls = 'rounded border border-border bg-surface px-2 py-1 text-text outline-none focus:border-primary/60'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="flex items-center justify-between gap-2"><span className="text-muted">{label}</span>{children}</div>
}

function Toggle({ on, onClick, children, title }: { on: boolean; onClick: () => void; children: React.ReactNode; title?: string }) {
  return (
    <button type="button" onClick={onClick} title={title}
      className={cn('flex h-7 w-7 items-center justify-center rounded border text-xs font-semibold', on ? 'border-primary bg-primary/15 text-primary' : 'border-border text-muted hover:text-text')}>
      {children}
    </button>
  )
}

/** Colour picker that understands rgba/hex8 (the old one fed `rgba(59,…` into
 *  <input type=color> and showed black). Live-previews while the native
 *  picker is open and commits when it closes (native `change`), with an
 *  optional alpha slider for fills. */
export function ColorInput({ value, onChange, allowClear, onClear, withAlpha }: {
  value: string; onChange: (c: string, commit: boolean) => void; allowClear?: boolean; onClear?: () => void; withAlpha?: boolean
}) {
  const parsed = parseColor(value)
  const [hex, setHex] = useState(parsed.hex)
  const [alpha, setAlpha] = useState(parsed.alpha)
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => { setHex(parsed.hex); setAlpha(parsed.alpha) }, [parsed.hex, parsed.alpha])

  // React's onChange fires on every `input`; the native `change` fires once
  // the picker closes — that's our commit point.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const commit = () => onChange(composeColor(el.value, alpha), true)
    el.addEventListener('change', commit)
    return () => el.removeEventListener('change', commit)
  }, [onChange, alpha])

  return (
    <div className="flex items-center gap-1.5">
      <input ref={ref} type="color" value={hex} onChange={(e) => { setHex(e.target.value); onChange(composeColor(e.target.value, alpha), false) }}
        className="h-7 w-9 cursor-pointer rounded border border-border bg-surface" />
      {withAlpha && (
        <input type="range" min={0} max={1} step={0.05} value={alpha} title={`Opacity ${Math.round(alpha * 100)}%`}
          onChange={(e) => { const a = Number(e.target.value); setAlpha(a); onChange(composeColor(hex, a), false) }}
          onMouseUp={(e) => onChange(composeColor(hex, Number((e.target as HTMLInputElement).value)), true)}
          onTouchEnd={(e) => onChange(composeColor(hex, Number((e.target as HTMLInputElement).value)), true)}
          className="w-14 accent-primary" />
      )}
      {allowClear && <button type="button" onClick={onClear} className="text-[10px] text-muted hover:text-text" title="Clear">none</button>}
    </div>
  )
}
