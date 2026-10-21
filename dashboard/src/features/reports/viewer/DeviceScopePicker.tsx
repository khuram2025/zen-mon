import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import * as Popover from '@radix-ui/react-popover'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Check, ChevronDown, Loader2, Search, Server, X } from 'lucide-react'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { QueryFailure } from '@/components/ui/QueryState'
import { fmtNumber } from '../render/format'

interface DeviceLite {
  id: string
  hostname: string | null
  ip_address: string | null
  device_type?: string | null
  location?: string | null
  status?: string | null
}

const PAGE = 200

/** Group, location and device-type scope (each: any of its values; all given filters must match). */
export interface DeviceScopeExtra {
  groupIds: string[]
  locations: string[]
  deviceTypes: string[]
}

export const EMPTY_SCOPE_EXTRA: DeviceScopeExtra = { groupIds: [], locations: [], deviceTypes: [] }

type ScopeTab = 'devices' | 'groups' | 'locations' | 'types'

interface GroupLite {
  id: string
  name: string
  device_count?: number
}

function sameList(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x) => b.includes(x))
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`
}

/** A searchable checklist for groups, locations or device types. */
function ScopeOptionList({ label, options, selected, onToggle, loading, error, onRetry, empty }: {
  label: string
  options: { value: string; label: string; hint?: string }[]
  selected: string[]
  onToggle: (value: string) => void
  loading: boolean
  error: unknown
  onRetry: () => void
  empty: string
}) {
  const [q, setQ] = useState('')
  const id = useId()
  const needle = q.trim().toLowerCase()
  const shown = needle ? options.filter((o) => o.label.toLowerCase().includes(needle)) : options
  return (
    <>
      <div className="border-b border-border p-2">
        <label htmlFor={`${id}-q`} className="sr-only">{`Search ${label.toLowerCase()}`}</label>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
          <input
            id={`${id}-q`}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={`Search ${label.toLowerCase()}…`}
            autoComplete="off"
            className="h-8 w-full rounded-md border border-border bg-surface pl-7 pr-2 text-xs text-text placeholder:text-muted focus-visible:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
          />
        </div>
      </div>
      <div className="max-h-72 min-h-[6rem] overflow-y-auto p-1">
        {error && !options.length ? (
          <QueryFailure compact error={error} what={label.toLowerCase()} onRetry={onRetry} retrying={loading} />
        ) : loading && !options.length ? (
          <p className="flex items-center justify-center gap-2 px-3 py-6 text-xs text-muted">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> {`Loading ${label.toLowerCase()}…`}
          </p>
        ) : shown.length === 0 ? (
          <p className="px-3 py-6 text-center text-xs text-muted">{needle ? `Nothing matches “${q.trim()}”` : empty}</p>
        ) : (
          <ul role="group" aria-label={label}>
            {shown.map((o) => {
              const on = selected.includes(o.value)
              return (
                <li key={o.value}>
                  <label className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs hover:bg-surface2">
                    <input type="checkbox" className="sr-only" checked={on} onChange={() => onToggle(o.value)} />
                    <span
                      aria-hidden
                      className={cn(
                        'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border',
                        on ? 'border-primary bg-primary text-on-primary' : 'border-border-strong',
                      )}
                    >
                      {on && <Check className="h-2.5 w-2.5" />}
                    </span>
                    <span className="min-w-0 flex-1 truncate font-medium text-text">{o.label}</span>
                    {o.hint && <span className="shrink-0 text-[10px] text-muted">{o.hint}</span>}
                  </label>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </>
  )
}

function deviceLabel(d: DeviceLite | undefined, id: string): string {
  if (!d) return `Device ${id.slice(0, 8)}`
  return d.hostname || (d.ip_address || '').split('/')[0] || `Device ${id.slice(0, 8)}`
}

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

export interface DeviceScopePickerProps {
  /** Selected device ids. */
  selected: string[]
  /** Called on Apply with the device ids (when ``onScopeChange`` is not given). */
  onChange: (ids: string[]) => void
  /** Group / location / device-type scope; shown as extra tabs when ``onScopeChange`` is given. */
  scope?: DeviceScopeExtra
  /** Called on Apply with the devices and the group / location / type scope together. */
  onScopeChange?: (ids: string[], scope: DeviceScopeExtra) => void
}

/**
 * Device scope for node-aware sections: a searchable multi-select listbox
 * (combobox pattern — arrows move, Enter toggles, Escape closes), plus —
 * when ``onScopeChange`` is given — device groups, locations and device
 * types. Every chosen filter must match. Choices are staged and applied
 * together so the report renders once.
 */
export function DeviceScopePicker({ selected, onChange, scope, onScopeChange }: DeviceScopePickerProps) {
  const extended = !!onScopeChange
  const current = scope ?? EMPTY_SCOPE_EXTRA
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<ScopeTab>('devices')
  const [q, setQ] = useState('')
  const [draft, setDraft] = useState<string[]>(selected)
  const [draftScope, setDraftScope] = useState<DeviceScopeExtra>(current)
  const [active, setActive] = useState(0)
  const search = useDebounced(q.trim(), 250)
  const known = useRef(new Map<string, DeviceLite>())
  const listRef = useRef<HTMLUListElement>(null)
  const id = useId()
  const listId = `${id}-list`

  const devices = useQuery<{ rows: DeviceLite[]; total: number }>({
    queryKey: ['devices', 'report-scope', search],
    queryFn: async () => {
      const res = (await api.get('/devices', { params: { limit: PAGE, ...(search ? { search } : {}) } })).data
      const rows = (Array.isArray(res) ? res : res?.data ?? []) as DeviceLite[]
      return { rows, total: Number(res?.meta?.total ?? rows.length) }
    },
    enabled: open,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  })
  const rows = devices.data?.rows ?? []
  for (const d of rows) known.current.set(d.id, d)

  const groups = useQuery<GroupLite[]>({
    queryKey: ['devices', 'groups', 'report-scope'],
    queryFn: async () => ((await api.get('/devices/groups')).data ?? []) as GroupLite[],
    enabled: extended && (open || current.groupIds.length > 0),
    staleTime: 5 * 60_000,
  })
  const locations = useQuery<string[]>({
    queryKey: ['devices', 'locations', 'report-scope'],
    queryFn: async () => ((await api.get('/devices/locations')).data ?? []) as string[],
    enabled: extended && open && tab === 'locations',
    staleTime: 5 * 60_000,
  })
  const types = useQuery<string[]>({
    queryKey: ['devices', 'device-types', 'report-scope'],
    queryFn: async () => ((await api.get('/devices/device-types')).data ?? []) as string[],
    enabled: extended && open && tab === 'types',
    staleTime: 5 * 60_000,
  })
  const groupName = (gid: string) => groups.data?.find((g) => g.id === gid)?.name ?? `Group ${gid.slice(0, 8)}`

  useEffect(() => {
    if (open) {
      setDraft(selected)
      setDraftScope(current)
      setTab('devices')
      setQ('')
      setActive(0)
    }
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => setActive(0), [search])

  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active])

  const draftSet = useMemo(() => new Set(draft), [draft])
  const toggle = (devId: string) =>
    setDraft((cur) => (cur.includes(devId) ? cur.filter((x) => x !== devId) : [...cur, devId]))
  const toggleIn = (key: keyof DeviceScopeExtra, value: string) =>
    setDraftScope((cur) => ({
      ...cur,
      [key]: cur[key].includes(value) ? cur[key].filter((x) => x !== value) : [...cur[key], value],
    }))
  const changed = !sameList(draft, selected)
    || (extended && (!sameList(draftScope.groupIds, current.groupIds)
      || !sameList(draftScope.locations, current.locations)
      || !sameList(draftScope.deviceTypes, current.deviceTypes)))
  const draftCount = draft.length + draftScope.groupIds.length + draftScope.locations.length + draftScope.deviceTypes.length

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((i) => Math.min(rows.length - 1, i + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((i) => Math.max(0, i - 1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (rows[active]) toggle(rows[active].id)
    } else if (e.key === 'PageDown') {
      e.preventDefault()
      setActive((i) => Math.min(rows.length - 1, i + 8))
    } else if (e.key === 'PageUp') {
      e.preventDefault()
      setActive((i) => Math.max(0, i - 8))
    }
  }

  const apply = () => {
    if (onScopeChange) onScopeChange(draft, draftScope)
    else onChange(draft)
    setOpen(false)
  }

  const single = selected.length === 1 ? known.current.get(selected[0]) : undefined
  const parts: string[] = []
  if (selected.length) parts.push(single ? deviceLabel(single, selected[0]) : plural(selected.length, 'device', 'devices'))
  if (extended) {
    const { groupIds, locations: locs, deviceTypes } = current
    if (groupIds.length) parts.push(groupIds.length === 1 ? groupName(groupIds[0]) : plural(groupIds.length, 'group', 'groups'))
    if (locs.length) parts.push(locs.length === 1 ? locs[0] : plural(locs.length, 'location', 'locations'))
    if (deviceTypes.length) parts.push(deviceTypes.length === 1 ? deviceTypes[0] : plural(deviceTypes.length, 'type', 'types'))
  }
  const scoped = parts.length > 0
  const label = scoped ? parts.join(' · ') : 'All devices'
  const tabs: { key: ScopeTab; label: string; count: number }[] = [
    { key: 'devices', label: 'Devices', count: draft.length },
    { key: 'groups', label: 'Groups', count: draftScope.groupIds.length },
    { key: 'locations', label: 'Locations', count: draftScope.locations.length },
    { key: 'types', label: 'Types', count: draftScope.deviceTypes.length },
  ]

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <Button
          variant="outline"
          size="sm"
          className={cn('h-8 gap-1.5', scoped && 'border-primary/50 bg-primary/5')}
          aria-label={`Device scope: ${scoped ? label : 'all devices'}`}
        >
          <Server className="h-3.5 w-3.5 text-primary" aria-hidden />
          <span className="max-w-[10rem] truncate text-xs font-semibold">{label}</span>
          <ChevronDown className={cn('h-3 w-3 opacity-60 transition-transform', open && 'rotate-180')} aria-hidden />
        </Button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={6}
          collisionPadding={12}
          aria-label="Device scope"
          onOpenAutoFocus={(e) => {
            e.preventDefault()
            document.getElementById(`${id}-q`)?.focus()
          }}
          className="z-50 flex w-[min(24rem,calc(100vw-1.5rem))] flex-col overflow-hidden rounded-lg border border-border bg-surface text-text shadow-elevated animate-fade-in"
        >
          <div className="border-b border-border px-3 py-2">
            <p className="text-xs font-semibold text-text">Limit the report to devices</p>
            <p className="text-[11px] text-muted">
              {extended
                ? 'Device sections use only the devices that match every choice. Leave empty for every device you can see.'
                : 'Device sections use only the devices you choose. Leave empty for every device you can see.'}
            </p>
            {extended && (
              <div role="tablist" aria-label="Scope by" className="mt-2 flex gap-1">
                {tabs.map((t) => (
                  <button
                    key={t.key}
                    type="button"
                    role="tab"
                    aria-selected={tab === t.key}
                    onClick={() => setTab(t.key)}
                    className={cn(
                      'rounded-md px-2 py-1 text-[11px] font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
                      tab === t.key ? 'bg-primary/10 text-primary' : 'text-muted hover:bg-surface2 hover:text-text',
                    )}
                  >
                    {t.label}{t.count ? ` (${t.count})` : ''}
                  </button>
                ))}
              </div>
            )}
          </div>

          {tab === 'groups' && (
            <ScopeOptionList
              label="Device groups"
              options={(groups.data ?? []).map((g) => ({
                value: g.id, label: g.name, hint: g.device_count !== undefined ? fmtNumber(g.device_count) : undefined,
              }))}
              selected={draftScope.groupIds}
              onToggle={(v) => toggleIn('groupIds', v)}
              loading={groups.isFetching}
              error={groups.error}
              onRetry={() => void groups.refetch()}
              empty="No device groups"
            />
          )}
          {tab === 'locations' && (
            <ScopeOptionList
              label="Locations"
              options={(locations.data ?? []).filter(Boolean).map((l) => ({ value: l, label: l }))}
              selected={draftScope.locations}
              onToggle={(v) => toggleIn('locations', v)}
              loading={locations.isFetching}
              error={locations.error}
              onRetry={() => void locations.refetch()}
              empty="No device has a location"
            />
          )}
          {tab === 'types' && (
            <ScopeOptionList
              label="Device types"
              options={(types.data ?? []).filter(Boolean).map((t) => ({ value: t, label: t }))}
              selected={draftScope.deviceTypes}
              onToggle={(v) => toggleIn('deviceTypes', v)}
              loading={types.isFetching}
              error={types.error}
              onRetry={() => void types.refetch()}
              empty="No device types"
            />
          )}

          {tab === 'devices' && <>
          <div className="border-b border-border p-2">
            <label htmlFor={`${id}-q`} className="sr-only">Search devices</label>
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
              <input
                id={`${id}-q`}
                role="combobox"
                aria-expanded="true"
                aria-controls={listId}
                aria-autocomplete="list"
                aria-activedescendant={rows[active] ? `${id}-o-${active}` : undefined}
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Search hostname or IP…"
                autoComplete="off"
                className="h-8 w-full rounded-md border border-border bg-surface pl-7 pr-7 text-xs text-text placeholder:text-muted focus-visible:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
              />
              {devices.isFetching && <Loader2 className="absolute right-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin text-muted" aria-hidden />}
            </div>
            {draft.length > 0 && (
              <ul role="list" aria-label="Selected devices" className="mt-2 flex max-h-16 flex-wrap gap-1 overflow-y-auto">
                {draft.map((devId) => {
                  const name = deviceLabel(known.current.get(devId), devId)
                  return (
                    <li key={devId} className="inline-flex items-center gap-0.5 rounded-md border border-primary/30 bg-primary/10 py-0.5 pl-1.5 pr-0.5 text-[11px] text-text">
                      <span className="max-w-[9rem] truncate">{name}</span>
                      <button
                        type="button"
                        onClick={() => toggle(devId)}
                        aria-label={`Remove ${name}`}
                        className="rounded p-0.5 text-muted hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                      >
                        <X className="h-3 w-3" aria-hidden />
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>

          <div className="max-h-72 min-h-[6rem] overflow-y-auto p-1">
            {devices.isError && !devices.data ? (
              <QueryFailure compact error={devices.error} what="devices" onRetry={() => void devices.refetch()} retrying={devices.isFetching} />
            ) : devices.isLoading ? (
              <p className="flex items-center justify-center gap-2 px-3 py-6 text-xs text-muted">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> Loading devices…
              </p>
            ) : rows.length === 0 ? (
              <p className="px-3 py-6 text-center text-xs text-muted">{search ? `No device matches “${search}”` : 'No devices are visible to you'}</p>
            ) : (
              <ul ref={listRef} id={listId} role="listbox" aria-multiselectable="true" aria-label="Devices">
                {rows.map((d, i) => {
                  const on = draftSet.has(d.id)
                  return (
                    <li
                      key={d.id}
                      id={`${id}-o-${i}`}
                      data-idx={i}
                      role="option"
                      aria-selected={on}
                      onMouseDown={(e) => e.preventDefault()}
                      onMouseEnter={() => setActive(i)}
                      onClick={() => toggle(d.id)}
                      className={cn(
                        'flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs',
                        i === active && 'bg-surface2',
                        on && 'text-text',
                      )}
                    >
                      <span
                        aria-hidden
                        className={cn(
                          'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border',
                          on ? 'border-primary bg-primary text-on-primary' : 'border-border-strong',
                        )}
                      >
                        {on && <Check className="h-2.5 w-2.5" />}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-medium text-text">{deviceLabel(d, d.id)}</span>
                        {(d.location || d.device_type) && (
                          <span className="block truncate text-[10px] text-muted">{[d.device_type, d.location].filter(Boolean).join(' · ')}</span>
                        )}
                      </span>
                      <span className="shrink-0 font-mono text-[10px] text-muted">{(d.ip_address || '').split('/')[0]}</span>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>

          {devices.data && devices.data.total > rows.length && (
            <p className="border-t border-border px-3 py-1.5 text-[11px] text-muted">
              Showing {fmtNumber(rows.length)} of {fmtNumber(devices.data.total)} — search to narrow the list.
            </p>
          )}
          </>}

          <div className="flex items-center gap-2 border-t border-border bg-surface2/40 px-3 py-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-8 text-xs"
              disabled={draftCount === 0}
              onClick={() => {
                setDraft([])
                setDraftScope(EMPTY_SCOPE_EXTRA)
              }}
            >
              Clear
            </Button>
            <span className="ml-auto text-[11px] text-muted" aria-live="polite">
              {draftCount ? `${draftCount} selected` : 'All devices'}
            </span>
            <Popover.Close asChild>
              <Button type="button" variant="outline" size="sm" className="h-8 text-xs">Cancel</Button>
            </Popover.Close>
            <Button type="button" size="sm" className="h-8 text-xs" disabled={!changed} onClick={apply}>
              Apply
            </Button>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}
