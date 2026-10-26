/**
 * Time zone picker for a report schedule: "Organisation time zone" (the
 * default, stored as the organisation's zone when the schedule is saved) or
 * any IANA zone, searchable by city or offset ("Riyadh", "GMT+3").
 */
import { useMemo, useState, type KeyboardEvent } from 'react'
import * as Popover from '@radix-ui/react-popover'
import { Building2, Check, ChevronsUpDown, Search } from 'lucide-react'
import { cn } from '@/lib/utils'
import { timezoneList, zoneLabel } from '@/lib/branding'
import { zoneName } from './model'

export function TimezoneSelect({
  id, value, orgTz, at, onChange, invalid,
}: {
  id?: string
  /** '' = the organisation time zone. */
  value: string
  orgTz: string
  /** The instant the offsets are shown for (the next run), so DST is right. */
  at: Date
  onChange: (tz: string) => void
  invalid?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const zones = useMemo(() => timezoneList(), [])
  const effective = value || orgTz
  const options = useMemo(() => {
    const s = q.trim().toLowerCase().replace(/\s+/g, '_')
    const match = (z: string) => !s || z.toLowerCase().includes(s) || zoneLabel(z, at).toLowerCase().includes(s)
    const list = zones.filter(match).slice(0, 400).map((z) => ({ value: z, label: zoneName(z) }))
    const org = { value: '', label: `Organisation time zone · ${zoneName(orgTz)}` }
    return !s || match(orgTz) || 'organisation'.includes(s) ? [org, ...list] : list
  }, [q, zones, orgTz, at])

  const pick = (v: string) => { onChange(v); setOpen(false); setQ('') }
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(options.length - 1, a + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(0, a - 1)) }
    else if (e.key === 'Enter') { e.preventDefault(); if (options[active]) pick(options[active].value) }
  }

  return (
    <Popover.Root open={open} onOpenChange={(o) => { setOpen(o); if (!o) setQ(''); setActive(0) }}>
      <Popover.Trigger asChild>
        <button
          id={id}
          type="button"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-invalid={invalid || undefined}
          className={cn(
            'flex h-9 w-full items-center justify-between gap-2 rounded-md border bg-surface px-3 text-sm text-text hover:bg-surface2',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40',
            invalid ? 'border-danger' : 'border-border',
          )}
        >
          <span className="flex min-w-0 items-center gap-1.5">
            {!value && <Building2 className="h-3.5 w-3.5 shrink-0 text-muted" aria-hidden />}
            <span className="truncate">{zoneName(effective)}</span>
            {!value && <span className="shrink-0 text-xs text-muted">(organisation)</span>}
          </span>
          <span className="flex shrink-0 items-center gap-2 text-xs tabular-nums text-muted">
            {zoneLabel(effective, at)}
            <ChevronsUpDown className="h-3.5 w-3.5" aria-hidden />
          </span>
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={4}
          collisionPadding={12}
          className="z-50 w-[var(--radix-popover-trigger-width)] min-w-[300px] rounded-md border border-border bg-surface p-1 shadow-elevated"
        >
          <div className="flex items-center gap-2 border-b border-border px-2 pb-1.5 pt-1">
            <Search className="h-3.5 w-3.5 text-muted" aria-hidden />
            <input
              autoFocus
              value={q}
              onChange={(e) => { setQ(e.target.value); setActive(0) }}
              onKeyDown={onKey}
              aria-label="Search time zones"
              placeholder="Search city or offset (e.g. Riyadh, GMT+3)"
              className="h-7 w-full bg-transparent text-sm text-text outline-none placeholder:text-muted"
            />
          </div>
          <div role="listbox" aria-label="Time zones" className="max-h-72 overflow-y-auto py-1">
            {options.length === 0 && <div className="px-2 py-3 text-center text-xs text-muted">No matching time zone</div>}
            {options.map((o, i) => {
              const tz = o.value || orgTz
              const selected = o.value === value
              return (
                <button
                  key={o.value || '__org'}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => pick(o.value)}
                  className={cn(
                    'flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-left text-sm',
                    i === active && 'bg-surface2',
                    selected && 'text-primary',
                    !o.value && 'border-b border-border',
                  )}
                >
                  <span className="truncate">{o.label}</span>
                  <span className="flex shrink-0 items-center gap-1.5 text-xs tabular-nums text-muted">
                    {zoneLabel(tz, at)}
                    {selected && <Check className="h-3.5 w-3.5 text-primary" aria-hidden />}
                  </span>
                </button>
              )
            })}
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}
