import { useEffect, useId, useMemo, useRef, useState, type CSSProperties } from 'react'
import { AlertTriangle, Eye, LayoutList, RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatTime, type Branding } from '@/lib/branding'
import { Button } from '@/components/ui/Button'
import { QueryFailure } from '@/components/ui/QueryState'
import { usePreviewReport } from '../api'
import { bucketNow, periodRange } from '../periods'
import { ReportDocument, ReportDocumentSkeleton } from '../render/ReportDocument'
import { sectionAnchorId } from '../render/format'
import type { PeriodId, RenderReport, SectionDef } from '../types'
import { MAX_SECTIONS, previewIndexFor, toEntries, type Draft, type SectionIndex } from './model'
import { PeriodSelect, prefersReducedMotion } from './ui'

export const PREVIEW_ID_PREFIX = 'dsn-pv'
const DEBOUNCE_MS = 600

export interface PreviewPaneProps {
  draft: Draft
  sections: SectionIndex
  sectionMeta: Record<string, SectionDef>
  branding: Branding
  categoryTitle?: string | null
  /** Entries with invalid options or unknown sections (the server would refuse the preview). */
  blockingIssues: number
  period: PeriodId
  onPeriodChange: (p: PeriodId) => void
  /** Index of the selected entry; the preview scrolls to its section. */
  selectedIndex: number
  className?: string
}

/**
 * Live preview of the unsaved template: POST /reports/preview about 600 ms
 * after the last change, rendered with the viewer's ReportDocument. Only the
 * newest request may update the pane; the previous render stays visible
 * (dimmed) while the next one loads.
 */
