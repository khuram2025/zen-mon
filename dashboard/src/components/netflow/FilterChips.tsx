import { useEffect, useState } from 'react'
import { AlertTriangle, Check, Link2, X } from 'lucide-react'
import { cn, copyText } from '@/lib/utils'
import {
  describeFlowFilter,
  FLOW_FILTER_KEYS,
  FLOW_FILTER_LABELS,
  type FlowFilter,
  type FlowFilterDraft,
  type FlowFilterErrors,
  type FlowFilterKey,
} from '@/lib/netflow/filters'

export interface FilterChipsProps {
  filter: FlowFilter
  /** Removes the given keys (a chip can stand for several, e.g. interface + direction). Omit for read-only chips. */
  onRemove?: (keys: FlowFilterKey[]) => void
  onClearAll?: () => void
  /** Rejected URL values (`parseFlowFilterDetailed().invalid`): shown as "not applied", removable. */
  invalid?: FlowFilterDraft
  errors?: FlowFilterErrors
  /** Zone hour/day filters are evaluated in (the page window's). */
  tz?: string
  /** App path + query of the current view; shows a "Copy link" action for sharing it. */
  copyLink?: string
  className?: string
}

/**
 * Every applied filter as a chip that states exactly what it matches ("Host 10.0.0.5 — source or
 * destination"), with keyboard-reachable remove buttons. Filters that were in the link but could
 * not be applied are listed too, so a page never looks filtered when it is not (or vice versa).
 */
export function FilterChips({ filter, onRemove, onClearAll, invalid, errors, tz, copyLink, className }: FilterChipsProps) {
  const chips = describeFlowFilter(filter, { tz })
  const bad = FLOW_FILTER_KEYS.filter((k) => invalid?.[k] !== undefined)
  if (chips.length === 0 && bad.length === 0 && !copyLink) return null
  return (
    <div className={cn('flex flex-wrap items-center gap-1.5', className)}>
      <ul role="list" className="flex min-w-0 flex-wrap items-center gap-1.5" aria-label="Applied filters">
        {chips.map((chip) => {
          const text = `${chip.label}: ${chip.value}`
          return (
            <li
              key={chip.key}
              className={cn(
                'inline-flex max-w-full items-center gap-1 rounded-md border bg-surface2/70 py-0.5 pl-2 text-[11px] text-text',
                chip.negated ? 'border-dashed border-border-strong' : 'border-border',
                onRemove ? 'pr-0.5' : 'pr-2',
              )}
              title={chip.hint ? `${text} (${chip.hint})` : text}
            >
              <span className="text-text2">{chip.label}</span>
              <span className="min-w-0 break-all font-medium">{chip.value}</span>
              {chip.hint && <span className="sr-only">({chip.hint})</span>}
              {onRemove && (
                <button
                  type="button"
                  onClick={() => onRemove(chip.keys)}
                  aria-label={`Remove filter ${text}`}
                  className="rounded p-0.5 text-text2 hover:bg-surface3 hover:text-text"
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              )}
            </li>
          )
        })}
        {bad.map((key) => {
          const message = errors?.[key] ?? `${FLOW_FILTER_LABELS[key]} is not valid`
          return (
            <li
              key={`invalid-${key}`}
              className={cn(
                'inline-flex max-w-full items-center gap-1 rounded-md border border-danger/40 bg-danger/10 py-0.5 pl-2 text-[11px] text-text',
                onRemove ? 'pr-0.5' : 'pr-2',
              )}
              title={message}
            >
              <AlertTriangle className="h-3 w-3 shrink-0 text-danger-fg" aria-hidden />
              <span className="min-w-0 break-all">
                {FLOW_FILTER_LABELS[key]} “{invalid?.[key]}” <span className="font-semibold text-danger-fg">not applied</span>
              </span>
              <span className="sr-only">: {message}</span>
              {onRemove && (
                <button
                  type="button"
                  onClick={() => onRemove([key])}
                  aria-label={`Remove invalid filter ${FLOW_FILTER_LABELS[key]}`}
                  className="rounded p-0.5 text-text2 hover:bg-surface3 hover:text-text"
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              )}
            </li>
          )
        })}
      </ul>
      {onClearAll && chips.length + bad.length > 1 && (
        <button type="button" onClick={onClearAll} className="rounded px-1.5 py-0.5 text-[11px] font-medium text-text2 underline-offset-2 hover:text-text hover:underline">
          Clear all
        </button>
      )}
      {copyLink && <CopyLinkButton path={copyLink} />}
    </div>
  )
}

function CopyLinkButton({ path }: { path: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  useEffect(() => {
    if (state === 'idle') return
    const t = window.setTimeout(() => setState('idle'), 2500)
    return () => window.clearTimeout(t)
  }, [state])
  const copy = async () => {
    const url = /^https?:\/\//.test(path) ? path : `${window.location.origin}${path.startsWith('/') ? '' : '/'}${path}`
    setState((await copyText(url)) ? 'copied' : 'failed')
  }
  return (
    <button
      type="button"
      onClick={copy}
      className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-text2 hover:text-text"
    >
      {state === 'copied' ? <Check className="h-3 w-3 text-success-fg" aria-hidden /> : <Link2 className="h-3 w-3" aria-hidden />}
      <span aria-live="polite">{state === 'copied' ? 'Link copied' : state === 'failed' ? 'Copy failed' : 'Copy link'}</span>
    </button>
  )
}
