import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent, type KeyboardEvent, type ReactNode } from 'react'
import { GripHorizontal, Maximize2, RotateCcw, Save, Settings2, X } from 'lucide-react'
import { useAuth } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'
import { cn } from '@/lib/utils'
import { changeWidgetLayout, defaultWidgetLayout, GRID_COLUMNS, GRID_GAP, GRID_ROW, mergeWidgetLayout, readWidgetLayout, restoreWidgetLayout, widgetLayoutKey, widgetTemplateKey, effectiveWidgetLayout, type WidgetDefinition, type WidgetPosition } from './widgetLayout'
import './deviceWidgetGrid.css'

export type DeviceWidget = WidgetDefinition & { title: string; content: ReactNode; compact?: boolean }
export type DeviceLayoutContext = { vendor?: string; deviceType?: string }
export function DeviceWidgetGrid({ deviceId, widgets, section = 'overview', vendor, deviceType, visibleWidgetIds, onStartEdit }: {
  deviceId: string; widgets: DeviceWidget[]; section?: string; visibleWidgetIds?: string[]; onStartEdit?: () => void
} & DeviceLayoutContext) {
  const userId = useAuth(state => state.user?.id) || 'anonymous'
  const storageKey = widgetLayoutKey(userId, deviceId, section)
  const templateKey = widgetTemplateKey(userId, vendor, deviceType, section)
  return <WidgetGrid key={`${storageKey}:${templateKey}`} storageKey={storageKey} templateKey={templateKey} templateLabel={`${vendor} / ${deviceType}`} widgets={widgets} visibleWidgetIds={visibleWidgetIds} onStartEdit={onStartEdit} />
}
function storedLayout(key: string | null) {
  try { return key ? readWidgetLayout(localStorage.getItem(key)) : null } catch { return null }
}

