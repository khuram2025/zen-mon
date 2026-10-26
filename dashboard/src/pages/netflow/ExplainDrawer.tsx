import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { ArrowDownRight, ArrowUpRight, Filter, Search, Sparkles, X } from 'lucide-react'
import { useTheme } from '@/stores/theme'
import { cn } from '@/lib/utils'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { nfParams, useNfQuery } from '@/lib/netflow/api'
import type { FlowFilter } from '@/lib/netflow/filters'
import { formatBytes, formatCountExact, formatDateTime } from '@/lib/netflow/format'
import type { NfMeta } from '@/lib/netflow/types'
import type { NfWindow } from '@/lib/netflow/window'
import { deltaReasonText, formatDelta, shareOfChangeText, signedBytes, type ExplainItem, type ExplainPayload } from './overviewModel'

// ─────────────────────────────────────────────────────────────────
// "What changed?" (CR-15). Compares the page window with a baseline
// period under the identical filter and ranks, per dimension, the items
// whose traffic changed most. Each item can be applied as a page filter
// or opened as raw flows for the exact window.
// ─────────────────────────────────────────────────────────────────

// Diverging pair (dataviz reference): increases blue, decreases red, with a sign glyph and text
// so the direction never relies on colour.
const DIVERGING = {
  light: { up: '#2a78d6', down: '#e34948' },
  dark: { up: '#3987e5', down: '#e66767' },
}

type Baseline = 'previous_equal' | 'same_time_last_week'

export interface ExplainDrawerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  window: NfWindow
  filter: FlowFilter
  /** Apply an item's filter to the page (closes the drawer). */
  onApplyFilter: (patch: Partial<FlowFilter>) => void
  /** Raw-flow link for an item: page filter + the item's filter, frozen to the explained window. */
  rawFlowsLink: (patch: Partial<FlowFilter>, meta: NfMeta | null) => string
}

const WEEK_MS = 7 * 86_400_000

