import { Clock, RefreshCw, ShieldCheck } from 'lucide-react'
import { serverNow } from '@/lib/serverClock'
import { cn } from '@/lib/utils'
import type { InventorySummary } from '@/pages/inventory/types'
import { DrillLink } from './DrillLink'
import { asOfLabel, fmtInt, fmtShare } from './model'

export interface InventoryMetaLineProps {
  summary?: InventorySummary
  loading?: boolean
  /** The request failed. With a `summary` it is the last one that loaded and may be out of date. */
  failed?: boolean
  onRetry?: () => void
  /** Tags the caller's scope is limited to; empty means the whole fleet. */
  scopeTags?: readonly string[]
  /** Injectable clock for tests. */
  now?: number
  className?: string
}

const MAX_TAGS_SHOWN = 3

/**
 * The line under the tab strip on every Inventory page (doc 03 section 1.3):
 * "Inventory as of 14:05 · 94.4% fresh · 15 aging · 6 stale · 2 never reported · 8 not reported by design".
 * Every count is a link to the assets behind it, and the drilled list's total is the number shown.
 * The percentage is of the assets that CAN report: those that cannot by design (ping-only and
 * controller-managed devices, servers whose collection mode has no inventory upload) are their own count and
 * are neither "never" nor part of the all-clear.
 */
export function InventoryMetaLine({ summary, loading, failed, onRetry, scopeTags = [], now, className }: InventoryMetaLineProps) {
  const clock = now ?? serverNow()
  const scoped = scopeTags.length > 0
  const base = cn('flex min-h-[20px] flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted', className)

  const scope = scoped && (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-info/30 bg-info/10 px-2 py-0.5 text-[11px] font-medium text-info-fg"
      title={`Your role limits what you see to assets tagged ${scopeTags.join(', ')}. Every number on these pages covers those assets only.`}
    >
      <ShieldCheck className="h-3 w-3" aria-hidden />
      Showing assets tagged {scopeTags.slice(0, MAX_TAGS_SHOWN).join(', ')}
      {scopeTags.length > MAX_TAGS_SHOWN && ` +${scopeTags.length - MAX_TAGS_SHOWN}`}
    </span>
  )

  if (!summary) {
    if (loading) {
      return (
        <div className={base} aria-busy="true" aria-label="Loading inventory freshness">
          <span className="h-3.5 w-72 max-w-full animate-shimmer rounded" />
          {scope}
        </div>
      )
    }
    return (
      <div className={base}>
        <span>Inventory freshness is unavailable right now.</span>
        {onRetry && (
          <button type="button" onClick={onRetry} className="inline-flex items-center gap-1 rounded font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">
            <RefreshCw className="h-3 w-3" aria-hidden /> Retry
          </button>
        )}
        {scope}
      </div>
    )
  }

  const { coverage, assets } = summary
  const byDesign = coverage.not_applicable ?? 0
  // The assets that can report: the share's denominator (the server's `fresh_pct` is the same figure, rounded).
  const eligible = assets.total - byDesign
  type Problem = { key: 'aging' | 'stale' | 'never'; text: string; count: number }
  const problems = ([
    { key: 'aging', text: 'aging', count: coverage.aging },
    { key: 'stale', text: 'stale', count: coverage.stale },
    { key: 'never', text: 'never reported', count: coverage.never },
  ] satisfies Problem[]).filter((p) => p.count > 0)

  const parts: { key: string; node: React.ReactNode }[] = [
    {
      key: 'as-of',
      node: (
        <>
          <Clock className="h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>
            Inventory as of{' '}
            <time dateTime={summary.generated_at} title={new Date(summary.generated_at).toLocaleString()} className="font-medium text-text2">
              {asOfLabel(summary.generated_at, clock)}
            </time>
          </span>
        </>
      ),
    },
  ]
  if (assets.total === 0) {
    parts.push({ key: 'empty', node: <span>no assets yet</span> })
  } else {
    if (eligible > 0) {
      parts.push({
        key: 'fresh',
        node: (
          <DrillLink
            filters={{ freshness: 'fresh' }}
            count={coverage.fresh}
            title={`${fmtInt(coverage.fresh)} of ${fmtInt(eligible)} assets that can report an inventory did so within their freshness target${byDesign > 0 ? `; ${fmtInt(byDesign)} more cannot report by design and are not counted` : ''}`}
            className="text-text2"
          >
            <strong className="font-semibold text-text">{fmtShare(coverage.fresh, eligible)}</strong> fresh
          </DrillLink>
        ),
      })
    }
    if (problems.length === 0) {
      if (eligible > 0) parts.push({ key: 'clear', node: <span className="font-medium text-success-fg">every asset that can report did so within its target</span> })
    } else {
      for (const p of problems) {
        parts.push({
          key: p.key,
          node: (
            <DrillLink
              filters={{ freshness: p.key }}
              count={p.count}
              className={p.key === 'aging' ? 'text-warning-fg' : p.key === 'stale' ? 'font-medium text-danger-fg' : 'text-text2'}
            >
              {fmtInt(p.count)} {p.text}
            </DrillLink>
          ),
        })
      }
    }
    if (byDesign > 0) {
      parts.push({
        key: 'by-design',
        node: (
          <DrillLink
            filters={{ freshness: 'not_applicable' }}
            count={byDesign}
            title="Ping-only and controller-managed devices, and servers whose collection mode cannot upload an inventory. They are listed but are not counted as never reported."
            className="text-text2"
          >
            {fmtInt(byDesign)} not reported by design
          </DrillLink>
        ),
      })
    }
  }

  return (
    <div className={base}>
      {/* The separator trails its item, so a wrapped line never starts with a dot. */}
      {parts.map((p, i) => (
        <span key={p.key} className="inline-flex items-center gap-2 whitespace-nowrap">
          {p.node}
          {i < parts.length - 1 && <Dot />}
        </span>
      ))}
      {failed && (
        <span className="inline-flex items-center gap-1 text-warning-fg" title="The last refresh failed; this is the last data that loaded.">
          may be out of date
          {onRetry && (
            <button type="button" onClick={onRetry} className="ml-1 rounded font-medium text-primary outline-none hover:underline focus-visible:ring-2 focus-visible:ring-primary">
              Retry
            </button>
          )}
        </span>
      )}
      {scope}
    </div>
  )
}

function Dot() {
  return <span aria-hidden className="text-muted/50">·</span>
}
