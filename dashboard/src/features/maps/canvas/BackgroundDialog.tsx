import { useState } from 'react'
import { ImageIcon } from 'lucide-react'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import type { MapBackgroundImage } from '../core'
import { pickImageFile, readImageFile } from './imageUpload'

/** Per-map background image (a floor plan, a site photo, a region outline).
 *  It sits under every node and cable, scales with the map and is stored in
 *  the map itself, so exports, versions and the NOC wall all carry it. */
export function BackgroundDialog({ initial, onCancel, onSave, saving }: {
  initial: MapBackgroundImage | null
  onCancel: () => void
  onSave: (bg: MapBackgroundImage | null) => void
  saving?: boolean
}) {
  const [src, setSrc] = useState(initial?.src || '')
  const [opacity, setOpacity] = useState(initial?.opacity ?? 0.5)
  const [w, setW] = useState(initial?.w_pct ?? 100)
  const [x, setX] = useState(initial?.x_pct ?? 0)
  const [y, setY] = useState(initial?.y_pct ?? 0)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const upload = async () => {
    const file = await pickImageFile()
    if (!file) return
    setBusy(true)
    try { setSrc(await readImageFile(file, 'background')); setErr(null) } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  const slider = (label: string, value: number, set: (v: number) => void, min: number, max: number, step: number, fmt: (v: number) => string) => (
    <label className="flex items-center gap-3 text-xs">
      <span className="w-20 shrink-0 font-medium text-muted">{label}</span>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(e) => set(Number(e.target.value))} className="flex-1 accent-primary" />
      <span className="w-12 text-right tabular-nums text-text2">{fmt(value)}</span>
    </label>
  )

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onCancel() }}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle className="flex items-center gap-2"><ImageIcon className="h-4 w-4 text-primary" /> Background image</DialogTitle></DialogHeader>
        <DialogDescription>
          Shown under every device and cable on this map — useful for floor plans, rack photos or a region outline.
          It is stored with the map (PNG, JPEG, WebP or SVG, scaled for large screens).
        </DialogDescription>

        <div className="flex h-40 items-center justify-center overflow-hidden rounded-lg border border-border bg-surface2/50">
          {src ? <img src={src} alt="" className="max-h-full max-w-full object-contain" style={{ opacity }} /> : <span className="text-xs text-muted">No background</span>}
        </div>
        <div className="flex items-center gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => void upload()} disabled={busy}>{busy ? 'Reading…' : src ? 'Replace image…' : 'Upload image…'}</Button>
          {src && <Button type="button" variant="outline" size="sm" onClick={() => setSrc('')}>Remove</Button>}
        </div>
        {err && <div className="text-2xs text-danger">{err}</div>}

        {src && (
          <div className="space-y-2">
            {slider('Opacity', opacity, setOpacity, 0.05, 1, 0.05, (v) => `${Math.round(v * 100)}%`)}
            {slider('Width', w, setW, 10, 200, 1, (v) => `${v}%`)}
            {slider('Left', x, setX, -50, 100, 1, (v) => `${v}%`)}
            {slider('Top', y, setY, -50, 100, 1, (v) => `${v}%`)}
            <p className="text-2xs text-muted">Width and position are a share of the whole drawing area; the height follows the image.</p>
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={saving}>Cancel</Button>
          <Button onClick={() => onSave(src ? { src, opacity, w_pct: w, x_pct: x, y_pct: y } : null)} disabled={saving || busy}>{saving ? 'Saving…' : 'Save'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