export function ExplainDrawer({ open, onOpenChange, window: w, filter, onApplyFilter, rawFlowsLink }: ExplainDrawerProps) {
  const [baseline, setBaseline] = useState<Baseline>('previous_equal')
  const spanMs = Date.parse(w.to) - Date.parse(w.from)
  const weekOk = spanMs <= WEEK_MS
  const effectiveBaseline: Baseline = weekOk ? baseline : 'previous_equal'
  const params = useMemo(() => nfParams(w, filter, { baseline: effectiveBaseline, limit: 5 }), [w, filter, effectiveBaseline])
  const q = useNfQuery<ExplainPayload>('/netflow/explain', params, { enabled: open, refetchInterval: false, keepPrevious: false })

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/50 data-[state=open]:animate-fade-in" />
        <DialogPrimitive.Content
          className="fixed inset-y-0 right-0 z-50 flex w-full max-w-[560px] flex-col border-l border-border bg-surface shadow-xl focus:outline-none"
          aria-describedby="nf-explain-desc"
        >
          <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
            <div className="min-w-0">
              <DialogPrimitive.Title className="flex items-center gap-1.5 text-base font-semibold text-text">
                <Sparkles className="h-4 w-4 text-primary" aria-hidden />
                What changed?
              </DialogPrimitive.Title>
              <DialogPrimitive.Description id="nf-explain-desc" className="mt-0.5 text-xs text-text2">
                This window against a baseline period, with the same filters in both. Items are ranked by the size of their change.
              </DialogPrimitive.Description>
            </div>
            <DialogPrimitive.Close className="rounded p-1 text-text2 hover:bg-surface2 hover:text-text" aria-label="Close">
              <X className="h-4 w-4" aria-hidden />
            </DialogPrimitive.Close>
          </div>
          <div className="border-b border-border px-4 py-2">
            <fieldset className="flex flex-wrap items-center gap-2 text-xs">
              <legend className="sr-only">Baseline period</legend>
              <span className="text-text2">Compare with</span>
              {(['previous_equal', 'same_time_last_week'] as Baseline[]).map((b) => {
                const disabled = b === 'same_time_last_week' && !weekOk
                const active = effectiveBaseline === b
                return (
                  <label
                    key={b}
                    className={cn(
                      'inline-flex cursor-pointer items-center gap-1.5 rounded-md border px-2 py-1',
                      active ? 'border-primary bg-primary/10 text-text' : 'border-border text-text2 hover:text-text',
                      disabled && 'cursor-not-allowed opacity-50',
                    )}
                    title={disabled ? 'Only for windows of at most 7 days' : undefined}
                  >
                    <input
                      type="radio"
                      name="nf-explain-baseline"
                      className="sr-only"
                      checked={active}
                      disabled={disabled}
                      onChange={() => setBaseline(b)}
                    />
                    {b === 'previous_equal' ? 'Previous period' : 'Same time last week'}
                  </label>
                )
              })}
            </fieldset>
          </div>
          <div className="flex-1 overflow-y-auto px-4 py-3">
            <QueryPanel query={q} label="the change explanation" skeleton="rows" isEmpty={(r) => r.data.totals.current_bytes === 0 && r.data.totals.baseline_bytes === 0}>
              {(r) => (
                <ExplainBody
                  data={r.data}
                  meta={r.meta}
                  tz={w.tz}
                  onApply={(patch) => {
                    onApplyFilter(patch)
                    onOpenChange(false)
                  }}
                  rawLink={(patch) => rawFlowsLink(patch, r.meta)}
                />
              )}
            </QueryPanel>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

function period(from: string, to: string, tz: string): string {
  return `${formatDateTime(from, tz, { zone: 'none', seconds: false })} → ${formatDateTime(to, tz, { zone: 'none', seconds: false })}`
}

function ExplainBody({
  data,
  meta,
  tz,
  onApply,
  rawLink,
}: {
  data: ExplainPayload
  meta: NfMeta | null
  tz: string
  onApply: (patch: Partial<FlowFilter>) => void
  rawLink: (patch: Partial<FlowFilter>) => string
}) {
  const theme = useTheme((s) => s.theme)
  const colors = DIVERGING[theme === 'dark' ? 'dark' : 'light']
  const t = data.totals
  const up = t.delta_bytes >= 0
  return (
    <div className="space-y-4">
      <section aria-label="Totals" className="rounded-md border border-border bg-surface2/40 p-3">
        <div className="grid grid-cols-1 gap-2 text-xs min-[420px]:grid-cols-2">
          <div>
            <div className="text-text2">This window</div>
            <div className="text-lg font-semibold tabular-nums text-text">{formatBytes(t.current_bytes)}</div>
            <div className="text-xs text-text2">{period(data.window.from, data.window.to, tz)}</div>
          </div>
          <div>
            <div className="text-text2">{data.baseline === 'previous_equal' ? 'Previous period' : 'Same time last week'}</div>
            <div className="text-lg font-semibold tabular-nums text-text">{formatBytes(t.baseline_bytes)}</div>
            <div className="text-xs text-text2">{period(data.baseline_window.from, data.baseline_window.to, tz)}</div>
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="inline-flex items-center gap-1 font-semibold text-text">
            {up ? <ArrowUpRight className="h-3.5 w-3.5" aria-hidden /> : <ArrowDownRight className="h-3.5 w-3.5" aria-hidden />}
            {up ? '+' : '−'}
            {formatBytes(Math.abs(t.delta_bytes))}
            {t.delta_pct != null && <span className="font-normal text-text2">({formatDelta(t.delta_pct)})</span>}
          </span>
          {t.delta_pct == null && t.delta_reason && (
            <span className="text-text2">No percentage: {deltaReasonText(t.delta_reason)}.</span>
          )}
          <span className="text-text2">
            {formatCountExact(t.current_flows)} vs {formatCountExact(t.baseline_flows)} flow records
          </span>
        </div>
        {(meta?.warnings ?? []).filter((w) => w.code.startsWith('baseline')).map((w) => (
          <p key={w.code} className="mt-2 text-xs text-warning-fg">{w.message}</p>
        ))}
      </section>
      {data.dimensions.map((d) => (
        <section key={d.dimension} aria-labelledby={`nf-ex-${d.dimension}`}>
          <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-2">
            <h3 id={`nf-ex-${d.dimension}`} className="text-sm font-semibold text-text">{d.label}</h3>
            {d.items.length > 0 && t.delta_bytes !== 0 && (
              <span className="text-xs text-text2" title="Net change of the listed items against the net change of all traffic">
                Listed items net <b className="font-semibold tabular-nums text-text">{signedBytes(t.delta_bytes - d.other_delta_bytes)}</b> of{' '}
                <span className="tabular-nums">{signedBytes(t.delta_bytes)}</span>
              </span>
            )}
          </div>
          {d.items.length === 0 ? (
            <p className="text-xs text-text2">No traffic in either period.</p>
          ) : (
            <ul className="space-y-1.5">
              {d.items.map((item) => (
                <ExplainRow key={item.key} item={item} max={Math.max(...d.items.map((i) => Math.abs(i.delta_bytes)), 1)} colors={colors} onApply={onApply} rawLink={rawLink} />
              ))}
            </ul>
          )}
        </section>
      ))}
      <p className="text-xs text-text2">{data.method}. A flow seen by several exporters counts at each of them.</p>
    </div>
  )
}

function ExplainRow({
  item,
  max,
  colors,
  onApply,
  rawLink,
}: {
  item: ExplainItem
  max: number
  colors: { up: string; down: string }
  onApply: (patch: Partial<FlowFilter>) => void
  rawLink: (patch: Partial<FlowFilter>) => string
}) {
  const up = item.delta_bytes >= 0
  const width = Math.max(2, Math.round((Math.abs(item.delta_bytes) / max) * 100))
  const trend = item.trend === 'new' ? 'new' : item.trend === 'gone' ? 'gone' : item.delta_pct != null ? formatDelta(item.delta_pct) : ''
  const patch = item.filter as Partial<FlowFilter>
  return (
    <li className="rounded-md border border-border p-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
        <span className="min-w-0 break-all font-mono text-xs font-medium text-text">{item.label}</span>
        <span className="text-xs font-semibold tabular-nums text-text">
          {up ? '+' : '−'}
          {formatBytes(Math.abs(item.delta_bytes))}
          {trend && <span className="ml-1 font-normal text-text2">{trend}</span>}
        </span>
      </div>
      <div className="mt-1 h-1.5 w-full rounded-full bg-surface2" aria-hidden>
        <div className="h-1.5 rounded-full" style={{ width: `${width}%`, background: up ? colors.up : colors.down }} />
      </div>
      <div className="mt-1 flex flex-wrap items-center justify-between gap-2 text-xs text-text2">
        <span>
          {formatBytes(item.current_bytes)} now · {formatBytes(item.baseline_bytes)} before
          {shareOfChangeText(item.share_of_change_pct) && ` · ${shareOfChangeText(item.share_of_change_pct)}`}
        </span>
        <span className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => onApply(patch)}
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-medium text-primary hover:bg-primary/10"
            aria-label={`Apply ${item.label} as a page filter`}
          >
            <Filter className="h-3 w-3" aria-hidden /> Apply as filter
          </button>
          <Link
            to={rawLink(patch)}
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-medium text-primary hover:bg-primary/10"
            aria-label={`Open raw flows for ${item.label}`}
          >
            <Search className="h-3 w-3" aria-hidden /> Raw flows
          </Link>
        </span>
      </div>
    </li>
  )
}
