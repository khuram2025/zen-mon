import { useSyncExternalStore, type ReactNode } from 'react'
import { AlertTriangle, CheckCircle2, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, CircleSlash, Clock3, RefreshCw, XCircle } from 'lucide-react'
import { relativeTime, cn } from '@/lib/utils'
import { clockOffsetMs, subscribeClock } from '@/lib/serverClock'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { absoluteLocal, absoluteUtc, stateOf, TONE_BADGE, TONE_TEXT, type Tone } from './model'

/** Relative age on the appliance clock with the absolute time on hover/focus. */
export function Ago({ iso, prefix = '', empty = 'never', className }: { iso?: string | null; prefix?: string; empty?: string; className?: string }) {
  useClockOffset()
  if (!iso) return <span className={cn('text-muted', className)}>{empty}</span>
  const title = `${absoluteUtc(iso)} (appliance) · ${absoluteLocal(iso)} (your time)`
  return <time dateTime={iso} title={title} className={className}>{prefix}{relativeTime(iso)}</time>
}

export function useClockOffset(): number {
  return useSyncExternalStore(subscribeClock, clockOffsetMs, clockOffsetMs)
}

const TONE_ICON: Record<Tone, any> = { danger: XCircle, warning: AlertTriangle, success: CheckCircle2, info: Clock3, neutral: CircleSlash }

export function ToneIcon({ tone, className }: { tone: Tone; className?: string }) {
  const Icon = TONE_ICON[tone]
  return <Icon aria-hidden className={cn('h-3.5 w-3.5 shrink-0', TONE_TEXT[tone], className)} />
}

export function StateBadge({ device, withVersions = false }: { device: any; withVersions?: boolean }) {
  const s = stateOf(device)
  return (
    <Badge variant={TONE_BADGE[s.tone]} title={s.description}>
      <ToneIcon tone={s.tone} className={s.tone === 'neutral' ? '' : 'text-current'} />
      {s.label}{withVersions && device.versions ? ` · ${device.versions} version${device.versions === 1 ? '' : 's'}` : ''}
    </Badge>
  )
}

/** Page window with first/last controls and a visible "Page X of Y". */
export function Pagination({ page, pageCount, onPage, total, pageSize, label = 'items' }: {
  page: number; pageCount: number; onPage: (p: number) => void; total: number; pageSize: number; label?: string
}) {
  if (!total) return null
  const from = (page - 1) * pageSize + 1
  const to = Math.min(page * pageSize, total)
  const start = Math.max(1, Math.min(page - 2, pageCount - 4))
  const pages = Array.from({ length: Math.min(5, pageCount) }, (_, i) => start + i)
  return (
    <nav aria-label="Pagination" className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
      <span>{from}–{to} of {total} {label}</span>
      {pageCount > 1 && (
        <div className="flex items-center gap-1">
          <Button variant="outline" size="icon" className="h-8 w-8" aria-label="First page" disabled={page <= 1} onClick={() => onPage(1)}><ChevronsLeft className="h-4 w-4" /></Button>
          <Button variant="outline" size="icon" className="h-8 w-8" aria-label="Previous page" disabled={page <= 1} onClick={() => onPage(page - 1)}><ChevronLeft className="h-4 w-4" /></Button>
          {pages.map((p) => (
            <Button key={p} variant={p === page ? 'default' : 'outline'} size="icon" className="h-8 w-8" aria-label={`Page ${p}`}
              aria-current={p === page ? 'page' : undefined} onClick={() => onPage(p)}>{p}</Button>
          ))}
          <Button variant="outline" size="icon" className="h-8 w-8" aria-label="Next page" disabled={page >= pageCount} onClick={() => onPage(page + 1)}><ChevronRight className="h-4 w-4" /></Button>
          <Button variant="outline" size="icon" className="h-8 w-8" aria-label="Last page" disabled={page >= pageCount} onClick={() => onPage(pageCount)}><ChevronsRight className="h-4 w-4" /></Button>
          <span className="ml-1 tabular-nums" aria-live="polite">Page {page} of {pageCount}</span>
        </div>
      )}
    </nav>
  )
}

/** Shown when a background refresh failed but earlier data is still displayed (B23). */
export function StaleBanner({ what, updatedAt, onRetry, retrying }: { what: string; updatedAt?: number; onRetry: () => void; retrying?: boolean }) {
  return (
    <div role="alert" className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
      <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden />
      <span className="flex-1">
        Could not refresh {what}. Showing data from {updatedAt ? <Ago iso={new Date(updatedAt).toISOString()} /> : 'an earlier load'}; it may be out of date. Actions that need current data are disabled.
      </span>
      <Button size="sm" variant="outline" onClick={onRetry} disabled={retrying}><RefreshCw className={cn('h-3.5 w-3.5', retrying && 'animate-spin')} /> Retry</Button>
    </div>
  )
}

export function SectionError({ what, onRetry }: { what: string; onRetry: () => void }) {
  return (
    <div role="alert" className="flex items-center justify-between gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
      <span>{what} could not be loaded. Nothing is assumed about it until it loads.</span>
      <Button size="sm" variant="outline" onClick={onRetry}><RefreshCw className="h-3.5 w-3.5" /> Retry</Button>
    </div>
  )
}

export function Kpi({ label, value, sub, tone = 'neutral', onClick, active }: {
  label: string; value: ReactNode; sub?: ReactNode; tone?: Tone; onClick?: () => void; active?: boolean
}) {
  const body = (
    <>
      <div className="text-xs font-medium text-muted">{label}</div>
      <div className={cn('mt-1 flex items-center gap-1.5 text-2xl font-semibold tabular-nums', tone === 'neutral' ? 'text-text' : TONE_TEXT[tone])}>
        {tone !== 'neutral' && <ToneIcon tone={tone} className="h-5 w-5" />}{value}
      </div>
      {sub && <div className="mt-0.5 text-xs text-muted">{sub}</div>}
    </>
  )
  const cls = cn('min-w-0 rounded-lg border bg-surface p-3 text-left', active ? 'border-primary ring-1 ring-primary/40' : 'border-border')
  return onClick
    ? <button type="button" onClick={onClick} aria-pressed={active} className={cn(cls, 'transition-colors hover:border-primary/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50')}>{body}</button>
    : <div className={cls}>{body}</div>
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="rounded-md border border-dashed border-border px-4 py-8 text-center text-sm text-muted">{children}</div>
}

/** A labelled definition row used by summaries. */
export function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 break-words text-sm">{children}</dd>
    </div>
  )
}