export function PreviewPane({
  draft, sections, sectionMeta, branding, categoryTitle, blockingIssues, period, onPeriodChange, selectedIndex, className,
}: PreviewPaneProps) {
  const headingId = useId()
  const periodLabelId = useId()
  const { mutateAsync } = usePreviewReport()
  const [report, setReport] = useState<RenderReport | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [pending, setPending] = useState(false)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [srStatus, setSrStatus] = useState('')
  const [nonce, setNonce] = useState(0)
  const seq = useRef(0)
  const scroller = useRef<HTMLDivElement>(null)

  const n = draft.entries.length
  const blocked: 'empty' | 'too-many' | 'invalid' | null =
    n === 0 ? 'empty' : n > MAX_SECTIONS ? 'too-many' : blockingIssues > 0 ? 'invalid' : null

  // What the server renders; the key changes only when the output could.
  const body = useMemo(() => ({
    name: draft.name.trim() || 'Untitled template',
    description: draft.description.trim() || null,
    category: draft.category || null,
    sections: toEntries(draft.entries, sections),
  }), [draft.name, draft.description, draft.category, draft.entries, sections])
  const bodyKey = JSON.stringify(body)

  useEffect(() => {
    if (blocked) {
      seq.current++ // ignore anything still in flight
      setPending(false)
      return
    }
    const my = ++seq.current
    setPending(true)
    const timer = window.setTimeout(() => {
      const { from, to } = periodRange(period, branding.timezone, branding.week_start, bucketNow())
      mutateAsync({ ...JSON.parse(bodyKey), from: from.toISOString(), to: to.toISOString() })
        .then((r) => {
          if (my !== seq.current) return
          setReport(r)
          setError(null)
          setUpdatedAt(Date.now())
          setSrStatus(`Preview updated: ${r.sections.length} section${r.sections.length === 1 ? '' : 's'}.`)
        })
        .catch((e) => {
          if (my !== seq.current) return
          setError(e)
          setSrStatus('The preview could not be updated.')
        })
        .finally(() => {
          if (my === seq.current) setPending(false)
        })
    }, DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
    // mutateAsync is stable; bodyKey captures every input of the render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bodyKey, period, blocked, nonce, branding.timezone, branding.week_start])

  // Follow the selected entry.
  const lastScrolled = useRef<string | null>(null)
  useEffect(() => {
    if (!report || selectedIndex < 0 || !scroller.current) return
    const uid = draft.entries[selectedIndex]?.uid ?? null
    const key = `${uid}:${report.sections.length}`
    if (lastScrolled.current === key) return
    lastScrolled.current = key
    const i = previewIndexFor(draft.entries, selectedIndex, report.sections)
    if (i < 0) return
    const box = scroller.current
    const el = box.querySelector<HTMLElement>(`#${CSS.escape(sectionAnchorId(PREVIEW_ID_PREFIX, i))}`)
    if (!el) return
    const top = el.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop - 12
    box.scrollTo({ top, behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
  }, [report, selectedIndex, draft.entries])

  const updating = pending && !!report

  return (
    <section
      aria-labelledby={headingId}
      className={cn('flex min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-card dark:shadow-card-dark', className)}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border px-3 py-2">
        <h2 id={headingId} className="flex items-center gap-1.5 text-sm font-semibold text-text">
          <Eye className="h-4 w-4 text-primary" aria-hidden /> Live preview
        </h2>
        <span className="text-[11px] text-muted">
          {blocked ? 'Paused' : pending ? 'Updating…' : updatedAt ? <>Updated {formatTime(updatedAt, branding)}</> : null}
        </span>
        <span role="status" className="sr-only">{srStatus}</span>
        <div className="ml-auto flex items-center gap-1.5">
          <span id={periodLabelId} className="text-[11px] text-muted">Period</span>
          <PeriodSelect value={period} onChange={(p) => p && onPeriodChange(p)} labelledBy={periodLabelId} size="sm" className="w-[10.5rem]" />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            aria-label="Refresh preview"
            title="Refresh preview"
            disabled={!!blocked || pending}
            onClick={() => setNonce((x) => x + 1)}
          >
            <RefreshCw className={cn('h-3.5 w-3.5', pending && 'animate-spin')} aria-hidden />
          </Button>
        </div>
      </div>

      <div className="relative h-0.5 shrink-0 overflow-hidden bg-transparent">
        {updating && <div role="progressbar" aria-label="Updating preview" className="zp-designer-progress absolute inset-y-0 w-1/3 rounded-full bg-primary" />}
      </div>

      <div
        ref={scroller}
        className="min-h-0 flex-1 overflow-y-auto bg-bg/60 p-3 sm:p-4"
        style={{ '--zp-report-offset': '0.75rem' } as CSSProperties}
      >
        {blocked === 'empty' ? (
          <PanelMessage
            icon={LayoutList}
            title="Nothing to preview yet"
            text="Add sections to the outline; the preview renders them with live data for the period above."
          />
        ) : blocked && !report ? (
          <PanelMessage
            icon={AlertTriangle}
            tone="warning"
            title="Preview paused"
            text={blocked === 'too-many'
              ? `A template holds at most ${MAX_SECTIONS} sections. Remove some to resume the preview.`
              : 'Fix the highlighted section options; the preview resumes automatically.'}
          />
        ) : error && !report ? (
          <QueryFailure error={error} what="the preview" onRetry={() => setNonce((x) => x + 1)} retrying={pending} />
        ) : !report ? (
          <ReportDocumentSkeleton compact sections={Math.min(3, n)} />
        ) : (
          <div className="space-y-3">
            {blocked && (
              <p role="note" className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/5 px-3 py-2 text-xs text-text2">
                <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-warning-fg" aria-hidden />
                Preview paused: {blocked === 'too-many' ? `remove sections to get back to ${MAX_SECTIONS}` : 'fix the highlighted section options'}. Showing the last preview.
              </p>
            )}
            {error != null && (
              <QueryFailure compact error={error} what="the latest preview" onRetry={() => setNonce((x) => x + 1)} retrying={pending} />
            )}
            <div className={cn('transition-opacity duration-200', (updating || blocked) && 'opacity-60')} aria-busy={updating}>
              <ReportDocument
                report={report}
                branding={branding}
                sectionMeta={sectionMeta}
                compact
                idPrefix={PREVIEW_ID_PREFIX}
                origin="custom"
                breadcrumb={false}
                categoryTitle={categoryTitle}
              />
            </div>
          </div>
        )}
      </div>

      <p className="border-t border-border px-3 py-1.5 text-[11px] text-muted">
        Rendered with your permissions and visibility scope. Other viewers see what their own access allows.
      </p>
    </section>
  )
}

function PanelMessage({ icon: Icon, title, text, tone }: { icon: typeof Eye; title: string; text: string; tone?: 'warning' }) {
  return (
    <div className="flex h-full min-h-[14rem] flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      <span className={cn('flex h-10 w-10 items-center justify-center rounded-full', tone === 'warning' ? 'bg-warning/10 text-warning-fg' : 'bg-surface2 text-muted')}>
        <Icon className="h-5 w-5" aria-hidden />
      </span>
      <p className="text-sm font-medium text-text">{title}</p>
      <p className="max-w-sm text-xs leading-relaxed text-muted">{text}</p>
    </div>
  )
}
