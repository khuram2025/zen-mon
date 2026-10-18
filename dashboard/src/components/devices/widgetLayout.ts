export type WidgetPosition = { id: string; x: number; y: number; w: number; h: number }
export type WidgetDefinition = { id: string; width: number; height: number; minWidth?: number; minHeight?: number }
export const GRID_COLUMNS = 12
export const GRID_ROW = 32
export const GRID_GAP = 12
const bounded = (value: unknown, fallback: number, min: number, max: number) => typeof value === 'number' && Number.isFinite(value) ? Math.max(min, Math.min(max, Math.round(value))) : fallback
export function overlaps(a: WidgetPosition, b: WidgetPosition): boolean {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y
}
function settle(items: WidgetPosition[], priority?: string): WidgetPosition[] {
  const placed: WidgetPosition[] = []
  const ordered = [...items].sort((a,b) => Number(b.id === priority) - Number(a.id === priority))
  for (const original of ordered) {
    const item = { ...original }
    let collision: WidgetPosition | undefined
    while ((collision = placed.find(other => overlaps(item, other)))) item.y = collision.y + collision.h
    placed.push(item)
  }
  return items.map(item => placed.find(placedItem => placedItem.id === item.id)!)
}
export function defaultWidgetLayout(widgets: WidgetDefinition[]): WidgetPosition[] {
  const items: WidgetPosition[] = []
  for (const widget of widgets) {
    const w = bounded(widget.width, 6, widget.minWidth || 2, GRID_COLUMNS)
    const h = bounded(widget.height, 8, widget.minHeight || 3, 24)
    let placed = false
    for (let y = 0; !placed; y++) {
      for (let x = 0; x <= GRID_COLUMNS - w; x++) {
        const item = { id: widget.id, x, y, w, h }
        if (!items.some(other => overlaps(item, other))) { items.push(item); placed = true; break }
      }
    }
  }
  return items
}
export function restoreWidgetLayout(widgets: WidgetDefinition[], saved: unknown): WidgetPosition[] {
  const defaults = defaultWidgetLayout(widgets)
  const entries = Array.isArray(saved) ? saved : []
  const restored = widgets.map((widget, index) => {
    const fallback = defaults[index]
    const entry = entries.find(value => value && typeof value === 'object' && value.id === widget.id)
    if (!entry) return fallback
    const w = bounded(entry.w, fallback.w, widget.minWidth || 2, GRID_COLUMNS)
    return { id: widget.id, w, h: bounded(entry.h, fallback.h, widget.minHeight || 3, 24), x: bounded(entry.x, fallback.x, 0, GRID_COLUMNS - w), y: bounded(entry.y, fallback.y, 0, 200) }
  })
  // Preserve existing positions; newly collected widgets fill space afterwards.
  const existing = restored.filter(item => entries.some(entry => entry?.id === item.id))
  const added = restored.filter(item => !entries.some(entry => entry?.id === item.id))
  const settled = settle([...existing, ...added])
  return widgets.map(widget => settled.find(item => item.id === widget.id)!)
}
export function changeWidgetLayout(items: WidgetPosition[], definition: WidgetDefinition, change: Partial<WidgetPosition>): WidgetPosition[] {
  const current = items.find(item => item.id === definition.id)
  if (!current) return items
  const w = bounded(change.w, current.w, definition.minWidth || 2, GRID_COLUMNS)
  const h = bounded(change.h, current.h, definition.minHeight || 3, 24)
  const x = bounded(change.x ?? current.x, current.x, 0, GRID_COLUMNS - w)
  const y = bounded(change.y, current.y, 0, 200)
  return settle(items.map(item => item.id === current.id ? { ...current, x, y, w, h } : item), current.id)
}
export function readWidgetLayout(raw: string | null): WidgetPosition[] | null {
  if (!raw) return null
  try { const parsed = JSON.parse(raw); return parsed.version === 1 && Array.isArray(parsed.items) ? parsed.items.filter((item: unknown) => item && typeof item === 'object' && typeof (item as WidgetPosition).id === 'string').slice(0, 100) : null } catch { return null }
}
export function mergeWidgetLayout(saved: WidgetPosition[] | null, current: WidgetPosition[]): WidgetPosition[] {
  return [...(saved || []).filter(item => !current.some(next => next.id === item.id)), ...current]
}
export function widgetLayoutKey(userId: string, deviceId: string, section = 'overview'): string {
  return `zenplus:device-widgets:v1:${userId}:${deviceId}${section === 'overview' ? '' : `:tab:${section}`}`
}

export function widgetTemplateKey(userId: string, vendor: string | undefined, deviceType: string | undefined, section = 'overview'): string | null {
  const normalize = (value?: string) => (value || '').trim().toLowerCase().replace(/\s+/g, ' ')
  const maker = normalize(vendor), type = normalize(deviceType)
  if (!maker || !type || ['unknown', 'not reported', 'n/a'].includes(maker)) return null
  return `zenplus:device-widget-template:v1:${encodeURIComponent(userId)}:${encodeURIComponent(maker)}:${encodeURIComponent(type)}:${encodeURIComponent(section)}`
}
export function effectiveWidgetLayout(own: WidgetPosition[] | null, template: WidgetPosition[] | null): WidgetPosition[] | null {
  return own ?? template
}

/* ── Server-saved layouts (migrate-165) ───────────────────────────────────
 * The server keeps each user's layouts (GET/PUT/DELETE /devices/{id}/widget-layouts/{section}), so they follow
 * the user to another browser. Browser storage stays as a cache that renders at once; an entry marked
 * `synced` mirrors the server, an unmarked one was saved only in this browser (before this release, or while
 * the server was unreachable) and is uploaded the first time the server has nothing for that slot. */

export type StoredLayout = { items: WidgetPosition[]; synced: boolean }

export function readStoredLayout(raw: string | null): StoredLayout | null {
  const items = readWidgetLayout(raw)
  if (!items) return null
  try { return { items, synced: JSON.parse(raw as string).synced === true } } catch { return null }
}

export function storedLayoutValue(items: WidgetPosition[], synced: boolean): string {
  return JSON.stringify({ version: 1, items, ...(synced ? { synced: true } : {}) })
}

/** Vendor and device type as the server keys a vendor/type template (null: no shared template). */
export function layoutTemplateIdentity(vendor: string | undefined, deviceType: string | undefined): { vendor: string; device_type: string } | null {
  const normalize = (value?: string) => (value || '').trim().toLowerCase().replace(/\s+/g, ' ')
  const maker = normalize(vendor), type = normalize(deviceType)
  if (!maker || !type || ['unknown', 'not reported', 'n/a'].includes(maker)) return null
  return { vendor: maker, device_type: type }
}

export type LayoutReconcile = { items: WidgetPosition[] | null; local: 'keep' | 'write' | 'remove'; upload: boolean }

/** What one layout slot (this device's tab, or the vendor/type template's tab) shows once the server answered.
 *  `server` undefined: not answered or unavailable, so the browser copy is used as is. */
export function reconcileLayout(local: StoredLayout | null, server: WidgetPosition[] | null | undefined): LayoutReconcile {
  if (server === undefined) return { items: local?.items ?? null, local: 'keep', upload: false }
  if (server !== null) return { items: server, local: 'write', upload: false }
  if (local && !local.synced) return { items: local.items, local: 'keep', upload: true }
  // A synced copy the server no longer has was reset on another browser.
  if (local) return { items: null, local: 'remove', upload: false }
  return { items: null, local: 'keep', upload: false }
}
