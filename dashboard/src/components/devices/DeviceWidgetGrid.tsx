import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent, type KeyboardEvent, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { GripHorizontal, Maximize2, RotateCcw, Save, Settings2, X } from 'lucide-react'
import { useAuth } from '@/stores/auth'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import {
  changeWidgetLayout, defaultWidgetLayout, effectiveWidgetLayout, GRID_COLUMNS, GRID_GAP, GRID_ROW, layoutTemplateIdentity, mergeWidgetLayout,
  readStoredLayout, reconcileLayout, restoreWidgetLayout, storedLayoutValue, widgetLayoutKey, widgetTemplateKey,
  type WidgetDefinition, type WidgetPosition,
} from './widgetLayout'
import './deviceWidgetGrid.css'

export type DeviceWidget = WidgetDefinition & { title: string; content: ReactNode; compact?: boolean }
export type DeviceLayoutContext = { vendor?: string; deviceType?: string }

type LayoutSlot = 'device' | 'template'
type TemplateIdentity = { vendor: string; device_type: string }
/** GET /devices/{id}/widget-layouts: the user's layouts for this device and its vendor/type, per section. */
type ServerLayouts = {
  device: Record<string, { items: WidgetPosition[]; updated_at?: string | null }>
  template: Record<string, { items: WidgetPosition[]; updated_at?: string | null }>
  template_key: TemplateIdentity | null
}

const layoutsKey = (deviceId: string, identity: TemplateIdentity | null) =>
  ['device-widget-layouts', deviceId, identity?.vendor ?? '', identity?.device_type ?? ''] as const

/** The user's server-saved layouts for one device page (every tab shares the one request). */
function useServerLayouts(deviceId: string, identity: TemplateIdentity | null) {
  return useQuery<ServerLayouts>({
    queryKey: layoutsKey(deviceId, identity),
    queryFn: async () => (await api.get(`/devices/${deviceId}/widget-layouts`, { params: identity ?? {} })).data,
    staleTime: 60_000,
    retry: false,
  })
}

export function DeviceWidgetGrid({ deviceId, widgets, section = 'overview', vendor, deviceType, visibleWidgetIds, onStartEdit }: {
  deviceId: string; widgets: DeviceWidget[]; section?: string; visibleWidgetIds?: string[]; onStartEdit?: () => void
} & DeviceLayoutContext) {
  const userId = useAuth(state => state.user?.id) || 'anonymous'
  const storageKey = widgetLayoutKey(userId, deviceId, section)
  const templateKey = widgetTemplateKey(userId, vendor, deviceType, section)
  const identity = useMemo(() => layoutTemplateIdentity(vendor, deviceType), [vendor, deviceType])
  const server = useServerLayouts(deviceId, identity)
  return <WidgetGrid key={`${storageKey}:${templateKey}`} storageKey={storageKey} templateKey={templateKey} templateLabel={`${vendor} / ${deviceType}`}
    widgets={widgets} visibleWidgetIds={visibleWidgetIds} onStartEdit={onStartEdit}
    remote={{ deviceId, section, identity, data: server.data }} />
}
function storedLayout(key: string | null) {
  try { return key ? readStoredLayout(localStorage.getItem(key)) : null } catch { return null }
}
function writeStored(key: string | null, items: WidgetPosition[] | null, synced: boolean) {
  if (!key) return
  try {
    if (items === null) localStorage.removeItem(key)
    else localStorage.setItem(key, storedLayoutValue(items, synced))
  } catch { /* private mode: the server copy still applies */ }
}

