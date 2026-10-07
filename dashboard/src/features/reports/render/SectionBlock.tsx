import { Clock3, LockKeyhole, TriangleAlert } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { Branding } from '@/lib/branding'
import type { RenderSection, SectionDef } from '../types'
import { ChartBlock } from './ChartBlock'
import { DataTable } from './DataTable'
import { KpiGrid } from './KpiGrid'
import { NoteList } from './NoteList'
import { sectionFailure } from './format'

export interface SectionBlockProps {
  section: RenderSection
  /** 0-based position in the report. */
  index: number
  /** DOM id (index based, see sectionAnchorId). */
  anchorId: string
  meta?: SectionDef
  branding: Pick<Branding, 'timezone' | 'date_format' | 'time_format'>
  compact?: boolean
  /** Sit beside another half-width section (narrower body layout). */
  half?: boolean
  canExport?: boolean
  filenameBase?: string
  window?: { from: number; to: number }
}

function Number2({ index, tone = 'primary' }: { index: number; tone?: 'primary' | 'muted' | 'warning' }) {
  return (
    <span
      aria-hidden
      className={cn(
        'flex h-6 min-w-[1.5rem] shrink-0 items-center justify-center rounded-md px-1 text-[11px] font-bold tabular-nums',
        tone === 'primary' && 'bg-primary/10 text-primary',
        tone === 'muted' && 'bg-surface2 text-muted',
        tone === 'warning' && 'bg-warning/15 text-warning-fg',
      )}
    >
      {String(index + 1).padStart(2, '0')}
    </span>
  )
}

/**
 * One report section: numbered heading, snapshot badge, KPIs, charts,
 * tables and notes. Failed sections render as a warning card and sections
 * the viewer's role may not see as a muted locked card — never as data.
 */
export function SectionBlock({
  section, index, anchorId, meta, branding, compact, half, canExport, filenameBase, window,
}: SectionBlockProps) {
  const headingId = `${anchorId}-h`
  const description = section.description || meta?.description
  // The catalog says whether a section is a point-in-time snapshot; a render may carry it too.
  const snapshot = (meta?.semantics ?? (section as RenderSection & { semantics?: string }).semantics) === 'snapshot'
  const notes = section.notes || []

  const shell = 'zp-section scroll-mt-[var(--zp-report-offset,6rem)] rounded-xl border focus:outline-none'

  if (section.restricted) {
    const requires = meta?.requires ?? []
    return (
      <section id={anchorId} aria-labelledby={headingId} tabIndex={-1} className={cn(shell, 'border-dashed border-border bg-surface2/40')}>
        <div className={cn('flex items-start gap-3', compact ? 'p-3' : 'px-5 py-4')}>
          <Number2 index={index} tone="muted" />
          <div className="min-w-0 flex-1">
            <h2 id={headingId} className="flex flex-wrap items-center gap-2 text-sm font-semibold text-text2">
              {section.title}
              <span className="inline-flex items-center gap-1 rounded-full border border-border bg-surface px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted">
                <LockKeyhole className="h-3 w-3" aria-hidden /> Restricted
              </span>
            </h2>
            {requires.length ? (
              <p className="mt-1 text-xs leading-relaxed text-muted">
                Your role does not include{' '}
                {requires.map((p, i) => (
                  <span key={p}>
                    {i > 0 && (i === requires.length - 1 ? ' and ' : ', ')}
                    <code className="rounded bg-surface px-1 py-px font-mono text-[11px] text-text2">{p}</code>
                  </span>
                ))}
                {', which this section needs, so its data is not shown. Ask an administrator if you need it.'}
              </p>
            ) : (
              <p className="mt-1 text-xs leading-relaxed text-muted">
                {notes[0] || 'Your role does not include the permission this section needs, so its data is not shown.'}
              </p>
            )}
          </div>
        </div>
      </section>
    )
  }

  if (section.error) {
    const failure = sectionFailure(notes)
    return (
      <section id={anchorId} aria-labelledby={headingId} tabIndex={-1} className={cn(shell, 'border-warning/40 bg-warning/5')}>
        <div className={cn('flex items-start gap-3', compact ? 'p-3' : 'px-5 py-4')}>
          <Number2 index={index} tone="warning" />
          <div className="min-w-0 flex-1">
            <h2 id={headingId} className="text-sm font-semibold text-text">{section.title}</h2>
            <div role="note" className="mt-1.5 flex items-start gap-2 text-xs leading-relaxed text-text2">
              <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning-fg" aria-hidden />
              <p className="min-w-0">
                <span className="font-semibold text-warning-fg">This section could not be generated.</span>{' '}
                {failure.detail || 'The rest of the report is unaffected; the appliance log has details.'}
                {failure.code && (
                  <code className="ml-1.5 rounded bg-surface px-1 py-px font-mono text-[10px] text-muted">{failure.code}</code>
                )}
              </p>
            </div>
            {failure.extra.length > 0 && <NoteList notes={failure.extra} className="mt-2" />}
          </div>
        </div>
      </section>
    )
  }

  const charts = section.charts || []
  const tables = section.tables || []
  const wide = !half && !compact
  const chartGrid = wide && charts.length > 1
  const tableGrid = wide && tables.length > 1 && tables.every((t) => t.headers.length <= 5)
  const empty = !section.kpis.length && !charts.length && !tables.length && !notes.length

  return (
    <section
      id={anchorId}
      aria-labelledby={headingId}
      tabIndex={-1}
      className={cn(shell, 'border-border bg-surface shadow-card dark:shadow-card-dark')}
    >
      <div className={cn('flex items-start gap-3 border-b border-border/70', compact ? 'px-3 py-2.5' : 'px-5 py-3.5')}>
        <Number2 index={index} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h2 id={headingId} className="text-[15px] font-semibold leading-6 text-text">{section.title}</h2>
            {snapshot && (
              <span
                className="inline-flex items-center gap-1 rounded-full border border-info/30 bg-info/10 px-2 py-0.5 text-[10px] font-medium text-info-fg"
                title="Current state when the report was generated, not a figure over the period"
              >
                <Clock3 className="h-3 w-3" aria-hidden /> As of generation
              </span>
            )}
          </div>
          {description && (
            <p className={cn('mt-0.5 text-xs leading-relaxed text-muted', compact && 'line-clamp-2')}>{description}</p>
          )}
        </div>
      </div>

      <div className={cn('space-y-4', compact ? 'p-3' : 'p-5')}>
        {section.kpis.length > 0 && <KpiGrid kpis={section.kpis} compact={compact || half} />}

        {charts.length > 0 && (
          <div className={cn('grid gap-5', chartGrid && 'lg:grid-cols-2')}>
            {charts.map((chart, i) => (
              <ChartBlock key={i} chart={chart} branding={branding} compact={compact} window={window} />
            ))}
          </div>
        )}

        {tables.length > 0 && (
          <div className={cn('grid gap-5', tableGrid && 'lg:grid-cols-2')}>
            {tables.map((table, i) => (
              <DataTable
                key={i}
                table={table}
                compact={compact}
                canExport={canExport}
                filenameBase={filenameBase}
                label={`${section.title}${tables.length > 1 ? ` table ${i + 1}` : ''}`}
              />
            ))}
          </div>
        )}

        <NoteList notes={notes} />

        {empty && <p className="py-4 text-center text-xs text-muted">Nothing to report for this window.</p>}
      </div>
    </section>
  )
}
