import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { CalendarRange, ChevronRight, Clock, Globe2, LockKeyhole, ShieldCheck } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatPeriod, logoUrl, onColor, type Branding, type Confidentiality } from '@/lib/branding'
import type { RenderReport } from '../types'

const HEX = /^#[0-9a-f]{6}$/i

const CLASSIFICATION: Record<Exclude<Confidentiality, ''>, string> = {
  Public: 'border-info/40 bg-info/10 text-info-fg',
  Internal: 'border-warning/40 bg-warning/10 text-warning-fg',
  Confidential: 'border-danger/40 bg-danger/10 text-danger-fg',
  Restricted: 'border-danger/60 bg-danger/15 text-danger-fg',
}

function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean)
  return (words.length > 1 ? words[0][0] + words[1][0] : (words[0] || 'Z').slice(0, 2)).toUpperCase()
}

/**
 * Organisation logo for the current theme: the light-background logo on the
 * light theme and on paper; the dark-background logo on the dark theme, or the
 * light logo inside a white chip when only that one exists.
 */
export function BrandLogo({ branding, name, className }: { branding: Branding; name: string; className?: string }) {
  const [failed, setFailed] = useState<{ light?: boolean; dark?: boolean }>({})
  const light = branding.has_logo && !failed.light ? logoUrl('light', branding.logo_version, false) : null
  const dark = branding.has_logo_dark && !failed.dark ? logoUrl('dark', branding.logo_dark_version, false) : null
  const brand = HEX.test(branding.brand_color) ? branding.brand_color : null

  if (!light && !dark) {
    return (
      <span
        aria-hidden
        className={cn('flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-xs font-bold', !brand && 'bg-primary/15 text-primary', className)}
        style={brand ? { background: brand, color: onColor(brand), printColorAdjust: 'exact', WebkitPrintColorAdjust: 'exact' } : undefined}
      >
        {initials(name)}
      </span>
    )
  }
  const img = 'block h-9 max-w-[180px] object-contain'
  return (
    <span className={cn('flex shrink-0 items-center', className)}>
      {light && (
        <span className={cn(dark ? 'dark:hidden print:!block' : 'dark:rounded-md dark:bg-white dark:px-2 dark:py-1 print:!bg-transparent print:!p-0')}>
          <img src={light} alt={`${name} logo`} className={img} onError={() => setFailed((f) => ({ ...f, light: true }))} />
        </span>
      )}
      {dark && (
        <span
          className={cn(light ? 'hidden dark:block print:!hidden' : 'rounded-md bg-text px-2 py-1 dark:bg-transparent dark:p-0')}
          style={light ? undefined : { printColorAdjust: 'exact', WebkitPrintColorAdjust: 'exact' }}
        >
          <img src={dark} alt={`${name} logo`} className={img} onError={() => setFailed((f) => ({ ...f, dark: true }))} />
        </span>
      )}
    </span>
  )
}

export interface BrandHeaderProps {
  report: RenderReport
  branding: Branding
  /** Built-in template or organisation (custom) template. */
  origin?: 'builtin' | 'custom'
  /** Replaces the default "Reports › Category" trail; false hides it. */
  breadcrumb?: ReactNode | false
  /** Replaces the scope chips derived from the report (e.g. removable NetFlow chips). */
  scope?: ReactNode
  /** Extra badges after Built-in / Custom (owner, visibility…). */
  badges?: ReactNode
  compact?: boolean
  titleId?: string
  /** Long reports print the header as a cover page. */
  coverBreak?: boolean
}

/** Scope chips from the server's scope label ("3 selected device(s) · NetFlow: …"). */
export function scopeChips(report: RenderReport): string[] {
  const label = (report.scope_label || '').trim()
  if (!label) return []
  return label.split(' · ').map((s) => s.trim()).filter(Boolean)
}