type Remote = { deviceId: string; section: string; identity: TemplateIdentity | null; data: ServerLayouts | undefined }
type Gesture = { mode: 'move' | 'resize'; widget: DeviceWidget; start: WidgetPosition; layout: WidgetPosition[]; clientX: number; clientY: number; scrollY: number; cell: number; pointerId: number }
function WidgetGrid({ storageKey, templateKey, templateLabel, widgets, visibleWidgetIds, onStartEdit, remote }: {
  storageKey: string; templateKey: string | null; templateLabel: string; widgets: DeviceWidget[]; visibleWidgetIds?: string[]; onStartEdit?: () => void
  remote: Remote
}) {
  const qc = useQueryClient()
  const [saved, setSaved] = useState<WidgetPosition[] | null>(() => storedLayout(storageKey)?.items ?? null)
  const [template, setTemplate] = useState<WidgetPosition[] | null>(() => storedLayout(templateKey)?.items ?? null)
  const [saveScope, setSaveScope] = useState<'device' | 'template'>('device')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const refresh = () => { setSaved(storedLayout(storageKey)?.items ?? null); setTemplate(storedLayout(templateKey)?.items ?? null) }
    window.addEventListener('storage', refresh)
    window.addEventListener('zenplus-widget-layout', refresh)
    return () => { window.removeEventListener('storage', refresh); window.removeEventListener('zenplus-widget-layout', refresh) }
  }, [storageKey, templateKey])

  /** Save (items) or forget (null) one slot on the server, then mirror it in the query cache. */
  async function persist(slot: LayoutSlot, items: WidgetPosition[] | null): Promise<boolean> {
    if (slot === 'template' && !remote.identity) return false
    const url = `/devices/${remote.deviceId}/widget-layouts/${encodeURIComponent(remote.section)}`
    try {
      if (items === null) await api.delete(url, { params: { scope: slot, ...(slot === 'template' ? remote.identity : {}) } })
      else await api.put(url, { scope: slot, ...(slot === 'template' ? remote.identity : {}), items })
    } catch { return false }
    qc.setQueryData<ServerLayouts>(layoutsKey(remote.deviceId, remote.identity), (old) => {
      if (!old) return old
      const next = { ...old[slot] }
      if (items === null) delete next[remote.section]
      else next[remote.section] = { items, updated_at: new Date().toISOString() }
      return { ...old, [slot]: next }
    })
    return true
  }

  // Once the server answers: its copy wins; a browser-only copy is uploaded once; a synced copy the server
  // no longer has was reset elsewhere and is dropped.
  const serverDevice = remote.data ? (remote.data.device?.[remote.section]?.items ?? null) : undefined
  const serverTemplate = remote.data ? (remote.identity ? (remote.data.template?.[remote.section]?.items ?? null) : null) : undefined
  const serverSignature = remote.data ? JSON.stringify([serverDevice, serverTemplate]) : ''
  useEffect(() => {
    if (!remote.data) return
    const slots: Array<[LayoutSlot, string | null, WidgetPosition[] | null | undefined, (v: WidgetPosition[] | null) => void]> = [
      ['device', storageKey, serverDevice, setSaved], ['template', templateKey, serverTemplate, setTemplate]]
    for (const [slot, key, server, set] of slots) {
      if (!key) continue
      const outcome = reconcileLayout(storedLayout(key), server)
      if (outcome.local === 'write') writeStored(key, outcome.items, true)
      if (outcome.local === 'remove') writeStored(key, null, false)
      set(outcome.items)
      if (outcome.upload && outcome.items) {
        const items = outcome.items
        void persist(slot, items).then((ok) => { if (ok) writeStored(key, items, true) })
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverSignature, storageKey, templateKey])
  const [draft, setDraft] = useState<WidgetPosition[] | null>(null)
  const editing = draft !== null
  const [standardDraft, setStandardDraft] = useState(false)
  const standard = !editing && !(effectiveWidgetLayout(saved, template)?.length)
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
  const ordered = standard ? widgets.map(widget => layout.find(item => item.id === widget.id)!) : [...layout].sort((a,b) => {
    const left = readingOrder.find(item => item.id === a.id) || a
    const right = readingOrder.find(item => item.id === b.id) || b
    return left.y - right.y || left.x - right.x
  })
  const definitions = new Map(widgets.map(widget => [widget.id, widget]))
  function apply(widget: DeviceWidget, change: Partial<WidgetPosition>) {
    const next = changeWidgetLayout(layout, widget, change)
    setStandardDraft(false)
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
    setStandardDraft(false)
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
  async function save() {
    const slot: LayoutSlot = saveScope
    const destination = slot === 'template' ? templateKey : storageKey
    if (!destination || busy) return
    // An empty override intentionally opts into the responsive standard layout.
    // It also overrides a template without deleting that template's preferences.
    const items = standardDraft ? [] : mergeWidgetLayout(slot === 'template' ? template : effectiveWidgetLayout(saved, template), layout)
    setBusy(true)
    const onServer = await persist(slot, items)
    // Choosing the template for this device also forgets this device's own layout.
    if (slot === 'template' && onServer) await persist('device', null)
    setBusy(false)
    try {
      localStorage.setItem(destination, storedLayoutValue(items, onServer))
      if (slot === 'template') { localStorage.removeItem(storageKey); setSaved(null); setTemplate(items) }
      else setSaved(items)
    } catch {
      if (!onServer) { toast.error('Could not save layout', 'The server and browser storage are both unavailable. Your edits are still open; try again or cancel.'); return }
      if (slot === 'template') { setSaved(null); setTemplate(items) } else setSaved(items)
    }
    setDraft(null)
    const message = slot === 'template' ? `Template saved for ${templateLabel}. Matching devices without their own layout inherit it.` : 'Layout saved for this device.'
    setNotice(message)
    window.dispatchEvent(new Event('zenplus-widget-layout'))
    if (onServer) toast.success('Layout saved', `${message} It follows your account to other browsers.`)
    else toast.warning('Layout saved in this browser only', 'The server could not be reached. It is uploaded the next time this page loads.')
  }
  async function inheritTemplate() {
    if (busy) return
    setBusy(true)
    const onServer = await persist('device', null)
    setBusy(false)
    try { localStorage.removeItem(storageKey) } catch { /* nothing cached */ }
    if (!onServer) toast.warning('Template applied in this browser only', 'The server could not be reached; your saved layout for this device is still on the server.')
    setSaved(null); setDraft(null)
    setNotice('This tab now follows the vendor and device type template.')
    window.dispatchEvent(new Event('zenplus-widget-layout'))
  }
  const handlers = (mode: Gesture['mode'], widget: DeviceWidget, item: WidgetPosition) => ({
    onPointerDown: (event: PointerEvent<HTMLButtonElement>) => begin(event, mode, widget, item),
    onPointerMove: move,
    onPointerUp: (event: PointerEvent<HTMLButtonElement>) => finish(event),
    onPointerCancel: (event: PointerEvent<HTMLButtonElement>) => finish(event, true),
    onLostPointerCapture: (event: PointerEvent<HTMLButtonElement>) => finish(event),
    onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => keyboard(event, mode, widget, item),
  })
  const focused = !editing && visibleWidgetIds !== undefined
  const renderWidget = (item: WidgetPosition) => {
    const widget = definitions.get(item.id)!
    const visible = !focused || visibleWidgetIds.includes(item.id)
    const style: CSSProperties = !visible ? { display: 'none' } : standard || focused ? {
      gridColumn: focused || width < 900 ? '1 / -1' : `span ${item.w}`,
    } : desktop ? { gridColumn: `${item.x + 1} / span ${item.w}`, gridRow: `${item.y + 1} / span ${item.h}` } : { gridColumn: widget.compact && width > 340 ? 'span 1' : 'span 2' }
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
  }
  const summaries = ordered.filter(item => definitions.get(item.id)?.compact)
  const panels = ordered.filter(item => !definitions.get(item.id)?.compact)
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <p className="text-xs text-muted" title={editing ? undefined : 'Layouts are saved for your account, separately for each device section, and follow you to other browsers.'}>{editing ? 'Move or resize cards using the handles or arrow keys. Save when finished.' : standard ? 'Standard layout' : saved ? 'Custom layout' : 'Vendor / type layout'}</p>
      <div className="flex flex-wrap items-center gap-2">
        {editing ? <>
          <label className="flex items-center gap-2 text-xs text-muted">Save layout for
            <select aria-label="Save layout for" value={saveScope} onChange={event => setSaveScope(event.target.value as 'device' | 'template')} className="h-8 max-w-full rounded border border-border bg-surface px-2 text-text">
              <option value="device">This device only</option>
              {templateKey && <option value="template">Same vendor and device type</option>}
            </select>
          </label>
          <Button size="sm" variant="outline" onClick={() => { setDraft(defaultWidgetLayout(widgets)); setStandardDraft(true); setNotice('Standard layout selected. Save to restore automatic sizing.') }}><RotateCcw className="h-3.5 w-3.5" />Restore standard</Button>
          <Button size="sm" variant="outline" onClick={() => { setDraft(null); setNotice('Layout changes discarded.') }}><X className="h-3.5 w-3.5" />Cancel</Button>
          <Button size="sm" onClick={() => void save()} disabled={busy}><Save className="h-3.5 w-3.5" />{busy ? 'Saving…' : 'Save layout'}</Button>
        </> : <>
          {saved && template && <Button size="sm" variant="outline" disabled={busy} onClick={() => void inheritTemplate()}>Use vendor/type template</Button>}
          <Button size="sm" variant="outline" onClick={() => { onStartEdit?.(); setSaveScope('device'); setStandardDraft(false); setDraft(layout); setNotice('Layout editing enabled.') }}><Settings2 className="h-3.5 w-3.5" />Edit layout</Button>
        </>}
      </div>
    </div>
    {editing && saveScope === 'template' && <p className="text-xs text-muted">Applies to this tab for {templateLabel} devices in your account. Device-specific layouts are kept; choose “Use vendor/type template” on those devices to inherit it.</p>}
    {editing && !desktop && <p className="text-xs text-muted">Widgets stack on smaller screens. Use the size controls or arrow keys; desktop positions are preserved.</p>}
    <div role="status" aria-live="polite" className="sr-only">{notice}</div>
    <div ref={grid} className={cn('device-widget-grid', editing && 'is-editing', desktop && 'is-desktop', (standard || focused) && 'is-standard', !standard && !focused && 'is-fixed')} style={standard || focused ? undefined : desktop ? { gridTemplateColumns: 'repeat(12, minmax(0, 1fr))', gridAutoRows: `${GRID_ROW}px`, gap: GRID_GAP } : { gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: GRID_GAP }}>
      {standard || focused ? <>
        {summaries.length > 0 && <div className="device-summary-grid" aria-label="Key device readings">{summaries.map(item => {
          const node = renderWidget(item)
          return <div key={item.id} className="min-w-0">{node}</div>
        })}</div>}
        <div className="device-panel-grid">{panels.map(renderWidget)}</div>
      </> : ordered.map(renderWidget)}
    </div>
  </div>
}