type Gesture = { mode: 'move' | 'resize'; widget: DeviceWidget; start: WidgetPosition; layout: WidgetPosition[]; clientX: number; clientY: number; scrollY: number; cell: number; pointerId: number }
function WidgetGrid({ storageKey, templateKey, templateLabel, widgets, visibleWidgetIds, onStartEdit }: {
  storageKey: string; templateKey: string | null; templateLabel: string; widgets: DeviceWidget[]; visibleWidgetIds?: string[]; onStartEdit?: () => void
}) {
  const [saved, setSaved] = useState<WidgetPosition[] | null>(() => storedLayout(storageKey))
  const [template, setTemplate] = useState<WidgetPosition[] | null>(() => storedLayout(templateKey))
  const [saveScope, setSaveScope] = useState<'device' | 'template'>('device')
  useEffect(() => {
    const refresh = () => { setSaved(storedLayout(storageKey)); setTemplate(storedLayout(templateKey)) }
    window.addEventListener('storage', refresh)
    window.addEventListener('zenplus-widget-layout', refresh)
    return () => { window.removeEventListener('storage', refresh); window.removeEventListener('zenplus-widget-layout', refresh) }
  }, [storageKey, templateKey])
  const [draft, setDraft] = useState<WidgetPosition[] | null>(null)
  const editing = draft !== null
  const [notice, setNotice] = useState('')
  const [active, setActive] = useState<string | null>(null)
  const grid = useRef<HTMLDivElement>(null)
  const gesture = useRef<Gesture | null>(null)
  const [width, setWidth] = useState(1200)
  useEffect(() => {
    const element = grid.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const desktop = width >= 900
  const layout = useMemo(() => restoreWidgetLayout(widgets, draft ?? effectiveWidgetLayout(saved, template)), [widgets, draft, saved, template])
  // Keep DOM nodes stationary during pointer capture; reorder reading order after drop.
  const readingOrder = gesture.current?.layout ?? layout
  const ordered = [...layout].sort((a,b) => {
    const left = readingOrder.find(item => item.id === a.id) || a
    const right = readingOrder.find(item => item.id === b.id) || b
    return left.y - right.y || left.x - right.x
  })
  const definitions = new Map(widgets.map(widget => [widget.id, widget]))
  function apply(widget: DeviceWidget, change: Partial<WidgetPosition>) {
    const next = changeWidgetLayout(layout, widget, change)
    setDraft(next)
    const item = next.find(item => item.id === widget.id)!
    setNotice(`${widget.title}: column ${item.x + 1}, row ${item.y + 1}, width ${item.w} of 12, height ${item.h * (GRID_ROW + GRID_GAP) - GRID_GAP} pixels.`)
  }
  function begin(event: PointerEvent<HTMLButtonElement>, mode: Gesture['mode'], widget: DeviceWidget, item: WidgetPosition) {
    if (!desktop || event.button !== 0) return
    event.preventDefault()
    event.currentTarget.focus()
    event.currentTarget.setPointerCapture(event.pointerId)
    gesture.current = { mode, widget, start: item, layout, clientX: event.clientX, clientY: event.clientY, scrollY: window.scrollY, cell: (width + GRID_GAP) / GRID_COLUMNS, pointerId: event.pointerId }
    setActive(widget.id)
  }
  function move(event: PointerEvent<HTMLButtonElement>) {
    const state = gesture.current
    if (!state || state.pointerId !== event.pointerId) return
    const dx = Math.round((event.clientX - state.clientX) / state.cell)
    const dy = Math.round((event.clientY - state.clientY + window.scrollY - state.scrollY) / (GRID_ROW + GRID_GAP))
    const change = state.mode === 'move' ? { x: state.start.x + dx, y: state.start.y + dy } : { w: state.start.w + dx, h: state.start.h + dy }
    setDraft(changeWidgetLayout(state.layout, state.widget, change))
  }
  function finish(event: PointerEvent<HTMLButtonElement>, cancel = false) {
    const state = gesture.current
    if (!state) return
    if (cancel) setDraft(state.layout)
    else setNotice(`${state.widget.title} ${state.mode === 'move' ? 'moved' : 'resized'}. Save layout to keep your changes.`)
    gesture.current = null
    setActive(null)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }
  function keyboard(event: KeyboardEvent<HTMLButtonElement>, mode: Gesture['mode'], widget: DeviceWidget, item: WidgetPosition) {
    if (event.key === 'Escape' && gesture.current) {
      setDraft(gesture.current.layout); gesture.current = null; setActive(null); return
    }
    const directions: Record<string, [number, number]> = { ArrowLeft: [-1,0], ArrowRight: [1,0], ArrowUp: [0,-1], ArrowDown: [0,1] }
    const delta = directions[event.key]
    if (!delta) return
    event.preventDefault()
    apply(widget, mode === 'move' ? { x: item.x + delta[0], y: item.y + delta[1] } : { w: item.w + delta[0], h: item.h + delta[1] })
  }
  function save() {
    const destination = saveScope === 'template' ? templateKey : storageKey
    if (!destination) return
    const items = mergeWidgetLayout(saveScope === 'template' ? template : effectiveWidgetLayout(saved, template), layout)
    try {
      localStorage.setItem(destination, JSON.stringify({ version: 1, items }))
      if (saveScope === 'template') { localStorage.removeItem(storageKey); setSaved(null); setTemplate(items) }
      else setSaved(items)
      setDraft(null)
      const message = saveScope === 'template' ? `Template saved for ${templateLabel}. Matching devices without their own layout inherit it.` : 'Layout saved for this device.'
      setNotice(message)
      window.dispatchEvent(new Event('zenplus-widget-layout'))
      toast.success('Layout saved', message)
    } catch { toast.error('Could not save layout', 'Browser storage is unavailable. Your edits are still open; try again or cancel.') }
  }
  function inheritTemplate() {
    try {
      localStorage.removeItem(storageKey); setSaved(null); setDraft(null)
      setNotice('This tab now follows the vendor and device type template.')
      window.dispatchEvent(new Event('zenplus-widget-layout'))
    } catch { toast.error('Could not apply template', 'Browser storage is unavailable.') }
  }
  const handlers = (mode: Gesture['mode'], widget: DeviceWidget, item: WidgetPosition) => ({
    onPointerDown: (event: PointerEvent<HTMLButtonElement>) => begin(event, mode, widget, item),
    onPointerMove: move,
    onPointerUp: (event: PointerEvent<HTMLButtonElement>) => finish(event),
    onPointerCancel: (event: PointerEvent<HTMLButtonElement>) => finish(event, true),
    onLostPointerCapture: (event: PointerEvent<HTMLButtonElement>) => finish(event),
    onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => keyboard(event, mode, widget, item),
  })
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <p className="text-xs text-muted">{editing ? 'Drag a widget header to move it. Drag its bottom-right corner to resize. Arrow keys also work on either handle.' : `This tab · ${saved ? 'device-specific layout' : template ? 'vendor / type template' : 'default layout'} · saved for your account in this browser`}</p>
      <div className="flex flex-wrap items-center gap-2">
        {editing ? <>
          <label className="flex items-center gap-2 text-xs text-muted">Save layout for
            <select aria-label="Save layout for" value={saveScope} onChange={event => setSaveScope(event.target.value as 'device' | 'template')} className="h-8 max-w-full rounded border border-border bg-surface px-2 text-text">
              <option value="device">This device only</option>
              {templateKey && <option value="template">Same vendor and device type</option>}
            </select>
          </label>
          <Button size="sm" variant="outline" onClick={() => { setDraft(defaultWidgetLayout(widgets)); setNotice('Default arrangement restored in preview. Save to apply it.') }}><RotateCcw className="h-3.5 w-3.5" />Reset layout</Button>
          <Button size="sm" variant="outline" onClick={() => { setDraft(null); setNotice('Layout changes discarded.') }}><X className="h-3.5 w-3.5" />Cancel</Button>
          <Button size="sm" onClick={save}><Save className="h-3.5 w-3.5" />Save layout</Button>
        </> : <>
          {saved && template && <Button size="sm" variant="outline" onClick={inheritTemplate}>Use vendor/type template</Button>}
          <Button size="sm" variant="outline" onClick={() => { onStartEdit?.(); setSaveScope('device'); setDraft(layout); setNotice('Layout editing enabled.') }}><Settings2 className="h-3.5 w-3.5" />Edit layout</Button>
        </>}
      </div>
    </div>
    {editing && saveScope === 'template' && <p className="text-xs text-muted">Applies to this tab for {templateLabel} devices in your account on this browser. Device-specific layouts are kept; choose “Use vendor/type template” on those devices to inherit it.</p>}
    {editing && !desktop && <p className="text-xs text-muted">Widgets stack on smaller screens. Use the size controls or arrow keys; desktop positions are preserved.</p>}
    <div role="status" aria-live="polite" className="sr-only">{notice}</div>
    <div ref={grid} className={cn('device-widget-grid', editing && 'is-editing', desktop && 'is-desktop')} style={desktop ? { gridTemplateColumns: 'repeat(12, minmax(0, 1fr))', gridAutoRows: `${GRID_ROW}px`, gap: GRID_GAP } : { gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: GRID_GAP }}>
      {ordered.map(item => {
        const widget = definitions.get(item.id)!
        const focused = !editing && visibleWidgetIds !== undefined
        const visible = !focused || visibleWidgetIds.includes(item.id)
        const style: CSSProperties = !visible ? { display: 'none' } : focused ? { gridColumn: '1 / -1', gridRow: desktop ? `span ${item.h}` : undefined } : desktop ? { gridColumn: `${item.x + 1} / span ${item.w}`, gridRow: `${item.y + 1} / span ${item.h}` } : { gridColumn: widget.compact && width > 340 ? 'span 1' : 'span 2' }
        return <section key={item.id} aria-label={`${widget.title} widget`} data-widget-id={item.id} data-grid-x={item.x} data-grid-y={item.y} data-grid-width={item.w} data-grid-height={item.h} className={cn('device-widget', active === item.id && 'is-active')} style={style}>
          {editing && <div className="device-widget-toolbar">
            <button type="button" aria-label={`Move ${widget.title} widget`} title="Drag to move. Arrow keys move one grid step." className="device-widget-move" {...handlers('move', widget, item)}><GripHorizontal className="h-4 w-4 shrink-0" /><span className="truncate">{widget.title}</span></button>
            <details className="relative shrink-0">
              <summary className="cursor-pointer list-none rounded p-1 hover:bg-primary/10 [&::-webkit-details-marker]:hidden" aria-label={`Size options for ${widget.title}`} title="Widget size options"><Settings2 className="h-3.5 w-3.5" /></summary>
              <div className="absolute right-0 z-40 mt-2 w-48 space-y-3 rounded-lg border border-border bg-surface p-3 shadow-xl">
                <label className="block text-xs text-muted">Width<select aria-label={`${widget.title} width`} value={item.w} onChange={event => apply(widget, { w: Number(event.target.value) })} className="mt-1 h-8 w-full rounded border border-border bg-surface2 px-2 text-text">{Array.from({length: 13 - (widget.minWidth || 2)},(_,index) => index + (widget.minWidth || 2)).map(value => <option key={value} value={value}>{Math.round(value / 12 * 100)}% of grid</option>)}</select></label>
                <label className="block text-xs text-muted">Height<select aria-label={`${widget.title} height`} value={item.h} onChange={event => apply(widget, { h: Number(event.target.value) })} className="mt-1 h-8 w-full rounded border border-border bg-surface2 px-2 text-text">{Array.from({length: 25 - (widget.minHeight || 3)},(_,index) => index + (widget.minHeight || 3)).map(value => <option key={value} value={value}>{value * (GRID_ROW + GRID_GAP) - GRID_GAP}px</option>)}</select></label>
              </div>
            </details>
          </div>}
          <div className="device-widget-content">{widget.content}</div>
          {editing && <button type="button" aria-label={`Resize ${widget.title} widget`} title="Drag to resize. Left/right adjusts width; up/down adjusts height." className="device-widget-resize" {...handlers('resize', widget, item)}><Maximize2 className="h-3.5 w-3.5" /></button>}
        </section>
      })}
    </div>
  </div>
}
