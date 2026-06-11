import { useRef, useState } from 'react'
import {
  Activity, ChevronDown, Circle, Diamond, Image as ImageIcon, Minus, Shapes,
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
          <div className="absolute left-0 top-9 z-50 w-44 overflow-hidden rounded-lg border border-border bg-surface p-1 shadow-xl">
            <MenuRow icon={<Shapes className="h-4 w-4" />} label="Network icon…" onClick={() => { setOpen(false); setIconOpen(true) }} />
            <MenuRow icon={<ImageIcon className="h-4 w-4" />} label="Image…" onClick={() => { setOpen(false); setImgOpen(true) }} />
            <div className="my-1 h-px bg-border" />
            <MenuRow icon={<TypeIcon className="h-4 w-4" />} label="Text" onClick={() => pick({ kind: 'text', text: 'Text', w_pct: 12, h_pct: 5, metadata: { fontSize: 18, align: 'center' } })} />
            <MenuRow icon={<Sticker className="h-4 w-4" />} label="Sticky note" onClick={() => pick({ kind: 'sticky', text: 'Note', w_pct: 12, h_pct: 9, fill: '#fde68a', metadata: { fontSize: 14, align: 'left' } })} />
            <div className="my-1 h-px bg-border" />
            <MenuRow icon={<Square className="h-4 w-4" />} label="Rectangle" onClick={() => pick({ kind: 'rectangle', w_pct: 14, h_pct: 9, metadata: { rounded: true } })} />
            <MenuRow icon={<Circle className="h-4 w-4" />} label="Ellipse" onClick={() => pick({ kind: 'circle', w_pct: 10, h_pct: 10 })} />
            <MenuRow icon={<Diamond className="h-4 w-4" />} label="Diamond" onClick={() => pick({ kind: 'diamond', w_pct: 10, h_pct: 10 })} />
            <MenuRow icon={<Minus className="h-4 w-4" />} label="Line" onClick={() => pick({ kind: 'line', w_pct: 14, h_pct: 2 })} />
            <div className="my-1 h-px bg-border" />
            <MenuRow icon={<Activity className="h-4 w-4" />} label="Top conversations (live)" onClick={() => pick({ kind: 'rectangle', w_pct: 13, h_pct: 14, fill: null, stroke: null, metadata: { widget: 'conversations', limit: 5, hours: 1 } })} />
          </div>
        </>
      )}

      {iconOpen && <IconPickerDialog onCancel={() => setIconOpen(false)} onPick={(icon) => { setIconOpen(false); onAdd({ kind: 'image', w_pct: 6, h_pct: 9, metadata: { icon } }) }} />}
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

