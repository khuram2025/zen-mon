import { useMemo, type ReactNode } from 'react'
import { FileQuestion, LockKeyhole, ShieldAlert } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { Branding } from '@/lib/branding'
import { Skeleton } from '@/components/ui/Skeleton'
import type { RenderReport, SectionDef } from '../types'
import { BrandHeader } from './BrandHeader'
import { layoutRows, parseTime, sectionAnchorId, slug } from './format'
import { SectionBlock } from './SectionBlock'

export interface ReportDocumentProps {
  report: RenderReport
  /** Organisation branding (logo, colour, timezone and formats); the report's own copy wins when present. */
  branding: Branding
  /** Catalog section definitions by id (snapshot semantics, descriptions, required permissions). */
  sectionMeta?: Record<string, SectionDef>
  /** Denser layout for previews (designer); no table search, no breadcrumb links. */
  compact?: boolean
  /** Offer per-table CSV downloads (needs reports.export). */
  canExport?: boolean
  /** Prefix for section anchors; must match the contents rail. */
  idPrefix?: string
  /** Header overrides (see BrandHeader). */
  origin?: 'builtin' | 'custom'
  breadcrumb?: ReactNode | false
  scope?: ReactNode
  badges?: ReactNode
  /** Display title of the report's category, from the catalog. */
  categoryTitle?: string | null
  className?: string
}

/** Why some of the report is missing or narrowed for this viewer. */
export function AccessBanner({ report }: { report: Pick<RenderReport, 'restricted_sections' | 'omitted_sections' | 'scoped'> }) {
  const restricted = report.restricted_sections ?? 0
  const omitted = report.omitted_sections ?? 0
  if (!restricted && !omitted && !report.scoped) return null
  const lines: string[] = []
  if (report.scoped) {
    lines.push('Figures are limited to the devices in your visibility scope.')
  }
  if (omitted) {
    lines.push(`${omitted} fleet-wide section${omitted === 1 ? ' was' : 's were'} left out because your account is limited to a visibility scope.`)
  }
  if (restricted) {
    lines.push(`${restricted} section${restricted === 1 ? ' is' : 's are'} restricted: your role lacks the permission ${restricted === 1 ? 'it needs' : 'they need'}, so ${restricted === 1 ? 'its' : 'their'} data is not shown.`)
  }
  return (
    <div role="note" className="flex items-start gap-3 rounded-lg border border-warning/35 bg-warning/5 px-4 py-3 text-xs text-text2">
      {restricted ? <LockKeyhole className="mt-px h-4 w-4 shrink-0 text-warning-fg" aria-hidden /> : <ShieldAlert className="mt-px h-4 w-4 shrink-0 text-warning-fg" aria-hidden />}
      <div className="min-w-0 space-y-0.5">
        <p className="font-semibold text-text">This report is narrowed for your account</p>
        {lines.map((l, i) => <p key={i}>{l}</p>)}
      </div>
    </div>
  )
}

/**
 * A rendered report as a document: branded header, access banner, numbered
 * sections (half-width sections side by side at xl) and the organisation's
 * footer and disclaimer. Used by the viewer and the designer's live preview.
 */
export function ReportDocument({
  report, branding: orgBranding, sectionMeta, compact, canExport, idPrefix = 'rpt', origin, breadcrumb, scope, badges, categoryTitle, className,
}: ReportDocumentProps) {
  const branding = useMemo(() => ({ ...orgBranding, ...(report.branding ?? {}) }), [orgBranding, report.branding])
  const rows = useMemo(() => layoutRows(report.sections), [report.sections])
  const span = useMemo(() => {
    const from = parseTime(report.from)
    const to = parseTime(report.to)
    return from != null && to != null ? { from, to } : undefined
  }, [report.from, report.to])
  const filenameBase = slug(report.title)
  const titleId = `${idPrefix}-title`

  return (
    <article aria-labelledby={titleId} className={cn('space-y-4', className)}>
      <BrandHeader
        report={report}
        branding={branding}
        origin={origin}
        breadcrumb={breadcrumb}
        scope={scope}
        badges={badges}
        compact={compact}
        titleId={titleId}
        coverBreak={report.sections.length > 2}
        categoryTitle={categoryTitle}
      />

      <AccessBanner report={report} />

      {report.sections.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center">
          <FileQuestion className="h-7 w-7 text-muted" aria-hidden />
          <p className="text-sm font-medium text-text">This report has no sections to show</p>
          <p className="max-w-md text-xs text-muted">
            None of its sections produced output for this period and scope. Try a longer period, or widen the scope.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {rows.map((row) => (
            <div key={row[0].index} className={cn('grid gap-4', row.length === 2 && (compact ? '2xl:grid-cols-2' : 'xl:grid-cols-2'))}>
              {row.map(({ section, index }) => (
                <SectionBlock
                  key={index}
                  section={section}
                  index={index}
                  anchorId={sectionAnchorId(idPrefix, index)}
                  meta={sectionMeta?.[section.section_id ?? section.id]}
                  branding={branding}
                  compact={compact}
                  half={row.length === 2}
                  canExport={canExport}
                  filenameBase={filenameBase}
                  window={span}
                />
              ))}
            </div>
          ))}
        </div>
      )}

      {(branding.report_footer || branding.report_disclaimer) && (
        <footer className="space-y-1 border-t border-border pt-3 text-xs leading-relaxed text-muted">
          {branding.report_footer && <p className="font-medium text-text2">{branding.report_footer}</p>}
          {branding.report_disclaimer && <p className="whitespace-pre-line">{branding.report_disclaimer}</p>}
        </footer>
      )}
    </article>
  )
}

/** Loading placeholder shaped like a report (header, KPI rows, a chart and a table). */
export function ReportDocumentSkeleton({ sections = 3, compact }: { sections?: number; compact?: boolean }) {
  return (
    <div role="status" className="space-y-4">
      <span className="sr-only">Loading report…</span>
      <div aria-hidden className={cn('rounded-xl border border-border bg-surface', compact ? 'p-4' : 'p-6')}>
        <div className="flex items-center gap-3">
          <Skeleton className="h-9 w-9 rounded-lg" />
          <Skeleton className="h-4 w-40" />
        </div>
        <Skeleton className="mt-6 h-3 w-28" />
        <Skeleton className="mt-2 h-7 w-2/5" />
        <Skeleton className="mt-2 h-4 w-3/5" />
        <div className="mt-5 grid gap-4 sm:grid-cols-3">
          <Skeleton className="h-9" />
          <Skeleton className="h-9" />
          <Skeleton className="h-9" />
        </div>
      </div>
      {Array.from({ length: sections }).map((_, i) => (
        <div key={i} aria-hidden className="rounded-xl border border-border bg-surface">
          <div className="flex items-center gap-3 border-b border-border/70 px-5 py-3.5">
            <Skeleton className="h-6 w-6" />
            <Skeleton className="h-4 w-48" />
          </div>
          <div className="space-y-4 p-5">
            <div className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
              {Array.from({ length: 4 }).map((__, j) => <Skeleton key={j} className="h-[68px] rounded-lg" />)}
            </div>
            {i % 2 === 0 ? <Skeleton className="h-[200px] rounded-lg" /> : (
              <div className="space-y-2">
                {Array.from({ length: 5 }).map((__, j) => <Skeleton key={j} className="h-6" />)}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
