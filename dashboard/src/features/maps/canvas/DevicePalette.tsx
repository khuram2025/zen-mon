import { useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight, Image as ImageIcon, Search } from 'lucide-react'
import { cn } from '@/lib/utils'
import { NetworkIcon, iconLabel, networkIcons, type IconKey } from '@/components/network-icons'
import { useDevices } from '../useMapData'
import {
  STATUS_COLOR,
  STATUS_ORDER,
  TYPE_TO_ICON,
  statusKey,
  type NodeStatus,
} from '../core'
import { ImageDialog, SHAPE_PRESETS, iconSpec, type ShapeSpec } from './Annotations'

/** DnD payload types — matched by the canvas drop handler. */
export const DEVICE_DND_TYPE = 'application/x-zenplus-device'
export const SHAPE_DND_TYPE = 'application/x-zenplus-shape'

type Tab = 'devices' | 'shapes'
const ICON_KEYS = Object.keys(networkIcons) as IconKey[]

/* Left rail (draw.io "shape library" + device inventory). Two tabs:
 *  • Devices — searchable, status-filtered list of monitored devices; drag a
 *    row onto the canvas to place it as a live node.
 *  • Shapes — icons, text, boxes, lines, widgets; drag to place, or click to
 *    drop at the viewport centre. */
