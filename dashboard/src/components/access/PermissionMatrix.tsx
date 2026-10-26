import { useMemo, useState } from 'react'
import { Check, Lock, Minus, Search, ShieldAlert } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { PermissionCatalogModule, PermissionCatalogPermission, PermissionRisk } from '@/types'
import { Input } from '@/components/ui/Input'
import { RISK_META, isElevated, type Catalog } from '@/lib/access'

/** Small risk marker. Never colour alone: the label is real text for assistive
 *  technology and colour-blind users. */
export function RiskBadge({ risk, className }: { risk: PermissionRisk; className?: string }) {
  if (risk === 'standard') return null
  const m = RISK_META[risk]
  return (
    <span title={m.hint} className={cn('inline-flex items-center gap-0.5 rounded border border-current/30 px-1 text-2xs font-semibold uppercase tracking-wide', m.tone, className)}>
      {risk === 'critical' || risk === 'privileged' ? <ShieldAlert className="h-2.5 w-2.5" aria-hidden /> : null}
      {m.label}
    </span>
  )
}

type Props = {
  catalog?: Catalog
  granted: Set<string>
  superPerm: string
  readOnly?: boolean
  /** Permissions the signed-in account may hand out; others are shown locked. */
  canGrant?: (id: string) => boolean
  onToggle?: (id: string, on: boolean) => void
  onToggleModule?: (m: PermissionCatalogModule, on: boolean) => void
  /** Permissions present when the editor opened, to mark what changed. */
  baseline?: Set<string>
  searchable?: boolean
}

/** Module × permission grid shared by the read-only view and the editor.
 *  Every control has a module-specific accessible name ("Devices: Manage"),
 *  the description of the focused/hovered permission is shown persistently
 *  (not only as native hover text), and risk is spelled out in text. */
export function PermissionMatrix({ catalog, granted, superPerm, readOnly, canGrant, onToggle, onToggleModule, baseline, searchable }: Props) {
  const full = granted.has(superPerm)
  const [query, setQuery] = useState('')
  const [focus, setFocus] = useState<{ module: string; perm: PermissionCatalogPermission } | null>(null)

  const modules = useMemo(() => {
    const q = query.trim().toLowerCase()
    return (catalog?.modules || [])
      .map((m) => ({
        ...m,
        permissions: q
          ? m.permissions.filter((p) => `${m.label} ${p.label} ${p.id} ${p.description}`.toLowerCase().includes(q))
          : m.permissions,
      }))
      .filter((m) => m.permissions.length > 0)
  }, [catalog, query])

  if (!catalog) return null

  return (
    <div className="space-y-2">
      {searchable && (
        <div className="relative max-w-xs">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
          <Input className="h-8 pl-8 text-xs" placeholder="Search permissions…" aria-label="Search permissions" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
      )}
      <div className="overflow-hidden rounded-md border border-border">
        <table className="w-full text-sm">
          <caption className="sr-only">Permissions by module</caption>
          <thead className="bg-surface2/50 text-xs uppercase tracking-wider text-muted">
            <tr>
              <th scope="col" className="w-40 px-3 py-2 text-left font-medium sm:w-48">Module</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">Permissions</th>
              {!readOnly && <th scope="col" className="w-16 px-3 py-2 text-center font-medium">All</th>}
            </tr>
          </thead>
          <tbody>
            {modules.map((m) => {
              const isSystemModule = m.permissions.some((p) => p.id === superPerm)
              const grantable = m.permissions.filter((p) => !isElevated(p.risk || 'standard') && (canGrant ? canGrant(p.id) : true))
              const allOn = grantable.length > 0 && grantable.every((p) => granted.has(p.id))
              const anyOn = grantable.some((p) => granted.has(p.id))
              return (
                <tr key={m.module} className={cn('border-t border-border', isSystemModule && 'bg-warning/[0.04]')}>
                  <th scope="row" className="px-3 py-2 text-left align-top font-medium">
                    <span className="flex items-center gap-1.5 text-sm">
                      {m.label}
                      {isSystemModule && <ShieldAlert className="h-3.5 w-3.5 text-warning" aria-hidden />}
                    </span>
                  </th>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-x-4 gap-y-2">
                      {m.permissions.map((p) => {
                        const isSuper = p.id === superPerm
                        const implied = full && !isSuper
                        const on = granted.has(p.id) || implied
                        const risk = p.risk || 'standard'
                        const name = `${m.label}: ${p.label}`
                        const changed = baseline ? baseline.has(p.id) !== granted.has(p.id) : false
                        if (readOnly) {
                          return (
                            <span key={p.id} className={cn('inline-flex items-center gap-1.5 text-xs', on ? 'text-text' : 'text-muted/70')}>
                              {on ? <Check className="h-3.5 w-3.5 text-success" aria-hidden /> : <Minus className="h-3.5 w-3.5" aria-hidden />}
                              <span className="sr-only">{on ? 'Granted: ' : 'Not granted: '}{m.label}: </span>
                              {p.label}
                              {on && <RiskBadge risk={risk} />}
                              {implied && <span className="text-2xs text-muted">(implied)</span>}
                            </span>
                          )
                        }
                        const locked = canGrant ? !canGrant(p.id) && !granted.has(p.id) : false
                        const disabled = implied || locked
                        return (
                          <label
                            key={p.id}
                            title={locked ? 'You cannot grant access you do not hold yourself' : p.description}
                            className={cn('inline-flex items-center gap-1.5 text-xs', disabled ? 'cursor-not-allowed opacity-70' : 'cursor-pointer', changed && 'font-semibold text-primary')}
                            onMouseEnter={() => setFocus({ module: m.label, perm: p })}
                            onFocus={() => setFocus({ module: m.label, perm: p })}
                          >
                            <input
                              type="checkbox"
                              className="h-3.5 w-3.5 accent-primary"
                              aria-label={name}
                              aria-describedby={focus?.perm.id === p.id ? 'perm-help' : undefined}
                              checked={on}
                              disabled={disabled}
                              onChange={(e) => onToggle?.(p.id, e.target.checked)}
                            />
                            <span aria-hidden>{p.label}</span>
                            {locked && <Lock className="h-3 w-3 text-muted" aria-label="You do not hold this permission" />}
                            <RiskBadge risk={risk} />
                          </label>
                        )
                      })}
                    </div>
                  </td>
                  {!readOnly && (
                    <td className="px-3 py-2 text-center">
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5 accent-primary"
                        aria-label={`${m.label}: all standard permissions`}
                        checked={allOn || (full && !isSystemModule)}
                        ref={(el) => { if (el) el.indeterminate = !allOn && anyOn && !full }}
                        disabled={grantable.length === 0 || (full && !isSystemModule)}
                        onChange={(e) => onToggleModule?.(m, e.target.checked)}
                        title={`Toggle every ${m.label} permission except privileged ones, which must be ticked individually`}
                      />
                    </td>
                  )}
                </tr>
              )
            })}
            {modules.length === 0 && (
              <tr><td colSpan={3} className="px-3 py-6 text-center text-xs text-muted">No permission matches “{query}”.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {!readOnly && (
        <p id="perm-help" role="status" className="min-h-[2.25rem] rounded-md bg-surface2/40 px-3 py-1.5 text-xs text-muted">
          {focus
            ? <><span className="font-medium text-text">{focus.module}: {focus.perm.label}</span> — {focus.perm.description}
                {focus.perm.implies && focus.perm.implies.length > 0 && <> Requires {focus.perm.implies.join(', ')}.</>}</>
            : 'Hover or focus a permission to read what it allows.'}
        </p>
      )}
    </div>
  )
}