/** Report cover: organisation identity, trail, title, origin and classification, period, generation time and scope. */
export function BrandHeader({
  report, branding, origin, breadcrumb, scope, badges, compact, titleId, coverBreak,
}: BrandHeaderProps) {
  const company = report.company_name || branding.display_name || branding.company_name || 'ZenPlus'
  const brand = HEX.test(branding.brand_color) ? branding.brand_color : null
  const kind = origin ?? (report.key === 'custom' ? 'custom' : 'builtin')
  const classification = branding.confidentiality
  const chips = scopeChips(report)
  const period = formatPeriod(report.from, report.to, branding)
  const tzName = report.timezone || branding.timezone

  const trail = breadcrumb === false ? null : breadcrumb ?? (
    <nav aria-label="Breadcrumb" className="print:hidden">
      <ol className="flex flex-wrap items-center gap-1 text-xs text-muted">
        <li>
          {compact ? <span>Reports</span> : <Link to="/reports" className="rounded hover:text-text hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">Reports</Link>}
        </li>
        {report.category && (
          <>
            <li aria-hidden><ChevronRight className="h-3 w-3" /></li>
            <li aria-current="page" className="text-text2">{report.category}</li>
          </>
        )}
      </ol>
    </nav>
  )

  return (
    <div
      className={cn(
        'zp-cover relative overflow-hidden rounded-xl border border-border bg-surface shadow-card dark:shadow-card-dark',
        coverBreak && 'zp-cover-break',
      )}
    >
      <span
        aria-hidden
        className={cn('absolute inset-x-0 top-0 h-1', !brand && 'bg-primary')}
        style={brand ? { background: brand, printColorAdjust: 'exact', WebkitPrintColorAdjust: 'exact' } : undefined}
      />
      <div className={cn(compact ? 'px-4 pb-4 pt-5' : 'px-6 pb-5 pt-6')}>
        <div className="flex items-start justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3">
            <BrandLogo branding={branding} name={company} />
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-text">{company}</p>
              {!compact && (branding.company_website || branding.company_email) && (
                <p className="truncate text-[11px] text-muted">
                  {[branding.company_website, branding.company_email].filter(Boolean).join(' · ')}
                </p>
              )}
            </div>
          </div>
          {classification && (
            <span
              className={cn(
                'inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-1 text-[10px] font-bold uppercase tracking-[0.12em]',
                CLASSIFICATION[classification],
              )}
              style={{ printColorAdjust: 'exact', WebkitPrintColorAdjust: 'exact' }}
            >
              <LockKeyhole className="h-3 w-3" aria-hidden />
              <span className="sr-only">Classification: </span>
              {classification}
            </span>
          )}
        </div>

        <div className={cn(compact ? 'mt-4' : 'mt-6 print:mt-16')}>
          {trail}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <h1 id={titleId} className={cn('font-semibold tracking-tight text-text', compact ? 'text-xl' : 'text-2xl print:text-3xl')}>
              {report.title}
            </h1>
            <span
              className={cn(
                'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium',
                kind === 'custom' ? 'border-info/30 bg-info/10 text-info-fg' : 'border-border bg-surface2 text-text2',
              )}
            >
              {kind === 'builtin' && <ShieldCheck className="h-3 w-3" aria-hidden />}
              {kind === 'custom' ? 'Custom template' : 'Built-in'}
            </span>
            {badges}
          </div>
          {report.description && (
            <p className={cn('mt-1.5 max-w-3xl text-sm leading-relaxed text-text2', compact && 'line-clamp-2')}>{report.description}</p>
          )}
        </div>

        <dl className={cn('grid gap-x-6 gap-y-2 text-xs', compact ? 'mt-3 sm:grid-cols-2' : 'mt-5 sm:grid-cols-2 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,1fr)]')}>
          <div className="min-w-0">
            <dt className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
              <CalendarRange className="h-3 w-3" aria-hidden /> Period
            </dt>
            <dd className="mt-0.5 font-medium tabular-nums text-text">{period}</dd>
          </div>
          <div className="min-w-0">
            <dt className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
              <Globe2 className="h-3 w-3" aria-hidden /> Timezone
            </dt>
            <dd className="mt-0.5 truncate font-medium text-text">
              {tzName.replace(/_/g, ' ')}
              {report.tz_label && <span className="ml-1 text-muted">({report.tz_label})</span>}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
              <Clock className="h-3 w-3" aria-hidden /> Generated
            </dt>
            <dd className="mt-0.5 font-medium tabular-nums text-text">{report.generated_label}</dd>
          </div>
        </dl>

        {(scope || chips.length > 0) && (
          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-border/70 pt-3">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">Scope</span>
            {scope ?? (
              <ul role="list" className="flex flex-wrap gap-1.5">
                {chips.map((c, i) => (
                  <li key={i} className="rounded-md border border-border bg-surface2/70 px-2 py-0.5 text-[11px] font-medium text-text2">
                    {c}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