export function DevicePalette({
  open,
  onToggle,
  usedIds,
  disabled,
  onInsert,
}: {
  open: boolean
  onToggle: () => void
  usedIds: Set<string>
  disabled: boolean
  /** Click-to-insert a shape/icon at the viewport centre. */
  onInsert?: (spec: ShapeSpec) => void
}) {
  const [tab, setTab] = useState<Tab>('devices')
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<'all' | NodeStatus>('all')
  const [hideUsed, setHideUsed] = useState(false)
  const [imgOpen, setImgOpen] = useState(false)
  const devicesQuery = useDevices()
  const allDevices = devicesQuery.data?.data || []

  const devices = useMemo(() => {
    const q = search.trim().toLowerCase()
    return allDevices.filter((d) => {
      if (status !== 'all' && d.status !== status) return false
      if (hideUsed && usedIds.has(d.id)) return false
      if (!q) return true
      return (
        d.hostname?.toLowerCase().includes(q) ||
        d.ip_address?.toLowerCase().includes(q) ||
        d.device_type?.toLowerCase().includes(q) ||
        (d.location || '').toLowerCase().includes(q)
      )
    })
  }, [allDevices, search, status, hideUsed, usedIds])

  if (!open) {
    return (
      <aside className="flex w-9 shrink-0 flex-col border-r border-border bg-surface">
        <button
          type="button"
          onClick={onToggle}
          className="flex h-full w-full items-center justify-center text-muted hover:text-text"
          title="Show palette"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </aside>
    )
  }

  const dragShape = (spec: ShapeSpec) => (e: React.DragEvent) => {
    e.dataTransfer.setData(SHAPE_DND_TYPE, JSON.stringify(spec))
    e.dataTransfer.effectAllowed = 'copy'
  }

  return (
    <aside className="flex w-72 shrink-0 flex-col border-r border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <div className="flex items-center rounded-md border border-border bg-surface2/40 p-0.5 text-[11px] font-semibold">
          <button type="button" onClick={() => setTab('devices')} className={cn('rounded px-2 py-0.5 transition', tab === 'devices' ? 'bg-primary/15 text-primary' : 'text-muted hover:text-text')}>Devices</button>
          <button type="button" onClick={() => setTab('shapes')} className={cn('rounded px-2 py-0.5 transition', tab === 'shapes' ? 'bg-primary/15 text-primary' : 'text-muted hover:text-text')}>Shapes</button>
        </div>
        <button
          type="button"
          onClick={onToggle}
          className="flex h-6 w-6 items-center justify-center rounded text-muted hover:bg-surface2 hover:text-text"
          title="Collapse"
        >
          <ChevronLeft className="h-3.5 w-3.5" />
        </button>
      </div>

      {tab === 'devices' ? (
        <>
          <div className="space-y-2 border-b border-border p-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search hostname, IP, type, site…"
                className="w-full rounded-md border border-border bg-surface px-2 py-1.5 pl-7 text-xs text-text outline-none focus:border-primary/60"
              />
            </div>
            <div className="flex flex-wrap items-center gap-1">
              {(['all', ...STATUS_ORDER] as const).map((s) => {
                const activeCls =
                  s === 'all' ? 'bg-primary/15 text-primary' :
                  s === 'up' ? 'bg-success/15 text-success' :
                  s === 'down' ? 'bg-danger/15 text-danger' :
                  s === 'degraded' ? 'bg-warning/15 text-warning' :
                  s === 'maintenance' ? 'bg-info/15 text-info' :
                  'bg-surface2 text-text2'
                return (
                  <button
                    key={s}
                    type="button"
                    onClick={() => setStatus(s)}
                    className={cn(
                      'rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide transition',
                      status === s ? activeCls : 'text-muted hover:bg-surface2 hover:text-text',
                    )}
                  >
                    {s === 'all' ? 'All' : String(s)}
                  </button>
                )
              })}
              <label className="ml-auto flex cursor-pointer items-center gap-1 text-[10px] text-muted" title="Hide devices already placed on this map">
                <input type="checkbox" checked={hideUsed} onChange={(e) => setHideUsed(e.target.checked)} className="accent-primary" /> hide placed
              </label>
            </div>
            <div className="text-[10px] text-muted">Drag a device onto the canvas to place it</div>
          </div>

          <div className="flex-1 overflow-y-auto p-2">
            {devicesQuery.isLoading ? (
              <div className="space-y-2 p-2">
                {[0, 1, 2, 3].map((i) => (
                  <div key={i} className="h-12 animate-pulse rounded-md bg-surface2/50" />
                ))}
              </div>
            ) : devices.length === 0 ? (
              <div className="rounded-md border border-dashed border-border p-4 text-center text-[11px] text-muted">
                No devices match these filters
              </div>
            ) : (
              <div className="space-y-1.5">
                {devices.map((d) => {
                  const used = usedIds.has(d.id)
                  const iconKey = TYPE_TO_ICON[d.device_type] || 'other'
                  const sk = statusKey(d.status)
                  const color = STATUS_COLOR[sk]
                  const grabbable = !disabled
                  return (
                    <div
                      key={d.id}
                      data-device-id={d.id}
                      data-testid="palette-device"
                      draggable={grabbable}
                      onDragStart={(e) => {
                        e.dataTransfer.setData(DEVICE_DND_TYPE, d.id)
                        e.dataTransfer.effectAllowed = 'copy'
                      }}
                      className={cn(
                        'group flex items-center gap-2 rounded-md border border-border bg-surface px-2 py-1.5 text-xs transition',
                        grabbable
                          ? 'cursor-grab hover:border-primary/45 hover:bg-primary/5 active:cursor-grabbing'
                          : 'opacity-50',
                        used && 'opacity-70',
                      )}
                      title={disabled ? 'Select or create a map first' : used ? 'Already on this map — drag again to add a second tile' : 'Drag onto canvas'}
                    >
                      <div className={cn('flex h-7 w-7 shrink-0 items-center justify-center rounded-md border bg-surface', color.ring)}>
                        <NetworkIcon name={iconKey} className="h-4 w-4" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1">
                          <span className="truncate font-medium text-text">{d.hostname}</span>
                          {used && <span className="rounded bg-primary/15 px-1 text-[9px] font-semibold text-primary">ON MAP</span>}
                        </div>
                        <div className="flex items-center gap-1.5 text-[10px] text-muted">
                          <span className={cn('h-1.5 w-1.5 rounded-full', color.dot)} />
                          <span className="truncate">{d.ip_address}</span>
                          <span className="text-muted/70">·</span>
                          <span className="capitalize">{d.device_type?.replace('_', ' ')}</span>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </>
      ) : (
        <div className="flex-1 overflow-y-auto p-3">
          <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Text & shapes</div>
          <div className="mb-3 grid grid-cols-2 gap-1.5">
            {SHAPE_PRESETS.filter((p) => p.key !== 'conversations').map((p) => (
              <button
                key={p.key}
                type="button"
                draggable={!disabled}
                onDragStart={dragShape(p.spec)}
                onClick={() => !disabled && onInsert?.(p.spec)}
                disabled={disabled}
                className="flex cursor-grab items-center gap-2 rounded-md border border-border bg-surface px-2 py-1.5 text-left text-[11px] text-text transition hover:border-primary/45 hover:bg-primary/5 active:cursor-grabbing disabled:opacity-50"
                title="Drag onto the canvas, or click to insert at the centre"
              >
                <span className="text-muted">{p.icon}</span>
                <span className="truncate">{p.label}</span>
              </button>
            ))}
          </div>

          <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Network icons</div>
          <div className="mb-3 grid grid-cols-5 gap-1.5">
            {ICON_KEYS.map((key) => (
              <button
                key={key}
                type="button"
                draggable={!disabled}
                onDragStart={dragShape(iconSpec(key))}
                onClick={() => !disabled && onInsert?.(iconSpec(key))}
                disabled={disabled}
                title={iconLabel[key]}
                className="flex aspect-square cursor-grab flex-col items-center justify-center gap-0.5 rounded-md border border-border bg-surface2/40 p-1 text-text2 transition hover:border-primary/45 hover:bg-primary/5 active:cursor-grabbing disabled:opacity-50"
              >
                <NetworkIcon name={key} className="h-5 w-5" />
                <span className="w-full truncate text-center text-[7.5px] leading-none text-muted">{iconLabel[key]}</span>
              </button>
            ))}
          </div>

          <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Media & widgets</div>
          <div className="grid grid-cols-1 gap-1.5">
            <button
              type="button"
              onClick={() => setImgOpen(true)}
              disabled={disabled}
              className="flex items-center gap-2 rounded-md border border-border bg-surface px-2 py-1.5 text-left text-[11px] text-text transition hover:border-primary/45 hover:bg-primary/5 disabled:opacity-50"
            >
              <span className="text-muted"><ImageIcon className="h-4 w-4" /></span> Image (URL or upload)…
            </button>
            {SHAPE_PRESETS.filter((p) => p.key === 'conversations').map((p) => (
              <button
                key={p.key}
                type="button"
                draggable={!disabled}
                onDragStart={dragShape(p.spec)}
                onClick={() => !disabled && onInsert?.(p.spec)}
                disabled={disabled}
                className="flex cursor-grab items-center gap-2 rounded-md border border-border bg-surface px-2 py-1.5 text-left text-[11px] text-text transition hover:border-primary/45 hover:bg-primary/5 active:cursor-grabbing disabled:opacity-50"
              >
                <span className="text-muted">{p.icon}</span> {p.label}
              </button>
            ))}
          </div>
          <p className="mt-3 text-[10px] leading-snug text-muted">
            Tip: right-click an empty spot on the canvas to add a shape exactly there. Double-click any shape to type a caption.
          </p>
          {imgOpen && <ImageDialog onCancel={() => setImgOpen(false)} onPick={(src) => { setImgOpen(false); onInsert?.({ kind: 'image', w_pct: 16, h_pct: 12, metadata: { src } }) }} />}
        </div>
      )}
    </aside>
  )
}
