import { useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight, Search } from 'lucide-react'
import { cn } from '@/lib/utils'
import { NetworkIcon, iconLabel } from '@/components/network-icons'
import { useDevices } from '../useMapData'
import {
  PALETTE_ICONS,
  STATUS_COLOR,
  STATUS_ORDER,
  TYPE_TO_ICON,
  statusKey,
  type NodeStatus,
} from '../core'

/** DnD payload type — matches the canvas drop handler + the v1 editor. */
export const DEVICE_DND_TYPE = 'application/x-zenplus-device'

/* Left rail: searchable, status-filtered list of devices. Drag a row onto the
 * canvas to place it as a node. Mirrors the v1 "Device Palette" so both editors
 * feel the same; data comes from the shared /devices endpoint. */
export function DevicePalette({
  open,
  onToggle,
  usedIds,
  disabled,
}: {
  open: boolean
  onToggle: () => void
  usedIds: Set<string>
  disabled: boolean
}) {
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState<'all' | NodeStatus>('all')
  const devicesQuery = useDevices()
  const allDevices = devicesQuery.data?.data || []

  const devices = useMemo(() => {
    const q = search.trim().toLowerCase()
    return allDevices.filter((d) => {
      if (status !== 'all' && d.status !== status) return false
      if (!q) return true
      return (
        d.hostname?.toLowerCase().includes(q) ||
        d.ip_address?.toLowerCase().includes(q) ||
        d.device_type?.toLowerCase().includes(q)
      )
    })
  }, [allDevices, search, status])

  if (!open) {
    return (
      <aside className="flex w-9 shrink-0 flex-col border-r border-border bg-surface">
        <button
          type="button"
          onClick={onToggle}
          className="flex h-full w-full items-center justify-center text-muted hover:text-text"
          title="Show device palette"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      </aside>
    )
  }

  return (
    <aside className="flex w-72 shrink-0 flex-col border-r border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <div>
          <div className="text-xs font-semibold text-text">Device Palette</div>
          <div className="text-[10px] text-muted">Drag onto canvas to place</div>
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

      {/* Search + status filter */}
      <div className="space-y-2 border-b border-border p-3">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search hostname, IP, type…"
            className="w-full rounded-md border border-border bg-surface px-2 py-1.5 pl-7 text-xs text-text outline-none focus:border-primary/60"
          />
        </div>
        <div className="flex flex-wrap gap-1">
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
        </div>
      </div>

      {/* Icon legend */}
      <div className="border-b border-border px-3 py-2">
        <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Icon styles</div>
        <div className="grid grid-cols-7 gap-1">
          {PALETTE_ICONS.map((key) => (
            <div
              key={key}
              title={iconLabel[key]}
              className="flex aspect-square items-center justify-center rounded border border-border bg-surface2/40 p-1 text-text2"
            >
              <NetworkIcon name={key} className="h-4 w-4" />
            </div>
          ))}
        </div>
      </div>

      {/* Device list */}
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
              const grabbable = !used && !disabled
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
                  )}
                  title={used ? 'Already on this map' : disabled ? 'Select or create a map first' : 'Drag onto canvas'}
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
    </aside>
  )
}