/* ── Style inspector (shown when a shape is selected) ────────────────────── */
export function ShapeInspector({ shape, devices, onChange, onDelete, onZ }: {
  shape: MapShape
  /** Devices on this map — used to bind live widgets to one exporter. */
  devices?: Array<{ hostname: string; ip: string }>
  onChange: (patch: Record<string, unknown>, commit: boolean) => void
  onDelete: () => void
  onZ: (dir: 'front' | 'back') => void
}) {
  const m = shape.metadata || {}
  const isWidget = m.widget === 'conversations'
  const isText = !isWidget && (shape.kind === 'text' || shape.kind === 'sticky')
  const isImage = !isWidget && shape.kind === 'image'
  const isIcon = isImage && !!m.icon
  const setMeta = (patch: Partial<ShapeStyle>, commit = true) => onChange({ metadata: { ...m, ...patch } }, commit)

  return (
    <div className="flex w-60 flex-col gap-3 rounded-lg border border-border bg-surface/95 p-3 text-xs shadow-xl backdrop-blur">
      <div className="flex items-center justify-between">
        <span className="font-semibold capitalize text-text">{isWidget ? 'Live conversations' : isIcon ? 'Icon' : shape.kind} options</span>
        <button type="button" onClick={onDelete} className="rounded p-1 text-muted hover:bg-danger/10 hover:text-danger" title="Delete (Del)"><Trash2 className="h-4 w-4" /></button>
      </div>

      {isWidget && (
        <>
          <Field label="Show top">
            <select value={m.limit || 5} onChange={(e) => setMeta({ limit: Number(e.target.value) })}
              className="rounded border border-border bg-surface px-2 py-1 text-text outline-none focus:border-primary/60">
              {[5, 10].map((n) => <option key={n} value={n}>{n} conversations</option>)}
            </select>
          </Field>
          <Field label="Window">
            <select value={m.hours || 1} onChange={(e) => setMeta({ hours: Number(e.target.value) })}
              className="rounded border border-border bg-surface px-2 py-1 text-text outline-none focus:border-primary/60">
              <option value={1}>Last 1h</option>
              <option value={6}>Last 6h</option>
              <option value={24}>Last 24h</option>
            </select>
          </Field>
          <Field label="Device">
            <select value={m.exporter || ''} onChange={(e) => setMeta({ exporter: e.target.value || null })}
              className="max-w-[8.5rem] rounded border border-border bg-surface px-2 py-1 text-text outline-none focus:border-primary/60">
              <option value="">All exporters</option>
              {(devices || []).map((d) => <option key={d.ip} value={d.ip}>{d.hostname}</option>)}
            </select>
          </Field>
          <p className="text-[10px] leading-snug text-muted">
            Live NetFlow data — refreshes every 30s. Bind to a device to show only conversations seen by that exporter.
          </p>
        </>
      )}

      {isText && (
        <>
          <Field label="Font size">
            <input type="number" min={8} max={120} value={m.fontSize || 16}
              onChange={(e) => setMeta({ fontSize: Number(e.target.value) || 16 })}
              className="w-20 rounded border border-border bg-surface px-2 py-1 text-text outline-none focus:border-primary/60" />
          </Field>
          <Field label="Font">
            <select value={m.fontFamily || FONTS[0]} onChange={(e) => setMeta({ fontFamily: e.target.value })}
              className="rounded border border-border bg-surface px-2 py-1 text-text outline-none focus:border-primary/60">
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
          <Field label="Background">
            <ColorInput value={shape.fill || '#00000000'} onChange={(c, commit) => onChange({ fill: c }, commit)} allowClear onClear={() => onChange({ fill: null }, true)} />
          </Field>
          <Field label="Border">
            <ColorInput value={shape.stroke || '#3b82f6'} onChange={(c, commit) => onChange({ stroke: c }, commit)} allowClear onClear={() => onChange({ stroke: null }, true)} />
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

      {!isText && !isImage && (
        <>
          <Field label="Fill"><ColorInput value={shape.fill || '#3b82f6'} onChange={(c, commit) => onChange({ fill: c }, commit)} /></Field>
          <Field label="Border"><ColorInput value={shape.stroke || '#3b82f6'} onChange={(c, commit) => onChange({ stroke: c }, commit)} /></Field>
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="flex items-center justify-between gap-2"><span className="text-muted">{label}</span>{children}</div>
}

function Toggle({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" onClick={onClick}
      className={cn('flex h-7 w-7 items-center justify-center rounded border text-xs font-semibold', on ? 'border-primary bg-primary/15 text-primary' : 'border-border text-muted hover:text-text')}>
      {children}
    </button>
  )
}

function ColorInput({ value, onChange, allowClear, onClear }: {
  value: string; onChange: (c: string, commit: boolean) => void; allowClear?: boolean; onClear?: () => void
}) {
  const safe = value && value.length >= 7 ? value.slice(0, 7) : '#000000'
  return (
    <div className="flex items-center gap-1">
      <input type="color" value={safe} onChange={(e) => onChange(e.target.value, false)} onBlur={(e) => onChange(e.target.value, true)}
        className="h-7 w-9 cursor-pointer rounded border border-border bg-surface" />
      {allowClear && <button type="button" onClick={onClear} className="text-[10px] text-muted hover:text-text" title="Clear">clear</button>}
    </div>
  )
}
