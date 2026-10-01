import { Link } from 'react-router-dom'
import { AlertTriangle, ArrowRight, CircleHelp, Info, Lightbulb, OctagonAlert, RefreshCw } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { cn } from '@/lib/utils'
import { groupInsights, insightsSummary, joinList, type Insight, type InsightBlock, type InsightSeverity, type InsightsResult } from '../overviewModel'

/** Links inside a tinted insight box: body text colour, underlined (≥ 4.5:1 on every tint, NF-29). */
const TINT_LINK = 'font-medium text-text underline decoration-primary/60 underline-offset-2 hover:decoration-primary'

// ─────────────────────────────────────────────────────────────────
// Telemetry insights (NF-23, formerly "Alerts / Incidents"). These are
// heuristics over this window's flow telemetry, not platform alerts: the
// count is the number of actionable items (never capped, neutral at 0),
// normal activity is context and never counted, and every actionable item
// links to where it is handled (Anomalies, Collector health, exporter).
// ─────────────────────────────────────────────────────────────────

const TONE: Record<InsightSeverity, { box: string; fg: string; icon: typeof Info; word: string }> = {
  critical: { box: 'border-danger/35 border-l-danger bg-danger/10', fg: 'text-danger-fg', icon: OctagonAlert, word: 'Critical' },
  warning: { box: 'border-warning/35 border-l-warning bg-warning/10', fg: 'text-warning-fg', icon: AlertTriangle, word: 'Warning' },
  info: { box: 'border-info/35 border-l-info bg-info/10', fg: 'text-info-fg', icon: Info, word: 'Info' },
}

export interface TelemetryInsightsProps {
  insights: InsightsResult
  links: { anomalies: string; alerts: string; health: string }
  loading?: boolean
  /** Re-run the inputs that failed. */
  onRetry?: () => unknown
  retrying?: boolean
  /** Error reference of a failed input, shown next to the "could not be evaluated" sentence. */
  errorRef?: string | null
}

