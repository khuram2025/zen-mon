import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import * as Popover from '@radix-ui/react-popover'
import { AppWindow, Check, ChevronDown, Loader2, Search } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { QueryFailure } from '@/components/ui/QueryState'
import { useRumApplications } from '../api'
import { fmtNumber } from '../render/format'

/** One option per application, plus "All applications" (value null) first. */
type Option = { value: string | null; title: string; detail?: string }

/**
 * Application scope for browser-RUM sections: a searchable single-select
 * listbox. Choosing applies at once (one render).
 */
export function ApplicationScopePicker({ selected, onChange }: { selected: string | null; onChange: (id: string | null) => void }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const listRef = useRef<HTMLUListElement>(null)
  const id = useId()

  const apps = useRumApplications(open || !!selected)

  const options = useMemo<Option[]>(() => {
    const s = q.trim().toLowerCase()
    const list = (apps.data ?? [])
      .filter((a) => !s || a.application_id.toLowerCase().includes(s) || (a.service_name || '').toLowerCase().includes(s))
      .map((a) => ({
        value: a.application_id,
        title: a.application_id,
        detail: `${a.service_name || 'No service name'} · ${fmtNumber(a.sessions)} session${a.sessions === 1 ? '' : 's'} in 30 days`,
      }))
    return s ? list : [{ value: null, title: 'All applications', detail: 'Every application you can see' }, ...list]
  }, [apps.data, q])

  useEffect(() => {
    if (!open) return
    setQ('')
    setActive(0)
  }, [open])
  useEffect(() => setActive(0), [q])
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [active])

  const choose = (value: string | null) => {
    if (value !== selected) onChange(value)
    setOpen(false)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((i) => Math.min(options.length - 1, i + 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((i) => Math.max(0, i - 1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const o = options[active]
      if (o) choose(o.value)
    }
  }

  const known = apps.data?.find((a) => a.application_id === selected)
  const label = selected ? known?.application_id ?? selected : 'All applications'

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <Button
          variant="outline"
          size="sm"
          className={cn('h-8 gap-1.5', selected && 'border-primary/50 bg-primary/5')}
          aria-label={`Application scope: ${label}`}
        >
          <AppWindow className="h-3.5 w-3.5 text-primary" aria-hidden />
          <span className="max-w-[10rem] truncate text-xs font-semibold">{label}</span>
          <ChevronDown className={cn('h-3 w-3 opacity-60 transition-transform', open && 'rotate-180')} aria-hidden />
        </Button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={6}
          collisionPadding={12}
          aria-label="Application scope"
          onOpenAutoFocus={(e) => {
            e.preventDefault()
            document.getElementById(`${id}-q`)?.focus()
          }}
          className="z-50 w-[min(22rem,calc(100vw-1.5rem))] overflow-hidden rounded-lg border border-border bg-surface text-text shadow-elevated animate-fade-in"
        >
          <div className="border-b border-border p-2">
            <label htmlFor={`${id}-q`} className="sr-only">Search applications</label>
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
              <input
                id={`${id}-q`}
                role="combobox"
                aria-expanded="true"
                aria-controls={`${id}-list`}
                aria-autocomplete="list"
                aria-activedescendant={options[active] ? `${id}-o-${active}` : undefined}
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Search applications…"
                autoComplete="off"
                className="h-8 w-full rounded-md border border-border bg-surface pl-7 pr-2 text-xs text-text placeholder:text-muted focus-visible:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
              />
            </div>
          </div>
          <div className="max-h-72 overflow-y-auto p-1">
            {apps.isError ? (
              <QueryFailure compact error={apps.error} what="applications" onRetry={() => void apps.refetch()} retrying={apps.isFetching} />
            ) : apps.isLoading ? (
              <p className="flex items-center justify-center gap-2 px-3 py-6 text-xs text-muted">
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> Loading applications…
              </p>
            ) : (
              <ul ref={listRef} id={`${id}-list`} role="listbox" aria-label="Applications">
                {options.map((o, i) => {
                  const on = o.value === selected
                  return (
                    <li
                      key={o.value ?? '__all'}
                      id={`${id}-o-${i}`}
                      data-idx={i}
                      role="option"
                      aria-selected={on}
                      onMouseDown={(e) => e.preventDefault()}
                      onMouseEnter={() => setActive(i)}
                      onClick={() => choose(o.value)}
                      className={cn('flex cursor-pointer items-center gap-2 rounded px-2 py-1.5', i === active && 'bg-surface2')}
                    >
                      <span className="min-w-0 flex-1">
                        <span className={cn('block truncate text-xs text-text', on ? 'font-semibold' : 'font-medium')}>{o.title}</span>
                        {o.detail && <span className="block truncate text-2xs text-muted">{o.detail}</span>}
                      </span>
                      {on && <Check className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />}
                    </li>
                  )
                })}
                {(apps.data ?? []).length === 0 && (
                  <li role="presentation" className="px-2.5 py-3 text-center text-xs text-muted">
                    No browser telemetry in the last 30 days — install a RUM key to populate this list.
                  </li>
                )}
                {(apps.data ?? []).length > 0 && options.length === 0 && (
                  <li role="presentation" className="px-2.5 py-3 text-center text-xs text-muted">No application matches “{q}”</li>
                )}
              </ul>
            )}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}
