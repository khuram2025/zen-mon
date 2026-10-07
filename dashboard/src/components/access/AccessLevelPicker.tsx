import { useMemo, useState } from 'react'
import {
  Activity, AppWindow, ChevronDown, ClipboardList, FileCode, FileText, Gauge, KeyRound, LayoutDashboard,
  Lock, Map as MapIcon, Network, Radar, ScanSearch, Search, Server, Settings, ShieldAlert, Siren, Users, Waypoints,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import type { PermissionCatalogModule } from '@/types'
import { Input } from '@/components/ui/Input'
import { RiskBadge } from './PermissionMatrix'
import {
  AREA_HINTS, applyLevel, extraPermissions, fullLabel, groupByArea, levelsFor, moduleLevel, pruneDangling,
  withDependencies, type AccessLevel,
} from './roleModel'

const MODULE_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  dashboard: LayoutDashboard, devices: Gauge, discovery: Radar, alerts: Siren, service_checks: Activity,
  netflow: Network, udt: ScanSearch, netpath: Waypoints, ncm: FileCode, servers: Server, apm: AppWindow,
  reports: FileText, maps: MapIcon, users: Users, audit: ClipboardList, settings: Settings, system: KeyRound,
}

const LEVEL_LABEL: Record<AccessLevel, string> = { none: 'No access', view: 'View', full: 'Full', custom: 'Custom' }

type Props = {
  modules: PermissionCatalogModule[]
  granted: Set<string>
  onChange: (next: Set<string>) => void
  superPerm: string
  canGrant: (id: string) => boolean
  /** Permissions present when the editor opened, to mark what changed. */
  baseline?: Set<string>
  /** Shown as "Clear all" next to the search box when set. */
  onClear?: () => void
}

/** Module-by-module access, grouped by product area. Each module offers
 *  presets (No access / View / Full) over its standard permissions; sensitive
 *  and privileged permissions are listed separately and ticked one by one. */