export function TelemetryInsights({ insights, links, loading, onRetry, retrying, errorRef }: TelemetryInsightsProps) {
  const { actionable, context, maxSeverity } = insights
  const summary = insightsSummary(insights, !!loading)
  const badgeTone = maxSeverity ? TONE[maxSeverity] : null
  return (
    <Card className="flex flex-col">
      <CardHeader className="flex flex-row items-start justify-between gap-2 pb-2">
        <div className="min-w-0">
          <CardTitle className="flex items-center gap-1.5 text-sm">
            <Lightbulb className="h-4 w-4 text-text2" aria-hidden />
            Telemetry insights
          </CardTitle>
          <p className="text-[11px] text-text2">Heuristics for this window and scope, not platform alerts</p>
        </div>
        <span
          className={cn(
            'inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold tabular-nums',
            badgeTone ? `${badgeTone.box} ${badgeTone.fg} border-l` : 'border-border bg-surface2 text-text2',
          )}
          aria-label={summary.aria}
        >
          {summary.badge}
        </span>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-2">
        {summary.empty === null && actionable.length === 0 ? (
          <div role="status" className="text-xs text-text2">Evaluating the window…</div>
        ) : actionable.length === 0 ? (
          <div
            role={summary.incomplete ? 'status' : undefined}
            className={cn(
              'rounded-md border border-dashed p-3 text-xs',
              summary.incomplete ? 'border-warning/40 bg-warning/10 text-text' : 'border-border bg-surface2/40 text-text2',
            )}
          >
            <div className="flex items-start gap-2">
              {summary.incomplete && <CircleHelp className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning-fg" aria-hidden />}
              <div className="min-w-0 space-y-1.5">
                <p>{summary.empty}</p>
                {summary.incomplete && <RetryLine onRetry={onRetry} retrying={retrying} errorRef={errorRef} />}
              </div>
            </div>
          </div>
        ) : (
          <>
            {(insights.unavailable ?? []).length > 0 && (
              <div role="status" className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-[11px] text-text">
                <CircleHelp className="h-3.5 w-3.5 shrink-0 text-warning-fg" aria-hidden />
                <span className="min-w-0">Not evaluated (query failed): {joinList(insights.unavailable ?? [])}.</span>
                <RetryLine onRetry={onRetry} retrying={retrying} errorRef={errorRef} />
              </div>
            )}
            <ul className="max-h-[420px] space-y-2 overflow-auto pr-1" aria-label="Actionable insights">
              {groupInsights(actionable).map((b) =>
                b.kind === 'item' ? <InsightRow key={b.insight.id} insight={b.insight} /> : <InsightGroup key={b.key} block={b} />,
              )}
            </ul>
          </>
        )}
        {context.length > 0 && (
          <div>
            <div className="mb-1 text-[11px] font-medium uppercase tracking-wide text-text2">Context</div>
            <ul className="space-y-1" aria-label="Context">
              {context.map((c) => (
                <li key={c.id} className="text-[11px] leading-snug text-text2">
                  <span className="font-medium text-text">{c.title}.</span> {c.body}
                  {c.link && (
                    <Link to={c.link.to} className="ml-1 font-medium text-primary hover:underline">
                      {c.link.label}
                    </Link>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="mt-auto flex flex-wrap gap-x-3 gap-y-1 border-t border-border pt-2 text-[11px]">
          <Link to={links.anomalies} className="inline-flex items-center gap-1 font-medium text-primary hover:underline">
            Anomaly findings <ArrowRight className="h-3 w-3" aria-hidden />
          </Link>
          <Link to={links.alerts} className="inline-flex items-center gap-1 font-medium text-primary hover:underline">
            Platform alerts <ArrowRight className="h-3 w-3" aria-hidden />
          </Link>
          <Link to={links.health} className="inline-flex items-center gap-1 font-medium text-primary hover:underline">
            Collector health <ArrowRight className="h-3 w-3" aria-hidden />
          </Link>
        </div>
      </CardContent>
    </Card>
  )
}

function RetryLine({ onRetry, retrying, errorRef }: { onRetry?: () => unknown; retrying?: boolean; errorRef?: string | null }) {
  if (!onRetry && !errorRef) return null
  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
      {errorRef && (
        <span className="text-[11px] text-text2">
          Reference <span className="select-all font-mono">{errorRef}</span>
        </span>
      )}
      {onRetry && (
        <button
          type="button"
          onClick={() => onRetry()}
          disabled={retrying}
          className="inline-flex items-center gap-1 text-[11px] font-semibold text-text underline-offset-2 hover:underline disabled:opacity-60"
        >
          <RefreshCw className={cn('h-3 w-3', retrying && 'animate-spin')} aria-hidden />
          {retrying ? 'Retrying…' : 'Retry'}
        </button>
      )}
    </span>
  )
}

function InsightRow({ insight }: { insight: Insight }) {
  const t = TONE[insight.severity]
  const Icon = t.icon
  return (
    <li className={cn('rounded-md border border-l-4 p-2.5', t.box)}>
      <div className="flex items-start gap-2">
        <Icon className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', t.fg)} aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="text-xs font-semibold leading-snug text-text">
            <span className={cn('mr-1 text-[10px] font-semibold uppercase tracking-wide', t.fg)}>{t.word}</span>
            {insight.title}
          </div>
          <div className="mt-0.5 break-words text-[11px] leading-snug text-text2">{insight.body}</div>
          {insight.link && (
            <Link to={insight.link.to} className={cn('mt-1 inline-flex items-center gap-1 text-[11px]', TINT_LINK)}>
              {insight.link.label} <ArrowRight className="h-3 w-3" aria-hidden />
            </Link>
          )}
        </div>
      </div>
    </li>
  )
}

/** Three or more items of one kind (e.g. silent exporters) in one box, one compact line each. */
function InsightGroup({ block }: { block: Extract<InsightBlock, { kind: 'group' }> }) {
  const t = TONE[block.severity]
  const Icon = t.icon
  return (
    <li className={cn('rounded-md border border-l-4 p-2.5', t.box)}>
      <div className="flex items-start gap-2">
        <Icon className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', t.fg)} aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="text-xs font-semibold leading-snug text-text">
            <span className={cn('mr-1 text-[10px] font-semibold uppercase tracking-wide', t.fg)}>{t.word}</span>
            {block.title}
          </div>
          <ul className="mt-1 divide-y divide-border/60" aria-label={block.title}>
            {block.items.map((i) => (
              <li key={i.id} className="flex flex-wrap items-baseline justify-between gap-x-2 py-1">
                <span className="min-w-0 text-[11px] leading-snug">
                  <span className="break-all font-medium text-text">{i.short ?? i.title}</span>
                  <span className="block text-text2">{i.body}</span>
                </span>
                {i.link && (
                  <Link to={i.link.to} className={cn('shrink-0 text-[11px]', TINT_LINK)} aria-label={`${i.link.label}: ${i.short ?? i.title}`}>
                    {i.link.label}
                  </Link>
                )}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </li>
  )
}