export function AccessLevelPicker({ modules, granted, onChange, superPerm, canGrant, baseline, onClear }: Props) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const full = granted.has(superPerm)

  const areas = useMemo(() => {
    const q = query.trim().toLowerCase()
    const shown = q
      ? modules.filter((m) => `${m.label} ${m.area || ''} ${m.permissions.map((p) => `${p.label} ${p.id} ${p.description}`).join(' ')}`.toLowerCase().includes(q))
      : modules
    return groupByArea(shown)
  }, [modules, query])

  function setLevel(m: PermissionCatalogModule, level: Exclude<AccessLevel, 'custom'>) {
    onChange(applyLevel(modules, m, level, granted, canGrant))
  }
  function toggle(id: string, on: boolean) {
    if (on) onChange(withDependencies(modules, granted, id))
    else { const next = new Set(granted); next.delete(id); onChange(pruneDangling(modules, next)) }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="relative w-full max-w-sm">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
          <Input className="h-8 pl-8 text-xs" placeholder="Find a module or permission…" aria-label="Find a module or permission" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        {onClear && <button type="button" onClick={onClear} className="rounded-md px-2 py-1 text-xs text-muted hover:bg-surface2 hover:text-text">Clear all</button>}
      </div>

      {areas.length === 0 && <p className="rounded-md border border-dashed border-border px-3 py-6 text-center text-xs text-muted">Nothing matches “{query}”.</p>}

      {areas.map(({ area, modules: ms }) => {
        const withAccess = ms.filter((m) => full || moduleLevel(m, granted) !== 'none').length
        return (
          <section key={area} aria-labelledby={`area-${area}`} className="overflow-hidden rounded-lg border border-border">
            <header className="flex items-start justify-between gap-3 border-b border-border bg-surface2/50 px-3 py-2">
              <div className="min-w-0">
                <h3 id={`area-${area}`} className="text-xs font-semibold uppercase tracking-wider text-text">{area}</h3>
                {AREA_HINTS[area] && <p className="text-[11px] text-muted">{AREA_HINTS[area]}</p>}
              </div>
              <span className={cn('shrink-0 rounded-full px-2 py-0.5 text-[10.5px] font-medium tabular-nums', withAccess ? 'bg-primary/10 text-primary' : 'bg-surface2 text-muted')}>
                {withAccess}/{ms.length} with access
              </span>
            </header>
            <ul className="divide-y divide-border">
              {ms.map((m) => {
                const Icon = MODULE_ICONS[m.module] || Settings
                const levels = levelsFor(m)
                const level = full && m.permissions.every((p) => p.id !== superPerm) ? 'full' : moduleLevel(m, granted)
                const extras = extraPermissions(m)
                const isOpen = open[m.module] ?? (levels.length === 0 || level === 'custom')
                const hint = m.permissions.find((p) => p.id.endsWith('.view'))?.description || m.permissions[0]?.description
                const changed = baseline ? m.permissions.some((p) => baseline.has(p.id) !== granted.has(p.id)) : false
                const isSystem = m.permissions.some((p) => p.id === superPerm)
                return (
                  <li key={m.module} className={cn('px-3 py-2.5', isSystem && 'bg-warning/[0.04]')}>
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                      <span className={cn('flex h-7 w-7 shrink-0 items-center justify-center rounded-md', level === 'none' ? 'bg-surface2 text-muted' : 'bg-primary/10 text-primary')}>
                        <Icon className="h-3.5 w-3.5" aria-hidden />
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5 text-sm font-medium">
                          {m.label}
                          {changed && <span className="rounded bg-primary/10 px-1 text-[9px] font-semibold uppercase tracking-wide text-primary">changed</span>}
                          {level === 'custom' && <span className="rounded border border-border px-1 text-[9px] font-semibold uppercase tracking-wide text-muted">custom</span>}
                        </div>
                        <p className="truncate text-[11px] text-muted" title={hint}>{hint}</p>
                      </div>

                      {levels.length > 0 && (
                        <div role="radiogroup" aria-label={`${m.label} access`} className="inline-flex shrink-0 overflow-hidden rounded-md border border-border text-[11px]">
                          {levels.map((lv) => {
                            const label = lv === 'full' ? fullLabel(m) : LEVEL_LABEL[lv]
                            const active = level === lv
                            const disabled = full || (lv !== 'none' && !m.permissions.some((p) => canGrant(p.id) || granted.has(p.id)))
                            return (
                              <button
                                key={lv}
                                type="button"
                                role="radio"
                                aria-checked={active}
                                disabled={disabled}
                                onClick={() => setLevel(m, lv as Exclude<AccessLevel, 'custom'>)}
                                className={cn(
                                  'px-2.5 py-1 font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50',
                                  active
                                    ? lv === 'none' ? 'bg-surface3 text-text' : 'bg-primary text-white'
                                    : 'bg-surface text-muted hover:bg-surface2 hover:text-text',
                                )}
                              >
                                {label}
                              </button>
                            )
                          })}
                        </div>
                      )}

                      {(extras.length > 0 || m.permissions.length > 1) && levels.length > 0 && (
                        <button
                          type="button"
                          aria-expanded={isOpen}
                          aria-controls={`perm-${m.module}`}
                          onClick={() => setOpen((o) => ({ ...o, [m.module]: !isOpen }))}
                          className="inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-muted hover:bg-surface2 hover:text-text"
                        >
                          {extras.some((p) => granted.has(p.id)) && <ShieldAlert className="h-3 w-3 text-warning" aria-label="Includes a sensitive or privileged permission" />}
                          Permissions
                          <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', isOpen && 'rotate-180')} aria-hidden />
                        </button>
                      )}
                    </div>

                    {isOpen && (
                      <div id={`perm-${m.module}`} className={cn('mt-2 grid gap-1.5 sm:grid-cols-2', levels.length > 0 && 'ml-10')}>
                        {m.permissions.map((p) => {
                          const isSuper = p.id === superPerm
                          const implied = full && !isSuper
                          const on = granted.has(p.id) || implied
                          const locked = !canGrant(p.id) && !granted.has(p.id)
                          const disabled = implied || locked
                          const changedOne = baseline ? baseline.has(p.id) !== granted.has(p.id) : false
                          return (
                            <label
                              key={p.id}
                              className={cn(
                                'flex items-start gap-2 rounded-md border px-2.5 py-1.5 text-xs transition-colors',
                                on ? 'border-primary/40 bg-primary/[0.05]' : 'border-border',
                                disabled ? 'cursor-not-allowed opacity-70' : 'cursor-pointer hover:border-primary/40',
                                isSuper && 'sm:col-span-2',
                              )}
                              title={locked ? 'You cannot grant access you do not hold yourself' : undefined}
                            >
                              <input
                                type="checkbox"
                                className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-primary"
                                checked={on}
                                disabled={disabled}
                                aria-label={`${m.label}: ${p.label}`}
                                onChange={(e) => toggle(p.id, e.target.checked)}
                              />
                              <span className="min-w-0">
                                <span className={cn('flex flex-wrap items-center gap-1.5 font-medium', changedOne && 'text-primary')}>
                                  {p.label}
                                  <RiskBadge risk={p.risk || 'standard'} />
                                  {locked && <Lock className="h-3 w-3 text-muted" aria-label="You do not hold this permission" />}
                                  {implied && <span className="text-[10px] font-normal text-muted">(included in full administration)</span>}
                                </span>
                                <span className="block text-[11px] leading-snug text-muted">{p.description}</span>
                                {p.implies && p.implies.length > 0 && <span className="block text-[10px] text-muted/80">Also adds: {p.implies.join(', ')}</span>}
                              </span>
                            </label>
                          )
                        })}
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          </section>
        )
      })}
    </div>
  )
}
